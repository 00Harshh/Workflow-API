# Workflow API — Full Project Document

> From problem statement to architecture to production deployment.

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

**The gap:** there is no lightweight, self-hosted, zero-infrastructure tool that turns an existing workflow into a properly authenticated, rate-limited, multi-tenant, monetizable API.

---

## 2. What Workflow API Is

Workflow API is a thin auth + proxy layer that sits between the outside world and a user's existing workflow.

It does these things:

1. Validates API keys on every incoming request (SHA-256 hashed, timing-attack-safe)
2. Enforces per-key rate limits (token bucket algorithm)
3. Forwards the request to the user's workflow URL (async httpx proxy)
4. Logs the request with tier and latency (async queue, never blocks)
5. Automates subscription key lifecycle via Stripe webhook integration
6. Enforces security at every layer: HMAC verification, SSRF protection, env-only secrets

It does not host workflows. It does not run compute. It does not touch billing. It is a layer — not a platform.

**One-line definition:**
> An open-source CLI tool that wraps any HTTP-accessible workflow in a production-ready API with key auth, per-user rate limiting, Stripe subscription automation, and a monetization flow.

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

### 3.5 Dual storage backends, protocol-driven

State lives in either `config.yaml` (YAML backend) or `workflow-api.db` (SQLite backend). Both implement the `KeyStore` protocol identically — switching backends is a one-command migration with zero code changes. SQLite (WAL mode) is recommended for production multi-worker deployments.

### 3.6 Secrets never in version control

The Stripe webhook secret is loaded **exclusively** from `STRIPE_WEBHOOK_SECRET` env var. `config.yaml` is gitignored. `.env` files are gitignored. Only `config.example.yaml` (with placeholder values) is committed.

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
│  HMAC / Admin Auth  │  → Stripe: verify Stripe-Signature header
│    (main.py)        │  → Admin: secrets.compare_digest
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│    Auth Layer       │  → SHA-256 hash Bearer token
│    (core/auth.py)   │  → lookup in store → resolve key record
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
│   SSRF Guard        │  → validate target URL
│  (core/security.py) │  → block private IPs, cloud metadata
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│   Proxy Engine      │  → strip auth headers
│   (core/proxy.py)   │  → forward body + query params via httpx
└────────┬────────────┘
         │
         ▼
  User's Workflow
  (n8n / Zapier / anything)
         │
         ▼
┌─────────────────────┐
│   Logger            │  → asyncio.Queue.put_nowait() — ~0.001ms
│   (core/logger.py)  │  → background writer → logs/usage.log
└─────────────────────┘
         │
         ▼
  Response returned to caller
```

### 4.2 Stripe Webhook Flow

```
POST /webhooks/stripe
  │
  ├─ construct_event() → stripe.Webhook.construct_event(body, sig, STRIPE_WEBHOOK_SECRET)
  │    HMAC mismatch → 401 Unauthorized
  │    Secret missing → 503 Service Unavailable
  │
  ├─ is_event_processed(event.id) → deduplicate retries
  │
  ├─ checkout.session.completed
  │    → extract price_id → map to allowed_gateways
  │    → create_key() → hash + store
  │    → async_send_api_key_email() → SMTP
  │
  ├─ customer.subscription.deleted
  │    → schedule_revocation() → write pending_cancellation to DB
  │    → key stays ACTIVE (48h grace window)
  │
  └─ customer.subscription.created / invoice.payment_succeeded
       → cancel_pending_revocation() → delete pending record
       → key stays active ✅
```

### 4.3 Grace Period Architecture

```
Background poller (asyncio.Task, started in lifespan):
  → runs every CANCELLATION_POLL_SECONDS (default: 60s)
  → calls get_due_cancellations() → DB rows where revoke_at <= now()
  → for each: revoke_key_by_stripe_subscription() + remove_pending_cancellation()
  → SQLite WAL: only one writer commits; DELETE is idempotent across workers
