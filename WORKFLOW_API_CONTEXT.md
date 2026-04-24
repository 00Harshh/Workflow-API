# Workflow API — AI Assistant Context File

> **Drop this file into Antigravity, Codex, or any AI assistant as a Knowledge Item or custom instructions document.
> It gives the AI full context about this codebase so it can help immediately without exploring files.**

---

## What is Workflow API?

Workflow API is a **self-hosted API Gateway** that sits in front of n8n (or any HTTP webhook-based workflow tool) and adds:

- **API key authentication** (SHA-256 hashed, never stored in plaintext)
- **Per-key rate limiting** (token bucket, configurable per-key in req/min)
- **Async request proxying** (httpx, non-blocking event loop)
- **Async structured logging** (queue-based, never blocks a request)
- **Dual storage backends** (YAML with file locking, or SQLite with WAL mode)
- **Admin dashboard** (`/__workflow-api/dashboard`) with Chart.js graphs and live activity feed
- **Stripe webhook integration** — auto-creates and emails keys after a successful payment
- **HMAC signature verification** — every Stripe webhook verified before any business logic runs
- **48-hour grace period** — cancellations are deferred, DB-backed, multi-worker safe
- **SMTP email delivery** — sends API keys to customers on creation
- **SSRF protection** — target URLs validated at startup, private IPs blocked

**Stack:** Python 3.11+, FastAPI, Uvicorn, httpx, PyYAML, sqlite3 (stdlib), smtplib (stdlib), Jinja2, stripe.

---

## Project Root

```
./   # (your local clone of Workflow-API)
```

**Activate the virtualenv before running anything:**
```bash
source .venv/bin/activate
```

---

## Directory Layout

```
workFlow-apiV2/
├── main.py                  # FastAPI app — all routes, lifespan, webhook handler
├── cli.py                   # Rich CLI: start, keys, migrate, n8n commands
├── config.yaml              # Master config (workflows, keys, storage, email, stripe) — gitignored
├── config.example.yaml      # Safe template to commit — no secrets
├── requirements.txt
├── nginx.conf               # Drop-in nginx reverse proxy config
├── Dockerfile
│
├── core/
│   ├── auth.py              # Key hashing, validation, create/revoke, migration utils
│   ├── cancellation_scheduler.py  # Persistent 48h grace period (DB-backed poller)
│   ├── store.py             # KeyStore Protocol + singleton factory (get_store())
│   ├── store_yaml.py        # YAML backend (fcntl file locking, mtime-cached reads)
│   ├── store_sqlite.py      # SQLite backend (WAL mode, indexed hash lookup)
│   ├── limiter.py           # In-memory token bucket rate limiter (thread-safe Lock)
│   ├── logger.py            # Async log queue — never blocks the event loop
│   ├── proxy.py             # httpx async request forwarder
│   ├── security.py          # SSRF protection, IP extraction
│   ├── email_sender.py      # SMTP email delivery with Jinja2 HTML template
│   └── stripe_webhooks.py   # Stripe event processor (HMAC, deduplication, grace period)
│
├── templates/
│   └── dashboard.html       # Admin dashboard — dark glassmorphism, Chart.js doughnut
│
└── logs/
    └── usage.log            # Append-only structured JSON log lines
```

---

## config.yaml — Annotated

```yaml
workflows:
  - name: n8ntest                           # Unique workflow name (used in allowed_gateways)
    endpoint: /run/n8ntest                  # Workflow API exposes this URL
    target: http://localhost:5678/webhook-test/n8ntest2  # Proxied to here
    method: POST

keys:                                       # Managed by CLI — do NOT edit manually
  - name: Test
    key_hash: bc6ab51d...                   # SHA-256 of raw key — raw key is NEVER stored
    key_prefix: wfapi-XXXXXXXXXX           # First 16 chars for display only
    rate_limit_per_minute: 60
    created_at: '2026-04-21'
    expires_at:                             # null = never expires
    allowed_gateways:
      - n8ntest                             # Which /run/* routes this key can access

storage:
  backend: yaml                             # "yaml" or "sqlite"
  sqlite_path: workflow-api.db

email:
  enabled: false
  smtp_host: smtp.gmail.com
  smtp_port: 587
  smtp_user: you@gmail.com
  smtp_password: app-password-here          # Use SMTP_PASSWORD env var instead
  from_address: noreply@yourapp.com
  from_name: Workflow API

stripe:
  # webhook_secret is loaded from STRIPE_WEBHOOK_SECRET env var ONLY — never here
  api_key: sk_live_...                      # Used to fetch checkout line items
  rate_limit_per_minute: 60
  price_to_gateway:
    price_xxx: ["n8ntest"]                  # Map Stripe price IDs to gateways

server:
  host: 0.0.0.0
  port: 8000
```

