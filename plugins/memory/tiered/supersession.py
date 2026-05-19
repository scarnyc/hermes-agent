"""P50/MOL-177 Phase 2 — tombstone supersession for the daily consolidation job.

Phase 1 (search.py ranker) downweights stale chat entries at query time.
Phase 2 (this module) runs once per consolidation: when a recent project entry
contains a completed MOL-xxx token, older chat entries mentioning the same
token are marked superseded so they drop out of search entirely instead of
just being downranked.

Dry-run (default) writes every decision to `tombstone_audit` with applied=0.
Live mode (tombstone_dry_run: false) also UPDATEs `memory_entries.superseded_by`.
Tombstones are reversible — see docs or PATCHES.md P50 for the rollback recipe.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import TieredMemoryDB

logger = logging.getLogger(__name__)

# Word-boundary-aware completion markers. Mirrors the set used by the Phase 1
# ranker in search.py so both phases agree on what counts as "done".
COMPLETION_KEYWORDS = {
    "done", "shipped", "closed", "completed", "superseded",
    "landed", "merged", "archived", "n/a",
}

MOL_TOKEN_RE = re.compile(r"MOL-\d+")

# Per-project-entry cap on distinct MOL tokens we'll scan for. An adversarial
# or malformed entry containing thousands of "MOL-\d+" fragments would
# otherwise trigger one full-table LIKE scan per token. Real entries produce
# ~1-5 tokens; 50 is a very generous ceiling.
MAX_TOKENS_PER_PROJECT = 50

# Per-token cap on stale chat rows pulled back per scan. Protects against
# a widely-referenced ticket (e.g. MOL-1) matching thousands of chats.
MAX_STALE_ROWS_PER_TOKEN = 500

# Use \b word boundaries so "closed" in "disclosed" or "done" in "undone"
# don't false-match. Case-insensitive for "SHIPPED" / "Shipped" / "shipped".
_COMPLETION_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in COMPLETION_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

# P83/MOL-367 — split content into sentence-sized chunks for per-token scoping.
# Sentence terminators (. ! ?) plus newlines, so bullet lists and headers act
# as boundaries too. A single completion keyword in one sentence must NOT
# tombstone a token mentioned only in a different sentence.
_SENTENCE_SPLIT_RE = re.compile(r"[.!?\n]+")


def _has_completion_marker(text: str) -> bool:
    """Return True if text contains any completion keyword at a word boundary.

    Used as a cheap project-level fast-path before the per-token scan.
    The authoritative check is _token_is_completed() — see MOL-367.
    """
    return bool(_COMPLETION_RE.search(text or ""))


def _token_is_completed(token: str, text: str) -> bool:
    """Return True iff `text` contains a sentence holding both `token`
    (word-bounded) and a completion keyword.

    P83/MOL-367 — replaces the project-wide _has_completion_marker gate
    that wrongly tombstoned older chats whenever a project entry mentioned
    ANY completed ticket alongside unrelated tokens. Both sides are
    word-bounded: token-side prevents "MOL-1" matching "MOL-168";
    keyword-side prevents "closed" matching "disclosed".
    """
    if not text or not token:
        return False
    token_re = re.compile(rf"\b{re.escape(token)}\b")
    for chunk in _SENTENCE_SPLIT_RE.split(text):
        if token_re.search(chunk) and _COMPLETION_RE.search(chunk):
            return True
    return False


def _extract_mol_tokens(text: str) -> set[str]:
    """Return the distinct MOL-\\d+ tokens present in text."""
    return set(MOL_TOKEN_RE.findall(text or ""))


def run_supersession(
    db: "TieredMemoryDB",
    dry_run: bool = True,
    window_hours: int = 24,
) -> dict:
    """Scan recent project entries, tombstone stale chat entries mentioning completed MOL tokens.

    Args:
      db: the TieredMemoryDB instance (consolidation already holds one).
      dry_run: if True (default), only writes to tombstone_audit with applied=0.
               If False, also UPDATEs memory_entries.superseded_by.
      window_hours: only scan project entries created within this window.
                    24h matches the consolidation cadence — keeps the scan cheap
                    and avoids re-processing tokens the previous run already handled.

    Returns:
      {"scanned": <project entries scanned>,
       "candidates": <tombstone decisions recorded>,
       "applied": <rows actually mutated — 0 in dry-run>}
    """
    scanned = 0
    audit_rows: list[dict] = []
    apply_map: dict[str, str] = {}

    conn = db._conn
    with db._lock:
        project_rows = conn.execute(
            "SELECT id, content, created_at FROM memory_entries "
            "WHERE category = 'project' "
            "AND archived_at IS NULL "
            "AND superseded_by IS NULL "
            "AND created_at >= datetime('now', ?)",
            (f"-{int(window_hours)} hours",),
        ).fetchall()

        for project_row in project_rows:
            project_id = project_row["id"]
            project_content = project_row["content"] or ""
            project_created = project_row["created_at"]

            if not _has_completion_marker(project_content):
                continue

            tokens = _extract_mol_tokens(project_content)
            if not tokens:
                continue

            if len(tokens) > MAX_TOKENS_PER_PROJECT:
                logger.warning(
                    "Supersession: project entry %s has %d MOL tokens, "
                    "capping at %d (possible adversarial or malformed content)",
                    project_id, len(tokens), MAX_TOKENS_PER_PROJECT,
                )
                tokens = set(list(tokens)[:MAX_TOKENS_PER_PROJECT])

            scanned += 1

            for token in tokens:
                # P83/MOL-367 — per-token scoping. Project entry must mark
                # THIS token completed in the same sentence, not just contain
                # any completion keyword somewhere in the body.
                if not _token_is_completed(token, project_content):
                    continue

                # LIKE match on content. MOL-\d+ is specific enough that a
                # substring match is safe (no MOL-1 would false-match MOL-10
                # because the pattern MOL-\d+ in the content greedily includes
                # all digits). For chat entries, that's fine — we care about
                # any chat that references the same ticket.
                # LIMIT guards against widely-referenced tickets (e.g. MOL-1)
                # pulling back thousands of rows.
                stale_rows = conn.execute(
                    "SELECT id FROM memory_entries "
                    "WHERE category = 'chat' "
                    "AND archived_at IS NULL "
                    "AND superseded_by IS NULL "
                    "AND id != ? "
                    "AND created_at < ? "
                    "AND content LIKE ? "
                    "LIMIT ?",
                    (project_id, project_created, f"%{token}%", MAX_STALE_ROWS_PER_TOKEN),
                ).fetchall()

                for stale in stale_rows:
                    stale_id = stale["id"]
                    # Still need a precise token check — LIKE '%MOL-168%' also
                    # matches 'MOL-1680'. Re-verify with the regex.
                    stale_content_row = conn.execute(
                        "SELECT content FROM memory_entries WHERE id = ?",
                        (stale_id,),
                    ).fetchone()
                    if not stale_content_row:
                        continue
                    stale_tokens = _extract_mol_tokens(stale_content_row["content"] or "")
                    if token not in stale_tokens:
                        continue

                    # One stale entry can be referenced by multiple completed
                    # project entries within the same scan. First writer wins
                    # for the apply_map; extra audit rows are fine — they
                    # document the decision trail.
                    if stale_id not in apply_map:
                        apply_map[stale_id] = project_id

                    audit_rows.append({
                        "entry_id": stale_id,
                        "superseded_by_id": project_id,
                        "mol_token": token,
                        "reason": "project entry marked completed",
                        "applied": 0 if dry_run else 1,
                    })

    # Dry-run: just record audit rows (applied=0) — no DB mutation.
    # Live: audit insert + memory_entries UPDATE must be atomic, else a
    # mid-run crash can leave applied=1 audit rows against un-tombstoned
    # entries (lying audit trail). apply_tombstones_and_audit wraps both
    # in BEGIN IMMEDIATE / COMMIT.
    applied = 0
    if dry_run:
        if audit_rows:
            db.record_tombstone_audit(audit_rows)
    else:
        applied, _ = db.apply_tombstones_and_audit(apply_map, audit_rows)

    logger.info(
        "Supersession: scanned=%d candidates=%d applied=%d dry_run=%s",
        scanned, len(audit_rows), applied, dry_run,
    )
    return {
        "scanned": scanned,
        "candidates": len(audit_rows),
        "applied": applied,
    }
