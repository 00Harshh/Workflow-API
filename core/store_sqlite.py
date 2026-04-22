"""
core/store_sqlite.py — SQLite-backed key store.

Uses WAL (Write-Ahead Logging) journal mode, allowing multiple uvicorn
worker processes to read concurrently with one writer at a time — no data
corruption without any extra locking mechanism.

Each thread gets its own sqlite3.Connection via threading.local() so the
store is safe in multi-threaded single-process mode too.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS keys (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    name                   TEXT    NOT NULL,
    key_hash               TEXT    NOT NULL UNIQUE,
    key_prefix             TEXT    NOT NULL,
    rate_limit_per_minute  INTEGER NOT NULL DEFAULT 60,
    created_at             TEXT    NOT NULL,
    expires_at             TEXT,
    allowed_gateways       TEXT,            -- JSON array e.g. '["n8ntest"]'
    stripe_subscription_id TEXT,
    email                  TEXT,
    active                 INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS stripe_events (
    event_id     TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL
);

-- Fix #3: Cross-worker resend cooldown table
CREATE TABLE IF NOT EXISTS resend_cooldown (
    email       TEXT    PRIMARY KEY,
    last_resend REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_keys_hash  ON keys (key_hash);
CREATE INDEX IF NOT EXISTS idx_keys_sub   ON keys (stripe_subscription_id);
CREATE INDEX IF NOT EXISTS idx_keys_email ON keys (email);
"""


def _open(db_path: str) -> sqlite3.Connection:
    import os
    path = Path(db_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    
    is_new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # multi-process safe
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL") # safe + fast with WAL
    
    if is_new:
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass  # Ignore on filesystems that don't support posix perm
            
    return conn


class SQLiteKeyStore:
    def __init__(self, db_path: str = "workflow-api.db"):
        self._db_path = db_path
        self._local = threading.local()
        # Bootstrap schema in initialising thread
        conn = self._conn()
        conn.executescript(_SCHEMA)
        conn.commit()

    def _conn(self) -> sqlite3.Connection:
        if not getattr(self._local, "conn", None):
            self._local.conn = _open(self._db_path)
        return self._local.conn

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        raw = d.get("allowed_gateways")
        if raw:
            try:
                d["allowed_gateways"] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                d["allowed_gateways"] = None
        return d

    # ── KeyStore protocol ─────────────────────────────────────────────────────

    def get_all_keys(self) -> list[dict]:
        rows = self._conn().execute(
            "SELECT * FROM keys WHERE active = 1"
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def find_key_by_hash(self, key_hash: str) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM keys WHERE key_hash = ? AND active = 1", (key_hash,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def find_key_by_email(self, email: str) -> list[dict]:
        email = email.strip().lower()
        rows = self._conn().execute(
            "SELECT * FROM keys WHERE lower(email) = ? AND active = 1", (email,)
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def create_key(self, record: dict) -> None:
        allowed = record.get("allowed_gateways")
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO keys
              (name, key_hash, key_prefix, rate_limit_per_minute,
               created_at, expires_at, allowed_gateways,
               stripe_subscription_id, email, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                record["name"],
                record["key_hash"],
                record.get("key_prefix", ""),
                record.get("rate_limit_per_minute", 60),
                record.get("created_at"),
                record.get("expires_at"),
                json.dumps(allowed) if allowed else None,
                record.get("stripe_subscription_id"),
                record.get("email"),
            ),
        )
        conn.commit()

    def revoke_key(self, name: str) -> bool:
        conn = self._conn()
        cur = conn.execute(
            "UPDATE keys SET active = 0 WHERE name = ? AND active = 1", (name,)
        )
        conn.commit()
        return cur.rowcount > 0

    def revoke_key_by_hash(self, key_hash: str) -> bool:
        conn = self._conn()
        cur = conn.execute(
            "UPDATE keys SET active = 0 WHERE key_hash = ? AND active = 1", (key_hash,)
        )
        conn.commit()
        return cur.rowcount > 0

    def revoke_key_by_stripe_subscription(self, subscription_id: str) -> bool:
        conn = self._conn()
        cur = conn.execute(
            "UPDATE keys SET active = 0 WHERE stripe_subscription_id = ? AND active = 1",
            (subscription_id,),
        )
        conn.commit()
        return cur.rowcount > 0

    def find_key_by_stripe_subscription(self, subscription_id: str) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM keys WHERE stripe_subscription_id = ? AND active = 1",
            (subscription_id,),
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def count_active_keys(self) -> int:
        from core.auth import is_key_expired
        return sum(1 for k in self.get_all_keys() if not is_key_expired(k))

    # ── Stripe event deduplication ────────────────────────────────────────────

    def is_stripe_event_processed(self, event_id: str) -> bool:
        row = self._conn().execute(
            "SELECT 1 FROM stripe_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return row is not None

    def mark_stripe_event_processed(self, event_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        conn = self._conn()
        conn.execute(
            "INSERT OR IGNORE INTO stripe_events (event_id, processed_at) VALUES (?, ?)",
            (event_id, now),
        )
        conn.commit()

    # ── Resend cooldown (Fix #3: cross-worker via SQLite transaction) ─────────

    def check_and_set_resend_cooldown(
        self, email: str, cooldown_seconds: int
    ) -> tuple[bool, int]:
        """
        Atomically check + set resend cooldown in a serialized SQLite transaction.
        Returns (allowed, remaining_seconds).
        """
        import time
        now = time.time()
        conn = self._conn()

        # BEGIN IMMEDIATE serializes writers while allowing readers
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT last_resend FROM resend_cooldown WHERE email = ?", (email,)
            ).fetchone()

            if row:
                last = row["last_resend"]
                elapsed = now - last
                if elapsed < cooldown_seconds:
                    conn.execute("ROLLBACK")
                    return (False, int(cooldown_seconds - elapsed))

            conn.execute(
                "INSERT OR REPLACE INTO resend_cooldown (email, last_resend) VALUES (?, ?)",
                (email, now),
            )
            conn.execute("COMMIT")
            return (True, 0)
        except Exception:
            conn.execute("ROLLBACK")
            raise
