"""
core/store_yaml.py — YAML file-based key store.

Uses a companion lock file (config.yaml.lock) with fcntl.flock(LOCK_EX) around
every read-modify-write cycle to prevent data corruption across uvicorn workers.
"""
from __future__ import annotations

import json
import sys
import time
from collections import deque
from pathlib import Path


PROJECT_ROOT = Path(__file__).parent.parent
STRIPE_EVENTS_PATH = PROJECT_ROOT / "logs" / "stripe_events.json"
COOLDOWN_PATH = PROJECT_ROOT / "resend_cooldown.json"
MAX_STRIPE_EVENTS = 1000
_LOCK_PATH = PROJECT_ROOT / "config.yaml.lock"


def _with_exclusive_lock(fn):
    """Run fn() under an exclusive file lock on config.yaml.lock (Unix only)."""
    if sys.platform == "win32":
        return fn()
    import fcntl
    with open(str(_LOCK_PATH), "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


class YAMLKeyStore:

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _read_config_fresh(self) -> dict:
        """Bypass the mtime cache — reads YAML directly from disk."""
        from core.auth import CONFIG_PATH, yaml
        with open(CONFIG_PATH, "r") as f:
            return yaml.load(f) or {}

    def _save(self, config: dict) -> None:
        from core.auth import save_config
        save_config(config)

    def _locked_update(self, fn):
        """
        Under an exclusive lock:
          1. Read config fresh from disk
          2. Call fn(config) → modified config
          3. Save to disk
        Returns whatever fn returns.
        """
        def _do():
            config = self._read_config_fresh()
            result = fn(config)
            self._save(config)
            return result
        return _with_exclusive_lock(_do)

    # ── KeyStore protocol ─────────────────────────────────────────────────────

    def get_all_keys(self) -> list[dict]:
        from core.auth import load_config
        return load_config().get("keys") or []

    def find_key_by_hash(self, key_hash: str) -> dict | None:
        for k in self.get_all_keys():
            stored = k.get("key_hash") or k.get("key")  # "key" = legacy plaintext
            if stored == key_hash:
                return k
        return None

    def find_key_by_email(self, email: str) -> list[dict]:
        email = email.strip().lower()
        return [k for k in self.get_all_keys() if (k.get("email") or "").lower() == email]

    def create_key(self, record: dict) -> None:
        def _mutate(config: dict):
            if not config.get("keys"):
                config["keys"] = []
            config["keys"].append(record)
        self._locked_update(_mutate)

    def revoke_key(self, name: str) -> bool:
        removed = [False]

        def _mutate(config: dict):
            keys = config.get("keys") or []
            before = len(keys)
            config["keys"] = [k for k in keys if k.get("name") != name]
            removed[0] = len(config["keys"]) < before

        self._locked_update(_mutate)
        return removed[0]

    def revoke_key_by_hash(self, key_hash: str) -> bool:
        removed = [False]

        def _mutate(config: dict):
            keys = config.get("keys") or []
            before = len(keys)
            config["keys"] = [k for k in keys if k.get("key_hash") != key_hash]
            removed[0] = len(config["keys"]) < before

        self._locked_update(_mutate)
        return removed[0]

    def revoke_key_by_stripe_subscription(self, subscription_id: str) -> bool:
        removed = [False]

        def _mutate(config: dict):
            keys = config.get("keys") or []
            before = len(keys)
            config["keys"] = [k for k in keys if k.get("stripe_subscription_id") != subscription_id]
            removed[0] = len(config["keys"]) < before

        self._locked_update(_mutate)
        return removed[0]

    def find_key_by_stripe_subscription(self, subscription_id: str) -> dict | None:
        for k in self.get_all_keys():
            if k.get("stripe_subscription_id") == subscription_id:
                return k
        return None

    def count_active_keys(self) -> int:
        from core.auth import is_key_expired
        return sum(1 for k in self.get_all_keys() if not is_key_expired(k))

    # ── Stripe event deduplication ────────────────────────────────────────────

    def _load_events(self) -> deque[str]:
        if not STRIPE_EVENTS_PATH.exists():
            return deque(maxlen=MAX_STRIPE_EVENTS)
        try:
            with open(STRIPE_EVENTS_PATH, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = []
        return deque((str(e) for e in data), maxlen=MAX_STRIPE_EVENTS)

    def _save_events(self, events: deque[str]) -> None:
        STRIPE_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(STRIPE_EVENTS_PATH, "w") as f:
            json.dump(list(events), f)

    def is_stripe_event_processed(self, event_id: str) -> bool:
        """Check dedup cache (non-atomic read — see mark_stripe_event_processed for safety)."""
        return event_id in self._load_events()

    def mark_stripe_event_processed(self, event_id: str) -> None:
        """
        Fix #10: Atomically check + insert under exclusive file lock.
        Prevents duplicate key creation when Stripe retries a webhook.
        """
        def _do():
            events = self._load_events()
            if event_id not in events:
                events.append(event_id)
                self._save_events(events)
        _with_exclusive_lock(_do)

    # ── Resend cooldown (cross-worker safe via file lock) ─────────────────────

    def check_and_set_resend_cooldown(
        self, email: str, cooldown_seconds: int
    ) -> tuple[bool, int]:
        """
        Fix #3: Cross-worker atomic cooldown check.
        Uses the same exclusive file lock as config writes.
        Returns (allowed, remaining_seconds).
        """
        result: list = [False, 0]

        def _do():
            now = time.time()
            try:
                with open(COOLDOWN_PATH, "r") as f:
                    data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                data = {}

            last = data.get(email, 0.0)
            elapsed = now - last
            if elapsed < cooldown_seconds:
                result[0] = False
                result[1] = int(cooldown_seconds - elapsed)
                return

            data[email] = now
            # Evict entries older than 24 hours to keep file small
            data = {k: v for k, v in data.items() if now - v < 86400}
            with open(COOLDOWN_PATH, "w") as f:
                json.dump(data, f)
            result[0] = True
            result[1] = 0

        _with_exclusive_lock(_do)
        return (result[0], result[1])
