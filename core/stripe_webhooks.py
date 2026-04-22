"""
core/stripe_webhooks.py — Stripe webhook event processing.

Handles:
  checkout.session.completed      → create API key, send email to customer
  customer.subscription.deleted   → revoke API key
"""
from __future__ import annotations

import re

import stripe

from core.auth import (
    create_key,
    find_key_by_stripe_subscription,
    get_gateway_names,
    load_config,
    revoke_key_by_stripe_subscription,
)
from core.logger import log_request


class StripeWebhookConfigError(Exception):
    """Raised when Stripe webhook config is missing or invalid."""


def _stripe_config(config: dict | None = None) -> dict:
    config = config or load_config()
    return config.get("stripe") or {}


def _portal_url() -> str:
    try:
        cfg = load_config()
        server = cfg.get("server") or {}
        host = server.get("host", "0.0.0.0")
        port = server.get("port", 8000)
        display_host = "localhost" if host in ("0.0.0.0", "::") else host
        return f"http://{display_host}:{port}/portal"
    except Exception:
        return "http://localhost:8000/portal"


# ── Stripe event deduplication ────────────────────────────────────────────────

def is_event_processed(event_id: str | None) -> bool:
    if not event_id:
        return False
    from core.store import get_store
    return get_store().is_stripe_event_processed(event_id)


def mark_event_processed(event_id: str | None) -> None:
    if not event_id:
        return
    from core.store import get_store
    get_store().mark_stripe_event_processed(event_id)


# ── Webhook signature verification ───────────────────────────────────────────

def construct_event(payload: bytes, signature: str | None):
    stripe_cfg = _stripe_config()
    webhook_secret = stripe_cfg.get("webhook_secret")
    if not webhook_secret:
        raise StripeWebhookConfigError("Stripe webhook_secret is not configured.")
    return stripe.Webhook.construct_event(payload, signature, webhook_secret)


# ── Helper utilities ──────────────────────────────────────────────────────────

def _sanitize_name(value: str | None, fallback: str) -> str:
    raw = value or fallback
    sanitized = re.sub(r"[^A-Za-z0-9_.@-]+", "-", raw).strip("-")
    return sanitized or fallback


