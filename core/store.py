"""
core/store.py — Storage abstraction layer.

Two backends:
  "yaml"   — config.yaml-based (backward-compatible, adds file locking)
  "sqlite" — SQLite with WAL mode (recommended for production / multi-worker)

Backend selection (first match wins):
  1. WORKFLOW_API_STORAGE env var  ("yaml" or "sqlite")
  2. config.yaml  storage.backend  field
  3. Default: "yaml"
"""
from __future__ import annotations

import os
from typing import Protocol, runtime_checkable


@runtime_checkable
class KeyStore(Protocol):
    def get_all_keys(self) -> list[dict]: ...
    def find_key_by_hash(self, key_hash: str) -> dict | None: ...
    def find_key_by_email(self, email: str) -> list[dict]: ...
    def create_key(self, record: dict) -> None: ...
    def revoke_key(self, name: str) -> bool: ...
    def revoke_key_by_hash(self, key_hash: str) -> bool: ...
    def revoke_key_by_stripe_subscription(self, subscription_id: str) -> bool: ...
    def find_key_by_stripe_subscription(self, subscription_id: str) -> dict | None: ...
    def count_active_keys(self) -> int: ...
    def is_stripe_event_processed(self, event_id: str) -> bool: ...
    def mark_stripe_event_processed(self, event_id: str) -> None: ...
    def check_and_set_resend_cooldown(self, email: str, cooldown_seconds: int) -> tuple[bool, int]: ...
    """
    Atomically check and set resend cooldown for an email address.
    Returns (allowed, remaining_seconds).
    - allowed=True: request is not in cooldown; cooldown is now set.
    - allowed=False: still in cooldown; remaining_seconds > 0.
    Cross-worker safe: YAML uses file locking, SQLite uses a DB table.
    """


_store: KeyStore | None = None


def get_store() -> KeyStore:
    global _store
    if _store is not None:
        return _store
    _store = _build_store()
    return _store


def reset_store() -> None:
    """Force re-initialisation (used by migrate commands)."""
    global _store
    _store = None


def _build_store() -> KeyStore:
    backend = os.environ.get("WORKFLOW_API_STORAGE", "").strip().lower()

    if not backend:
        try:
            from core.auth import load_config
            cfg = load_config()
            backend = (cfg.get("storage") or {}).get("backend", "yaml")
        except Exception:
            backend = "yaml"

    backend = (backend or "yaml").strip().lower()

    if backend == "sqlite":
        from core.store_sqlite import SQLiteKeyStore
        try:
            from core.auth import load_config
            cfg = load_config()
            path = (cfg.get("storage") or {}).get("sqlite_path", "workflow-api.db")
        except Exception:
            path = "workflow-api.db"
        store = SQLiteKeyStore(path)
        print(f"✅ Storage: SQLite ({path})")
        return store

    from core.store_yaml import YAMLKeyStore
    store = YAMLKeyStore()
    print("✅ Storage: YAML (config.yaml)")
    return store
