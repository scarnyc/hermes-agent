"""Dedup ledger for MOL-246 evening enrichment.

Tracks which ``SignalItem`` candidates (by ``source`` + ``source_id``) have
already been appended to the daily task-list, so re-runs of the evening cron
do not double-post. The log is time-windowed: after ``window_days`` an entry
is considered expired and the item will be re-emitted on the next run.

Public surface:
    * ``SCHEMA_SQL``  — idempotent ``CREATE TABLE`` + ``CREATE INDEX``.
    * ``init_schema`` — apply ``SCHEMA_SQL`` against a connection.
    * ``filter_new``  — read-only; returns items not present in the
      recent-window log.
    * ``record``      — bulk ``INSERT OR IGNORE`` of fresh items.

The ``signal_ingestion_log`` table is intentionally minimal: ``(source,
source_id)`` is the dedup key, ``ingested_at`` is an ISO-8601 UTC timestamp
used for window expiry. No ``cron_run_id``; a window-days expiry is simpler
to reason about than join-on-run semantics.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .types import SignalItem

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS signal_ingestion_log (
  source TEXT NOT NULL,
  source_id TEXT NOT NULL,
  ingested_at TEXT NOT NULL,
  UNIQUE(source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_sig_ingested ON signal_ingestion_log(ingested_at);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    """Create the ``signal_ingestion_log`` table + index if absent.

    Idempotent — safe to call on every evening-enrichment run. Uses
    ``executescript`` so both statements in ``SCHEMA_SQL`` apply in one call.
    """
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def filter_new(
    conn: sqlite3.Connection,
    items: Iterable[SignalItem],
    window_days: int = 14,
) -> list[SignalItem]:
    """Return only items whose ``(source, source_id)`` is NOT logged within
    the last ``window_days``.

    Items older than the window are treated as expired and re-emitted. The
    comparison parses ``ingested_at`` via ``datetime.fromisoformat`` and
    compares against ``now - timedelta(days=window_days)`` in UTC. The
    connection is used read-only (no ``INSERT``, no ``COMMIT``).
    """
    items_list = list(items)
    if not items_list:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    cur = conn.execute(
        "SELECT source, source_id, ingested_at FROM signal_ingestion_log"
    )
    recent: set[tuple[str, str]] = set()
    for source, source_id, ingested_at in cur.fetchall():
        try:
            ts = datetime.fromisoformat(ingested_at)
        except ValueError:
            # Corrupt row — treat as expired so the item re-emits.
            continue
        # Normalize naive timestamps to UTC for safety; schema always writes
        # timezone-aware strings, but be defensive.
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            recent.add((source, source_id))

    return [item for item in items_list if (item.source, item.source_id) not in recent]


def record(conn: sqlite3.Connection, items: Iterable[SignalItem]) -> int:
    """Bulk ``INSERT OR IGNORE`` the given items with ``ingested_at = now``.

    Returns the number of rows actually inserted (collisions on the
    ``(source, source_id)`` UNIQUE constraint are silently ignored and do
    not count). Computed via per-row ``cursor.rowcount`` summation so the
    return value is independent of any other writes made on ``conn``.
    """
    items_list = list(items)
    if not items_list:
        return 0

    now_iso = datetime.now(timezone.utc).isoformat()
    inserted = 0
    for item in items_list:
        cur = conn.execute(
            "INSERT OR IGNORE INTO signal_ingestion_log "
            "(source, source_id, ingested_at) VALUES (?, ?, ?)",
            (item.source, item.source_id, now_iso),
        )
        if cur.rowcount == 1:
            inserted += 1
    conn.commit()
    return inserted
