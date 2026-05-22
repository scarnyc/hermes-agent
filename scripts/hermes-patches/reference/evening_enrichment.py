#!/Users/wills_mac_mini/.hermes/hermes-agent/venv/bin/python3
# NOTE: shebang is machine-specific — the runtime copy at
# ~/.hermes/scripts/evening_enrichment.py uses the absolute path to the
# user's hermes-agent venv so markdown-it-py, granola MCP import, and
# sqlite3 all resolve. A different install requires editing this line.
# P55/MOL-246 — runtime deploy marker (picked up by verify_patches.sh)
"""Evening enrichment orchestrator (MOL-246).

Peer of MOL-239's morning preserve-then-mutate composer. Pulls action
items from Calendar / Gmail / Granola at 21:00 ET, dedupes against the
``signal_ingestion_log`` SQLite table, and appends surviving bullets
into today's task-list file at ``# NOW`` / ``# This Week`` section
anchors. The morning job carries them forward into tomorrow's file via
the existing byte-faithful drop pass — no coordination required.

Safety posture:
    * ``--dry-run`` defaults TRUE. Apply mode requires both
      ``cfg["enrichment"]["auto_apply"]`` true AND absence of
      ``--dry-run`` AND presence of ``--apply``. Dry-run never writes
      to the dedup log, keeping previews reproducible.
    * Today-file existence + vault-symlink bridge are pre-flighted; a
      tripwire marker + exit 2 signals downstream cron verification.
    * Each per-section ``append_items`` call is wrapped in a
      ``SectionNotFoundError`` catch so a missing heading only skips
      that section, never crashes the job.
    * ``ENRICH:`` summary line is printed unconditionally so zero-item
      runs are visibly attributed (cal=N gmail=M granola=K,
      filtered=Q deduped).
"""
from __future__ import annotations

import argparse
import difflib
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

# Co-located reference layout: evening_enrichment.py sits next to
# task_list_io.py + the enrichment/ package. Runtime deploy preserves
# the same relative layout under ~/.hermes/scripts/.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from task_list_io import (  # noqa: E402
    ET,
    TASK_LIST_DIR,
    SNAP_DIR,
    _atomic_write_pair,
    _bridge_ok,
    _date_filename,
    _remove_stale_tripwire,
    _write_tripwire,
)
from enrichment.append import SectionNotFoundError, append_items  # noqa: E402
from enrichment.dedup import filter_new, init_schema, record  # noqa: E402
from enrichment.extractors import (  # noqa: E402
    extract_calendar_items,
    extract_gmail_items,
    extract_granola_items,
)
from enrichment.types import SignalItem  # noqa: E402


DEFAULT_DB_PATH = Path.home() / ".hermes" / "memory" / "hermes.db"
DEFAULT_CONFIG_PATH = Path.home() / ".hermes" / "config.yaml"
_SECTIONS = ("# NOW", "# This Week")


def _enrich_snap_filename(d: date) -> str:
    """Pre-enrichment snapshot name — distinct from morning's -original.md."""
    return f"{d.strftime('%b')} {d.day} {d.year}-pre-enrich.md"


def _load_config(path: Path) -> dict:
    """Best-effort config read. Missing file or YAML error → empty dict so
    orchestrator falls back to defensive defaults (``auto_apply=False``).
    """
    if not path.exists():
        return {}
    try:
        import yaml  # PyYAML ships with hermes-agent venv

        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001 — any yaml failure defers to defaults
        sys.stderr.write(f"WARN: config load failed ({exc}); using defaults\n")
        return {}


def _resolve_dry_run(args: argparse.Namespace, cfg: dict) -> bool:
    """Dry-run true unless: cfg auto_apply true AND --apply passed AND NOT --dry-run.
    This triple-gate is intentional; two boolean guards prevented a premature
    flip during earlier-session scope.
    """
    auto_apply = bool(cfg.get("enrichment", {}).get("auto_apply", False))
    if args.dry_run:
        return True
    if args.apply and auto_apply:
        return False
    # No --apply, or config not flipped: stay in dry-run.
    return True


def _build_summary(
    items: list[SignalItem],
    items_new: list[SignalItem],
    per_source_total: dict[str, int],
) -> str:
    """Format the unconditional ``ENRICH:`` status line."""
    filtered = len(items) - len(items_new)
    return (
        f"ENRICH: {len(items_new)} items "
        f"(cal={per_source_total.get('calendar', 0)} "
        f"gmail={per_source_total.get('gmail', 0)} "
        f"granola={per_source_total.get('granola', 0)}; "
        f"filtered={filtered} deduped)"
    )


