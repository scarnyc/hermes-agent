"""Tests for enrichment.dedup (MOL-246)."""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make ``enrichment`` importable when this file is collected by pytest from
# the repo root (scripts/hermes-patches/reference/ is not on PYTHONPATH by
# default).
REFERENCE_DIR = Path(__file__).resolve().parents[1]
if str(REFERENCE_DIR) not in sys.path:
    sys.path.insert(0, str(REFERENCE_DIR))

from enrichment.dedup import filter_new, init_schema, record  # noqa: E402
from enrichment.types import SignalItem  # noqa: E402


def test_init_schema_idempotent() -> None:
    """Running init_schema multiple times must not error and must leave
    exactly one signal_ingestion_log table in sqlite_master."""
    conn = sqlite3.connect(":memory:")
    try:
        init_schema(conn)
        init_schema(conn)
        init_schema(conn)

        rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='signal_ingestion_log'"
        ).fetchall()
        assert len(rows) == 1, f"expected 1 table row, got {len(rows)}"
    finally:
        conn.close()


def test_dedup_prevents_reinjection() -> None:
    """End-to-end dedup: insert, re-insert (ignored), filter existing,
    filter mixed, and re-emit expired entries."""
    conn = sqlite3.connect(":memory:")
    try:
        # 1. init_schema is idempotent — prove it twice.
        init_schema(conn)
        init_schema(conn)

        # 2. Fresh item — first record() returns 1.
        item = SignalItem(
            source="gmail",
            section="# This Week",
            body="Reply: X",
            source_id="thread-001",
        )
        inserted = record(conn, [item])
        assert inserted == 1, f"expected 1 insert, got {inserted}"

        # 3. Re-record the same item — UNIQUE constraint kicks in.
        reinserted = record(conn, [item])
        assert reinserted == 0, f"expected 0 reinserts, got {reinserted}"

        # 4. filter_new sees it as already logged within the window.
        result = filter_new(conn, [item], window_days=14)
        assert result == [], f"expected empty filter result, got {result}"

        # 5. Second item with a different source_id — only it should come back.
        item2 = SignalItem(
            source="gmail",
            section="# This Week",
            body="Reply: Y",
            source_id="thread-002",
        )
        result2 = filter_new(conn, [item, item2], window_days=14)
        assert result2 == [item2], f"expected [item2], got {result2}"

        # 6. Manually insert a row dated 30 days ago; matching item should
        # re-emit because it's outside the 14-day window.
        thirty_days_ago = (
            datetime.now(timezone.utc) - timedelta(days=30)
        ).isoformat()
        conn.execute(
            "INSERT INTO signal_ingestion_log (source, source_id, ingested_at) "
            "VALUES (?, ?, ?)",
            ("calendar", "evt-old", thirty_days_ago),
        )
        conn.commit()

        old_item = SignalItem(
            source="calendar",
            section="# NOW",
            body="Prep: stale meeting",
            source_id="evt-old",
        )
        result3 = filter_new(conn, [old_item], window_days=14)
        assert result3 == [old_item], (
            f"expected expired item to re-emit, got {result3}"
        )
    finally:
        conn.close()
