from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import get_settings

SCHEMA_VERSION = 3

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS probe_results (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  service_name TEXT NOT NULL,
  status       TEXT NOT NULL,
  response_ms  INTEGER,
  http_status  INTEGER,
  error        TEXT,
  source       TEXT NOT NULL,
  checked_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_probe_service_time ON probe_results(service_name, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_probe_time ON probe_results(checked_at DESC);

CREATE TABLE IF NOT EXISTS incidents (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  service_name TEXT NOT NULL,
  started_at   TEXT NOT NULL,
  ended_at     TEXT,
  duration_min INTEGER,
  resolved     INTEGER NOT NULL DEFAULT 0,
  cause        TEXT,            -- admin-authored root-cause / post-mortem (plain text, nullable)
  cause_at     TEXT             -- ISO-8601 UTC when the cause was last written/edited
);
CREATE INDEX IF NOT EXISTS idx_incidents_service_time ON incidents(service_name, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_incidents_unresolved ON incidents(resolved, service_name);

CREATE TABLE IF NOT EXISTS daily_uptime (
  service_name  TEXT NOT NULL,
  day           TEXT NOT NULL,
  uptime_pct    REAL NOT NULL,
  total_checks  INTEGER NOT NULL,
  failed_checks INTEGER NOT NULL,
  PRIMARY KEY (service_name, day)
);

CREATE TABLE IF NOT EXISTS shard_snapshot (
  cluster_idx INTEGER NOT NULL,
  shard_id    INTEGER NOT NULL,
  status      TEXT NOT NULL,
  latency_ms  INTEGER,
  guild_count INTEGER,
  fetched_at  TEXT NOT NULL,
  PRIMARY KEY (cluster_idx, shard_id)
);

CREATE TABLE IF NOT EXISTS announcements (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  type        TEXT NOT NULL CHECK (type IN ('maintenance','incident')),
  severity    TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
  title       TEXT NOT NULL,
  body        TEXT NOT NULL,
  created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_announcements_active ON announcements(resolved_at, created_at DESC);

CREATE TABLE IF NOT EXISTS announcement_updates (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  announcement_id INTEGER NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
  status          TEXT NOT NULL CHECK (status IN ('investigating','identified','monitoring','resolved')),
  body            TEXT NOT NULL,
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_ann_updates_ann ON announcement_updates(announcement_id, created_at);

CREATE TABLE IF NOT EXISTS alert_state (
  service_name  TEXT PRIMARY KEY,
  last_alert_at TEXT NOT NULL,
  last_status   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_alert_state (
  kind     TEXT PRIMARY KEY,
  last_at  TEXT NOT NULL
);

-- Admin accounts for the web panel. Each staff member sets their own
-- password (scrypt) and enrolls a TOTP authenticator during one-time setup.
CREATE TABLE IF NOT EXISTS admin_users (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  username       TEXT NOT NULL,
  username_lc    TEXT NOT NULL UNIQUE,
  role           TEXT NOT NULL DEFAULT 'staff' CHECK (role IN ('owner','staff')),
  password_hash  TEXT,
  password_salt  TEXT,
  totp_secret    TEXT,
  active         INTEGER NOT NULL DEFAULT 0,
  setup_token    TEXT,
  setup_expires  TEXT,
  failed_logins  INTEGER NOT NULL DEFAULT 0,
  locked_until   TEXT,
  last_totp_step INTEGER,
  created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  last_login_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_admin_users_setup ON admin_users(setup_token);
"""


def _connect_raw(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=10000")
    return conn


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Yield a configured SQLite connection. Caller manages transactions
    via explicit BEGIN/COMMIT or the autocommit default. WAL mode is
    enabled per-connection so reader/writer concurrency is safe."""
    conn = _connect_raw(get_settings().db_path)
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Create tables if missing and apply migrations. Idempotent."""
    with connect() as conn:
        conn.executescript(_SCHEMA_SQL)
        cur = conn.cursor()
        cur.execute("SELECT version FROM schema_version LIMIT 1")
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
        elif row["version"] != SCHEMA_VERSION:
            _migrate(conn, row["version"], SCHEMA_VERSION)
            cur.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))


def _migrate(conn: sqlite3.Connection, current: int, target: int) -> None:
    """Apply forward migrations between current and target schema versions.

    `CREATE TABLE IF NOT EXISTS` (in _SCHEMA_SQL) is a no-op against an
    existing table, so a new COLUMN on an existing DB only lands here.
    Each step must be idempotent — guard ALTERs with a PRAGMA check so a
    re-run (or a fresh DB that already has the column) is safe."""
    if current > target:
        raise RuntimeError(f"Refusing to downgrade schema: have v{current}, want v{target}")

    # v1 → v2: incident root-cause / post-mortem columns.
    incident_cols = {r["name"] for r in conn.execute("PRAGMA table_info(incidents)").fetchall()}
    if "cause" not in incident_cols:
        conn.execute("ALTER TABLE incidents ADD COLUMN cause TEXT")
    if "cause_at" not in incident_cols:
        conn.execute("ALTER TABLE incidents ADD COLUMN cause_at TEXT")


def prune_old_probes(retention_days: int = 30) -> int:
    """Delete probe_results older than the retention window. Returns rows pruned."""
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM probe_results WHERE checked_at < strftime('%Y-%m-%dT%H:%M:%fZ','now',?)",
            (f"-{retention_days} days",),
        )
        return cur.rowcount or 0


def vacuum() -> None:
    """Reclaim disk space after pruning."""
    with connect() as conn:
        conn.execute("VACUUM")
