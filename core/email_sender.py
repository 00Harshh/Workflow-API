"""
core/email_sender.py — SMTP-based API key delivery.

Configure via config.yaml `email` section or environment variables:
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM_ADDRESS

If email is not configured (or enabled: false), the key is printed loudly
to stdout so the operator can forward it manually.
"""
from __future__ import annotations

import asyncio
import html as html_lib
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def _smtp_cfg() -> dict | None:
    """Return resolved SMTP settings or None if email is disabled/unconfigured."""
    try:
        from core.auth import load_config
        cfg = load_config().get("email") or {}
    except Exception:
        cfg = {}

    if not cfg.get("enabled", False):
        return None

    host = os.environ.get("SMTP_HOST") or cfg.get("smtp_host", "")
    if not host:
        return None

    return {
        "host": host,
        "port": int(os.environ.get("SMTP_PORT") or cfg.get("smtp_port") or 587),
        "user": os.environ.get("SMTP_USER") or cfg.get("smtp_user") or "",
        "password": os.environ.get("SMTP_PASSWORD") or cfg.get("smtp_password") or "",
        "from_address": (
            os.environ.get("SMTP_FROM_ADDRESS")
            or cfg.get("from_address")
            or "noreply@workflow-api.app"
        ),
        "from_name": cfg.get("from_name") or "Workflow API",
    }


def _html_email(
    key: str, name: str, gateways: list[str], rate_limit: int, portal_url: str
) -> str:
    # Fix #7: Escape all user-supplied values before interpolating into HTML.
    # 'name' comes from Stripe customer data; 'gateways' from config (could contain
    # user-influenced data). Never trust any field for HTML context.
    name_s      = html_lib.escape(name)
    key_s       = html_lib.escape(key)
    gateways_s  = html_lib.escape(", ".join(gateways) if gateways else "All gateways")
    limit_s     = html_lib.escape(f"{rate_limit} req/min" if rate_limit > 0 else "Unlimited")
    example_gw  = html_lib.escape(gateways[0] if gateways else "my-workflow")
    portal_s    = html_lib.escape(portal_url.rstrip("/"))
    curl = (
        f'curl -X POST {portal_s}/run/{example_gw} \\\n'
        f'  -H "Authorization: Bearer {key_s}" \\\n'
        f'  -H "Content-Type: application/json" \\\n'
        f"  -d '{{\"input\": \"hello\"}}'"
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#09090f;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
  <div style="max-width:580px;margin:48px auto;padding:0 16px">
    <div style="background:linear-gradient(135deg,#6d28d9,#7c3aed);padding:32px;border-radius:16px 16px 0 0;text-align:center">
      <h1 style="margin:0;color:#fff;font-size:24px;font-weight:700;letter-spacing:-0.5px">Workflow API</h1>
      <p style="margin:8px 0 0;color:#ddd6fe;font-size:14px">Your API Access Is Ready</p>
    </div>
    <div style="background:#13131f;border:1px solid #1e1e2e;border-top:none;padding:32px;border-radius:0 0 16px 16px">
      <p style="color:#94a3b8;margin:0 0 24px;font-size:14px;line-height:1.6">
        Hi <strong style="color:#e2e8f0">{name_s}</strong>, your Workflow API API key is ready.
        Save it somewhere safe — for security, we only show it once.
      </p>

      <div style="background:#09090f;border:1px solid #312e81;border-radius:10px;padding:20px;margin-bottom:24px">
        <p style="margin:0 0 8px;color:#7c3aed;font-size:11px;letter-spacing:1.5px;text-transform:uppercase;font-weight:600">Your API Key</p>
        <code style="color:#a78bfa;font-size:13px;word-break:break-all;font-family:'Courier New',Courier,monospace;line-height:1.6">{key_s}</code>
      </div>

      <table style="width:100%;border-collapse:collapse;margin-bottom:24px">
        <tr>
          <td style="padding:12px 0;color:#64748b;font-size:13px;border-bottom:1px solid #1e1e2e;width:45%">Rate Limit</td>
          <td style="padding:12px 0;color:#e2e8f0;font-size:13px;border-bottom:1px solid #1e1e2e;font-weight:500">{limit_s}</td>
        </tr>
        <tr>
          <td style="padding:12px 0;color:#64748b;font-size:13px">Gateway Access</td>
          <td style="padding:12px 0;color:#e2e8f0;font-size:13px;font-weight:500">{gateways_s}</td>
        </tr>
      </table>

      <div style="background:#09090f;border:1px solid #1e1e2e;border-radius:10px;padding:20px;margin-bottom:28px">
        <p style="margin:0 0 12px;color:#7c3aed;font-size:11px;letter-spacing:1.5px;text-transform:uppercase;font-weight:600">Example Request</p>
        <pre style="margin:0;color:#94a3b8;font-size:11px;overflow-x:auto;white-space:pre-wrap;font-family:'Courier New',Courier,monospace;line-height:1.7">{curl}</pre>
      </div>

      <div style="text-align:center">
        <a href="{portal_url}" style="display:inline-block;background:linear-gradient(135deg,#6d28d9,#7c3aed);color:#fff;text-decoration:none;padding:12px 32px;border-radius:8px;font-size:14px;font-weight:600">
          Manage My Key →
        </a>
      </div>

      <p style="color:#4a4a6a;font-size:11px;text-align:center;margin:24px 0 0;line-height:1.6">
        Lost your key? Visit <a href="{portal_url}" style="color:#7c3aed">{portal_s}</a> to regenerate it.
      </p>
    </div>
  </div>
</body>
</html>"""


def send_api_key_email(
    to: str,
    key: str,
    name: str,
    gateways: list[str],
    rate_limit: int,
    portal_url: str = "http://localhost:8000/portal",
) -> bool:
    """
    Send the API key to the user via SMTP.
    Returns True if sent, False if email is not configured.
    Falls back to printing the key loudly to stdout.
    """
    smtp = _smtp_cfg()
    if not smtp:
        print("\n" + "=" * 72)
        print(f"⚠️  EMAIL NOT CONFIGURED — forward this key manually to: {to}")
        print(f"   Key      : {key}")
        print(f"   Gateways : {', '.join(gateways) if gateways else 'All'}")
        print(f"   Rate     : {rate_limit} req/min")
        print("=" * 72 + "\n")
        return False

    html = _html_email(key, name, gateways, rate_limit, portal_url)
    plain = (
        f"Your Workflow API API Key\n\n"
        f"Key: {key}\n"
        f"Rate limit: {rate_limit} req/min\n"
        f"Gateways: {', '.join(gateways) if gateways else 'All'}\n\n"
        f"Manage your key: {portal_url}\n"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Your Workflow API API Key"
    msg["From"] = f"{smtp['from_name']} <{smtp['from_address']}>"
    msg["To"] = to
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(smtp["host"], smtp["port"]) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            if smtp["user"]:
                s.login(smtp["user"], smtp["password"])
            s.sendmail(smtp["from_address"], [to], msg.as_string())
        return True
    except Exception as exc:
        print(f"⚠️  Email send failed to {to}: {exc}")
        return False


async def async_send_api_key_email(*args, **kwargs) -> bool:
    """Non-blocking wrapper — runs SMTP in the thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, lambda: send_api_key_email(*args, **kwargs)
    )
