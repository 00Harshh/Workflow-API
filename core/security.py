"""
core/security.py — Shared security validation utilities.

SSRF Protection:
  validate_target_url() checks workflow target URLs against blocked IP ranges
  and hostnames before Workflow API starts forwarding requests to them.

Real IP Extraction:
  get_real_client_ip() respects X-Real-IP / X-Forwarded-For from nginx.
"""
from __future__ import annotations

import ipaddress
import urllib.parse
from typing import Optional

# ── SSRF blocklist ─────────────────────────────────────────────────────────────

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),      # Loopback
    ipaddress.ip_network("10.0.0.0/8"),        # RFC 1918 private
    ipaddress.ip_network("172.16.0.0/12"),     # RFC 1918 private
    ipaddress.ip_network("192.168.0.0/16"),    # RFC 1918 private
    ipaddress.ip_network("169.254.0.0/16"),    # Link-local (AWS/GCP/Azure metadata)
    ipaddress.ip_network("100.64.0.0/10"),     # Shared address space (RFC 6598)
    ipaddress.ip_network("0.0.0.0/8"),         # This host on this network
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
]

_BLOCKED_HOSTNAMES = {
    "localhost",
    "metadata.google.internal",
    "metadata.goog",
}

_ALLOWED_SCHEMES = {"http", "https"}


def validate_target_url(url: str) -> None:
    """
    Validates a workflow target URL to prevent Server-Side Request Forgery (SSRF).
    Raises ValueError with a descriptive message if the URL is blocked.

    Called at Workflow API startup for every configured workflow target.
    """
    import os
    if os.environ.get("WORKFLOW_API_ENV", "production").lower() == "development":
        return

    if not url or not url.strip():
        raise ValueError("Target URL must not be empty.")

    try:
        parsed = urllib.parse.urlparse(url.strip())
    except Exception as exc:
        raise ValueError(f"Could not parse target URL {url!r}: {exc}") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(
            f"Disallowed scheme {scheme!r} in target URL. Only 'http' and 'https' are allowed."
        )

    host = (parsed.hostname or "").lower().strip(".")
    if not host:
        raise ValueError(f"Target URL has no hostname: {url!r}")

    if host in _BLOCKED_HOSTNAMES:
        raise ValueError(
            f"Blocked hostname {host!r} in target URL. "
            "Requests to localhost/cloud-metadata services are not allowed."
        )

    # Resolve as IP address and check against blocked networks
    try:
        addr = ipaddress.ip_address(host)
        for net in _BLOCKED_NETWORKS:
            if addr in net:
                raise ValueError(
                    f"Blocked IP address {host} in target URL. "
                    "Requests to private/reserved/link-local IP ranges are not allowed. "
                    f"(matched network: {net})"
                )
    except ValueError as exc:
        # Re-raise our own explicit block errors
        if "Blocked" in str(exc):
            raise
        # ip_address() raised — it's a hostname, not a raw IP. That's fine.
        pass


# ── Real client IP extraction ─────────────────────────────────────────────────

def get_real_client_ip(request_headers: dict) -> Optional[str]:
    """
    Extract the real client IP, respecting headers set by a trusted reverse proxy.
    Priority: X-Real-IP → first item of X-Forwarded-For → None.

    NOTE: Only trust these headers when Workflow API is behind nginx with
    'proxy_set_header X-Real-IP $remote_addr' configured.
    """
    real_ip = request_headers.get("x-real-ip") or request_headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()

    xff = request_headers.get("x-forwarded-for") or request_headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()

    return None
