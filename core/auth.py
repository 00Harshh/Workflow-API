import secrets
import re
from pathlib import Path
from datetime import date, datetime, time, timedelta, timezone

from ruamel.yaml import YAML

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

yaml = YAML()
yaml.preserve_quotes = True
yaml.indent(mapping=2, sequence=4, offset=2)


class ExpiredKeyError(Exception):
    """Raised when an otherwise valid API key is past its expiration time."""


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


def parse_expiration(*, expires_at: str | None = None, expires_in: str | None = None) -> str | None:
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


def count_active_keys() -> int:
    return sum(1 for key in get_all_keys() if not is_key_expired(key))


def load_config() -> dict:
    with open(CONFIG_PATH, "r") as f:
        return yaml.load(f) or {}


def save_config(config: dict):
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(config, f)


def get_gateways(config: dict | None = None) -> list[dict]:
    """Returns configured gateways, accepting legacy `workflows` configs."""
    config = config or load_config()
    return config.get("gateways") or config.get("workflows") or []


def get_gateway_names(config: dict | None = None) -> set[str]:
    return {gateway["name"] for gateway in get_gateways(config) if gateway.get("name")}


def parse_allowed_gateways(value: str | list[str] | tuple[str, ...] | None) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        names = [part.strip() for part in value.split(",") if part.strip()]
    else:
        names = [str(part).strip() for part in value if str(part).strip()]
    return names or None


def validate_allowed_gateways(allowed_gateways: list[str] | None, config: dict | None = None):
    if not allowed_gateways:
        return

    available = get_gateway_names(config)
    unknown = sorted(set(allowed_gateways) - available)
    if unknown:
        available_text = ", ".join(sorted(available)) or "none configured"
        unknown_text = ", ".join(unknown)
        raise ValueError(f"Unknown gateway(s): {unknown_text}. Available gateways: {available_text}.")


def key_allowed_for_gateway(key_record: dict, gateway_name: str) -> bool:
    allowed_gateways = key_record.get("allowed_gateways")
    if not allowed_gateways:
        return True
    return gateway_name in allowed_gateways


def get_all_keys() -> list[dict]:
    config = load_config()
    return config.get("keys") or []


def find_key(raw_key: str) -> dict | None:
    """Returns the key record if valid, None if not found."""
    for k in get_all_keys():
        if secrets.compare_digest(k["key"], raw_key):
            return k
    return None


def validate_and_resolve(authorization: str | None) -> dict | None:
    """
    Validates the Authorization header.
    Returns the key record (including rate_limit) if valid, else None.
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
) -> dict:
    """Generates a new key, saves it to config, and returns the record."""
    config = load_config()
    if "keys" not in config or config["keys"] is None:
        config["keys"] = []

    validate_allowed_gateways(allowed_gateways, config)

    raw = "wfapi-" + secrets.token_urlsafe(32)
    record = {
        "name": name,
        "key": raw,
        "rate_limit_per_minute": rate_limit_per_minute,
        "created_at": _utc_now().strftime("%Y-%m-%d"),
        "expires_at": expires_at,
        "allowed_gateways": allowed_gateways,
    }
    if stripe_subscription_id:
        record["stripe_subscription_id"] = stripe_subscription_id

    config["keys"].append(record)
    save_config(config)
    return record


def revoke_key(name: str) -> bool:
    """Removes all keys with the given name. Returns True if any were removed."""
    config = load_config()
    keys = config.get("keys") or []
    before = len(keys)
    config["keys"] = [k for k in keys if k["name"] != name]
    if len(config["keys"]) < before:
        save_config(config)
        return True
    return False
