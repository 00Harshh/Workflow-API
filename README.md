```text
 __      __         _  __  __               _    ___ ___  
 \ \    / /__ _ _ _| |/ _|/ /_____ __ __   /_\  | _ \_ _|
  \ \/\/ / _ \ '_| |  _| | / _ \ V  V /  / _ \ |  _/ | | 
   \_/\_/\___/_| |_|_| |_|_\___/\_/\_/  /_/ \_\|_|  |___|
```

# Workflow API

Turn any HTTP workflow into a protected, rate-limited, monetizable API.

Workflow API is a small self-hosted gateway for n8n, Zapier, custom scripts, or any workflow with a webhook URL. It sits in front of your workflow and adds:

- API key authentication
- Per-key rate limits
- Optional key expiration
- Optional per-workflow key scoping
- JSON access logs
- Usage stats and a read-only dashboard
- Optional Stripe webhook automation for subscription-based key creation and revocation
- 48-hour grace period on subscription cancellation with automatic reactivation
- HMAC webhook signature verification on all Stripe events

Workflow API does not run your workflow, process payments, or require a database. Everything lives in `config.yaml` and `logs/`.



---

## How It Works

Workflow API acts as a transparent, high-performance gateway between your customers and your backend workflows. It excels at **protecting lead generation endpoints**, AI app routing, or SaaS integrations.

```text
Customer or app
  -> Workflow API endpoint, for example /run/generate-lead
  -> API key validation
  -> per-key rate limit check
  -> optional gateway scope check
  -> your workflow webhook URL
  -> response returned unchanged
```

**GET vs POST Requests:**
Workflow API completely supports both `GET` and `POST` methods. When configuring a workflow in `config.yaml`, you declare the HTTP method it listens to. If you need a single endpoint strategy that accommodates both (for example, fetching records and creating records), you simply register two workflow configurations spanning both methods, and your user's API key will seamlessly handle both limits.

**How it Works with SQL Databases:**
Workflow API uses an embedded **local SQLite database** (`workflow-api.db`) to quickly handle key hashing, rate limits, and billing without forcing you to set up an external database. 
If your specific business logic (e.g. fetching lead-gen data) requires querying Postgres or MySQL, **you connect that SQL Database directly to your Workflow Engine (like n8n), NOT to Workflow API.**
1. The user sends a request to the Workflow API.
2. The API secures, rate-limits, and forwards it to n8n.
3. n8n queries your raw PostgreSQL/MySQL database.
4. n8n returns the data, which Workflow API securely bounces back to the user.
This separation of concerns means you can protect literally any tech stack without rewriting connection code.

In this README, "gateway" means a configured workflow entry. The current CLI writes these under `workflows:` in `config.yaml`, and Workflow API also supports a `gateways:` section for newer configs.

---

## 2 Ways to Generate an API Key

How keys are created usually depends on whether you're selling access or creating internal tooling. Workflow API fully supports both out of the box:

### 1. Fully Automated (Self-Serve via Stripe)
If you are generating income by selling access to your API:
1. You share a Stripe Payment Link.
2. The user buys a subscription.
3. Stripe hits Workflow API's background webhook mechanism.
4. Workflow API instantly provisions a new scoped, rate-limited key, embeds it in an HTML email, and dispatches it automatically to the buyer. Passive income, zero manual work.

### 2. Manual Command Line (Free / Internal Use)
For internal use, individual clients, or free access, you can run a single command on your terminal to instantly mint a key:
```bash
workflow-api keys create --name "Client A" --rate-limit 120
```
This produces a hash-safeguarded secure key (`wfapi-...`) that you can directly send to your client via Slack/Email.

### Security Note: Where are API Keys Saved?
When you (or the Stripe integration) generate an API key, the system does **not** save the actual API key. 
Instead, it runs the raw key through a one-way mathematical function called a **SHA-256 Hash**, and only saves that scrambled hash in your `workflow-api.db` SQLite database (or your `config.yaml` depending on configuration). 
Because the key is securely hashed, neither you nor an attacker can reverse-engineer the original key. If a customer loses their key, there is no "Show API Key" button—you simply revoke the old one and generate a fresh key for them.

