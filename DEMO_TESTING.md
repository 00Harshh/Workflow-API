# Workflow API Demo Testing Guide

This guide walks through a complete local demo of Workflow API without needing n8n, Zapier, Stripe live mode, or a VPS.

You will test:

- A mock workflow webhook
- Workflow API proxying
- API key auth
- Key scoping with `allowed_gateways`
- Rate limiting
- Logs
- Stats
- Dashboard
- Optional Stripe webhook behavior

The demo uses three terminals.

---

## 0. Start From The Project Root

```bash
cd /path/to/workflow-api
```

If you are using this current checkout:

```bash
cd /Users/harshjoshi/Downloads/workFlow-apiV2
```

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Optional shortcut:

```bash
alias workflow-api="python3 cli.py"
```

If you do not use the alias, replace `workflow-api` with `python3 cli.py`.

---

## 1. Back Up Your Existing Config

This demo writes to `config.yaml`, so back up the current file first.

```bash
cp config.yaml config.yaml.backup-demo
```

After the demo, restore it with:

```bash
mv config.yaml.backup-demo config.yaml
```

---

## 2. Terminal 1: Start A Mock Workflow Server

Run this in Terminal 1:

```bash
python3 - <<'PY'
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import time


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)
        try:
            parsed_body = json.loads(raw_body or b"{}")
        except json.JSONDecodeError:
            parsed_body = raw_body.decode("utf-8", errors="replace")

        self._send_json(200, {
            "ok": True,
            "workflow_path": self.path,
            "received": parsed_body,
            "timestamp": time.time(),
        })

    def log_message(self, format, *args):
        print("[mock-workflow]", format % args)


server = ThreadingHTTPServer(("127.0.0.1", 5678), Handler)
print("Mock workflow server running at http://127.0.0.1:5678")
print("Try: POST http://127.0.0.1:5678/webhook/demo")
server.serve_forever()
PY
```

Leave this terminal running.

Quick direct test from another terminal:

```bash
curl -X POST http://127.0.0.1:5678/webhook/demo \
  -H "Content-Type: application/json" \
  -d '{"direct": true}'
```

Expected response:

```json
{"ok": true, "workflow_path": "/webhook/demo", "received": {"direct": true}, "...": "..."}
```

---

## 3. Terminal 2: Create A Demo Workflow API Config

Run this from the Workflow API project root:

```bash
cat > config.yaml <<'YAML'
workflows:
  - name: demo
    endpoint: /run/demo
    target: http://127.0.0.1:5678/webhook/demo
    method: POST
  - name: private-demo
    endpoint: /run/private
    target: http://127.0.0.1:5678/webhook/private
    method: POST

keys: []

logging:
  file: logs/usage.log

admin:
  api_key: "demo-admin-secret"

stripe:
  webhook_secret: null
  api_key: null
  rate_limit_per_minute: 60
  price_to_gateway: {}

server:
  host: 0.0.0.0
  port: 8000
YAML
```

Create a normal key that can access all workflows:

```bash
workflow-api key create --name DemoAll --rate-limit 60
```

Copy the generated key. You will use it in Terminal 3.

Create a scoped key that can access only `demo`:

```bash
workflow-api key create --name DemoScoped --rate-limit 60 --gateways demo
```

Copy that generated key too.

List keys and confirm the scope column:

```bash
workflow-api key list
```

Expected:

```text
DemoAll      ... Scope All
DemoScoped   ... Scope demo
```

---

## 4. Terminal 2: Start Workflow API

```bash
workflow-api start
```

Leave this terminal running.

Workflow API should start on:

```text
http://localhost:8000
```

---

## 5. Terminal 3: Test A Successful Proxied Request

First, export the keys you copied in Terminal 2:

```bash
export WORKFLOW_API_ALL_KEY="wfapi-paste-your-demoall-key-here"
export WORKFLOW_API_SCOPED_KEY="wfapi-paste-your-demoscoped-key-here"
```

Use the all-access key:

```bash
curl -i -X POST http://localhost:8000/run/demo \
  -H "Authorization: Bearer $WORKFLOW_API_ALL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message": "hello through Workflow API"}'
```

Expected:

```text
HTTP/1.1 200 OK
```

Response body should include:

```json
{
  "ok": true,
  "workflow_path": "/webhook/demo",
  "received": {
    "message": "hello through Workflow API"
  }
}
```

This proves Workflow API accepted the key and proxied the request to the mock workflow.

---

## 6. Test Missing Or Invalid API Key

No key:

```bash
curl -i -X POST http://localhost:8000/run/demo \
  -H "Content-Type: application/json" \
  -d '{"message": "no key"}'
```

Expected:

```text
HTTP/1.1 401 Unauthorized
```

Invalid key:

```bash
curl -i -X POST http://localhost:8000/run/demo \
  -H "Authorization: Bearer wfapi-not-real" \
  -H "Content-Type: application/json" \
  -d '{"message": "bad key"}'
```

Expected:

```text
HTTP/1.1 401 Unauthorized
```

---

## 7. Test Key Scoping

The scoped key can call `demo`:

```bash
curl -i -X POST http://localhost:8000/run/demo \
  -H "Authorization: Bearer $WORKFLOW_API_SCOPED_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message": "allowed"}'
```

Expected:

```text
HTTP/1.1 200 OK
```

The same scoped key cannot call `private-demo`:

```bash
curl -i -X POST http://localhost:8000/run/private \
  -H "Authorization: Bearer $WORKFLOW_API_SCOPED_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message": "blocked"}'
```

Expected:

