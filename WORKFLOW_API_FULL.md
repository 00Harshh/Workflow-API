# Workflow API — Full Project Document

> From problem statement to architecture to future roadmap.

---

## 1. Problem Statement

Workflow builders — people using n8n, Zapier, or custom Python scripts — have no clean way to expose their workflows to external users or paying customers.

The gap looks like this:

- A non-technical creator builds a powerful n8n workflow
- They want to give API access to 10 paying customers
- Each customer should have their own key
- Each tier should have its own rate limit
- None of it should require the creator to know infrastructure

The existing options all fail in some way:

| Option | Problem |
|---|---|
| n8n webhook URL | No auth, no rate limiting, one URL for everyone |
| Zapier webhook | Same — no per-user access control |
| Build a FastAPI wrapper yourself | Requires backend knowledge |
| Use a cloud API gateway (AWS, Cloudflare) | Complex setup, ongoing cost, vendor lock-in |

**The gap:** there is no lightweight, self-hosted, zero-infrastructure tool that turns an existing workflow into a properly authenticated, rate-limited, multi-tenant API.

---

## 2. What Workflow API Is

Workflow API is a thin auth + proxy layer that sits between the outside world and a user's existing workflow.

It does exactly four things:

1. Validates API keys on every incoming request
2. Enforces per-key rate limits
3. Forwards the request to the user's workflow URL
4. Logs the request with tier and latency

It does not host workflows. It does not run compute. It does not touch billing. It is a layer — not a platform.

**One-line definition:**
> An open-source CLI tool that wraps any HTTP-accessible workflow in a production-ready API with key auth, per-user rate limiting, and a monetization flow.

---

## 3. Core Design Decisions

### 3.1 Self-hosted by default

Workflow API runs wherever the user's workflow runs. Local machine, VPS, home server — it doesn't matter. The user brings their own hosting. This eliminates vendor lock-in and makes it free to run.

### 3.2 Config-driven, CLI-managed

All configuration lives in one file: `config.yaml`. But users never edit it manually. The CLI (`cli.py`) manages it entirely — the `init` wizard writes the config, `keys create` appends keys, `keys revoke` removes them.

### 3.3 Per-key rate limiting, not global

Each API key has its own isolated token bucket. A Free tier user hitting their limit has zero effect on a Pro tier user. This is the foundation of the monetization model.

### 3.4 Pure passthrough proxy

Workflow API does not transform requests or responses. It strips auth headers, forwards the body as-is to the target URL, and returns whatever the workflow returns. No schema enforcement, no data manipulation. This keeps it compatible with anything.

### 3.5 No database

All state lives in `config.yaml` (keys, workflows, server config) and `logs/usage.log` (flat JSON lines). No Postgres, no Redis, no external dependencies. This keeps setup under 5 minutes.

---

## 4. System Architecture

### 4.1 Request Flow

```
External Caller
      │
      │  POST /run/my-workflow
      │  Authorization: Bearer wfapi-xxx
      ▼
┌─────────────────────┐
│    Auth Layer       │  → validates key against config.yaml
│    (core/auth.py)   │  → resolves key record (name, rate limit)
└────────┬────────────┘
         │  key record
         ▼
┌─────────────────────┐
│   Rate Limiter      │  → per-key token bucket
│   (core/limiter.py) │  → returns 429 if exceeded
└────────┬────────────┘
         │  allowed
         ▼
┌─────────────────────┐
│   Proxy Engine      │  → strips auth headers
│   (core/proxy.py)   │  → forwards body + query params to target URL
└────────┬────────────┘
         │
         ▼
  User's Workflow
  (n8n / Zapier / anything)
         │
         ▼
┌─────────────────────┐
│   Logger            │  → writes timestamp, endpoint, tier, status, latency
│   (core/logger.py)  │  → to logs/usage.log
└─────────────────────┘
         │
         ▼
  Response returned to caller
```

### 4.2 File Structure

```
workflow-api/
│
├── cli.py                ← primary interface for the user
├── main.py               ← FastAPI server, registers routes from config
├── keygen.py             ← (legacy) replaced by cli.py keys
├── config.yaml           ← single source of truth, managed by CLI
│
├── core/
│   ├── auth.py           ← key CRUD, validation, resolution
│   ├── proxy.py          ← async HTTP forwarding via httpx
│   ├── limiter.py        ← token bucket, per-key, singleton instance
│   └── logger.py         ← flat JSON line writer
│
├── logs/
│   └── usage.log         ← one JSON object per request
│
├── Dockerfile            ← for VPS / live deployment
├── requirements.txt
└── README.md
```

### 4.3 Config Structure

