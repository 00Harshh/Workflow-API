"""
core/cancellation_scheduler.py — Persistent, multi-worker-safe grace period.

Architecture (fixes P0 + P1 from security audit):

  OLD (broken):  in-memory dict of asyncio.Tasks — lost on restart, invisible
                 across workers.

  NEW (this):    Pending cancellations are written to the store (SQLite table
                 or YAML JSON file).  A background poller checks every
                 POLL_INTERVAL_SECONDS for rows whose revoke_at has passed
                 and revokes those keys.

  Why this is safe:
    - Survives restarts: state is in the DB, not in RAM.
    - Multi-worker safe: every worker polls the same DB.  SQLite WAL mode
      guarantees only one writer commits the revocation; the DELETE after
      revocation is idempotent.
    - No asyncio.Task leak: the only long-running task is the poller itself.

  Env vars:
    CANCELLATION_GRACE_SECONDS   — grace window (default: 172800 = 48h)
    CANCELLATION_POLL_SECONDS    — poller interval (default: 60)
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

from core.auth import (
    find_key_by_stripe_subscription,
    revoke_key_by_stripe_subscription,
)
from core.logger import log_request

# ── Configuration ─────────────────────────────────────────────────────────────

GRACE_PERIOD_SECONDS: int = int(
    os.environ.get("CANCELLATION_GRACE_SECONDS", 48 * 3600)
)
POLL_INTERVAL_SECONDS: int = int(
    os.environ.get("CANCELLATION_POLL_SECONDS", 60)
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_utc(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ── Public API (called from stripe_webhooks.py) ──────────────────────────────

def schedule_revocation(subscription_id: str) -> dict:
    """
    Persist a pending cancellation.  The key stays active until the background
    poller picks it up after GRACE_PERIOD_SECONDS.
    """
    from core.store import get_store
    store = get_store()

    key_record = find_key_by_stripe_subscription(subscription_id)
    if not key_record:
        return {"action": "not_found", "subscription_id": subscription_id}

    revoke_at = _format_utc(_utc_now() + timedelta(seconds=GRACE_PERIOD_SECONDS))
    store.add_pending_cancellation(subscription_id, revoke_at)

    log_request(
        "/webhooks/stripe", 200, 0,
        gateway="stripe",
        event="cancellation_scheduled",
        level="INFO",
    )
    return {
        "action": "pending_cancellation",
        "subscription_id": subscription_id,
        "revoke_at": revoke_at,
        "grace_seconds": GRACE_PERIOD_SECONDS,
    }


def cancel_pending_revocation(subscription_id: str) -> bool:
    """
    Remove a pending cancellation (customer resubscribed / paid).
    Returns True if a pending record existed and was removed.
    """
    from core.store import get_store
    removed = get_store().remove_pending_cancellation(subscription_id)

    if removed:
        log_request(
            "/webhooks/stripe", 200, 0,
            gateway="stripe",
            event="cancellation_cancelled",
            level="INFO",
        )
    return removed


# ── Background poller ─────────────────────────────────────────────────────────

async def _poll_due_cancellations() -> None:
    """
    Periodically check the store for cancellations whose grace window has
    expired, revoke the corresponding keys, and clean up the record.

    Runs forever until cancelled (from lifespan teardown).
    """
    from core.store import get_store

    while True:
        try:
            store = get_store()
            due = store.get_due_cancellations()

            for record in due:
                sub_id = record["subscription_id"]
                revoked = revoke_key_by_stripe_subscription(sub_id)
                # Always remove the pending record — even if the key was
                # already revoked (by admin, by another worker, etc.)
                store.remove_pending_cancellation(sub_id)

                log_request(
                    "/webhooks/stripe", 200, 0,
                    gateway="stripe",
                    event="grace_period_revoked" if revoked else "grace_period_revoke_miss",
                    level="INFO" if revoked else "WARNING",
                )
        except Exception:
            # Log but don't crash the poller — it'll retry next interval
            log_request(
                "/webhooks/stripe", 500, 0,
                gateway="stripe",
                event="cancellation_poller_error",
                level="ERROR",
            )

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


# ── Lifecycle (called from main.py lifespan) ──────────────────────────────────

_poller_task: asyncio.Task | None = None


async def start() -> None:
    """Start the background poller.  Safe to call multiple times."""
    global _poller_task
    if _poller_task is not None and not _poller_task.done():
        return  # already running
    _poller_task = asyncio.create_task(
        _poll_due_cancellations(),
        name="cancellation-poller",
    )


async def shutdown() -> None:
    """Stop the background poller (called from lifespan teardown)."""
    global _poller_task
    if _poller_task is not None and not _poller_task.done():
        _poller_task.cancel()
        try:
            await _poller_task
        except asyncio.CancelledError:
            pass
    _poller_task = None
