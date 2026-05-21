#!/Users/wills_mac_mini/.hermes/hermes-agent/venv/bin/python3
# NOTE: shebang is machine-specific — the runtime copy at
# ~/.hermes/scripts/task_list_io.py uses the absolute path to this
# user's hermes-agent venv so `markdown_it` resolves. A different install
# location requires editing this line during deploy.
"""Shared I/O + path constants for the daily task-list pipeline (MOL-239 / MOL-246).

Extracted from compose_task_list.py so the morning composer AND the evening
enrichment orchestrator can both use the same atomic-write, bridge-check,
tripwire, and path helpers without one importing the other's CLI surface.

No behavior change vs. the original inline definitions in compose_task_list.py;
this module is a pure move.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")

TASK_LIST_DIR = Path.home() / "Will's Vault" / "Task List"
SNAP_DIR = Path.home() / ".hermes" / "cron" / "output" / "daily-task-list"


def _date_filename(d: date) -> str:
    return f"{d.strftime('%b')} {d.day} {d.year}.md"


def _snap_filename(d: date) -> str:
    return f"{d.strftime('%b')} {d.day} {d.year}-original.md"


def _atomic_write_pair(today_snap: Path, today: Path, payload: str) -> None:
    """Write snapshot then canonical via tmp-sibling + rename when possible.

    MOL-566 made the Obsidian vault writable (file-write-create + file-write-data)
    but kept file-write-unlink denied, so ``rename(src, existing_dst)`` raises
    EPERM for in-vault destinations: rename-over-existing is implemented as
    unlink-then-link at the kernel level. MOL-661 retired the bridge and made
    the canonical path live in the vault, so this matters every overwrite day.

    Strategy:
      * If the destination does not yet exist, do the standard tmp-sibling +
        rename dance — that's a pure create, no unlink needed, fully atomic.
      * If the destination exists, write the payload directly via O_TRUNC
        (allowed under file-write-data). Skip the tmp sibling entirely so a
        failed unlink doesn't leave a ``.tmp`` orphan visible to Obsidian.

    Snapshot dir lives in ``~/.hermes/cron/output/`` where unlink is allowed,
    so the conventional tmp-sibling dance is fine there.

    Not truly atomic in the ACID sense — a SIGKILL between the snap rename and
    the canonical write leaves the snapshot alone on disk; the compose path is
    idempotent and the next run replaces both files cleanly.
    """
    today_snap.parent.mkdir(parents=True, exist_ok=True)
    today.parent.mkdir(parents=True, exist_ok=True)

    _write_with_atomic_rename(today_snap, payload)
    _write_with_atomic_rename(today, payload)


def _write_with_atomic_rename(dst: Path, payload: str) -> None:
    """Write ``payload`` to ``dst`` atomically when possible.

    When ``dst`` already exists, write directly (O_TRUNC overwrite) since
    rename-over-existing would require unlink permission. When ``dst`` does
    not exist, write a ``.tmp`` sibling and rename — pure create, no unlink.
    """
    if dst.exists():
        dst.write_text(payload, encoding="utf-8")
        return

    tmp = dst.with_suffix(dst.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    try:
        os.rename(tmp, dst)
    except OSError:
        # Race: dst was created between exists()-check and rename. Fall back
        # to direct write of dst, then attempt to remove the tmp sibling.
        # The unlink may itself fail (deny-unlink subtree), in which case we
        # accept the .tmp leftover — direct triage via `ls`.
        dst.write_text(payload, encoding="utf-8")
        try:
            tmp.unlink()
        except OSError:
            pass


def _target_ok() -> tuple[bool, str]:
    """Verify the vault task-list directory is present and writable.

    MOL-661 retired the prior symlink bridge — TASK_LIST_DIR now points
    directly at ``~/Will's Vault/Task List``. The only structural check we
    need is that the path exists and is a real directory (vault writability
    is enforced by MOL-566's granular SBPL rules).
    """
    if not TASK_LIST_DIR.exists():
        return False, f"task-list dir missing: {TASK_LIST_DIR}"
    if not TASK_LIST_DIR.is_dir():
        return False, f"{TASK_LIST_DIR} is not a directory"
    return True, ""


def _write_tripwire(today_date: date, reason: str) -> Path | None:
    """Write ABORTED marker. Returns the marker path, or None if writing
    the marker itself failed (caller still emits ABORT: + exits 2 — the
    banner is the load-bearing signal, the marker is a bonus for downstream
    cron-verifier escalation).
    """
    try:
        SNAP_DIR.mkdir(parents=True, exist_ok=True)
        marker = SNAP_DIR / f"ABORTED-{today_date.isoformat()}.marker"
        stamp = datetime.now(ET).isoformat()
        marker.write_text(f"{stamp}\nreason: {reason}\n", encoding="utf-8")
        return marker
    except OSError as exc:
        print(f"WARN: tripwire write failed: {exc}", file=sys.stderr)
        return None


def _remove_stale_tripwire(today_date: date) -> None:
    marker = SNAP_DIR / f"ABORTED-{today_date.isoformat()}.marker"
    try:
        marker.unlink()
    except (FileNotFoundError, PermissionError):
        pass