---

## Prerequisites

- Python 3.11 or newer
- A reachable workflow/webhook URL, for example an n8n Webhook node URL
- Optional: Docker for VPS deployment
- Optional: Stripe account and Stripe CLI for subscription automation

On many machines the command is `python3`, not `python`. The examples below use `python3`.

---

## 1. Install Workflow API

```bash
git clone https://github.com/yourusername/workflow-api.git
cd workflow-api

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

Optional convenience alias:

```bash
alias workflow-api="python3 cli.py"
```

If you do not add the alias, use `python3 cli.py` anywhere this README shows `workflow-api`.

---

## 2. Create Your First Gateway

For n8n, the easiest path is the one-command setup:

```bash
python3 cli.py n8n --url http://localhost:5678/webhook-test/n8ntest2 --name n8ntest --force
```

This writes `config.yaml`, creates a scoped test API key, and prints the exact `curl` command to test your Workflow API endpoint.

If you want the step-by-step wizard instead, run:

Run the setup wizard:

```bash
workflow-api init
```

You will be asked for:

```text
Workflow name: my-workflow
Webhook / target URL: http://localhost:5678/webhook/your-webhook-id
Endpoint path: /run/my-workflow
HTTP method: POST
Port: 8000
Create first API key now: Yes
```

This creates `config.yaml`.

Example:

```yaml
workflows:
  - name: my-workflow
    endpoint: /run/my-workflow
    target: http://localhost:5678/webhook/your-webhook-id
    method: POST

keys:
  - name: Pro
    key: wfapi-example
    rate_limit_per_minute: 100
    created_at: "2026-04-21"
    expires_at: null
    allowed_gateways: null

logging:
  file: logs/usage.log

server:
  host: 0.0.0.0
  port: 8000
```

You normally do not need to edit `config.yaml` manually except for optional admin and Stripe settings.

---

## 3. Start Workflow API

Make sure your workflow service is running first. For n8n, that usually means n8n is running and the webhook URL is active.

Then start Workflow API:

```bash
workflow-api start
```

Workflow API will listen on the configured port, usually:

```text
http://localhost:8000
```

Interactive API docs are available at:

```text
http://localhost:8000/docs
```

---

## 4. Call Your Workflow Through Workflow API

Use the key printed by `workflow-api init` or create a new one:

```bash
workflow-api key create --name Pro --rate-limit 100
```

Call the public Workflow API endpoint:

```bash
curl -X POST http://localhost:8000/run/my-workflow \
  -H "Authorization: Bearer wfapi-your-key-here" \
  -H "Content-Type: application/json" \
  -d '{"input": "hello"}'
```

Workflow API forwards the request body and query params to your workflow URL, then returns the workflow response.

---

## 5. Manage API Keys

Create an unlimited key:

```bash
workflow-api key create --name Enterprise --rate-limit 0
```

Create a temporary trial key:

```bash
workflow-api key create --name Trial --rate-limit 20 --expires-in 30d
```

Create a key that expires on a specific date:

```bash
workflow-api key create --name Trial --rate-limit 20 --expires-at 2026-12-31
```

List keys:

```bash
workflow-api key list
```

Revoke all keys with a given name:

```bash
workflow-api key revoke Trial
```

`key` and `keys` both work:

```bash
workflow-api keys list
workflow-api key list
```

---

## 6. Restrict Keys To Specific Gateways

By default, a key can call every configured workflow. To limit a key to specific workflow names, use `--gateways`.

Example:

```bash
workflow-api key create \
  --name Basic \
  --rate-limit 30 \
  --gateways my-workflow
```

Multiple gateways:

```bash
workflow-api key create \
  --name Pro \
  --rate-limit 200 \
  --gateways summarize,translate