```yaml
workflows:
  - name: summarize
    endpoint: /run/summarize
    target: http://localhost:5678/webhook/abc123
    method: POST

keys:
  - name: Free
    key: wfapi-xxxxxxxxxxxxxxxx
    rate_limit_per_minute: 10
    created_at: "2026-04-21"

  - name: Pro
    key: wfapi-yyyyyyyyyyyyyyyy
    rate_limit_per_minute: 200
    created_at: "2026-04-21"

  - name: Enterprise
    key: wfapi-zzzzzzzzzzzzzzzz
    rate_limit_per_minute: 0     # 0 = unlimited
    created_at: "2026-04-21"

server:
  host: "0.0.0.0"
  port: 8000
```

### 4.4 Rate Limiter Design

Token bucket algorithm. Each key gets its own bucket:

- Bucket capacity = `rate_limit_per_minute`
- Refill rate = `capacity / 60` tokens per second (continuous)
- On each request: attempt to consume 1 token
- If tokens < 1: return 429 with tier name in error body
- Rate limit = 0: always allowed (unlimited tier)
- Thread-safe via a single `threading.Lock`

The limiter is a singleton instantiated once at server startup and shared across all requests.

### 4.5 Auth Design

- Keys stored in plain text in `config.yaml` (user controls their own security)
- Validation uses `secrets.compare_digest` to prevent timing attacks
- `validate_and_resolve()` returns the full key record on success — the rate limiter reads `rate_limit_per_minute` directly from it
- Key generation uses `secrets.token_urlsafe(32)` prefixed with `wfapi-`

### 4.6 Proxy Design

Built on `httpx` async client:

- Strips `Authorization`, `Host`, `Content-Length` headers before forwarding
- Forwards raw body bytes and query params unchanged
- Attempts JSON parse on response; falls back to `{"response": raw_text}`
- 30 second timeout
- Returns `502` on connection error (workflow not running)
- Returns `504` on timeout

---

## 5. CLI Reference

The entire user-facing interface. Users never touch `config.yaml` or `main.py` directly.

```
python cli.py init              First-time setup wizard
python cli.py start             Start the API server
python cli.py status            Show all workflows + active keys

python cli.py keys create       Interactive: name + rate limit → new key
python cli.py keys list         Table of all active keys
python cli.py keys revoke Pro   Revoke all keys named 'Pro'
```

### Init wizard flow

```
1. Workflow name?          → summarize
2. Target URL?             → http://localhost:5678/webhook/abc
3. Endpoint path?          → /run/summarize  (auto-suggested)
4. HTTP method?            → POST
5. Add another workflow?   → No
6. Port?                   → 8000
7. Create first key now?   → Yes
   Key name / tier?        → Free
   Rate limit (req/min)?   → 30
```

Writes `config.yaml`, prints the generated key, and shows next steps.

---

## 6. Monetization Model

Workflow API does not own the billing layer. It provides the infrastructure that makes billing possible.

The intended flow for a workflow creator:

```
User pays (Stripe / Gumroad / manual invoice / anything)
    ↓
Creator runs: python cli.py keys create
    Name: Pro
    Rate limit: 200
    ↓
Key is printed once. Creator copies and sends to user.
    ↓
User calls the API with their key.
    ↓
User cancels subscription
    ↓
Creator runs: python cli.py keys revoke Pro
    ↓
Key is immediately invalidated.
```

The rate limit IS the tier. Free = 10 req/min. Pro = 200. Enterprise = unlimited.

The logs track which tier made each request, giving the creator usage data per customer without building any analytics infrastructure.

---

## 7. Stack

| Component | Technology | Why |
|---|---|---|
| API server | FastAPI + uvicorn | async, fast, auto docs at /docs |
| HTTP forwarding | httpx | async, clean API, good error handling |
| Config | PyYAML | human-readable, no database needed |
| CLI | Click | subcommand groups, clean argument parsing |
| Terminal output | Rich | tables, panels, color without complexity |
| Auth | Python secrets | timing-attack-safe comparison |
| Rate limiting | Custom token bucket | no Redis dependency, per-key capacity |
| Containerization | Docker | one command to run on any VPS |

---

## 8. What Workflow API Deliberately Does Not Do

These are conscious non-decisions, not missing features:

- **No billing integration** — Workflow API doesn't touch money. The creator handles that externally.
- **No dashboard UI** — the CLI is the UI. A web dashboard adds complexity and a server dependency.
- **No workflow execution** — Workflow API only proxies. It never runs n8n, Zapier, or Python code itself.
- **No database** — `config.yaml` and flat log files are intentionally sufficient for the v1 scope.
- **No multi-user admin** — one operator per deployment. Not a SaaS platform.
- **No request transformation** — body goes in, body comes out. Schema enforcement is the workflow's job.

---

## 9. Deployment

### Local (development)

```bash
python cli.py init
python cli.py start
```

### VPS / live server

