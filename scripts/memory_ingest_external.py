#!/usr/bin/env python3
"""Cron pre-script: external memory ingest + tombstone supersession.

Runs before the Tiered Memory Consolidation job.

Phases:
1. External Memory Ingest: Ingest Obsidian vault + Claude diaries.
2. P50/MOL-177 Phase 2 — Tombstone supersession: tombstone older chat entries
   that mention MOL tickets a newer project entry marks as completed.
   Dry-run by default (memory.consolidation.tombstone_dry_run).

Stdout is injected as context into the consolidation prompt.

Note (MOL-177 cleanup): an earlier `maintain_tasks()` phase was removed here.
It imported `SessionDB` from a module where that class never existed (so the
import always failed), iterated over TASKS.md items with an empty `pass` body
(so no pruning happened), and always printed "Sync complete" regardless. Since
MOL-235 (2026-04-19) TASKS.md is archive-frozen — see CLAUDE.md Key Paths.
"""

import os
import sys
import time
import traceback
from pathlib import Path

HERMES_ROOT = Path.home() / ".hermes"
HERMES_AGENT = HERMES_ROOT / "hermes-agent"
if str(HERMES_AGENT) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT))

DB_PATH = os.environ.get("HERMES_MEMORY_DB", str(HERMES_ROOT / "memory" / "hermes.db"))


def ingest_hermes_diaries():
    """P79 / MOL-215 — resolve the Hermes diary dir for ingest_all_external.

    Returns a list[str] of diary dirs (always single-element under current
    layout) or None if the dir doesn't exist. Wired into ingest_external()
    below as the ``hermes_diary_dirs`` arg.
    """
    diary = Path(
        os.environ.get("HERMES_DIARY", str(Path.home() / ".hermes" / "memory" / "diary"))
    ).expanduser()
    if not diary.is_dir():
        return None
    return [str(diary)]


def ingest_external():
    """External memory ingest — Obsidian vault + Claude diaries + Hermes diaries (MOL-215)."""
    try:
        from plugins.memory.tiered.store import TieredMemoryDB
        from plugins.memory.tiered.ingest_external import ingest_all_external
    except Exception as e:
        print(f"[memory_ingest_external] import failed: {e}")
        return

    vault = Path(os.environ.get("HERMES_OBSIDIAN_VAULT", str(Path.home() / "Will's Vault"))).expanduser()
    diary = Path(os.environ.get("HERMES_CLAUDE_DIARY", str(Path.home() / ".claude" / "memory" / "diary"))).expanduser()
    db_file = Path(DB_PATH).expanduser()

    if not db_file.exists():
        return

    vault_arg = str(vault) if vault.is_dir() else None
    diary_arg = [str(diary)] if diary.is_dir() else None
    hermes_diary_arg = ingest_hermes_diaries()
    if not vault_arg and not diary_arg and not hermes_diary_arg:
        return

    db = TieredMemoryDB(str(db_file))
    try:
        t0 = time.time()
        results = ingest_all_external(
            db,
            obsidian_vault=vault_arg,
            claude_diary_dirs=diary_arg,
            hermes_diary_dirs=hermes_diary_arg,
        )
        elapsed = time.time() - t0

        print(f"\n## External Memory Ingest ({elapsed:.1f}s)")
        for src, counts in results.items():
            print(f"- {src}: scanned={counts.get('scanned', 0)} added={counts.get('added', 0)}")
    finally:
        db.close()


def run_tombstone_supersession():
    """P50/MOL-177 Phase 2 — tombstone stale chat entries mentioning completed MOL tokens.

    Runs every consolidation cycle. Reads memory.consolidation.tombstone_dry_run
    from config; ships dry-run on first deploy so the first week of audit rows
    is inspectable before mutations start.
    """
    try:
        from plugins.memory.tiered.store import TieredMemoryDB
        from plugins.memory.tiered.supersession import run_supersession
        from hermes_cli.config import load_config
    except Exception as e:
        print(f"[supersession] import failed: {e}")
        return

    db_file = Path(DB_PATH).expanduser()
    if not db_file.exists():
        return

    cfg = (load_config().get("memory", {}) or {}).get("consolidation", {}) or {}
    dry_run = bool(cfg.get("tombstone_dry_run", True))
    window_hours = int(cfg.get("tombstone_window_hours", 24))

    db = TieredMemoryDB(str(db_file))
    try:
        t0 = time.time()
        result = run_supersession(db, dry_run=dry_run, window_hours=window_hours)
        elapsed = time.time() - t0
        mode = "DRY-RUN" if dry_run else "LIVE"
        print(f"\n## Tombstone Supersession ({mode}, {elapsed:.1f}s)")
        print(f"- scanned project entries: {result['scanned']}")
        print(f"- tombstone candidates:    {result['candidates']}")
        print(f"- applied (rows mutated):  {result['applied']}")
        if result["candidates"] > 0:
            print(f"- audit rows in last run recorded to tombstone_audit table")
    except Exception as e:
        print(f"[supersession] run failed: {e}")
        traceback.print_exc()
    finally:
        db.close()