```

Workflow API validates that each gateway name exists in `config.yaml`.

If a scoped key calls a gateway it is not allowed to use, Workflow API returns:

```json
{"detail": "Key not authorized for this workflow"}
```

Existing keys without `allowed_gateways` continue to work for all gateways.

---

## 7. View Logs

Show the last 20 log entries:

```bash
workflow-api logs
```

Follow logs live:

```bash
workflow-api logs --follow
```

Filter by severity:

```bash
workflow-api logs --level ERROR
```

Default log path:

```text
logs/usage.log
```

Override the log path with an environment variable:

```bash
WORKFLOW_API_LOG_FILE=/var/log/workflow-api.log workflow-api start
```

Or configure it in `config.yaml`:

```yaml
logging:
  file: logs/usage.log
```

Example log line:

```json
{"time": "2026-04-21T10:00:00Z", "level": "INFO", "event": "request", "endpoint": "/run/my-workflow", "gateway": "my-workflow", "tier": "Pro", "status": 200, "latency_ms": 142.3}
```

---

## 8. Enable Admin Stats And Dashboard

Localhost can access stats and the dashboard without an admin key.

Stats:

```bash
curl http://localhost:8000/__workflow-api/stats
```

Dashboard:

```text
http://localhost:8000/__workflow-api/dashboard
```

For remote access, set an admin key. You can use an environment variable:

```bash
export WORKFLOW_API_ADMIN_KEY="change-this-admin-secret"
workflow-api start
```

Then call:

```bash
curl http://your-server:8000/__workflow-api/stats \
  -H "Authorization: Bearer change-this-admin-secret"
```

Or:

```bash
curl http://your-server:8000/__workflow-api/stats \
  -H "X-Admin-Key: change-this-admin-secret"
```

You can also store the admin key in `config.yaml`:

```yaml
admin:
  api_key: "change-this-admin-secret"
```

Dashboard URL:

```text
http://your-server:8000/__workflow-api/dashboard
```

The dashboard is read-only. It does not create, edit, or revoke anything.

---

## 9. Optional Stripe Automation

Stripe automation lets Workflow API create and revoke API keys from subscription events.

What it does:

- `checkout.session.completed` creates a new scoped, rate-limited API key and emails it to the customer
- The key scope comes from the Stripe Price ID mapping
- The key gets `stripe_subscription_id` for lifecycle tracking
- `customer.subscription.deleted` schedules key revocation after a **48-hour grace period**
- `customer.subscription.created` or `invoice.payment_succeeded` cancels a pending revocation and keeps the key active
- Duplicate Stripe events are deduplicated and ignored
- Every incoming webhook is verified via **HMAC signature validation** before any business logic runs

What it does not do:

- Workflow API does not manage Stripe products or prices
- Workflow API does not expose the generated key in the webhook response

### 9.1 HMAC Webhook Signature Verification

Every request to `/webhooks/stripe` is verified using Stripe's `Stripe-Signature` header before any business logic executes. This prevents forged or tampered webhook payloads from reaching your system.

**How it works:**

```text
Stripe sends POST /webhooks/stripe
  → Read raw body + Stripe-Signature header
  → stripe.Webhook.construct_event(body, signature, secret)
  → HMAC mismatch? → 401 Unauthorized (request rejected)
  → Signature valid? → proceed to process event
```

**Configuration:**

The webhook signing secret is loaded **exclusively** from the `STRIPE_WEBHOOK_SECRET` environment variable. It is never read from `config.yaml` to prevent accidental leakage into version control.

```bash
export STRIPE_WEBHOOK_SECRET="whsec_your_webhook_secret"
```

Get this value from:
- **Stripe Dashboard:** Developers → Webhooks → your endpoint → Signing secret
- **Stripe CLI (local testing):** `stripe listen --forward-to localhost:8000/webhooks/stripe` prints it

If `STRIPE_WEBHOOK_SECRET` is not set, the endpoint returns `503` and all webhooks are rejected.

### 9.2 Cancellation Grace Period (48 Hours)

When a customer cancels their subscription, Workflow API does **not** revoke the API key immediately. Instead, it schedules revocation after a 48-hour grace period. If the customer resubscribes or pays an outstanding invoice within that window, the pending revocation is cancelled and the key stays active.

**Flow:**

```text
Stripe fires customer.subscription.deleted
  → Workflow API writes a pending_cancellation record to the database
  → Key stays ACTIVE for 48 hours