**Environment variable overrides (secrets always go here, never in config.yaml):**
```bash
WORKFLOW_API_STORAGE=sqlite
WORKFLOW_API_ADMIN_KEY=your-admin-secret
STRIPE_WEBHOOK_SECRET=whsec_...              # REQUIRED for Stripe webhooks → 503 if missing
CANCELLATION_GRACE_SECONDS=172800            # 48h default
CANCELLATION_POLL_SECONDS=60                 # poller interval
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASSWORD=secret
```

---

## CLI Reference

```bash
# ── Start the server ──────────────────────────────────────────
python3 cli.py start                        # Single worker (dev)
python3 cli.py start --workers 4            # Multi-worker (production)
python3 cli.py start --port 8080

# ── Manage API Keys ───────────────────────────────────────────
python3 cli.py keys create \
  --name "CustomerName" \
  --rate-limit 120 \
  --gateways n8ntest \
  --expires 2027-01-01

python3 cli.py keys list
python3 cli.py keys revoke CustomerName

# ── Setup n8n workflows ───────────────────────────────────────
python3 cli.py n8n \
  --url http://localhost:5678/webhook-test/mywebhook \
  --name myworkflow \
  --force

# ── Database migrations ───────────────────────────────────────
python3 cli.py migrate hash-keys
python3 cli.py migrate yaml-to-sqlite
python3 cli.py migrate yaml-to-sqlite --switch
```

---

## HTTP API Reference

| Method | Route | Auth | Description |
|--------|-------|------|-------------|
| `POST` / `GET` | `/run/{name}` | Bearer key | Proxy request to workflow target |
| `GET` | `/health` | None | Active keys + gateway list |
| `GET` | `/__workflow-api/stats` | Admin / localhost | JSON traffic stats |
| `GET` | `/__workflow-api/dashboard` | Admin / localhost | HTML Dashboard (Chart.js) |
| `POST` | `/webhooks/stripe` | Stripe HMAC signature | Stripe checkout/subscription handler |
| `GET` | `/docs` | None | Swagger UI (FastAPI auto-generated) |

**Example request:**
```bash
curl -X POST http://localhost:8000/run/n8ntest \
  -H "Authorization: Bearer wfapi-YOUR_KEY_HERE" \
  -H "Content-Type: application/json" \
  -d '{"input":"hello","test":true}'
```

---

## Key Design Decisions

### 1. Key Hashing (SHA-256)
- Raw key format: `wfapi-{44 random chars}` via `secrets.token_urlsafe(33)`
- Immediately hashed before storage. Only `key_hash` + `key_prefix` (16 chars, display only) persist.
- On each request: Bearer token hashed → compared to stored hash.
- **The raw key can only be seen once at creation. It cannot be recovered.**

### 2. Storage Layer (`core/store.py`)
`get_store()` returns a singleton implementing `KeyStore` protocol:
```python
class KeyStore(Protocol):
    def get_all_keys(self) -> list[dict]: ...
    def add_key(self, record: dict) -> None: ...
    def revoke_key_by_hash(self, key_hash: str) -> bool: ...
    def find_key_by_hash(self, key_hash: str) -> dict | None: ...
    def find_key_by_email(self, email: str) -> list[dict]: ...
    def add_pending_cancellation(self, subscription_id: str, revoke_at: str) -> None: ...
    def remove_pending_cancellation(self, subscription_id: str) -> bool: ...
    def get_due_cancellations(self) -> list[dict]: ...
    def is_stripe_event_processed(self, event_id: str) -> bool: ...
    def mark_stripe_event_processed(self, event_id: str) -> None: ...
```
Switch backends via `config.yaml: storage.backend: sqlite` — zero code changes.

### 3. HMAC Webhook Verification (`core/stripe_webhooks.py`)
- `construct_event()` reads `STRIPE_WEBHOOK_SECRET` from env var **only** — never from config.
- Missing secret → `StripeWebhookConfigError` → HTTP 503 (all webhooks rejected).
- HMAC mismatch → `stripe.error.SignatureVerificationError` → HTTP 401.
- Signature check runs before any business logic or DB access.

### 4. Grace Period Architecture (`core/cancellation_scheduler.py`)
```
customer.subscription.deleted
  → schedule_revocation() writes pending_cancellation to DB
  → Key stays ACTIVE

Background poller (asyncio.Task, started in lifespan):
  → polls every CANCELLATION_POLL_SECONDS
  → calls get_due_cancellations() → revokes expired keys

customer.subscription.created / invoice.payment_succeeded
  → cancel_pending_revocation() removes the DB record
  → Key stays active ✅
```
Survives restarts and works across multiple uvicorn workers because state lives in the DB, not RAM.

