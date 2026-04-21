import time
import os
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from core.auth import ExpiredKeyError, count_active_keys, load_config, validate_and_resolve
from core.proxy import forward_request
from core.limiter import limiter
from core.logger import build_stats, log_request

# ── Boot ──────────────────────────────────────────────────────────────────────

config = load_config()
server_cfg = config.get("server", {})
PORT = server_cfg.get("port", 8000)
HOST = server_cfg.get("host", "0.0.0.0")

app = FastAPI(
    title="workflow-api",
    description="A thin auth + proxy layer over your workflows.",
    version="2.0.0",
    docs_url="/docs",
)

# ── Register workflow routes ───────────────────────────────────────────────────

workflows = config.get("workflows", [])

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

    authorization = request.headers.get("Authorization") or ""
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return secrets.compare_digest(parts[1], configured_admin_key)


@app.get("/__flowgate/stats")
async def stats(request: Request):
    if not _is_stats_authorized(request):
        return JSONResponse({"detail": "Forbidden"}, status_code=403)
    return build_stats(active_keys=count_active_keys())

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