Case A: Customer resubscribes within 48h
  → Stripe fires customer.subscription.created or invoice.payment_succeeded
  → Workflow API deletes the pending_cancellation record
  → Key remains active ✅

Case B: 48 hours pass without reactivation
  → Background poller detects the due cancellation
  → Key is revoked 🔒
```

The grace period is **persistent** — it survives server restarts and works correctly across multiple workers because the state is stored in the database (SQLite table or YAML JSON file), not in memory.

**Configuration:**

```bash
# Override the default 48-hour grace period (value in seconds)
export CANCELLATION_GRACE_SECONDS=172800

# Override how often the poller checks for due revocations (default: 60s)
export CANCELLATION_POLL_SECONDS=60
```

### 9.3 Stripe Events Handled

| Stripe Event | Action |
|---|---|
| `checkout.session.completed` | Create API key, map Price ID to gateway scope, email key to customer |
| `customer.subscription.deleted` | Schedule key revocation after 48h grace period |
| `customer.subscription.created` | Cancel pending revocation if one exists for this subscription |
| `invoice.payment_succeeded` | Cancel pending revocation if one exists for this subscription |
| Any other event | Acknowledged but ignored |

### 9.4 Configure Stripe

Set environment variables for secrets:

```bash
export STRIPE_WEBHOOK_SECRET="whsec_your_webhook_secret"
```

Edit `config.yaml` for non-secret settings:

```yaml
stripe:
  api_key: "sk_live_or_test_key"
  rate_limit_per_minute: 100
  price_to_gateway:
    "price_basic": ["my-workflow"]
    "price_pro": ["my-workflow", "another-workflow"]
```

Notes:

- `api_key` is used to fetch checkout line items when Stripe does not include them in the event payload.
- `price_to_gateway` maps Stripe Price IDs to Workflow API workflow names.
- `rate_limit_per_minute` is optional and defaults to `60` for Stripe-created keys.

### 9.5 Add The Stripe Webhook Endpoint

In Stripe, set your webhook endpoint URL to:

```text
https://your-domain.com/webhooks/stripe
```

Select these events:

```text
checkout.session.completed
customer.subscription.deleted
customer.subscription.created
invoice.payment_succeeded
```

For local testing with Stripe CLI:

```bash
stripe listen --forward-to localhost:8000/webhooks/stripe
```

Copy the `whsec_...` secret printed by Stripe CLI into your environment:

```bash
export STRIPE_WEBHOOK_SECRET="whsec_..."
```

### 9.6 Test Stripe Locally

Start Workflow API:

```bash
export STRIPE_WEBHOOK_SECRET="whsec_from_stripe_listen"
workflow-api start
```

In another terminal:

```bash
stripe trigger checkout.session.completed
```

Then check your database. A new key should appear with:

```yaml
allowed_gateways:
  - my-workflow
stripe_subscription_id: sub_...
```

Trigger deletion (with grace period):

```bash
stripe trigger customer.subscription.deleted
```

The key will be marked as pending cancellation. It will be revoked after 48 hours unless a reactivation event arrives.

Important: Stripe's default trigger payload may use a test Price ID that is not in your `price_to_gateway` mapping. If no key is created, check `workflow-api logs` for a `stripe_price_unmapped` warning and add the test Price ID to the mapping.

---

## 10. Building a Custom Dashboard or Portal (Headless Mode)

Workflow API is built as a pure **Faceless Developer Tool**. For maximum security, it does not include public-facing HTML lookups or a dashboard by default.

If you want to build your own custom GUI (e.g. in React, Next.js, or Vue):
- **Admin Dashboard:** Make an authenticated `GET` request from your Frontend/Backend to `http://your-server:8000/__workflow-api/stats` using your `X-Admin-Key`. This returns a rich JSON payload containing live traffic metrics, active keys, and recent activity.
- **Customer Portal:** If you want your customers to look up their API usage or endpoints, your custom frontend should query the `workflow-api.db` SQLite database directly (or you can use the CLI dynamically).