def main(
    argv: Optional[list[str]] = None,
    *,
    _extractors: Optional[dict[str, Callable[[], list[SignalItem]]]] = None,
    _config_override: Optional[dict] = None,
) -> int:
    """Compose evening enrichment for today's task list.

    Production cron mode: ``--dry-run`` auto-resolved via config; bridge
    check enabled; today-file existence gated; DB at
    ``~/.hermes/memory/hermes.db``.

    Test / manual mode: the ``_extractors`` and ``_config_override``
    kwargs are dependency-injection hooks used by ``test_evening_enrichment``
    to supply mock extractors + inline config without touching the live
    filesystem. ``--today`` / ``--today-snap`` / ``--db-path`` path flags
    cover the remaining file-side overrides. ``--dry-run`` combined with
    any path override skips the bridge check (isolates the dry-run +
    diff paths for unit testing without a live vault symlink).
    """
    parser = argparse.ArgumentParser(
        description="Append calendar / gmail / granola signals into today's task-list file."
    )
    parser.add_argument("--dry-run", action="store_true", help="Default behaviour — print diff, do not write.")
    parser.add_argument("--apply", action="store_true", help="Write mutation. Honoured only with config auto_apply=true.")
    parser.add_argument("--today", type=Path)
    parser.add_argument("--today-snap", type=Path)
    parser.add_argument("--today-date")
    parser.add_argument("--db-path", type=Path)
    parser.add_argument("--config-path", type=Path)
    args = parser.parse_args(argv)

    if args.today_date:
        today_d = date.fromisoformat(args.today_date)
    else:
        today_d = datetime.now(ET).date()
    tomorrow_d = today_d + timedelta(days=1)

    today_path = args.today or (TASK_LIST_DIR / _date_filename(today_d))
    today_snap_path = args.today_snap or (SNAP_DIR / _enrich_snap_filename(today_d))
    db_path = args.db_path or DEFAULT_DB_PATH

    paths_overridden = any([args.today, args.today_snap, args.db_path])
    skip_bridge = args.dry_run and paths_overridden

    # Pre-flight 1: vault-symlink bridge (reused from MOL-239).
    if not skip_bridge:
        ok, reason = _bridge_ok()
        if not ok:
            _write_tripwire(today_d, reason)
            print(f"ABORT: {reason}", file=sys.stderr)
            return 2
        _remove_stale_tripwire(today_d)

    # Pre-flight 2: today-file exists. Cascading morning failure must
    # not silently skip evening enrichment — surface it.
    if not today_path.exists():
        reason = f"today file not found: {today_path}"
        if not skip_bridge:
            _write_tripwire(today_d, reason)
        print(f"ABORT: {reason}", file=sys.stderr)
        return 2

    today_text = today_path.read_text(encoding="utf-8")

    # DB connection + schema init (idempotent).
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        init_schema(conn)

        # Extractor dispatch — injection hook for tests, real wiring for prod.
        if _extractors is None:
            _extractors = {
                "calendar": lambda: extract_calendar_items(tomorrow_d),
                "gmail": lambda: extract_gmail_items(),
                "granola": lambda: extract_granola_items(today_d),
            }
        all_items: list[SignalItem] = []
        per_source_total: dict[str, int] = {}
        for name, fn in _extractors.items():
            produced = list(fn())
            per_source_total[name] = len(produced)
            all_items.extend(produced)

        cfg = _config_override if _config_override is not None else _load_config(
            args.config_path or DEFAULT_CONFIG_PATH
        )
        dedup_window = int(cfg.get("enrichment", {}).get("dedup_window_days", 14))
        items_new = filter_new(conn, all_items, window_days=dedup_window)

        # Per-section append with missing-section guard.
        enriched = today_text
        for section in _SECTIONS:
            section_items = [i for i in items_new if i.section == section]
            if not section_items:
                continue
            try:
                enriched = append_items(enriched, section, section_items)
            except SectionNotFoundError:
                sys.stderr.write(
                    f"WARN: section '{section}' not found in {today_path}; "
                    f"skipping {len(section_items)} items\n"
                )

        summary = _build_summary(all_items, items_new, per_source_total)
        dry_run = _resolve_dry_run(args, cfg)

        if dry_run:
            print(summary)
            diff = difflib.unified_diff(
                today_text.splitlines(keepends=True),
                enriched.splitlines(keepends=True),
                fromfile=f"{today_path} (current)",
                tofile=f"{today_path} (enriched)",
            )
            sys.stdout.writelines(diff)
            return 0

        # Apply mode: snapshot pre-state, write canonical, record dedup.
        _atomic_write_pair(today_snap_path, today_path, enriched)
        record(conn, items_new)
        print(summary)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
