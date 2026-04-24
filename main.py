from __future__ import annotations
import asyncio
import time
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import stripe
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from core.auth import (
    ExpiredKeyError,
    count_active_keys,
    get_gateways,
    key_allowed_for_gateway,
    load_config,
    validate_and_resolve,
)
from core.proxy import forward_request
from core.limiter import limiter
from core.logger import build_stats, log_request, recent_log_entries, start_log_writer
from core.stripe_webhooks import StripeWebhookConfigError, construct_event, process_event
from core.store import get_store
from core.security import validate_target_url, get_real_client_ip  # Fix #1, #2, #4

# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise storage, start background workers; tear down cleanly."""
    from core.cancellation_scheduler import start as start_cancellation_poller
    from core.cancellation_scheduler import shutdown as stop_cancellation_poller

    get_store()                                          # warm up storage singleton
    log_task = asyncio.create_task(start_log_writer())  # async log queue writer
    await start_cancellation_poller()                    # grace-period poller
    yield
    # ── Teardown ──────────────────────────────────────────────────────────────
    await stop_cancellation_poller()
    log_task.cancel()
    try:
        await log_task
    except asyncio.CancelledError:
        pass

# ── Boot ──────────────────────────────────────────────────────────────────────

config = load_config()
server_cfg = config.get("server", {})
PORT = server_cfg.get("port", 8000)
HOST = server_cfg.get("host", "0.0.0.0")
STARTED_AT = time.monotonic()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Fix #15: Disable Swagger UI in production
_is_dev = os.environ.get("WORKFLOW_API_ENV", "production").lower() == "development"

app = FastAPI(
    title="workflow-api",
    description="A thin auth + proxy layer over your workflows.",
    version="2.0.0",
    docs_url="/docs" if _is_dev else None,  # disabled in production
    redoc_url=None,
    lifespan=lifespan,
)

# ── Register workflow routes ───────────────────────────────────────────────────

workflows = get_gateways(config)

# Fix #1: Validate all target URLs for SSRF at startup
for wf in workflows:
    try:
        validate_target_url(wf["target"])
    except ValueError as exc:
        raise RuntimeError(
            f"[SECURITY] Blocked insecure target URL in workflow '{wf['name']}': {exc}\n"
            "Fix: change the target to an external HTTPS URL, not a private IP or localhost."
        )

for wf in workflows:
    endpoint = wf["endpoint"]
    target   = wf["target"]
    method   = wf.get("method", "POST")
    name     = wf["name"]

    def make_handler(target_url: str, wf_method: str, wf_name: str):
        async def handler(request: Request):
            start = time.monotonic()

            # Auth — resolve key record
            try:
                key_record = validate_and_resolve(request.headers.get("Authorization"))
            except ExpiredKeyError:
                latency = (time.monotonic() - start) * 1000
                log_request(
                    request.url.path, 403, latency,
                    gateway=wf_name, event="expired_key", level="WARNING",
                )
                return JSONResponse({"detail": "API key expired"}, status_code=403)

            if not key_record:
                latency = (time.monotonic() - start) * 1000
                log_request(
                    request.url.path, 401, latency,
                    gateway=wf_name, event="auth_failed", level="WARNING",
                )
                return JSONResponse({"error": "Invalid or missing API key."}, status_code=401)

            if not key_allowed_for_gateway(key_record, wf_name):
                latency = (time.monotonic() - start) * 1000
                log_request(
                    request.url.path, 403, latency, key_record.get("name"),
                    gateway=wf_name, event="scope_denied", level="WARNING",
                )
                return JSONResponse(
                    {"detail": "Key not authorized for this workflow"}, status_code=403,
                )

            # Rate limit
            rpm = key_record.get("rate_limit_per_minute", 60)
            if not limiter.is_allowed(key_record["key_hash"], rpm):
                latency = (time.monotonic() - start) * 1000
                log_request(
                    request.url.path, 429, latency, key_record.get("name"),
                    gateway=wf_name, event="rate_limited", level="WARNING",
                )
                return JSONResponse(
                    {"error": "Rate limit exceeded.", "limit": rpm, "tier": key_record.get("name")},
                    status_code=429,
                )

            # Proxy
            response = await forward_request(request, target_url, wf_method)

            latency = (time.monotonic() - start) * 1000
            log_request(
                request.url.path, response.status_code, latency,
                key_record.get("name"), gateway=wf_name,
            )
            return response

        handler.__name__ = f"handler_{wf_name}"
        return handler

    app.add_api_route(
        path=endpoint,
        endpoint=make_handler(target, method, name),
        methods=[method],
        summary=f"Run: {name}",
    )

# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "workflows": [wf["name"] for wf in workflows],
        "gateways": [wf["name"] for wf in workflows],
        "active_keys": count_active_keys(),
    }


# ── Admin auth helpers ────────────────────────────────────────────────────────

def _get_bearer_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    parts = auth.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return ""