EXTRACTION_PROMPT = (
    "You are reviewing a conversation transcript. Extract ONLY high-confidence "
    "durable facts. Be strict — it is better to return [] than to invent facts.\n\n"
    "A fact is HIGH-CONFIDENCE only if at least ONE of these is present in the transcript:\n"
    "  (1) a specific command + flags (e.g. `gws gmail +triage` or `grep -r foo`)\n"
    "  (2) a specific file path (e.g. `~/.hermes/config.yaml` or `/etc/hosts`)\n"
    "  (3) a direct user quote expressing a preference, rule, or correction\n"
    "  (4) a specific error message with its resolution\n"
    "  (5) a named tool, API, or library with a version-specific quirk\n\n"
    "Reject and DO NOT extract if the only evidence is:\n"
    "- vague references (e.g. 'user likes APIs', 'prefer simple solutions')\n"
    "- inferred preferences without an explicit quote or rule\n"
    "- generic 'X is good/bad' without a concrete anchor\n"
    "- today's transient task context (what they were working on)\n"
    "- greetings, acknowledgements, session meta\n"
    "- reviewer feedback or audit output (lines containing [REVIEWER FEEDBACK] or 'REVIEWER: N concern')\n\n"
    "Output a JSON array. Each element MUST include an `evidence` field quoting "
    "or referencing the transcript anchor:\n"
    "  {\"title\": \"concise_identifier\", \"content\": \"one-to-three sentence rule with the anchor embedded\", "
    "\"category\": \"principle|project|chat\", \"evidence\": \"quote or path or command from transcript\"}\n\n"
    "Categories: 'principle' = explicit rules/corrections. 'project' = environment/tool quirks. "
    "'chat' = preferences explicitly stated.\n"
    "Output [] if nothing meets the high-confidence bar. "
    "Output ONLY the JSON array, no prose around it, no code fences."
)