```

State is in the **database**, not in memory — survives server restarts, works across multiple uvicorn workers.

### 4.4 File Structure

```
workflow-api/
│
├── cli.py                ← primary CLI interface
├── main.py               ← FastAPI server, routes, lifespan hooks
├── config.yaml           ← single source of truth (gitignored)
├── config.example.yaml   ← safe template for version control
│
├── core/
│   ├── auth.py           ← key CRUD, validation, SHA-256 hashing
│   ├── cancellation_scheduler.py  ← 48h grace period poller
│   ├── store.py          ← KeyStore protocol + singleton factory
│   ├── store_sqlite.py   ← SQLite backend (WAL mode)
│   ├── store_yaml.py     ← YAML backend (fcntl file locking)
│   ├── limiter.py        ← token bucket, per-key, singleton
│   ├── logger.py         ← async queue log writer
│   ├── proxy.py          ← httpx async request forwarder
│   ├── security.py       ← SSRF protection, IP extraction
│   ├── email_sender.py   ← SMTP with Jinja2 HTML template
│   └── stripe_webhooks.py ← Stripe event processor
│
├── templates/
│   └── dashboard.html    ← Admin dashboard (Chart.js, glassmorphism)
│
├── logs/
│   └── usage.log         ← one JSON object per request
│
├── nginx.conf            ← drop-in reverse proxy config
├── Dockerfile
└── requirements.txt
```

### 4.5 Rate Limiter Design

Token bucket algorithm. Each key gets its own bucket:

- Bucket capacity = `rate_limit_per_minute`
- Refill rate = `capacity / 60` tokens per second (continuous)
- On each request: attempt to consume 1 token
- If tokens < 1: return 429 with tier name in error body
- Rate limit = 0: always allowed (unlimited tier)
- Thread-safe via a single `threading.Lock`

The limiter is a singleton instantiated once at server startup and shared across all requests.

### 4.6 Auth Design

- Keys hashed with SHA-256 before storage. Only `key_hash` + `key_prefix` (first 16 chars) are persisted.
- Bearer token on each request is hashed and compared via direct equality (constant-time via hash comparison).
- Admin endpoint uses `secrets.compare_digest` explicitly to prevent timing attacks.
- Key generation uses `secrets.token_urlsafe(33)` prefixed with `wfapi-`.

### 4.7 Proxy Design

Built on `httpx` async client:

- Strips `Authorization`, `Host`, `Content-Length` headers before forwarding
- Forwards raw body bytes and query params unchanged
- Attempts JSON parse on response; falls back to `{"response": raw_text}`
- 30 second timeout
- Returns `502` on connection error (workflow not running)
- Returns `504` on timeout

---

## 5. CLI Reference

The entire user-facing management interface.

```
python3 cli.py init              First-time setup wizard
python3 cli.py start             Start the API server
python3 cli.py start --workers 4 Multi-worker production start
python3 cli.py status            Show all workflows + active keys

python3 cli.py keys create       Create a new API key
python3 cli.py keys list         Table of all active keys
python3 cli.py keys revoke Pro   Revoke all keys named 'Pro'

python3 cli.py n8n --url ...     One-command n8n workflow setup
python3 cli.py migrate hash-keys  Migrate plaintext keys → SHA-256 hashes
python3 cli.py migrate yaml-to-sqlite  Migrate YAML store → SQLite
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

Writes `config.yaml`, prints the generated key (once only), and shows next steps.

---

## 6. Monetization Model

Workflow API does not own the billing layer. It provides the infrastructure that makes billing possible.

**Automated flow (Stripe):**

```
User visits Stripe Payment Link
    ↓
User pays subscription
    ↓
Stripe fires checkout.session.completed
    ↓
Workflow API: HMAC verify → create key → email to customer
    ↓
Customer uses API with their key

    ↓ later...

Customer cancels subscription
    ↓
Stripe fires customer.subscription.deleted
    ↓
Workflow API: schedule revocation (48h grace)
    ↓
48h later → key automatically revoked 🔒
```

**Manual flow:**

```
Creator runs: python3 cli.py keys create --name Pro --rate-limit 200
    ↓
Key printed once. Creator copies and sends to user.
    ↓
User calls the API with their key.
    ↓
Creator runs: python3 cli.py keys revoke Pro
    ↓
Key is immediately invalidated.
```

The rate limit IS the tier. Free = 10 req/min. Pro = 200. Enterprise = unlimited (0).

---

## 7. Stack

| Component | Technology | Why |
|---|---|---|
| API server | FastAPI + uvicorn | async, fast, auto docs at /docs |
| HTTP forwarding | httpx | async, clean API, good error handling |
| Config | PyYAML | human-readable, no database needed for dev |
| Storage (prod) | SQLite (stdlib) | WAL mode, zero dependencies, multi-worker safe |
| CLI | Click | subcommand groups, clean argument parsing |
| Terminal output | Rich | tables, panels, color without complexity |
| Auth | hashlib + secrets | SHA-256 hashing, timing-attack-safe comparison |
| Stripe | stripe-python | Official SDK, HMAC `Webhook.construct_event` |
| Rate limiting | Custom token bucket | no Redis dependency, per-key capacity |
| Email | smtplib + Jinja2 | stdlib, no external email service required |
| Containerization | Docker | one command to run on any VPS |

---

## 8. What Workflow API Deliberately Does Not Do

These are conscious non-decisions, not missing features:

- **No public portal** — removed in V2 for security. Operators build their own customer-facing UI using the `/stats` JSON endpoint.
- **No billing integration** — Workflow API doesn't touch money. The creator configures Stripe externally.
- **No workflow execution** — Workflow API only proxies. It never runs n8n, Zapier, or Python code itself.
- **No request transformation** — body goes in, body comes out. Schema enforcement is the workflow's job.
- **No multi-user admin** — one operator per deployment. Not a SaaS platform.
- **No external database required** — SQLite (stdlib) handles everything. Postgres/MySQL is for your n8n workflows, not Workflow API.