```text
HTTP/1.1 403 Forbidden
```

Expected body:

```json
{"detail": "Key not authorized for this workflow"}
```

---

## 8. Test Rate Limiting

Create a low-limit key:

```bash
workflow-api key create --name DemoLimited --rate-limit 2 --gateways demo
```

Copy the key:

```bash
export WORKFLOW_API_LIMITED_KEY="wfapi-paste-your-demolimited-key-here"
```

Send three quick requests:

```bash
for i in 1 2 3; do
  curl -s -o /dev/null -w "request $i -> %{http_code}\n" \
    -X POST http://localhost:8000/run/demo \
    -H "Authorization: Bearer $WORKFLOW_API_LIMITED_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"request\": $i}"
done
```

Expected:

```text
request 1 -> 200
request 2 -> 200
request 3 -> 429
```

The exact result can vary slightly if enough time passes between requests, but the third quick request should usually be rate limited.

---

## 9. Test Key Expiration

Create an already-expired key:

```bash
workflow-api key create --name ExpiredDemo --rate-limit 60 --expires-at 2000-01-01 --gateways demo
```

Copy the key:

```bash
export WORKFLOW_API_EXPIRED_KEY="wfapi-paste-your-expireddemo-key-here"
```

Call Workflow API:

```bash
curl -i -X POST http://localhost:8000/run/demo \
  -H "Authorization: Bearer $WORKFLOW_API_EXPIRED_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message": "expired"}'
```

Expected:

```text
HTTP/1.1 403 Forbidden
```

Expected body:

```json
{"detail": "API key expired"}
```

---

## 10. Test Logs

Show the last 20 log entries:

```bash
workflow-api logs
```

Show only warnings:

```bash
workflow-api logs --level WARNING
```

Follow logs live:

```bash
workflow-api logs --follow
```

While `logs --follow` is running, send another request from a different terminal:

```bash
curl -X POST http://localhost:8000/run/demo \
  -H "Authorization: Bearer $WORKFLOW_API_ALL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message": "log follow test"}'
```

You should see a new JSON log line appear.

---

## 11. Test Stats Endpoint

Localhost can access stats without a key:

```bash
curl http://localhost:8000/__workflow-api/stats
```

Expected shape:

```json
{
  "requests_total": 10,
  "requests_by_gateway": {
    "demo": 7,
    "private-demo": 3
  },
  "active_keys": 4,
  "rate_limited_requests": 1
}
```

Test admin header too:

```bash
curl http://localhost:8000/__workflow-api/stats \
  -H "X-Admin-Key: demo-admin-secret"
```

---

## 12. Test Dashboard

Open:

```text
http://localhost:8000/__workflow-api/dashboard
```

You should see:

- Active key count
- Total requests
- Rate-limited request count
- Uptime
- Requests by gateway
- Recent activity table

The page auto-refreshes every 30 seconds.

---

## 13. Optional: Test Stripe Webhook Flow Locally

This section requires the Stripe CLI.

### 13.1 Configure A Test Stripe Mapping

You need a Stripe Price ID. If you already have one, put it into `config.yaml`:

```yaml
stripe:
  webhook_secret: "replace-after-stripe-listen"
  api_key: "sk_test_your_key"
  rate_limit_per_minute: 60
  price_to_gateway:
    "price_your_test_price_id": ["demo"]
```

### 13.2 Start Stripe Listener

In a new terminal:

```bash
stripe listen --forward-to localhost:8000/webhooks/stripe
```

Stripe prints a webhook secret like:

```text
whsec_...
```

Copy it into `config.yaml` as `stripe.webhook_secret`.

Restart Workflow API after editing `config.yaml`.

### 13.3 Trigger Checkout Completion

```bash
stripe trigger checkout.session.completed
```

Check logs:

```bash
workflow-api logs --level WARNING
workflow-api logs --level INFO
```

If the test event uses a Price ID that is not mapped, you will see:

```text
stripe_price_unmapped
```

Add that Price ID under `stripe.price_to_gateway`, restart Workflow API, and trigger again.

When the mapping matches, `config.yaml` should get a new key with:

```yaml
allowed_gateways:
  - demo
stripe_subscription_id: sub_...
```

### 13.4 Trigger Subscription Deletion

```bash
stripe trigger customer.subscription.deleted
```

If the subscription ID matches a Stripe-created key, that key is removed from `config.yaml`.

Note: Stripe CLI fixture events may not always share the same subscription ID between separate trigger commands. For a perfect end-to-end Stripe test, use a real test-mode Checkout Session and cancel the created subscription from the Stripe Dashboard.

---

## 14. Clean Up Demo State

Stop Workflow API with `Ctrl+C`.

Stop the mock workflow server with `Ctrl+C`.

Restore your previous config:

```bash
mv config.yaml.backup-demo config.yaml
```

Optional: clear demo logs:

```bash
rm -f logs/usage.log logs/stripe_events.json
```

If you want to keep the log file but empty it:

```bash
: > logs/usage.log
```

---

## Demo Checklist

Use this checklist to confirm everything worked:

- `POST /run/demo` with a valid key returns `200`.
- Missing or bad key returns `401`.
- Scoped key works on `/run/demo`.
- Scoped key returns `403` on `/run/private`.
- Low-limit key returns `429` after quick repeated requests.
- Expired key returns `403`.
- `workflow-api logs` shows structured JSON access logs.
- `/__workflow-api/stats` returns request counters.
- `/__workflow-api/dashboard` shows the read-only dashboard.
- Optional Stripe test creates and revokes scoped keys.