---


## 10. Deploy 24/7 (Live Production)

To deploy the Workflow API so it runs automatically 24/7—even after server reboots—choose one of these highly resilient options:

### Option A: The "It Just Works" Cloud (Render / Railway)
Best for developers who want a hands-off deployment. You can push workflows directly to Platform-as-a-service providers.
1. Push this code to a private GitHub repo.
2. Connect it to [Render.com](https://render.com) or [Railway.app](https://railway.app) as a new Web Service.
3. Add your `WORKFLOW_API_ADMIN_KEY` as an environment variable.
4. *Important:* Attach a Persistent Disk/Volume to the `/app/workflow-api.db` file so your API key hashes survive cloud redeploys.

### Option B: The "Self-Hosted" VPS (DigitalOcean / AWS)
Best for maximum edge-performance and total control. Place a `docker-compose.yml` file on an Ubuntu server:
```yaml
version: '3.8'
services:
  workflow-api:
    build: .
    container_name: workflow-api
    restart: always  # Guarantees 24/7 uptime unconditionally
    ports:
      - "8000:8000"
    volumes:
      - ./config.yaml:/app/config.yaml
      - ./workflow-api.db:/app/workflow-api.db
      - ./logs:/app/logs
    environment:
      - WORKFLOW_API_ADMIN_KEY=change-this-admin-secret
```
Turn it on by typing `docker-compose up -d --build`. The container will instantly boot up and securely stay alive in the background.

With admin key:

```bash
docker run -d \
  -p 8000:8000 \
  -e WORKFLOW_API_ADMIN_KEY="change-this-admin-secret" \
  -v $(pwd)/config.yaml:/app/config.yaml \
  -v $(pwd)/logs:/app/logs \
  --name workflow-api \
  workflow-api
```

View container logs:

```bash
docker logs -f workflow-api
```

View Workflow API access logs from the mounted directory:

```bash
tail -f logs/usage.log
```

---

## 11. Production Checklist

Before exposing Workflow API publicly:

**Security:**

- Put Workflow API behind HTTPS, for example Caddy, Nginx, Traefik, or a cloud load balancer.
- Set `WORKFLOW_API_ADMIN_KEY` as an environment variable.
- Set `STRIPE_WEBHOOK_SECRET` as an environment variable (never in `config.yaml`).
- Keep `config.yaml` private because it contains API key hashes.
- Use filesystem permissions so only the Workflow API user can read `config.yaml` and `workflow-api.db`.
- Use Stripe test mode before switching to live mode.

**Persistence:**

- Back up `config.yaml` and/or `workflow-api.db`.
- Mount `logs/` and `workflow-api.db` as persistent volumes if using Docker.
- Configure log rotation for long-running deployments.

**Required environment variables:**

```bash
export WORKFLOW_API_ADMIN_KEY="your-admin-secret"
export STRIPE_WEBHOOK_SECRET="whsec_your_webhook_secret"

# Optional overrides
export CANCELLATION_GRACE_SECONDS=172800   # default: 48 hours
export CANCELLATION_POLL_SECONDS=60        # default: 60s
export SMTP_HOST="smtp.gmail.com"
export SMTP_PASSWORD="your-app-password"
```

---

## 12. Troubleshooting

`401 Invalid or missing API key`

Check the header format:

```text
Authorization: Bearer wfapi-your-key
```

`403 API key expired`

Create a new key or use one without `expires_at`.

`403 Key not authorized for this workflow`

The key has `allowed_gateways` and the workflow name is not included. Run:

```bash
workflow-api key list
```

Then create a correctly scoped key:

```bash
workflow-api key create --name Pro --rate-limit 200 --gateways my-workflow
```

`429 Rate limit exceeded`

The key has used its per-minute allowance. Create a higher-tier key or wait for the bucket to refill.

`502 Could not reach workflow`

Workflow API is running, but your target workflow URL is not reachable. Check that n8n, Zapier, or your app is running and that the target URL in `config.yaml` is correct.

`Stripe webhook returns 401`

The `Stripe-Signature` header failed HMAC verification. This means either:
- The `STRIPE_WEBHOOK_SECRET` env var does not match the secret for your Stripe webhook endpoint.
- The request was not sent by Stripe (forged payload).

`Stripe webhook returns 503`

`STRIPE_WEBHOOK_SECRET` environment variable is not set. Set it and restart.

`Stripe webhook succeeds but no key appears`

Check:

- The event is `checkout.session.completed`.
- The Checkout Session has a subscription ID.
- The Stripe Price ID exists in `stripe.price_to_gateway` in `config.yaml`.
- The mapped gateway names exist in `workflows:` or `gateways:`.
- `workflow-api logs` for `stripe_price_unmapped` or `stripe_scope_invalid`.

`Key not revoked after subscription cancellation`

Key revocation has a 48-hour grace period by default. The key will be revoked automatically after the grace window expires. Check `workflow-api logs` for `cancellation_scheduled` to confirm the pending revocation was recorded.

---

## CLI Reference

```bash
workflow-api init
workflow-api start
workflow-api status

workflow-api key create
workflow-api key create --name Pro --rate-limit 200
workflow-api key create --name Trial --rate-limit 20 --expires-in 30d
workflow-api key create --name Basic --rate-limit 30 --gateways my-workflow
workflow-api key list
workflow-api key revoke Pro

workflow-api logs
workflow-api logs --follow
workflow-api logs --level ERROR
```

Without the alias:

```bash
python3 cli.py status
python3 cli.py key list
```

---

## Project Structure

```text
workflow-api/
  cli.py                          # CLI: start, keys, migrate, n8n commands
  main.py                         # FastAPI app: routes, lifespan, webhook handler
  config.yaml                     # Master config (gitignored — use config.example.yaml)
  core/
    auth.py                       # Key hashing, validation, create/revoke
    cancellation_scheduler.py     # Persistent 48h grace period poller
    email_sender.py               # SMTP email delivery with HTML template
    limiter.py                    # In-memory token bucket rate limiter
    logger.py                     # Async log queue — never blocks the event loop
    proxy.py                      # httpx async request forwarder
    security.py                   # SSRF protection, IP extraction
    store.py                      # KeyStore protocol + singleton factory
    store_sqlite.py               # SQLite backend (WAL mode, production)
    store_yaml.py                 # YAML backend (file locking, dev)
    stripe_webhooks.py            # Stripe event processing + HMAC verification
  templates/
    dashboard.html                # Admin dashboard (Chart.js)
  logs/
    usage.log                     # Structured JSON access log
  nginx.conf                      # Drop-in nginx reverse proxy config
  Dockerfile
  requirements.txt
```

---

## Security

Workflow API includes the following security measures:

| Feature | Description |
|---------|-------------|
| **SHA-256 key hashing** | API keys are hashed before storage. Raw keys are shown once at creation and cannot be recovered. |
| **HMAC webhook verification** | Every Stripe webhook is verified via `Stripe-Signature` header before any business logic runs. Failures return `401`. |
| **Env-only secrets** | The Stripe webhook secret (`STRIPE_WEBHOOK_SECRET`) is loaded exclusively from environment variables, never from config files. |
| **Scrubbed error responses** | Internal errors on the webhook endpoint return generic messages. Exception details are logged internally only. |
| **SSRF protection** | Target workflow URLs are validated at startup. Requests to localhost, private IPs, and cloud metadata endpoints are blocked. |
| **Constant-time admin auth** | Admin key comparison uses `secrets.compare_digest` to prevent timing attacks. |
| **Persistent grace period** | Pending cancellations are stored in the database (not memory), surviving restarts and working across multiple workers. |
| **Gitignore coverage** | `config.yaml`, database files, state files (`pending_cancellations.json`, `resend_cooldown.json`), and logs are all gitignored. |

---

## License

MIT
