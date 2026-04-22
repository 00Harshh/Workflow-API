"""
core/auth.py — Authentication, key management, and config utilities.

Key hashing: API keys are stored as SHA-256 hashes. The raw key is only
visible at creation time. Auth compares hash(incoming_token) against the
stored hash — constant-time comparison is not needed here because dictionary
lookup is already indexed by hash, not compared in a loop.

Storage: all key operations delegate to core.store.get_store() so the
backend (YAML or SQLite) is transparent to callers.
"""
from __future__ import annotations

import hashlib
import secrets
import re
import threading
from pathlib import Path
from datetime import date, datetime, time, timedelta, timezone

from ruamel.yaml import YAML

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

yaml = YAML()
yaml.preserve_quotes = True
yaml.indent(mapping=2, sequence=4, offset=2)

# ── Config cache ───────────────────────────────────────────────────────────────
# Avoids re-parsing config.yaml on every request.
# Invalidated whenever the file's mtime changes on disk.
_config_cache: dict | None = None
_config_mtime: float = -1.0
_config_lock = threading.Lock()


class ExpiredKeyError(Exception):
    """Raised when an otherwise valid API key is past its expiration time."""


# ── Date / time utilities ──────────────────────────────────────────────────────

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_duration(value: str) -> timedelta:
    match = re.fullmatch(r"\+?(\d+)([dhm])", value.strip().lower())
    if not match:
        raise ValueError("Use a relative expiration like 30d, +30d, 12h, or 45m.")
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "d":
        return timedelta(days=amount)
    if unit == "h":
        return timedelta(hours=amount)
    return timedelta(minutes=amount)


def _parse_datetime(value: str) -> datetime:
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        parsed_date = datetime.strptime(text, "%Y-%m-%d").date()
        return datetime.combine(parsed_date, time(23, 59, 59), tzinfo=timezone.utc)
    normalized = text.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_expiration(
    *, expires_at: str | None = None, expires_in: str | None = None
) -> str | None:
    """Returns an ISO 8601 UTC expiration string, or None for immortal keys."""
    if expires_at and expires_in:
        raise ValueError("Use either --expires-at or --expires-in, not both.")
    if expires_in:
        return _format_utc(_utc_now() + _parse_duration(expires_in))
    if expires_at:
        text = expires_at.strip()
        if text.startswith("+") or re.fullmatch(r"\d+[dhm]", text.lower()):
            return _format_utc(_utc_now() + _parse_duration(text))
        return _format_utc(_parse_datetime(text))
    return None


def _coerce_expiration(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time(23, 59, 59), tzinfo=timezone.utc)
    return _parse_datetime(str(value))


def is_key_expired(key_record: dict, now: datetime | None = None) -> bool:
    expires_at = key_record.get("expires_at")
    if not expires_at:
        return False
    try:
        expires_dt = _coerce_expiration(expires_at)
    except (TypeError, ValueError):
        return True
    return (now or _utc_now()) > expires_dt


# ── Config I/O ────────────────────────────────────────────────────────────────

def load_config() -> dict:
    """Load config, returning the cached version if the file hasn't changed."""
    global _config_cache, _config_mtime
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except FileNotFoundError:
        raise
    with _config_lock:
        if _config_cache is not None and mtime == _config_mtime:
            return _config_cache
        with open(CONFIG_PATH, "r") as f:
            fresh = yaml.load(f) or {}
        _config_cache = fresh
        _config_mtime = mtime
        return fresh


def save_config(config: dict) -> None:
    """Persist config to disk and immediately update the in-memory cache."""
    global _config_cache, _config_mtime
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(config, f)
    with _config_lock:
        _config_cache = config
        try:
            _config_mtime = CONFIG_PATH.stat().st_mtime
        except OSError:
            _config_mtime = -1.0


# ── Gateway helpers ───────────────────────────────────────────────────────────

def get_gateways(config: dict | None = None) -> list[dict]:
    """Returns configured gateways; accepts legacy `workflows` key."""
    config = config or load_config()
    return config.get("gateways") or config.get("workflows") or []


def get_gateway_names(config: dict | None = None) -> set[str]:
    return {gw["name"] for gw in get_gateways(config) if gw.get("name")}


def parse_allowed_gateways(value: str | list[str] | tuple[str, ...] | None) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        names = [part.strip() for part in value.split(",") if part.strip()]
    else:
        names = [str(part).strip() for part in value if str(part).strip()]
    return names or None


def validate_allowed_gateways(
    allowed_gateways: list[str] | None, config: dict | None = None
) -> None:
    if not allowed_gateways:
        return
    available = get_gateway_names(config)
    unknown = sorted(set(allowed_gateways) - available)
    if unknown:
        available_text = ", ".join(sorted(available)) or "none configured"
        raise ValueError(
            f"Unknown gateway(s): {', '.join(unknown)}. Available: {available_text}."
        )


def key_allowed_for_gateway(key_record: dict, gateway_name: str) -> bool:
    allowed_gateways = key_record.get("allowed_gateways")
    if not allowed_gateways:
        return True
    return gateway_name in allowed_gateways