---

## 9. Security Architecture

| Layer | Mechanism |
|---|---|
| Webhook integrity | HMAC via `stripe.Webhook.construct_event` — 401 on mismatch, 503 if secret missing |
| Key storage | SHA-256 hash only — raw key shown once, never recoverable |
| Secret management | `STRIPE_WEBHOOK_SECRET`, `SMTP_PASSWORD`, `WORKFLOW_API_ADMIN_KEY` — env vars only |
| Admin auth | `secrets.compare_digest` — prevents timing attacks |
| SSRF protection | Target URL validated at startup — localhost, RFC1918, cloud metadata blocked |
| Event deduplication | Stripe `event.id` tracked in DB — prevents double provisioning on retries |
| Persistent grace period | Cancellations stored in DB — survives restarts, multi-worker safe |
| Gitignore | `config.yaml`, `*.db`, `*.lock`, `logs/`, `.env*`, `site/` — nothing sensitive committed |

---

## 10. Deployment

### Local (development)

```bash
python3 cli.py init
python3 cli.py start
```

### VPS / live server (Docker)

```bash
docker build -t workflow-api .
docker run -d \
  -p 8000:8000 \
  -e WORKFLOW_API_ADMIN_KEY="change-this-admin-secret" \
  -e STRIPE_WEBHOOK_SECRET="whsec_..." \
  -v $(pwd)/config.yaml:/app/config.yaml \
  -v $(pwd)/workflow-api.db:/app/workflow-api.db \
  -v $(pwd)/logs:/app/logs \
  --name workflow-api \
  workflow-api
```

Works on any Ubuntu VPS, DigitalOcean Droplet, Hetzner server, Raspberry Pi, or home server.

### Cloud PaaS (Render / Railway)

1. Push to a private GitHub repo.
2. Connect to Render or Railway as a new Web Service.
3. Set `WORKFLOW_API_ADMIN_KEY` and `STRIPE_WEBHOOK_SECRET` as environment variables.
4. Attach a Persistent Disk to `/app/workflow-api.db` so key data survives redeploys.

---

## 11. Known Limitations (V2)

- **Log file grows unbounded** — no rotation implemented. For long-running deployments, configure `logrotate` at the OS level.
- **Rate limiter is in-memory** — in multi-worker mode, each worker has its own token bucket. For globally accurate rate limiting across workers, a shared backend (e.g. Redis) would be needed. For most use cases this is acceptable.
- **Restart required for workflow config changes** — workflows added after startup are not picked up until the server restarts.
- **No key rotation** — revoking and recreating a key requires sending the new key to the user manually (or via Stripe automation).

---

## 12. Future Improvements

### 12.1 Redis-backed global rate limiter (medium priority)
Replace per-worker in-memory token buckets with Redis so rate limits are globally accurate across all uvicorn workers. Zero API surface change — just swap the limiter backend.

### 12.2 Webhook on usage events (medium priority)
An optional `on_request` hook in config that Workflow API pings after every successful request, enabling pipe-through to Stripe metered billing, Slack, or a custom analytics endpoint.

### 12.3 Admin REST API (low priority)
A `/admin/*` set of endpoints protected by the admin key, allowing key management over HTTP instead of CLI. This would enable integrating key management into third-party dashboards or custom portals.

### 12.4 Per-workflow rate limits (medium priority, harder)
Currently rate limiting is per-key globally. A more granular model would allow different limits per (key, endpoint) pair — e.g., 200 RPM for `/run/summarize` and 50 RPM for `/run/translate` on the same key.

### 12.5 n8n native adapter (low priority)
Auto-detect n8n workflows via the n8n API so users don't need to copy-paste webhook URLs during setup. Significant scope increase — only relevant after the tool has a user base.

---

## 13. Summary

Workflow API is a single-operator, self-hosted API gateway designed for workflow creators who want to monetize access to their automations without infrastructure complexity.

The core insight is that the gap between "I built a workflow" and "I can sell API access to it" is entirely an auth + rate limiting + billing automation problem. Workflow API solves exactly that, nothing more, and stays out of the way of everything else.

**V2 production hardening adds:**
- SHA-256 key hashing (plaintext keys removed)
- HMAC Stripe webhook signature verification
- Persistent 48-hour cancellation grace period (DB-backed, multi-worker safe)
- Dual storage backends with migration tooling
- SSRF protection on all target URLs
- Clean gitignore covering all secrets and runtime state

---

*Built with FastAPI, httpx, Click, Rich, PyYAML, SQLite, stripe-python.*
*License: MIT*