def _is_stats_authorized(request: Request) -> bool:
    """
    Fix #2 + #4: Proper admin auth.

    If WORKFLOW_API_ADMIN_KEY is set → ALWAYS require it, even from localhost.
    This eliminates the localhost-process bypass and uses constant-time compare.

    If no key is configured → restrict to loopback only.
    Real IP is extracted from X-Real-IP (set by nginx) to prevent spoofing.
    """
    cfg = load_config()
    configured_admin_key = (
        os.environ.get("WORKFLOW_API_ADMIN_KEY")
        or (cfg.get("admin") or {}).get("api_key")
        or ""
    ).strip()

    if configured_admin_key:
        # Key is configured — require it from ALL callers (including localhost)
        provided = (
            request.headers.get("X-Admin-Key", "")
            or _get_bearer_token(request)
        )
        if not provided:
            return False
        # Fix #2: always run compare_digest for constant-time comparison
        return secrets.compare_digest(provided, configured_admin_key)

    # No admin key configured — restrict to loopback only
    # Use X-Real-IP if set by nginx, otherwise fall back to direct connection IP
    real_ip = (
        get_real_client_ip(dict(request.headers))
        or (request.client.host if request.client else "")
    )
    return real_ip in {"127.0.0.1", "::1"}


def _format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _mask_value(value: str | None) -> str:
    if not value or value == "unknown":
        return "unknown"
    if len(value) <= 8:
        return value[0] + "***"
    return value[:4] + "***" + value[-4:]


# ── Admin stats / dashboard ───────────────────────────────────────────────────

@app.get("/__workflow-api/stats")
async def stats(request: Request):
    if not _is_stats_authorized(request):
        return JSONResponse({"detail": "Forbidden"}, status_code=403)
    return build_stats(active_keys=count_active_keys())


@app.get("/__workflow-api/dashboard")
async def dashboard(request: Request):
    if not _is_stats_authorized(request):
        return JSONResponse({"detail": "Forbidden"}, status_code=403)

    stats_payload = build_stats(active_keys=count_active_keys())
    recent_activity = []
    for entry in recent_log_entries(limit=10):
        recent_activity.append({
            "time":       entry.get("time", "-"),
            "gateway":    entry.get("gateway") or entry.get("endpoint") or "-",
            "status":     entry.get("status", "-"),
            "event":      entry.get("event", "request"),
            "level":      entry.get("level", "INFO"),
            "key":        _mask_value(entry.get("tier")),
            "latency_ms": entry.get("latency_ms", "-"),
        })

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "stats": stats_payload,
            "recent_activity": recent_activity,
            "uptime": _format_uptime(time.monotonic() - STARTED_AT),
        },
    )


# ── User self-serve portal ────────────────────────────────────────────────────

# [REMOVED] The built-in portal was removed to maintain a pure headless Developer Tool architecture. 
# Users can build their own portal by hitting the SQLite database directly from their Next.js/React frontend.


# ── Stripe webhook ────────────────────────────────────────────────────────────

@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    signature = request.headers.get("Stripe-Signature")

    try:
        event = construct_event(payload, signature)
    except StripeWebhookConfigError as exc:
        # Fix #11: Log internally, return generic message to caller
        log_request(
            "/webhooks/stripe", 503, 0,
            gateway="stripe", event=f"stripe_config_error:{exc}", level="ERROR",
        )
        return JSONResponse({"detail": "Webhook configuration error."}, status_code=503)
    except ValueError:
        log_request(
            "/webhooks/stripe", 400, 0,
            gateway="stripe", event="stripe_invalid_payload", level="WARNING",
        )
        return JSONResponse({"detail": "Invalid Stripe payload"}, status_code=400)
    except stripe.error.SignatureVerificationError:
        log_request(
            "/webhooks/stripe", 401, 0,
            gateway="stripe", event="stripe_signature_failed", level="WARNING",
        )
        return JSONResponse({"detail": "Invalid Stripe signature"}, status_code=401)

    try:
        result = await process_event(event)
    except Exception as exc:
        # Log full error internally — never expose to caller
        log_request(
            "/webhooks/stripe", 500, 0,
            gateway="stripe",
            event=f"stripe_processing_error:{type(exc).__name__}",
            level="ERROR",
        )
        return JSONResponse(
            {"detail": "Webhook processing error."}, status_code=500
        )

    return JSONResponse(result)


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    keys = load_config().get("keys") or []
    active_key_count = count_active_keys()
    if not keys:
        print("\n⚠️  No API keys found.")
        print("   Run: python cli.py keys create")
    else:
        print(f"\n🔑 {active_key_count} active key(s): {', '.join(k['name'] for k in keys)}")

    if not os.environ.get("WORKFLOW_API_ADMIN_KEY"):
        print("\n⚠️  WORKFLOW_API_ADMIN_KEY is not set.")
        print("   Dashboard access is restricted to localhost only.")
        print("   Set it in production: export WORKFLOW_API_ADMIN_KEY=your-secret")

    print(f"\n🚀 workflow-api starting...")
    print(f"   Listening on http://{HOST}:{PORT}")
    for wf in workflows:
        print(f"   → {wf['endpoint']}  (proxies to {wf['target']})")
    print(f"\n   Docs:   {'http://localhost:' + str(PORT) + '/docs' if _is_dev else 'disabled (set WORKFLOW_API_ENV=development to enable)'}")
    print(f"   Portal: http://localhost:{PORT}/portal\n")

    uvicorn.run(app, host=HOST, port=PORT)