```bash
docker build -t workflow-api .
docker run -d \
  -p 8000:8000 \
  -v $(pwd)/config.yaml:/app/config.yaml \
  -v $(pwd)/logs:/app/logs \
  --name workflow-api \
  workflow-api
```

Works on any Ubuntu VPS, DigitalOcean Droplet, Hetzner server, Raspberry Pi, or home server.

---

## 10. Future Improvements

Roughly ordered by impact and buildability.

### 10.1 Usage stats endpoint (high priority, easy)

A `/stats` endpoint that reads `usage.log` and returns per-tier usage counts, total requests, error rates, and average latency. No new dependencies — just aggregate the existing log file.

```json
{
  "total_requests": 1420,
  "by_tier": {
    "Free": { "requests": 340, "errors": 12 },
    "Pro": { "requests": 1080, "errors": 3 }
  },
  "avg_latency_ms": 187
}
```

### 10.2 Key expiry (high priority, easy)

Add an `expires_at` field to each key record. The auth layer checks the date on every request and returns `401` with `"error": "Key expired"` if past expiry. The CLI gets an `--expires` flag:

```bash
python cli.py keys create --name "Trial" --rate-limit 20 --expires 2026-05-01
```

### 10.3 Per-workflow key scoping (medium priority, medium effort)

Currently a key grants access to all workflows. Add an optional `workflows` list to each key record:

```yaml
keys:
  - name: Free
    key: wfapi-xxx
    rate_limit_per_minute: 10
    workflows: [summarize]     # can only call /run/summarize
```

If `workflows` is absent, the key has access to everything (current behaviour, backwards compatible).

### 10.4 Webhook on usage events (medium priority, medium effort)

An optional `on_request` webhook in config that Workflow API pings after every successful request:

```yaml
hooks:
  on_request: https://your-server.com/usage-event
```

Payload: `{tier, endpoint, timestamp, latency_ms}`. This lets the creator pipe usage data into Stripe metered billing, their own database, or Slack without any changes to Workflow API itself.

### 10.5 `cli.py logs` command (low priority, easy)

A CLI command to tail and filter the log file:

```bash
python cli.py logs                  # last 20 requests
python cli.py logs --tier Pro       # filter by tier
python cli.py logs --errors         # only 4xx / 5xx
python cli.py logs --follow         # live tail
```

### 10.6 Multiple workflows per key with different rate limits (medium priority, hard)

Currently rate limiting is per-key, globally. A more granular model would allow:

```yaml
keys:
  - name: Pro
    rate_limits:
      /run/summarize: 200
      /run/translate: 50
```

Requires restructuring the limiter to bucket on `(key, endpoint)` pairs.

### 10.7 Admin API (low priority, medium effort)

A `/admin` set of endpoints protected by a separate master key, allowing key management over HTTP instead of CLI:

```
POST   /admin/keys          create key
GET    /admin/keys          list keys
DELETE /admin/keys/{name}   revoke key
GET    /admin/stats         usage stats
```

This would allow building a web dashboard or integrating key management into a Stripe webhook handler.

### 10.8 n8n / Zapier native adapters (low priority, hard)

Currently Workflow API works with n8n and Zapier because they both expose webhook URLs. A deeper integration would involve:

- Auto-detecting n8n workflows via the n8n API
- Listing available webhooks and letting the user pick one during `init`
- Removing the need to copy-paste webhook URLs manually

This is a significant scope increase and only makes sense after the core tool is stable and has users.

### 10.9 SQLite backend (optional, future)

Replace `config.yaml` + flat log file with SQLite when the log file grows large or multi-operator support is needed. The swap should be invisible to the CLI user. This is not needed until someone has enough users to produce millions of log lines.

---

## 11. Known Limitations (v1)

- **Keys stored in plaintext** in `config.yaml`. For high-security deployments, the user should encrypt the config file or use filesystem permissions.
- **No key rotation** — revoking and recreating a key requires sending the new key to the user manually.
- **Single process** — uvicorn runs as a single worker. For high-concurrency deployments, run behind Gunicorn with multiple workers.
- **Log file grows unbounded** — no rotation implemented. For long-running deployments, set up `logrotate` on the OS level.
- **Restart required for config changes** — workflows added after startup are not picked up until `python cli.py start` is run again.

---

## 12. Summary

Workflow API is a single-operator, self-hosted API gateway designed for workflow creators who want to monetize access to their automations without infrastructure complexity.

The core insight is that the gap between "I built a workflow" and "I can sell API access to it" is entirely an auth + rate limiting problem. Workflow API solves exactly that, nothing more, and stays out of the way of everything else.

The entire system is under 500 lines of Python across 6 files, ships in a single Docker container, requires no database, and can be set up in under 5 minutes by a non-technical user.

---

*Built with FastAPI, httpx, Click, Rich, PyYAML.*
*License: MIT*