def run_session_consolidation(session_limit: int = 5, dry_run: bool = False):
    """P63/MOL-268 — deterministic session consolidation, no agent loop.

    Replaces the agent-driven consolidation step that caused the 2026-04-23
    151-tool runaway. Reads N most recent cli/telegram sessions from state.db,
    extracts durable facts via `llm_compose` (single-shot per session;
    post-MOL-602 the composer is deepseek-v4-pro via DeepSeek API — was Kimi K2.6/OpenRouter
    (P169/MOL-560) which had retired the local Ollama qwen3 primary). Dedups via mxbai-embed-large
    cosine similarity (post-P168/MOL-546 — was bge-m3) and the built-in
    content-hash dedup in insert_entry.
    """
    import sqlite3
    import json as _json
    try:
        from plugins.memory.tiered.store import TieredMemoryDB
        from plugins.memory.tiered.llm import llm_compose
        from plugins.memory.tiered.embeddings import generate_embedding
    except Exception as e:
        print(f"[session_consolidation] import failed: {e}")
        return

    state_db_path = str(Path.home() / ".hermes" / "state.db")
    if not Path(state_db_path).exists():
        print("[session_consolidation] state.db not found; skipping")
        return

    db_file = Path(DB_PATH).expanduser()
    if not db_file.exists():
        print("[session_consolidation] memory db not found; skipping")
        return

    t0 = time.time()
    candidates: list[dict] = []
    # Wrap sqlite3.connect + the subsequent transaction in try/except so a
    # locked state.db (gateway holds WAL during concurrent write) doesn't
    # propagate OperationalError up to main() and abort the whole script —
    # that would silently drop the already-printed ingest + supersession
    # output sections from the cron delivery. Fail-safe: print a diagnostic
    # + empty consolidation section, return.
    try:
        conn = sqlite3.connect(state_db_path, timeout=10.0)
    except sqlite3.Error as e:
        print(f"[session_consolidation] sqlite3.connect failed: {e}; skipping")
        return
    conn.row_factory = sqlite3.Row
    try:
        # 1. Most recent user-facing sessions. Cron sessions excluded —
        #    those are outputs, not inputs to consolidate.
        sessions = conn.execute(
            """SELECT id, source, started_at, ended_at
               FROM sessions
               WHERE source IN ('cli', 'telegram')
                 AND ended_at IS NOT NULL
               ORDER BY started_at DESC
               LIMIT ?""",
            (session_limit,),
        ).fetchall()

        if not sessions:
            elapsed = time.time() - t0
            mode = "DRY-RUN" if dry_run else "LIVE"
            print(f"\n## Session Consolidation ({mode}, {elapsed:.1f}s)")
            print("- sessions scanned: 0 (no recent cli/telegram sessions)")
            return

        # 2. Extract durable facts via single-shot llm_compose per session.
        # P169/MOL-560 review fix-pass S2: track composer failure rate so a
        # silent ALL-FAILED batch (e.g. DEEPSEEK_API_KEY drift) is loud in
        # cron output instead of "0 candidates" looking like quiet input.
        sessions_attempted = 0
        sessions_composer_failed = 0
        for sess in sessions:
            rows = conn.execute(
                """SELECT role, content FROM messages
                   WHERE session_id = ? AND role IN ('user','assistant')
                     AND content IS NOT NULL AND content != ''
                   ORDER BY timestamp ASC""",
                (sess["id"],),
            ).fetchall()
            parts: list[str] = []
            for r in rows:
                c = (r["content"] or "").strip()
                if not c:
                    continue
                # Belt+suspenders: skip reviewer-contaminated rows (MOL-271
                # server-side filter ticketed but not yet live as of P63).
                if ("[REVIEWER FEEDBACK" in c
                        or "🔍 REVIEWER:" in c
                        or c.startswith("REVIEWER:")):
                    continue
                parts.append(f"{r['role'].upper()}: {c}")
            transcript = "\n\n".join(parts)[-50_000:]
            if not transcript.strip():
                continue

            sessions_attempted += 1
            raw = llm_compose(EXTRACTION_PROMPT, transcript)
            if not raw:
                sessions_composer_failed += 1
                continue
            raw = raw.strip()
            # Lenient JSON parse: strip code fences if present
            if raw.startswith("```"):
                lines = raw.split("\n")
                if len(lines) > 2:
                    raw = "\n".join(lines[1:-1]).strip()
            try:
                facts = _json.loads(raw)
                if not isinstance(facts, list):
                    continue
            except Exception:
                continue

            for f in facts:
                if not isinstance(f, dict):
                    continue
                title = str(f.get("title", "")).strip()
                content = str(f.get("content", "")).strip()
                category = str(f.get("category", "chat")).strip()
                evidence = str(f.get("evidence", "")).strip()
                if not title or not content:
                    continue
                # High-confidence gate: require an evidence anchor of >=15 chars.
                # Short/empty evidence = likely fabricated candidate; skip it.
                if len(evidence) < 15:
                    continue
                if category not in ("principle", "project", "chat"):
                    category = "chat"
                candidates.append({
                    "title": title,
                    "content": content,
                    "category": category,
                    "evidence": evidence,
                    "source_session": sess["id"],
                })
        # P169/MOL-560 review fix-pass S2: escalate ALL-FAILED batches.
        # Without this, a fully-degraded composer (key drift, expired auth)
        # produces "0 candidates" output that reads as quiet input, not as
        # systemic failure. >50% threshold catches the rare 1-2 transient
        # failures without false alarming.
        # MOL-602 follow-up: composer is now deepseek-v4-pro (was Kimi K2.6/OpenRouter).
        if sessions_attempted > 0:
            fail_rate = sessions_composer_failed / sessions_attempted
            if fail_rate > 0.5:
                print(
                    f"[session_consolidation] WARNING: composer failed on "
                    f"{sessions_composer_failed}/{sessions_attempted} sessions "
                    f"({fail_rate * 100:.0f}%). Check ~/.hermes/state/composer-*.json "
                    f"and gateway.log for DEEPSEEK_API_KEY / deepseek-v4-pro issues."
                )
    except sqlite3.OperationalError as _db_err:
        # Lock contention, corruption, or schema drift — fail fast with a
        # visible diagnostic but DO NOT propagate (upstream ingest +
        # supersession output already printed).
        print(f"[session_consolidation] state.db query failed: {_db_err}; skipping")
        conn.close()
        return
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # 3. Dedup + insert. `insert_entry` has its own content-hash dedup built
    #    in (returns existing id on hash match, line 313-317 of store.py), so
    #    we only need semantic (embedding) dedup here to catch near-duplicates.
    db = TieredMemoryDB(str(db_file))
    try:
        count_before = db._conn.execute(
            "SELECT COUNT(*) FROM memory_entries"
        ).fetchone()[0]

        dispatched = 0
        semantic_dupes = 0
        for cand in candidates:
            emb = generate_embedding(f"{cand['title']} {cand['content']}")
            if emb is not None:
                similar = db.search_vec(emb, limit=1, category=cand["category"])
                if similar and similar[0].get("distance", 1.0) < 0.15:
                    semantic_dupes += 1
                    continue
            if dry_run:
                continue
            db.insert_entry(cand["title"], cand["content"], cand["category"])
            dispatched += 1

        count_after = db._conn.execute(
            "SELECT COUNT(*) FROM memory_entries"
        ).fetchone()[0]
        truly_new = count_after - count_before
        hash_dupes = max(0, dispatched - truly_new)

        elapsed = time.time() - t0
        mode = "DRY-RUN" if dry_run else "LIVE"
        print(f"\n## Session Consolidation ({mode}, {elapsed:.1f}s)")
        print(f"- sessions scanned: {len(sessions)}")
        print(f"- candidates extracted: {len(candidates)}")
        print(f"- semantic duplicates filtered: {semantic_dupes}")
        if dry_run:
            print(f"- would insert: {len(candidates) - semantic_dupes}")
        else:
            print(f"- hash duplicates skipped (insert_entry built-in): {hash_dupes}")
            print(f"- newly inserted: {truly_new}")
    finally:
        db.close()


def main():
    ingest_external()
    run_tombstone_supersession()
    run_session_consolidation()
    return 0


if __name__ == "__main__":
    sys.exit(main())
