#!/Users/wills_mac_mini/.hermes/hermes-agent/venv/bin/python3
# NOTE: shebang is machine-specific — the runtime copy at
# ~/.hermes/scripts/compose_task_list.py uses the absolute path to this
# user's hermes-agent venv so `markdown_it` resolves. A different install
# location requires editing this line during deploy.
"""Preserve-then-mutate morning briefing composer (MOL-239).

Reads yesterday's task-list file, drops completed `[x]` list items (including
nested children), atomically writes the result to today's snapshot + canonical
paths. No external data sources. No LLM. Byte-slice preservation.

Determinism: `compose()` depends only on the `markdown-it-py` tokenizer — no
clock, no filesystem, no network. Given the same input string, it always
returns the same output.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from markdown_it import MarkdownIt

from task_list_io import (
    ET, TASK_LIST_DIR, SNAP_DIR, VAULT_SYMLINK, BRIDGE_TARGET,
    _date_filename, _snap_filename, _atomic_write_pair, _bridge_ok,
    _write_tripwire, _remove_stale_tripwire,
)


def _assert_map_semantics() -> None:
    """Runtime defense-in-depth: fail fast if markdown-it-py's `.map` ever
    narrows its semantics so a parent `list_item_open` no longer covers
    nested children. Pre-flight verified on 4.0.0 that outer map[1] reaches
    past the nested list's last line. If a future version regresses this,
    the drop pass would leave orphan sub-bullets surviving silently — this
    check prevents that.
    """
    probe = "- [x] foo\n\t- [ ] bar\n"
    tokens = MarkdownIt("commonmark").parse(probe)
    outer = next((t for t in tokens if t.type == "list_item_open"), None)
    if outer is None or outer.map is None or outer.map[1] < 2:
        raise RuntimeError(
            "markdown-it-py .map semantics regression: outer list_item_open "
            f"does not cover nested children (map={outer.map if outer else None}). "
            "Drop pass would leave orphan sub-bullets. Pin markdown-it-py<5 or "
            "update _find_drop_line_ranges to walk descendants."
        )


def _find_drop_line_ranges(text: str) -> list[tuple[int, int]]:
    """Return list of (start, end_exclusive) line ranges covering [x] list items.

    For each `list_item_open`, inspect ONLY the item's own inline content
    (the prefix before any nested `bullet_list_open`/`ordered_list_open`).
    If that inline starts with `[x]` / `[X]`, the whole item's `.map` range
    drops. A bare parent with an `[x]` nested child does NOT trip — the
    parent's inline scan terminates at the nested list before the child's
    inline comes into view.
    """
    md = MarkdownIt("commonmark").enable("table")
    tokens = md.parse(text)
    ranges: list[tuple[int, int]] = []
    for i, tok in enumerate(tokens):
        if tok.type != "list_item_open" or tok.map is None:
            continue
        first_inline = None
        for follower in tokens[i + 1:]:
            # Stop before descending into a nested list — the first inline we
            # want is THIS item's own content, not a nested child's content.
            if follower.type in ("bullet_list_open", "ordered_list_open"):
                break
            if follower.type == "list_item_close":
                break
            if follower.type == "inline":
                first_inline = follower
                break
        if first_inline is None:
            continue
        content = first_inline.content.lstrip()
        if content[:3].lower() == "[x]":
            ranges.append((tok.map[0], tok.map[1]))
    return ranges


def compose(yesterday_text: str) -> tuple[str, int]:
    """Pure transformation: yesterday's text → today's text + dropped count.

    Splits source on line separators, drops every line whose 0-indexed position
    falls inside any `[x]` list-item range, rejoins with original separators
    preserved exactly. `splitlines(keepends=True)` preserves \\n / \\r\\n / \\r.
    """
    _assert_map_semantics()
    lines = yesterday_text.splitlines(keepends=True)
    drop_ranges = _find_drop_line_ranges(yesterday_text)
    drop_set: set[int] = set()
    for start, end in drop_ranges:
        for idx in range(start, end):
            drop_set.add(idx)
    kept = [ln for i, ln in enumerate(lines) if i not in drop_set]
    dropped_count = len(drop_ranges)
    return "".join(kept), dropped_count


def main(argv: list[str] | None = None) -> int:
    """Compose today's task list from yesterday.

    Production cron mode: all CLI flags absent; dates from datetime.now(ET);
    bridge check enabled; atomic write to canonical vault paths.

    Test / manual mode: CLI flags override paths and date. `--dry-run`
    combined with path overrides skips the bridge check (isolates the
    compose() + output-format paths for unit testing without needing a
    live vault symlink); `--dry-run` alone still asserts bridge OK.
    """
    parser = argparse.ArgumentParser(description="Compose today's task list from yesterday.")
    parser.add_argument("--yesterday", type=Path)
    parser.add_argument("--today", type=Path)
    parser.add_argument("--today-snap", type=Path)
    parser.add_argument("--today-date")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.today_date:
        today_d = date.fromisoformat(args.today_date)
    else:
        today_d = datetime.now(ET).date()
    yesterday_d = today_d - timedelta(days=1)

    yesterday_path = args.yesterday or (TASK_LIST_DIR / _date_filename(yesterday_d))
    today_path = args.today or (TASK_LIST_DIR / _date_filename(today_d))
    today_snap_path = args.today_snap or (SNAP_DIR / _snap_filename(today_d))

    paths_overridden = any([args.yesterday, args.today, args.today_snap])
    skip_bridge = args.dry_run and paths_overridden

    if not skip_bridge:
        ok, reason = _bridge_ok()
        if not ok:
            _write_tripwire(today_d, reason)
            print(f"ABORT: {reason}", file=sys.stderr)
            return 2
        _remove_stale_tripwire(today_d)

    if not yesterday_path.exists():
        reason = f"yesterday file not found: {yesterday_path}"
        _write_tripwire(today_d, reason)
        print(f"ABORT: {reason}", file=sys.stderr)
        return 2

    yesterday_text = yesterday_path.read_text(encoding="utf-8")
    composed, dropped = compose(yesterday_text)

    if args.dry_run:
        print(f"DRY-RUN would write snapshot: {today_snap_path}")
        print(f"DRY-RUN would write canonical: {today_path}")
        print(f"COMPOSED: {today_path}")
        print(f"dropped: {dropped} completed items")
        return 0

    _atomic_write_pair(today_snap_path, today_path, composed)

    print(f"COMPOSED: {today_path}")
    print(f"dropped: {dropped} completed items")
    return 0


if __name__ == "__main__":
    sys.exit(main())
