import time
import os
import secrets
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
from core.logger import build_stats, log_request, recent_log_entries
from core.stripe_webhooks import StripeWebhookConfigError, construct_event, process_event

# ── Boot ──────────────────────────────────────────────────────────────────────

config = load_config()
server_cfg = config.get("server", {})
PORT = server_cfg.get("port", 8000)
HOST = server_cfg.get("host", "0.0.0.0")
STARTED_AT = time.monotonic()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(
    title="workflow-api",
    description="A thin auth + proxy layer over your workflows.",
    version="2.0.0",
    docs_url="/docs",
)

# ── Register workflow routes ───────────────────────────────────────────────────

workflows = get_gateways(config)

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
                    request.url.path,
                    403,
                    latency,
                    gateway=wf_name,
                    event="expired_key",
                    level="WARNING",
                )
                return JSONResponse({"detail": "API key expired"}, status_code=403)

            if not key_record:
                latency = (time.monotonic() - start) * 1000
                log_request(
                    request.url.path,
                    401,
                    latency,
                    gateway=wf_name,
                    event="auth_failed",
                    level="WARNING",
                )
                return JSONResponse({"error": "Invalid or missing API key."}, status_code=401)

            if not key_allowed_for_gateway(key_record, wf_name):
                latency = (time.monotonic() - start) * 1000
                log_request(
                    request.url.path,
                    403,
                    latency,
                    key_record.get("name"),
                    gateway=wf_name,
                    event="scope_denied",
                    level="WARNING",
                )
                return JSONResponse(
                    {"detail": "Key not authorized for this workflow"},
                    status_code=403,
                )

            # Rate limit — use this key's individual limit
            rpm = key_record.get("rate_limit_per_minute", 60)
            if not limiter.is_allowed(key_record["key"], rpm):
                latency = (time.monotonic() - start) * 1000
                log_request(
                    request.url.path,
                    429,
                    latency,
                    key_record.get("name"),
                    gateway=wf_name,
                    event="rate_limited",
                    level="WARNING",
                )
                return JSONResponse(
                    {
                        "error": "Rate limit exceeded.",
                        "limit": rpm,
                        "tier": key_record.get("name"),
                    },
                    status_code=429,
                )

            # Proxy
            response = await forward_request(request, target_url, wf_method)

            # Log
            latency = (time.monotonic() - start) * 1000
            log_request(
                request.url.path,
                response.status_code,
                latency,
                key_record.get("name"),
                gateway=wf_name,
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

# ── Health + key info ─────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "workflows": [wf["name"] for wf in workflows],
        "gateways": [wf["name"] for wf in workflows],
        "active_keys": count_active_keys(),
    }


def _is_stats_authorized(request: Request) -> bool:
    client_host = request.client.host if request.client else ""
    if client_host in {"127.0.0.1", "::1", "localhost"}:
        return True

    cfg = load_config()
    configured_admin_key = (
        os.environ.get("FLOWGATE_ADMIN_KEY")
        or (cfg.get("admin") or {}).get("api_key")
        or cfg.get("admin_api_key")
    )
    if not configured_admin_key:
        return False

    x_admin_key = request.headers.get("X-Admin-Key")
    if x_admin_key and secrets.compare_digest(x_admin_key, configured_admin_key):
        return True

    authorization = request.headers.get("Authorization") or ""
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return secrets.compare_digest(parts[1], configured_admin_key)


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


@app.get("/__flowgate/stats")
async def stats(request: Request):
    if not _is_stats_authorized(request):
        return JSONResponse({"detail": "Forbidden"}, status_code=403)
    return build_stats(active_keys=count_active_keys())


@app.get("/__flowgate/dashboard")
async def dashboard(request: Request):
    if not _is_stats_authorized(request):
        return JSONResponse({"detail": "Forbidden"}, status_code=403)

    stats_payload = build_stats(active_keys=count_active_keys())
    recent_activity = []
    for entry in recent_log_entries(limit=10):
        recent_activity.append({
            "time": entry.get("time", "-"),
            "gateway": entry.get("gateway") or entry.get("endpoint") or "-",
            "status": entry.get("status", "-"),
            "event": entry.get("event", "request"),
            "level": entry.get("level", "INFO"),
            "key": _mask_value(entry.get("tier")),
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


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    signature = request.headers.get("Stripe-Signature")

    try:
        event = construct_event(payload, signature)
    except StripeWebhookConfigError as exc:
        log_request(
            "/webhooks/stripe",
            503,
            0,
            gateway="stripe",
            event="stripe_config_error",
            level="ERROR",
        )
        return JSONResponse({"detail": str(exc)}, status_code=503)
    except ValueError:
        log_request(
            "/webhooks/stripe",
            400,
            0,
            gateway="stripe",
            event="stripe_invalid_payload",
            level="WARNING",
        )
        return JSONResponse({"detail": "Invalid Stripe payload"}, status_code=400)
    except stripe.error.SignatureVerificationError:
        log_request(
            "/webhooks/stripe",
            400,
            0,
            gateway="stripe",
            event="stripe_signature_failed",
            level="WARNING",
        )
        return JSONResponse({"detail": "Invalid Stripe signature"}, status_code=400)

    try:
        result = process_event(event)
    except Exception as exc:
        log_request(
            "/webhooks/stripe",
            500,
            0,
            gateway="stripe",
            event="stripe_processing_error",
            level="ERROR",
        )
        return JSONResponse({"detail": f"Stripe webhook processing failed: {exc}"}, status_code=500)

    return result

# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    keys = load_config().get("keys") or []
    active_key_count = count_active_keys()
    if not keys:
        print("\n⚠️  No API keys found.")
        print("   Run: python keygen.py --name 'Free' --rate-limit 30")
        print("   Run: python keygen.py --name 'Pro'  --rate-limit 200\n")
    else:
        print(f"\n🔑 {active_key_count} active key(s): {', '.join(k['name'] for k in keys)}")

    print(f"\n🚀 workflow-api starting...")
    print(f"   Listening on http://{HOST}:{PORT}")
    for wf in workflows:
        print(f"   → {wf['endpoint']}  (proxies to {wf['target']})")
    print(f"\n   Docs: http://localhost:{PORT}/docs\n")

    uvicorn.run(app, host=HOST, port=PORT)
