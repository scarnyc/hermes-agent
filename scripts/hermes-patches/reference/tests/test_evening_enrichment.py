"""Orchestrator integration test for MOL-246 evening enrichment (test 7).

Covers the ``--dry-run-never-records`` invariant + ``ENRICH:`` summary
contract. Other tests live in ``test_append.py`` / ``test_dedup.py`` /
``test_extractors.py`` — keeping modules focused so parallel authors
do not collide on a single test file.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_REF_DIR = Path(__file__).resolve().parents[1]
if str(_REF_DIR) not in sys.path:
    sys.path.insert(0, str(_REF_DIR))

import evening_enrichment  # noqa: E402
from enrichment.types import SignalItem  # noqa: E402


_SEED = """\
# NOW

- [ ] existing NOW bullet

# This Week

- [ ] existing week bullet

# Later

- [ ] tail bullet
"""


def _mk_items():
    return [
        SignalItem(
            source="calendar",
            section="# NOW",
            body="Prep for 10:00 standup",
            source_id="evt-001",
        ),
        SignalItem(
            source="gmail",
            section="# This Week",
            body="Reply: review doc (Philippe)",
            source_id="thread-aaaaaaaaaaaaaaaa",
        ),
    ]


def _mk_extractors(items):
    # Split by source so the orchestrator's per-source counter is exercised.
    by_source = {"calendar": [], "gmail": [], "granola": []}
    for item in items:
        by_source.setdefault(item.source, []).append(item)
    return {name: (lambda lst=lst: lst) for name, lst in by_source.items()}


def test_dry_run_never_records(tmp_path, capsys):
    today_path = tmp_path / "today.md"
    today_snap_path = tmp_path / "today-pre-enrich.md"
    db_path = tmp_path / "hermes.db"
    today_path.write_text(_SEED, encoding="utf-8")

    items = _mk_items()
    extractors = _mk_extractors(items)

    # --- Dry-run pass: ENRICH: summary printed, diff shown, DB untouched. ---
    rc_dry = evening_enrichment.main(
        [
            "--dry-run",
            "--today",
            str(today_path),
            "--today-snap",
            str(today_snap_path),
            "--db-path",
            str(db_path),
            "--today-date",
            "2026-04-22",
        ],
        _extractors=extractors,
        _config_override={"enrichment": {"auto_apply": False, "dedup_window_days": 14}},
    )
    out_dry = capsys.readouterr().out
    assert rc_dry == 0
    assert "ENRICH: 2 items (cal=1 gmail=1 granola=0; filtered=0 deduped)" in out_dry
    # Diff should include BOTH new bullets.
    assert "+- [ ] Prep for 10:00 standup" in out_dry
    assert "+- [ ] Reply: review doc (Philippe)" in out_dry
    # Today-file is untouched on dry-run.
    assert today_path.read_text(encoding="utf-8") == _SEED
    # Snapshot was not written.
    assert not today_snap_path.exists()
    # Dedup log is empty.
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT count(*) FROM signal_ingestion_log").fetchone()
    finally:
        conn.close()
    assert rows == (0,)

    # --- Apply pass: same items, auto_apply true, --apply flag. Records. ---
    rc_apply = evening_enrichment.main(
        [
            "--apply",
            "--today",
            str(today_path),
            "--today-snap",
            str(today_snap_path),
            "--db-path",
            str(db_path),
            "--today-date",
            "2026-04-22",
        ],
        _extractors=extractors,
        _config_override={"enrichment": {"auto_apply": True, "dedup_window_days": 14}},
    )
    out_apply = capsys.readouterr().out
    assert rc_apply == 0
    assert "ENRICH: 2 items (cal=1 gmail=1 granola=0; filtered=0 deduped)" in out_apply
    # File was mutated.
    new_text = today_path.read_text(encoding="utf-8")
    assert new_text != _SEED
    assert "- [ ] Prep for 10:00 standup" in new_text
    assert "- [ ] Reply: review doc (Philippe)" in new_text
    # Snapshot written (pre-mutation content).
    assert today_snap_path.exists()
    # Dedup log has both items.
    conn = sqlite3.connect(str(db_path))
    try:
        count = conn.execute("SELECT count(*) FROM signal_ingestion_log").fetchone()[0]
        sources = {
            row[0]
            for row in conn.execute(
                "SELECT source FROM signal_ingestion_log"
            ).fetchall()
        }
    finally:
        conn.close()
    assert count == 2
    assert sources == {"calendar", "gmail"}

    # --- Re-run apply: items are deduped, ENRICH: shows filtered=2. ---
    rc_rerun = evening_enrichment.main(
        [
            "--apply",
            "--today",
            str(today_path),
            "--today-snap",
            str(today_snap_path),
            "--db-path",
            str(db_path),
            "--today-date",
            "2026-04-22",
        ],
        _extractors=extractors,
        _config_override={"enrichment": {"auto_apply": True, "dedup_window_days": 14}},
    )
    out_rerun = capsys.readouterr().out
    assert rc_rerun == 0
    assert "ENRICH: 0 items (cal=1 gmail=1 granola=0; filtered=2 deduped)" in out_rerun


def test_dry_run_default_when_auto_apply_false(tmp_path, capsys):
    """--apply without config.auto_apply=true stays in dry-run."""
    today_path = tmp_path / "today.md"
    today_path.write_text(_SEED, encoding="utf-8")
    db_path = tmp_path / "hermes.db"

    items = _mk_items()
    rc = evening_enrichment.main(
        [
            "--apply",  # caller asks for apply
            "--today",
            str(today_path),
            "--today-snap",
            str(tmp_path / "snap.md"),
            "--db-path",
            str(db_path),
            "--today-date",
            "2026-04-22",
        ],
        _extractors=_mk_extractors(items),
        _config_override={"enrichment": {"auto_apply": False, "dedup_window_days": 14}},
    )
    out = capsys.readouterr().out
    assert rc == 0
    # Still dry-run → diff printed, file untouched.
    assert "ENRICH: 2 items" in out
    assert today_path.read_text(encoding="utf-8") == _SEED
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT count(*) FROM signal_ingestion_log").fetchone()
    finally:
        conn.close()
    assert rows == (0,)


def test_today_file_missing_aborts(tmp_path, capsys):
    """Cascading morning failure surfaces as exit-2 + ABORT banner."""
    today_path = tmp_path / "nonexistent.md"
    db_path = tmp_path / "hermes.db"
    rc = evening_enrichment.main(
        [
            "--dry-run",
            "--today",
            str(today_path),
            "--today-snap",
            str(tmp_path / "snap.md"),
            "--db-path",
            str(db_path),
            "--today-date",
            "2026-04-22",
        ],
        _extractors={"calendar": lambda: [], "gmail": lambda: [], "granola": lambda: []},
        _config_override={"enrichment": {"auto_apply": False, "dedup_window_days": 14}},
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "ABORT: today file not found" in err
