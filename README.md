# FlowGate

> Turn any workflow into a monetizable API — in under 5 minutes.

No cloud. No platform. Runs wherever your workflow runs. You host it, you own it.

---

## What this is

A thin layer that sits in front of your workflow (n8n, Zapier, Python script — anything with a URL) and gives it:

- API key authentication
- Per-key rate limiting (charge different tiers different limits)
- A clean endpoint you can share with paying users
- Request logging with tier tracking

You generate a key per customer. They call your API. You own the billing.

---

## Setup

```bash
git clone https://github.com/yourusername/flowgate.git
cd flowgate
pip install -r requirements.txt
python cli.py init
```

`init` is a wizard — it asks for your workflow URL, port, and creates your first API key. You never need to touch `config.yaml` manually.

---

## CLI reference

```bash
python cli.py init              # First-time setup wizard
python cli.py start             # Start the API server
python cli.py status            # See all workflows and active keys
python cli.py logs              # Show the last 20 access log entries
python cli.py logs --follow     # Stream access logs live
```

```bash
python cli.py keys create       # Generate a new key (name + rate limit)
python cli.py keys create --name Trial --rate-limit 20 --expires-in 30d
python cli.py keys list         # List all active keys
python cli.py keys revoke Pro   # Revoke all keys named 'Pro'
```

`python cli.py key ...` is also supported as a singular alias for `keys`.

---

## Monetization flow

Create a key per customer tier:

```bash
python cli.py keys create
# Key name: Free
# Rate limit: 10

python cli.py keys create
# Key name: Pro
# Rate limit: 200

python cli.py keys create
# Key name: Enterprise
# Rate limit: 0        ← 0 means unlimited
```

Each key gets its own isolated rate limit bucket. A Pro user hitting their limit doesn't affect a Free user.

User pays → you run `keys create` → send them the key → they're live.
User cancels → `keys revoke` → key is dead instantly.

---

## Calling the API

```bash
curl -X POST http://your-host:8000/run/my-workflow \
  -H "Authorization: Bearer wfapi-xxxxxxxxxxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{"input": "hello"}'
```

Interactive docs available at `http://localhost:8000/docs`.

---

## Docker (for live hosting)

```bash
docker build -t flowgate .
docker run -p 8000:8000 -v $(pwd)/config.yaml:/app/config.yaml flowgate
```

Works on any VPS, home server, or cloud VM.

---

## Logs

Every request is logged to `logs/usage.log` with tier tracking:

```json
{"time": "2026-04-21T10:00:00Z", "level": "INFO", "event": "request", "endpoint": "/run/my-workflow", "gateway": "my-workflow", "tier": "Pro", "status": 200, "latency_ms": 142.3}
```

Use `FLOWGATE_LOG_FILE=/path/to/flowgate.log` or `logging.file` in `config.yaml` to customize the log path.

Filter by severity:

```bash
python cli.py logs --level ERROR
```

---

## Stats

Localhost can read aggregate stats at:

```bash
curl http://localhost:8000/__flowgate/stats
```

For non-local access, set `FLOWGATE_ADMIN_KEY` or `admin.api_key` in `config.yaml` and call with `Authorization: Bearer <admin-key>`.

---

## Works with

- **n8n** — use the Webhook node URL as `target`
- **Zapier** — use the Catch Hook URL as `target`
- **Any FastAPI / Flask / Express app** — point at its endpoint
- **Anything that accepts HTTP requests**

---

## File structure

```
flowgate/
├── cli.py              ← everything you interact with
├── main.py             ← API server (started by cli.py start)
├── config.yaml         ← auto-managed by the CLI
├── core/
│   ├── auth.py         ← key validation
│   ├── proxy.py        ← request forwarding
│   ├── limiter.py      ← per-key rate limiting
│   └── logger.py       ← usage logging
├── logs/usage.log
└── Dockerfile
```

---

## License

MIT — use it however you want.
