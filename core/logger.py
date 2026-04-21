import json
import os
import time
from collections import Counter
from pathlib import Path

from core.auth import load_config

PROJECT_ROOT = Path(__file__).parent.parent
LEGACY_LOG_PATH = PROJECT_ROOT / "logs" / "usage.log"
DEFAULT_LOG_PATH = PROJECT_ROOT / "flowgate.log"


def _resolve_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def get_log_path() -> Path:
    env_path = os.environ.get("FLOWGATE_LOG_FILE")
    if env_path:
        return _resolve_path(env_path)

    try:
        config = load_config()
    except FileNotFoundError:
        config = {}

    logging_cfg = config.get("logging") or {}
    configured = logging_cfg.get("file") or config.get("log_file")
    if configured:
        return _resolve_path(configured)

    if LEGACY_LOG_PATH.exists():
        return LEGACY_LOG_PATH
    return DEFAULT_LOG_PATH


def level_for_status(status_code: int) -> str:
    if status_code >= 500:
        return "ERROR"
    if status_code >= 400:
        return "WARNING"
    return "INFO"


def log_request(
    endpoint: str,
    status_code: int,
    latency_ms: float,
    tier: str = "unknown",
    gateway: str | None = None,
    event: str = "request",
    level: str | None = None,
):
    log_path = get_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level": level or level_for_status(status_code),
        "event": event,
        "endpoint": endpoint,
        "gateway": gateway or endpoint,
        "tier": tier,
        "status": status_code,
        "latency_ms": round(latency_ms, 2),
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def iter_log_entries():
    log_path = get_log_path()
    if not log_path.exists():
        return

    with open(log_path, "r") as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def build_stats(active_keys: int) -> dict:
    entries = list(iter_log_entries() or [])
    requests_by_gateway = Counter(entry.get("gateway") or entry.get("endpoint") or "unknown" for entry in entries)
    rate_limited_requests = sum(
        1
        for entry in entries
        if entry.get("status") == 429 or entry.get("event") == "rate_limited"
    )

    return {
        "requests_total": len(entries),
        "requests_by_gateway": dict(requests_by_gateway),
        "active_keys": active_keys,
        "rate_limited_requests": rate_limited_requests,
    }
