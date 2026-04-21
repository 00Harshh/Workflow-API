# FlowGate

Turn any HTTP workflow into a protected, rate-limited, monetizable API.

FlowGate is a small self-hosted gateway for n8n, Zapier, custom scripts, or any workflow with a webhook URL. It sits in front of your workflow and adds:

- API key authentication
- Per-key rate limits
- Optional key expiration
- Optional per-workflow key scoping
- JSON access logs
- Usage stats and a read-only dashboard
- Optional Stripe webhook automation for subscription-based key creation and revocation

FlowGate does not run your workflow, process payments, or require a database. Everything lives in `config.yaml` and `logs/`.

Want a local dry run first? Follow [DEMO_TESTING.md](DEMO_TESTING.md) to test FlowGate end to end with a mock webhook server.

---

## How It Works

```text
Customer or app
  -> FlowGate endpoint, for example /run/summarize
  -> API key validation
  -> per-key rate limit check
  -> optional gateway scope check
  -> your workflow webhook URL
  -> response returned unchanged
```

In this README, "gateway" means a configured workflow entry. The current CLI writes these under `workflows:` in `config.yaml`, and FlowGate also supports a `gateways:` section for newer configs.

---

## Prerequisites

- Python 3.11 or newer
- A reachable workflow/webhook URL, for example an n8n Webhook node URL
- Optional: Docker for VPS deployment
- Optional: Stripe account and Stripe CLI for subscription automation

On many machines the command is `python3`, not `python`. The examples below use `python3`.

---

## 1. Install FlowGate

```bash
git clone https://github.com/yourusername/flowgate.git
cd flowgate

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

Optional convenience alias:

```bash
alias flowgate="python3 cli.py"
```

If you do not add the alias, use `python3 cli.py` anywhere this README shows `flowgate`.

---

## 2. Create Your First Gateway

Run the setup wizard:

```bash
flowgate init
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

## 3. Start FlowGate

Make sure your workflow service is running first. For n8n, that usually means n8n is running and the webhook URL is active.

Then start FlowGate:

```bash
flowgate start
```

FlowGate will listen on the configured port, usually:

```text
http://localhost:8000
```

Interactive API docs are available at:

```text
http://localhost:8000/docs
```

---

## 4. Call Your Workflow Through FlowGate

Use the key printed by `flowgate init` or create a new one:

```bash
flowgate key create --name Pro --rate-limit 100
```

Call the public FlowGate endpoint:

```bash
curl -X POST http://localhost:8000/run/my-workflow \
  -H "Authorization: Bearer wfapi-your-key-here" \
  -H "Content-Type: application/json" \
  -d '{"input": "hello"}'
```

FlowGate forwards the request body and query params to your workflow URL, then returns the workflow response.

---

## 5. Manage API Keys

Create an unlimited key:

```bash
flowgate key create --name Enterprise --rate-limit 0
```

Create a temporary trial key:

```bash
flowgate key create --name Trial --rate-limit 20 --expires-in 30d
```

Create a key that expires on a specific date:

```bash
flowgate key create --name Trial --rate-limit 20 --expires-at 2026-12-31
```

List keys:

```bash
flowgate key list
```

Revoke all keys with a given name:

```bash
flowgate key revoke Trial
```

`key` and `keys` both work:

```bash
flowgate keys list
flowgate key list
```

---

## 6. Restrict Keys To Specific Gateways

By default, a key can call every configured workflow. To limit a key to specific workflow names, use `--gateways`.

Example:

```bash
flowgate key create \
  --name Basic \
  --rate-limit 30 \
  --gateways my-workflow
```

Multiple gateways:

```bash
flowgate key create \
  --name Pro \
  --rate-limit 200 \
  --gateways summarize,translate
```

FlowGate validates that each gateway name exists in `config.yaml`.

If a scoped key calls a gateway it is not allowed to use, FlowGate returns:

```json
{"detail": "Key not authorized for this workflow"}
```

Existing keys without `allowed_gateways` continue to work for all gateways.

---

## 7. View Logs

Show the last 20 log entries:

```bash
flowgate logs
```

Follow logs live:

```bash
flowgate logs --follow
```

Filter by severity:

```bash
flowgate logs --level ERROR
```

Default log path:

```text
logs/usage.log
```

Override the log path with an environment variable:

```bash
FLOWGATE_LOG_FILE=/var/log/flowgate.log flowgate start
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
curl http://localhost:8000/__flowgate/stats
```

Dashboard:

```text
http://localhost:8000/__flowgate/dashboard
```

For remote access, set an admin key. You can use an environment variable:

```bash
export FLOWGATE_ADMIN_KEY="change-this-admin-secret"
flowgate start
```

Then call:

```bash
curl http://your-server:8000/__flowgate/stats \
  -H "Authorization: Bearer change-this-admin-secret"
```

Or:

```bash
curl http://your-server:8000/__flowgate/stats \
  -H "X-Admin-Key: change-this-admin-secret"
```

You can also store the admin key in `config.yaml`:

```yaml
admin:
  api_key: "change-this-admin-secret"
```

Dashboard URL:

```text
http://your-server:8000/__flowgate/dashboard
```

The dashboard is read-only. It does not create, edit, or revoke anything.

---

## 9. Optional Stripe Automation

Stripe automation lets FlowGate create and revoke API keys from subscription events.