def _object_get(obj, key: str, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _extract_price_id_from_line_item(line_item) -> str | None:
    price = _object_get(line_item, "price")
    if price:
        price_id = _object_get(price, "id")
        if price_id:
            return price_id
    return _object_get(line_item, "price_id")


def _line_items_from_session_object(session) -> list:
    line_items = _object_get(session, "line_items")
    if not line_items:
        return []
    return list(_object_get(line_items, "data", line_items) or [])


def _fetch_line_items(session_id: str, stripe_cfg: dict) -> list:
    configured_api_key = stripe_cfg.get("api_key")
    if configured_api_key:
        stripe.api_key = configured_api_key
    if not stripe.api_key:
        return []
    line_items = stripe.checkout.Session.list_line_items(session_id, limit=100)
    return list(_object_get(line_items, "data", []) or [])


def _extract_price_id(session, stripe_cfg: dict) -> str | None:
    for line_item in _line_items_from_session_object(session):
        price_id = _extract_price_id_from_line_item(line_item)
        if price_id:
            return price_id
    session_id = _object_get(session, "id")
    if not session_id:
        return None
    for line_item in _fetch_line_items(session_id, stripe_cfg):
        price_id = _extract_price_id_from_line_item(line_item)
        if price_id:
            return price_id
    return None


def _customer_email(session) -> str | None:
    customer_details = _object_get(session, "customer_details") or {}
    return _object_get(customer_details, "email")


def _customer_reference(session, subscription_id: str) -> str:
    email = _customer_email(session)
    return (
        _object_get(session, "client_reference_id")
        or email
        or _object_get(session, "customer")
        or f"stripe-{subscription_id}"
    )


# ── Checkout completed → create key + send email ─────────────────────────────

async def _create_key_for_checkout_session(session, event_id: str | None) -> dict:
    stripe_cfg = _stripe_config()
    subscription_id = _object_get(session, "subscription")
    if not subscription_id:
        log_request(
            "/webhooks/stripe", 200, 0,
            gateway="stripe", event="stripe_checkout_missing_subscription", level="WARNING",
        )
        return {"action": "skipped", "reason": "missing_subscription"}

    existing_key = find_key_by_stripe_subscription(subscription_id)
    if existing_key:
        return {"action": "exists", "subscription_id": subscription_id}

    price_id = _extract_price_id(session, stripe_cfg)
    price_to_gateway = stripe_cfg.get("price_to_gateway") or {}
    allowed_gateways = price_to_gateway.get(price_id)
    if not price_id or not allowed_gateways:
        log_request(
            "/webhooks/stripe", 200, 0,
            gateway="stripe", event="stripe_price_unmapped", level="WARNING",
        )
        return {"action": "skipped", "reason": "unmapped_price", "price_id": price_id}

    unknown_gateways = sorted(set(allowed_gateways) - get_gateway_names())
    if unknown_gateways:
        log_request(
            "/webhooks/stripe", 200, 0,
            gateway="stripe", event="stripe_scope_invalid", level="ERROR",
        )
        return {"action": "skipped", "reason": "unknown_gateways", "gateways": unknown_gateways}

    email = _customer_email(session)
    name = _sanitize_name(_customer_reference(session, subscription_id), f"stripe-{subscription_id}")
    rate_limit = int(stripe_cfg.get("rate_limit_per_minute") or 60)

    key_record = create_key(
        name=name,
        rate_limit_per_minute=rate_limit,
        expires_at=None,
        allowed_gateways=list(allowed_gateways),
        stripe_subscription_id=subscription_id,
        email=email,
    )

    log_request(
        "/webhooks/stripe", 200, 0, key_record.get("name"),
        gateway="stripe", event="stripe_key_created", level="INFO",
    )

    # Send API key to customer's email (non-blocking)
    if email:
        from core.email_sender import async_send_api_key_email
        await async_send_api_key_email(
            to=email,
            key=key_record["key"],   # raw key — only available right now
            name=name,
            gateways=list(allowed_gateways),
            rate_limit=rate_limit,
            portal_url=_portal_url(),
        )

    return {"action": "created", "subscription_id": subscription_id, "event_id": event_id}


# ── Subscription deleted → revoke key ────────────────────────────────────────

def _revoke_key_for_deleted_subscription(subscription, event_id: str | None) -> dict:
    subscription_id = _object_get(subscription, "id")
    if not subscription_id:
        return {"action": "skipped", "reason": "missing_subscription_id"}

    revoked = revoke_key_by_stripe_subscription(subscription_id)
    log_request(
        "/webhooks/stripe", 200, 0,
        gateway="stripe",
        event="stripe_key_revoked" if revoked else "stripe_key_revoke_miss",
        level="INFO" if revoked else "WARNING",
    )
    return {
        "action": "revoked" if revoked else "not_found",
        "subscription_id": subscription_id,
        "event_id": event_id,
    }


# ── Main dispatcher ───────────────────────────────────────────────────────────

async def process_event(event) -> dict:
    event_id = _object_get(event, "id")
    if is_event_processed(event_id):
        return {"received": True, "duplicate": True}

    event_type = _object_get(event, "type")
    event_data = _object_get(event, "data") or {}
    event_object = _object_get(event_data, "object") or {}

    if event_type == "checkout.session.completed":
        result = await _create_key_for_checkout_session(event_object, event_id)
    elif event_type == "customer.subscription.deleted":
        result = _revoke_key_for_deleted_subscription(event_object, event_id)
    else:
        result = {"action": "ignored", "event_type": event_type}

    mark_event_processed(event_id)
    return {"received": True, **result}
