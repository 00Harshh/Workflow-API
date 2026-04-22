# Workflow API — AI Assistant Context File

> **Drop this file into your Antigravity, Codex, or any AI assistant as a Knowledge Item or custom instructions document.
> It gives the AI full context about this codebase so it can help immediately without exploring files.**

---

## What is Workflow API?

Workflow API is a **self-hosted API Gateway** that sits in front of n8n (or any HTTP webhook-based workflow tool) and adds:

- **API key authentication** (SHA-256 hashed, never stored in plaintext)
- **Per-key rate limiting** (token bucket, configurable per-key in req/min)
- **Async request proxying** (httpx, non-blocking event loop)
- **Async structured logging** (queue-based, never blocks a request)
- **Dual storage backends** (YAML with file locking, or SQLite with WAL mode)
- **Self-serve user portal** (`/portal`) for customers to look up and regenerate their own keys
- **Admin dashboard** (`/__workflow-api/dashboard`) with Chart.js graphs and live activity feed
- **Stripe webhook integration** — auto-creates and emails keys after a successful payment
- **SMTP email delivery** — sends API keys to customers on creation/regeneration

**Stack:** Python 3.11+, FastAPI, Uvicorn, httpx, PyYAML, sqlite3 (stdlib), smtplib (stdlib), Jinja2.

---

## Project Root

```
/Users/harshjoshi/Downloads/workFlow-apiV2/
```

**Activate the virtualenv before running anything:**
```bash
source /Users/harshjoshi/myenv-tf/bin/activate
```

---

## Directory Layout

```
workFlow-apiV2/
├── main.py                  # FastAPI app — all routes, lifespan, portal endpoints
├── cli.py                   # Rich CLI: start, keys, migrate, n8n commands
├── config.yaml              # Master config (workflows, keys, storage, email, stripe)
├── load_test.py             # Async load tester (asyncio + httpx, configurable RPS)
├── requirements.txt
├── nginx.conf               # Drop-in nginx reverse proxy config
├── Dockerfile
│
├── core/
│   ├── auth.py              # Key hashing, validation, create/revoke, migration utils
│   ├── store.py             # KeyStore Protocol + singleton factory (get_store())
│   ├── store_yaml.py        # YAML backend (fcntl file locking, mtime-cached reads)
│   ├── store_sqlite.py      # SQLite backend (WAL mode, indexed hash lookup)
│   ├── limiter.py           # In-memory token bucket rate limiter (thread-safe Lock)
│   ├── logger.py            # Async log queue — never blocks the event loop
│   ├── proxy.py             # httpx async request forwarder
│   ├── email_sender.py      # SMTP email delivery with Jinja2 HTML template
│   └── stripe_webhooks.py   # Stripe event processor (async, deduplication)
│
├── templates/
│   ├── dashboard.html       # Admin dashboard — dark glassmorphism, Chart.js doughnut
│   └── portal.html          # User self-serve portal — email lookup + key regeneration
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
    key_prefix: wfapi-IDqQT_NRP_           # First 16 chars for display only
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
  smtp_password: app-password-here
  from_address: noreply@yourapp.com
  from_name: Workflow API

stripe:
  webhook_secret: whsec_...
  api_key: sk_live_...
  rate_limit_per_minute: 60
  price_to_gateway:
    price_xxx: ["n8ntest"]                  # Map Stripe price IDs to gateways

server:
  host: 0.0.0.0
  port: 8000
```