What it does:

- `checkout.session.completed` creates a new FlowGate key
- The key scope comes from the Stripe Price ID mapping
- The key gets `stripe_subscription_id`
- `customer.subscription.deleted` removes the matching key
- Duplicate Stripe events are ignored

What it does not do:

- FlowGate does not email the key to the customer
- FlowGate does not manage Stripe products or prices
- FlowGate does not expose the generated key in the webhook response

### 9.1 Configure Stripe

Edit `config.yaml`:

```yaml
stripe:
  webhook_secret: "whsec_your_webhook_secret"
  api_key: "sk_live_or_test_key"
  rate_limit_per_minute: 100
  price_to_gateway:
    "price_basic": ["my-workflow"]
    "price_pro": ["my-workflow", "another-workflow"]
```

Notes:

- `webhook_secret` comes from the Stripe webhook endpoint settings.
- `api_key` is used to fetch checkout line items when Stripe does not include them in the event payload.
- `price_to_gateway` maps Stripe Price IDs to FlowGate workflow names.
- `rate_limit_per_minute` is optional and defaults to `60` for Stripe-created keys.

### 9.2 Add The Stripe Webhook Endpoint

In Stripe, set your webhook endpoint URL to:

```text
https://your-domain.com/webhooks/stripe
```

Select these events:

```text
checkout.session.completed
customer.subscription.deleted
```

For local testing with Stripe CLI:

```bash
stripe listen --forward-to localhost:8000/webhooks/stripe
```

Copy the `whsec_...` secret printed by Stripe CLI into `config.yaml`.

### 9.3 Test Stripe Locally

Start FlowGate:

```bash
flowgate start
```

In another terminal:

```bash
stripe trigger checkout.session.completed
```

Then check `config.yaml`. A new key should appear with:

```yaml
allowed_gateways:
  - my-workflow
stripe_subscription_id: sub_...
```

Trigger deletion:

```bash
stripe trigger customer.subscription.deleted
```

The matching key should be removed.

Important: Stripe's default trigger payload may use a test Price ID that is not in your `price_to_gateway` mapping. If no key is created, check `flowgate logs` for an `stripe_price_unmapped` warning and add the test Price ID to the mapping.

---

## 10. Deploy With Docker

Build:

```bash
docker build -t flowgate .
```

Run:

```bash
docker run -d \
  -p 8000:8000 \
  -v $(pwd)/config.yaml:/app/config.yaml \
  -v $(pwd)/logs:/app/logs \
  --name flowgate \
  flowgate
```

With admin key:

```bash
docker run -d \
  -p 8000:8000 \
  -e FLOWGATE_ADMIN_KEY="change-this-admin-secret" \
  -v $(pwd)/config.yaml:/app/config.yaml \
  -v $(pwd)/logs:/app/logs \
  --name flowgate \
  flowgate
```

View container logs:

```bash
docker logs -f flowgate
```

View FlowGate access logs from the mounted directory:

```bash
tail -f logs/usage.log
```

---

## 11. Production Checklist

Before exposing FlowGate publicly:

- Put FlowGate behind HTTPS, for example Caddy, Nginx, Traefik, or a cloud load balancer.
- Set `FLOWGATE_ADMIN_KEY` or `admin.api_key`.
- Keep `config.yaml` private because it contains API keys.
- Use filesystem permissions so only the FlowGate user can read `config.yaml`.
- Back up `config.yaml`.
- Mount `logs/` as a persistent volume if using Docker.
- Configure log rotation for long-running deployments.
- Use Stripe test mode before switching to live mode.

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
flowgate key list
```

Then create a correctly scoped key:

```bash
flowgate key create --name Pro --rate-limit 200 --gateways my-workflow
```

`429 Rate limit exceeded`

The key has used its per-minute allowance. Create a higher-tier key or wait for the bucket to refill.

`502 Could not reach workflow`

FlowGate is running, but your target workflow URL is not reachable. Check that n8n, Zapier, or your app is running and that the target URL in `config.yaml` is correct.

`Stripe webhook returns 503`

`stripe.webhook_secret` is missing from `config.yaml`.

`Stripe webhook succeeds but no key appears`

Check:

- The event is `checkout.session.completed`.
- The Checkout Session has a subscription ID.
- The Stripe Price ID exists in `stripe.price_to_gateway`.
- The mapped gateway names exist in `workflows:` or `gateways:`.
- `flowgate logs` for `stripe_price_unmapped` or `stripe_scope_invalid`.

---

## CLI Reference

```bash
flowgate init
flowgate start
flowgate status

flowgate key create
flowgate key create --name Pro --rate-limit 200
flowgate key create --name Trial --rate-limit 20 --expires-in 30d
flowgate key create --name Basic --rate-limit 30 --gateways my-workflow
flowgate key list
flowgate key revoke Pro

flowgate logs
flowgate logs --follow
flowgate logs --level ERROR
```

Without the alias:

```bash
python3 cli.py status
python3 cli.py key list
```

---

## Project Structure

```text
flowgate/
  cli.py
  main.py
  config.yaml
  core/
    auth.py
    limiter.py
    logger.py
    proxy.py
    stripe_webhooks.py
  templates/
    dashboard.html
  logs/
    usage.log
  Dockerfile
  requirements.txt
```

---

## License

MIT
