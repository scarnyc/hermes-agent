"""Test suite for compose_task_list.py (MOL-239)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from markdown_it import MarkdownIt

REF_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REF_DIR))

import compose_task_list  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCRIPT_PATH = REF_DIR / "compose_task_list.py"


def _expected_after_drop(source: str) -> tuple[str, int]:
    """Independently compute expected drop output via the same walk semantics.

    Mirrors compose_task_list._find_drop_line_ranges (including the
    bullet_list_open / ordered_list_open break), implemented separately so
    the test asserts against an oracle rather than the helper itself.
    """
    md = MarkdownIt("commonmark").enable("table")
    tokens = md.parse(source)
    ranges: list[tuple[int, int]] = []
    for i, tok in enumerate(tokens):
        if tok.type != "list_item_open" or tok.map is None:
            continue
        first_inline = None
        for follower in tokens[i + 1:]:
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
    drop_set: set[int] = set()
    for s, e in ranges:
        for idx in range(s, e):
            drop_set.add(idx)
    lines = source.splitlines(keepends=True)
    kept = [ln for i, ln in enumerate(lines) if i not in drop_set]
    return "".join(kept), len(ranges)


def test_replay_apr19_to_apr20():
    src = (FIXTURES / "apr_19_2026.md").read_text(encoding="utf-8")
    expected, expected_dropped = _expected_after_drop(src)
    output, dropped = compose_task_list.compose(src)
    assert dropped > 0, "fixture should contain [x] items"
    assert dropped == expected_dropped
    assert output == expected


def test_preservation_all_bytes_preserved():
    """Floor-and-ceiling: dropped lines lose their markers, surviving lines retain theirs."""
    src = (FIXTURES / "preservation_seed.md").read_text(encoding="utf-8")
    output, dropped = compose_task_list.compose(src)

    # Seed has 4 [x] items: 2 bare, 1 with **bold** (2 ``**``), 1 with `backtick` (2 ``)
    assert dropped == 4

    # Ceiling: specific surviving markers still present byte-exact.
    assert "**Switch from OpenClaw to Hermes Blog:**" in output
    assert "`hvac-controller`" in output
    assert "`sqlite-vec`" in output
    assert "**Blog Post: Agentic Task Accuracy:**" in output

    # Dropped content is gone.
    assert "discard me" not in output
    assert "drop this bold line" not in output
    assert "drop this `backtick` line" not in output

    # Floor: count deltas match EXACTLY what dropped lines contained.
    # Two `**` in "**drop this bold line**" → output loses 2 asterisks.
    assert output.count("**") == src.count("**") - 2
    # Two backticks in `backtick` → output loses 2.
    assert output.count("`") == src.count("`") - 2
    # No tabs on any dropped line → tab count unchanged (nested bullets only
    # appear under surviving parents in this seed).
    assert output.count("\t") == src.count("\t")


def test_drop_includes_nested_children():
    seed = "- [x] done parent\n\t- [ ] orphan child\n- [ ] live sibling\n"
    output, dropped = compose_task_list.compose(seed)
    assert output == "- [ ] live sibling\n"
    assert dropped == 1


def test_bare_parent_with_nested_x_keeps_siblings():
    """Regression guard for the bare-parent false-positive (code-reviewer finding).

    A parent list item with no text content whose child is `[x]` must NOT
    cause the parent's whole `.map` range to drop — only the nested `[x]`
    child drops. The parent + any non-[x] nested siblings survive.
    """
    seed = (
        "- \n"
        "\t- [x] done child\n"
        "\t- [ ] pending child\n"
        "- [ ] next top-level\n"
    )
    output, dropped = compose_task_list.compose(seed)
    # Only the nested [x] drops. Parent, pending child, next top-level all stay.
    assert "pending child" in output, "false-positive: bare parent dropped its pending child"
    assert "next top-level" in output
    assert "done child" not in output
    assert dropped == 1


def test_bridge_abort_no_write(tmp_path, monkeypatch):
    """Run script with HOME pointing at a tempdir that has no vault symlink.

    Asserts the SPECIFIC bridge-failure ABORT fires (not the yesterday-file-
    missing ABORT which has a different message). Seeds yesterday's file so
    the yesterday-file branch can't pre-empt the bridge check.
    """
    fake_home = tmp_path
    tl_dir = fake_home / ".hermes" / "notes" / "obsidian" / "task-list"
    tl_dir.mkdir(parents=True)
    yesterday_file = tl_dir / "Apr 20 2026.md"
    yesterday_file.write_text("- [ ] placeholder\n", encoding="utf-8")

    today_path = tmp_path / "today.md"
    snap_path = tmp_path / "snap.md"

    env = os.environ.copy()
    env["HOME"] = str(fake_home)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--today-date",
            "2026-04-21",
            "--today",
            str(today_path),
            "--today-snap",
            str(snap_path),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 2, f"stdout={result.stdout} stderr={result.stderr}"
    # Lock in the SPECIFIC abort message so a future refactor that removes
    # the bridge check can't silently pass via the yesterday-file-missing path.
    assert "ABORT:" in result.stderr
    assert "is not a symlink" in result.stderr, f"unexpected abort reason: {result.stderr}"
    assert not today_path.exists()
    assert not snap_path.exists()

    tripwire = fake_home / ".hermes" / "cron" / "output" / "daily-task-list" / "ABORTED-2026-04-21.marker"
    assert tripwire.exists(), f"expected tripwire at {tripwire}"


def test_atomic_write_rollback_on_canonical_fail(tmp_path, monkeypatch):
    """Snapshot is written first; canonical rename failure rolls back snapshot.

    Captures mid-test state to guard against implementation-specific call
    ordering drift — the test asserts that the snapshot DID exist before the
    canonical rename attempt (otherwise `not snap.exists()` would pass
    vacuously when the first rename never ran).
    """
    snap = tmp_path / "snap.md"
    today = tmp_path / "today.md"

    real_rename = os.rename
    calls = {"n": 0, "snap_existed_before_canonical": False}

    def flaky_rename(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            # Capture pre-canonical state so we can assert the snapshot
            # was actually written by call 1 before we blow up call 2.
            calls["snap_existed_before_canonical"] = snap.exists()
            raise OSError("simulated canonical rename failure")
        return real_rename(src, dst)

    monkeypatch.setattr(compose_task_list.os, "rename", flaky_rename)

    with pytest.raises(OSError, match="simulated canonical rename failure"):
        compose_task_list._atomic_write_pair(snap, today, "payload\n")

    assert calls["n"] == 2, f"expected 2 rename attempts, got {calls['n']}"
    assert calls["snap_existed_before_canonical"], (
        "snapshot must exist before canonical rename attempt — otherwise the "
        "rollback assertion below is vacuous"
    )
    assert not snap.exists(), "snapshot should have been rolled back"
    assert not today.exists()