**Environment variable overrides:**
```bash
WORKFLOW_API_STORAGE=sqlite
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASSWORD=secret
WORKFLOW_API_ADMIN_KEY=your-admin-secret
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
| `POST` | `/run/{name}` | Bearer key | Proxy request to workflow target |
| `GET` | `/health` | None | Active keys + gateway list |
| `GET` | `/__workflow-api/stats` | Admin / localhost | JSON traffic stats |
| `GET` | `/__workflow-api/dashboard` | Admin / localhost | HTML Dashboard (Chart.js) |
| `GET` | `/portal` | None | User self-serve portal |
| `POST` | `/portal/lookup` | None | Look up key by email |
| `POST` | `/portal/resend` | None | Regenerate key + send email |
| `POST` | `/webhooks/stripe` | Stripe signature | Stripe checkout handler |
| `GET` | `/docs` | None | Swagger UI |

**Example request:**
```bash
curl -X POST http://localhost:8000/run/n8ntest \
  -H "Authorization: Bearer wfapi-IDqQT_NRP_QqD5w6cl0LXkkAWawVjdZ0wa2qyoHBIec" \
  -H "Content-Type: application/json" \
  -d '{"name":"Harsh","test":true}'
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
```
Switch backends via `config.yaml: storage.backend: sqlite` — zero code changes.

### 3. Async Logging (`core/logger.py`)
- `log_request()` = `asyncio.Queue.put_nowait()` — **~0.001ms, never stalls**.
- Background task `start_log_writer()` drains the queue and writes to `logs/usage.log`.

### 4. Auth Hot Path Performance
| Step | Cost |
|------|------|
| SHA-256 hash of token | ~0.005ms |
| Config mtime check + cache | ~0.01ms |
| Key lookup | ~0.01ms |
| Rate limiter | ~0.01ms |
| Log enqueue | ~0.001ms |
| **Total Workflow API overhead** | **~0.05ms** |

---

## Load Testing

```bash
# 20 RPS for 10 seconds
python3 load_test.py \
  --key wfapi-Yj2EcMui9CzgHq1VMBSioPuPLzRl7Ln8_DWCYqxUPqI \
  --rps 20 --duration 10

# 50 RPS stress test
python3 load_test.py \
  --key wfapi-Yj2EcMui9CzgHq1VMBSioPuPLzRl7Ln8_DWCYqxUPqI \
  --rps 50 --duration 15
```

---

## Stripe Integration Flow

1. Customer pays → Stripe fires `checkout.session.completed` → `/webhooks/stripe`
2. `stripe_webhooks.py` extracts `customer_email`, maps `price_id` → `allowed_gateways`
3. `create_key()` called → raw key shown once → stored as hash
4. `async_send_api_key_email()` sends raw key to customer
5. Customer can use `/portal` to look up or regenerate at any time

---

## Production Deployment Checklist

```bash
# 1. Switch to SQLite
python3 cli.py migrate yaml-to-sqlite --switch

# 2. Multi-worker start
python3 cli.py start --workers 4

# 3. Set environment secrets
export WORKFLOW_API_ADMIN_KEY="random-secret"
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
| 4 workers + Redis limiter | ~6,000 (globally accurate) |

> n8n itself tops out at ~20–100 RPS. Workflow API's ~0.05ms overhead will never be your bottleneck.

---

## Current Keys (2026-04-21)

| Name | Prefix | Rate Limit | Scope |
|------|--------|------------|-------|
| Test | `wfapi-IDqQT_NRP_` | 60 RPM | n8ntest |
| LoadTest | `wfapi-Yj2EcMui9C` | 1200 RPM | n8ntest |

> Raw keys shown once at creation only. Recreate if lost.

---

## Common AI Assistant Tasks

| Task | Command |
|------|---------|
| Add a new workflow | `python3 cli.py n8n --url ... --name ...` |
| Create a key | `python3 cli.py keys create --name X --rate-limit 60 --gateways n8ntest` |
| Revoke a key | `python3 cli.py keys revoke X` |
| Switch to SQLite | `python3 cli.py migrate yaml-to-sqlite --switch` |
| Enable email | Set `email.enabled: true` + SMTP in `config.yaml` |
| Check live stats | `curl http://localhost:8000/__workflow-api/stats` |
| Run load test | `python3 load_test.py --key wfapi-... --rps 20 --duration 10` |
| View dashboard | `http://localhost:8000/__workflow-api/dashboard` |
| User portal | `http://localhost:8000/portal` |
| Restart server | Kill port 8000, then `python3 cli.py start` |