# ── Key hashing ───────────────────────────────────────────────────────────────

def hash_key(raw_key: str) -> str:
    """SHA-256 digest of a raw API key. Stored instead of the plaintext key."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


# ── Key CRUD — delegates to store ────────────────────────────────────────────

def get_all_keys() -> list[dict]:
    from core.store import get_store  # lazy — avoids circular import at module load
    return get_store().get_all_keys()


def count_active_keys() -> int:
    from core.store import get_store
    return get_store().count_active_keys()


def find_key(raw_key: str) -> dict | None:
    """Look up a key record by its raw (Bearer) value."""
    from core.store import get_store
    return get_store().find_key_by_hash(hash_key(raw_key))


def validate_and_resolve(authorization: str | None) -> dict | None:
    """
    Validates the Authorization header.
    Returns the key record if valid, None if not found.
    Raises ExpiredKeyError if the key exists but is past its expiration.
    """
    if not authorization:
        return None
    parts = authorization.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    key_record = find_key(parts[1])
    if key_record and is_key_expired(key_record):
        raise ExpiredKeyError
    return key_record


def create_key(
    name: str,
    rate_limit_per_minute: int,
    expires_at: str | None = None,
    allowed_gateways: list[str] | None = None,
    stripe_subscription_id: str | None = None,
    email: str | None = None,
) -> dict:
    """
    Generate a new API key, persist it (hashed) to the store, and return the
    full record — including the raw key which is only available at this moment.
    """
    from core.store import get_store
    validate_allowed_gateways(allowed_gateways)

    raw = "wfapi-" + secrets.token_urlsafe(32)
    record: dict = {
        "name": name,
        "key_hash": hash_key(raw),
        "key_prefix": raw[:16],
        "rate_limit_per_minute": rate_limit_per_minute,
        "created_at": _utc_now().strftime("%Y-%m-%d"),
        "expires_at": expires_at,
        "allowed_gateways": allowed_gateways,
    }
    if stripe_subscription_id:
        record["stripe_subscription_id"] = stripe_subscription_id
    if email:
        record["email"] = email.strip().lower()

    get_store().create_key(record)

    # Return record + raw key (caller must display it now — it won't be stored)
    return {**record, "key": raw}


def find_key_by_stripe_subscription(subscription_id: str) -> dict | None:
    from core.store import get_store
    return get_store().find_key_by_stripe_subscription(subscription_id)


def revoke_key_by_stripe_subscription(subscription_id: str) -> bool:
    from core.store import get_store
    return get_store().revoke_key_by_stripe_subscription(subscription_id)


def revoke_key(name: str) -> bool:
    """Removes all keys with the given name. Returns True if any were removed."""
    from core.store import get_store
    return get_store().revoke_key(name)


# ── Migration utilities ───────────────────────────────────────────────────────

def migrate_keys_to_hashed() -> int:
    """
    Scan config.yaml for plaintext `key` fields and replace them with
    `key_hash` + `key_prefix` in-place. Safe to run multiple times.
    Returns the number of keys migrated.
    """
    config = load_config()
    keys = config.get("keys") or []
    migrated = 0
    for k in keys:
        if "key" in k and "key_hash" not in k:
            raw = k.pop("key")
            k["key_hash"] = hash_key(raw)
            k["key_prefix"] = raw[:16]
            migrated += 1
        elif "key" in k and "key_hash" in k:
            # Remove redundant plaintext copy
            k.pop("key")
            migrated += 1
    if migrated:
        save_config(config)
    return migrated


def migrate_yaml_to_sqlite(sqlite_path: str = "workflow-api.db") -> int:
    """
    Copy all keys from config.yaml into a SQLite database.
    Returns the number of keys successfully migrated.
    """
    from core.store_sqlite import SQLiteKeyStore
    sqlite_store = SQLiteKeyStore(sqlite_path)
    config = load_config()
    keys = config.get("keys") or []
    migrated = 0

    for k in keys:
        raw_key = k.get("key")
        key_hash = k.get("key_hash")
        if raw_key and not key_hash:
            key_hash = hash_key(raw_key)
        if not key_hash:
            continue

        allowed = k.get("allowed_gateways")
        if isinstance(allowed, str):
            allowed = [g.strip() for g in allowed.split(",") if g.strip()]

        record = {
            "name": k.get("name", "unknown"),
            "key_hash": key_hash,
            "key_prefix": (k.get("key") or key_hash)[:16],
            "rate_limit_per_minute": k.get("rate_limit_per_minute", 60),
            "created_at": k.get("created_at", _utc_now().strftime("%Y-%m-%d")),
            "expires_at": k.get("expires_at"),
            "allowed_gateways": allowed,
            "stripe_subscription_id": k.get("stripe_subscription_id"),
            "email": k.get("email"),
        }
        try:
            sqlite_store.create_key(record)
            migrated += 1
        except Exception:
            pass  # already exists — skip duplicate

    return migrated
