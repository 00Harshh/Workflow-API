import asyncio
import json
import os
import time
from collections import Counter, deque
from pathlib import Path
from queue import Empty, Queue
from threading import Lock

from core.auth import load_config

PROJECT_ROOT = Path(__file__).parent.parent
LEGACY_LOG_PATH = PROJECT_ROOT / "logs" / "usage.log"
DEFAULT_LOG_PATH = PROJECT_ROOT / "workflow-api.log"

# ── Async log queue ────────────────────────────────────────────────────────────
# log_request() enqueues entries instantly (no disk I/O in the hot path).
# start_log_writer() runs as a background asyncio task and flushes to disk.
_log_queue: Queue = Queue()

# ── Fix #9: In-memory stats counters ──────────────────────────────────────────
# build_stats() previously read the ENTIRE log file on every dashboard request.
# After 1M requests this becomes a 500MB scan — potential DoS.
# Now we maintain live counters in memory, updated by log_request().
_stats_lock = Lock()
_gateway_counter: Counter = Counter()
_rate_limited_total: int = 0
_requests_total: int = 0


def _resolve_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def get_log_path() -> Path:
    env_path = os.environ.get("WORKFLOW_API_LOG_FILE")
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
    """Enqueue a log entry and update in-memory counters. No disk I/O on call path."""
    global _requests_total, _rate_limited_total

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
    _log_queue.put_nowait(entry)

    # Update in-memory counters (thread-safe)
    with _stats_lock:
        _requests_total += 1
        _gateway_counter[gateway or endpoint] += 1
        if status_code == 429 or event == "rate_limited":
            _rate_limited_total += 1


def _write_entries_sync(log_path: Path, entries: list[dict]):
    """Blocking write — runs in a thread pool, not on the event loop."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


async def start_log_writer():
    """
    Background asyncio coroutine. Started once at app startup via the lifespan.
    Drains the log queue every 50 ms and writes to disk in a thread pool executor.
    """
    loop = asyncio.get_event_loop()
    log_path = get_log_path()

    while True:
        await asyncio.sleep(0.05)  # 50 ms batching window

        entries: list[dict] = []
        try:
            while True:
                entries.append(_log_queue.get_nowait())
        except Empty:
            pass

        if entries:
            await loop.run_in_executor(None, _write_entries_sync, log_path, entries)


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
    """
    Returns aggregate stats using in-memory counters (O(1), no disk I/O).
    Fix #9: previously scanned the entire log file on every dashboard request.
    """
    with _stats_lock:
        return {
            "requests_total": _requests_total,
            "requests_by_gateway": dict(_gateway_counter),
            "active_keys": active_keys,
            "rate_limited_requests": _rate_limited_total,
        }


def recent_log_entries(limit: int = 10) -> list[dict]:
    entries = deque(maxlen=limit)
    for entry in iter_log_entries() or []:
        entries.append(entry)
    return list(entries)