### 5. Async Logging (`core/logger.py`)
- `log_request()` = `asyncio.Queue.put_nowait()` — **~0.001ms, never stalls**.
- Background task `start_log_writer()` drains the queue and writes to `logs/usage.log`.

### 6. Auth Hot Path Performance
| Step | Cost |
|------|------|
| SHA-256 hash of token | ~0.005ms |
| Config mtime check + cache | ~0.01ms |
| Key lookup | ~0.01ms |
| Rate limiter | ~0.01ms |
| Log enqueue | ~0.001ms |
| **Total Workflow API overhead** | **~0.05ms** |

---

## Stripe Integration Flow

1. **HMAC first:** Every `/webhooks/stripe` request verified via `Stripe-Signature` header
   using `STRIPE_WEBHOOK_SECRET` env var. Invalid signatures → 401 before any business logic.
2. **Deduplication:** `event.id` checked against `stripe_events` table; duplicates are silently acknowledged.
3. Customer pays → Stripe fires `checkout.session.completed` → `/webhooks/stripe`
4. `stripe_webhooks.py` extracts `customer_email`, maps `price_id` → `allowed_gateways`
5. `create_key()` called → raw key shown once → stored as SHA-256 hash
6. `async_send_api_key_email()` sends raw key to customer

**Grace period on cancellation:**
7. Customer cancels → Stripe fires `customer.subscription.deleted`
8. Key is NOT revoked immediately — `pending_cancellation` record written to DB with `revoke_at` timestamp
9. Background poller checks every 60s for due cancellations
10. If `customer.subscription.created` or `invoice.payment_succeeded` arrives within 48h:
    → pending record is deleted → key stays active
11. If 48h pass with no reactivation → key is revoked automatically

---

## Security Summary

| Feature | Implementation |
|---------|----------------|
| SHA-256 key hashing | `hashlib.sha256` in `core/auth.py` — raw key never persisted |
| HMAC webhook verification | `stripe.Webhook.construct_event` in `core/stripe_webhooks.py` |
| Env-only webhook secret | `STRIPE_WEBHOOK_SECRET` only — never read from `config.yaml` |
| SSRF protection | `core/security.py` — blocks localhost, RFC1918, cloud metadata IPs |
| Constant-time admin auth | `secrets.compare_digest` in `main.py` |
| Persistent grace period | SQLite table / YAML JSON — survives restarts, multi-worker safe |
| Event deduplication | `stripe_events` table — prevents double-key-creation on retries |
| Gitignore coverage | `config.yaml`, `*.db`, `*.lock`, `logs/`, `site/`, `.env*` all excluded |

---

## Production Deployment Checklist

```bash
# 1. Switch to SQLite (recommended for production)
python3 cli.py migrate yaml-to-sqlite --switch

# 2. Multi-worker start
python3 cli.py start --workers 4

# 3. Set environment secrets (NEVER put these in config.yaml)
export WORKFLOW_API_ADMIN_KEY="random-secret"
export STRIPE_WEBHOOK_SECRET="whsec_..."
export SMTP_HOST="smtp.gmail.com"
export SMTP_PASSWORD="your-app-password"

# 4. Put nginx in front (see nginx.conf)
nginx -t && nginx
```

**Expected throughput:**

| Setup | RPS |
|-------|-----|
| 1 worker, YAML | ~1,500 |
| 1 worker, SQLite | ~2,500 |
| 4 workers, SQLite | ~8,000 |

> n8n itself tops out at ~20–100 RPS. Workflow API's ~0.05ms overhead will never be your bottleneck.

---

## Common AI Assistant Tasks

| Task | Command |
|------|---------|
| Add a new workflow | `python3 cli.py n8n --url ... --name ...` |
| Create a key | `python3 cli.py keys create --name X --rate-limit 60 --gateways n8ntest` |
| Revoke a key | `python3 cli.py keys revoke X` |
| Switch to SQLite | `python3 cli.py migrate yaml-to-sqlite --switch` |
| Enable email | Set `email.enabled: true` + SMTP creds in `config.yaml` or env vars |
| Check live stats | `curl http://localhost:8000/__workflow-api/stats` |
| View dashboard | `http://localhost:8000/__workflow-api/dashboard` |
| Restart server | Kill port 8000, then `python3 cli.py start` |
| Test Stripe locally | `stripe listen --forward-to localhost:8000/webhooks/stripe` |
| Trigger test event | `stripe trigger checkout.session.completed` |
