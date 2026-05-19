"""Ingest external knowledge sources (Obsidian vault, Claude diaries) into tiered memory.

Idempotent: uses content_hash to skip unchanged files. Designed to run as Step 0 of
the nightly consolidation pipeline so new notes land in memory_search automatically.

Guardrails:
- Per-note content cap (default 4KB) to keep DB bloat bounded.
- Content scrubbed through scrub_content() before storage (redacts any leaked secrets).
- Excludes .obsidian/, Excalidraw/, and binary attachments.
- Skips files larger than MAX_FILE_BYTES to avoid pathological notes.
- Errors on individual files never abort the batch — they're logged and counted.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .store import TieredMemoryDB

logger = logging.getLogger(__name__)

# Cap per-entry content to keep the DB lean. Notes beyond this are truncated
# with an ellipsis marker so the summary stays queryable.
CONTENT_CHAR_CAP = 4000

# Hard ceiling on file size we'll even attempt to read (1 MB).
MAX_FILE_BYTES = 1 * 1024 * 1024

# Obsidian subdirectories to skip outright.
# Anywhere-in-path: matches hidden/metadata dirs at any depth (e.g., nested .obsidian).
# "Task List" is NOT here — it's handled by a top-level-only check in the ingest
# loop (P38) so the daily-task-list skill's folder is excluded without over-matching
# any user-created subfolder named "Task List" elsewhere in the vault (e.g.,
# ~/Will's Vault/Archive/Task List/). User corrections flow into tiered memory via
# the explicit diff→memory_observe path in the daily-task-list skill.
OBSIDIAN_EXCLUDE_DIRS = {".obsidian", "Excalidraw", ".trash", ".git"}


def _normalize_content(raw: str) -> str:
    """Truncate to CONTENT_CHAR_CAP, strip whitespace.

    Note: db.insert_entry() already runs scrub_content() internally, so we
    don't scrub here — doing it twice would redact twice and double-count.
    """
    text = raw.strip()
    if len(text) > CONTENT_CHAR_CAP:
        text = text[:CONTENT_CHAR_CAP].rstrip() + "\n\n[...truncated for memory ingest]"
    return text


def _ingest_file(
    db: TieredMemoryDB,
    path: Path,
    title_prefix: str,
    title_override: Optional[str] = None,
) -> str:
    """Ingest a single markdown file. Returns 'added', 'skipped', or 'error'.

    Dedup is handled by db.insert_entry() via content_hash (on raw content).
    On re-runs with unchanged file content, insert_entry returns the existing
    id and nothing is written. We detect that by comparing row counts before/after.
    """
    try:
        size = path.stat().st_size
        if size == 0 or size > MAX_FILE_BYTES:
            return "skipped"
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.warning("ingest_external: failed to read %s: %s", path, e)
        return "error"

    base_title = title_override or path.stem
    title = f"{title_prefix}{base_title}"
    content = _normalize_content(raw)
    if not content:
        return "skipped"

    try:
        with db._lock:
            before = db._conn.execute(
                "SELECT COUNT(*) as c FROM memory_entries"
            ).fetchone()["c"]
        db.insert_entry(title=title, content=content, category="project")
        with db._lock:
            after = db._conn.execute(
                "SELECT COUNT(*) as c FROM memory_entries"
            ).fetchone()["c"]
        return "added" if after > before else "skipped"
    except Exception as e:
        logger.warning("ingest_external: insert_entry failed for %s: %s", path, e)
        return "error"


def ingest_obsidian_vault(
    db: TieredMemoryDB,
    vault_path: str | Path,
) -> dict:
    """Walk an Obsidian vault and ingest all markdown notes.

    Returns {'added': N, 'skipped': N, 'error': N, 'scanned': N}.
    """
    vault = Path(vault_path).expanduser()
    counts = {"added": 0, "skipped": 0, "error": 0, "scanned": 0}

    if not vault.is_dir():
        logger.warning("ingest_obsidian_vault: vault not found at %s", vault)
        return counts

    for md_path in vault.rglob("*.md"):
        # Compute vault-relative path once — used for both scoping and titling.
        try:
            rel_parts = md_path.relative_to(vault).parts
        except ValueError:
            rel_parts = md_path.parts
        # Top-level-only exclusion for Task List (owned by daily-task-list skill).
        # A user subfolder like ~/Will's Vault/Archive/Task List/ is NOT excluded.
        if rel_parts and rel_parts[0] == "Task List":
            continue
        # Anywhere-in-path exclusion for hidden/metadata dirs (.obsidian, Excalidraw, etc).
        if any(part in OBSIDIAN_EXCLUDE_DIRS for part in md_path.parts):
            continue
        counts["scanned"] += 1
        # Use vault-relative path as title so nested notes don't collide
        try:
            rel = md_path.relative_to(vault)
            title = str(rel.with_suffix(""))
        except ValueError:
            title = md_path.stem
        result = _ingest_file(db, md_path, title_prefix="[obsidian] ", title_override=title)
        counts[result] = counts.get(result, 0) + 1

    logger.info(
        "ingest_obsidian_vault: scanned=%d added=%d skipped=%d error=%d",
        counts["scanned"], counts["added"], counts["skipped"], counts["error"],
    )
    return counts


def ingest_claude_diaries(
    db: TieredMemoryDB,
    diary_dir: str | Path,
) -> dict:
    """Ingest all Claude Code session diaries from a directory (non-recursive).

    Returns {'added': N, 'skipped': N, 'error': N, 'scanned': N}.
    """
    diary = Path(diary_dir).expanduser()
    counts = {"added": 0, "skipped": 0, "error": 0, "scanned": 0}

    if not diary.is_dir():
        logger.warning("ingest_claude_diaries: diary dir not found at %s", diary)
        return counts

    for md_path in sorted(diary.glob("*.md")):
        counts["scanned"] += 1
        result = _ingest_file(db, md_path, title_prefix="[diary] ", title_override=md_path.stem)
        counts[result] = counts.get(result, 0) + 1

    logger.info(
        "ingest_claude_diaries: scanned=%d added=%d skipped=%d error=%d",
        counts["scanned"], counts["added"], counts["skipped"], counts["error"],
    )
    return counts


# === P79 / MOL-215: Hermes-side diary ingestion ===
def ingest_hermes_diaries(
    db: TieredMemoryDB,
    diary_dir: str | Path,
) -> dict:
    """Ingest Hermes session diaries from ~/.hermes/memory/diary/ (non-recursive).

    Same content shape as ``ingest_claude_diaries`` (per MOL-215 design); only
    the title prefix differs so wing/room classification can distinguish the
    two sources downstream. Returns {'added': N, 'skipped': N, 'error': N,
    'scanned': N}. Idempotent via insert_entry's content-hash dedup.
    """
    diary = Path(diary_dir).expanduser()
    counts = {"added": 0, "skipped": 0, "error": 0, "scanned": 0}

    if not diary.is_dir():
        logger.warning("ingest_hermes_diaries: diary dir not found at %s", diary)
        return counts

    for md_path in sorted(diary.glob("*.md")):
        counts["scanned"] += 1
        result = _ingest_file(db, md_path, title_prefix="[hermes-diary] ", title_override=md_path.stem)
        counts[result] = counts.get(result, 0) + 1

    logger.info(
        "ingest_hermes_diaries: scanned=%d added=%d skipped=%d error=%d",
        counts["scanned"], counts["added"], counts["skipped"], counts["error"],
    )
    return counts


def ingest_all_external(
    db: TieredMemoryDB,
    obsidian_vault: Optional[str | Path] = None,
    claude_diary_dirs: Optional[list[str | Path]] = None,
    hermes_diary_dirs: Optional[list[str | Path]] = None,
) -> dict:
    """Run all configured external ingest sources. Returns aggregate counts per source."""
    results: dict = {}
    if obsidian_vault:
        results["obsidian"] = ingest_obsidian_vault(db, obsidian_vault)
    if claude_diary_dirs:
        agg = {"added": 0, "skipped": 0, "error": 0, "scanned": 0}
        for d in claude_diary_dirs:
            r = ingest_claude_diaries(db, d)
            for k in agg:
                agg[k] += r.get(k, 0)
        results["claude_diary"] = agg
    if hermes_diary_dirs:
        agg = {"added": 0, "skipped": 0, "error": 0, "scanned": 0}
        for d in hermes_diary_dirs:
            r = ingest_hermes_diaries(db, d)
            for k in agg:
                agg[k] += r.get(k, 0)
        results["hermes_diary"] = agg
    return results
