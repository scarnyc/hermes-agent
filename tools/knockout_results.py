"""Deterministic results collector for the autonomous-knockout swarm (P274/MOL-2219).

Part 2 of the knockout results-reporting redesign. The 22:00 cron emits a
DISPATCH RECEIPT (what the swarm was sent to do). This collector runs later —
fired by the gateway swarm-results watcher on the synthesizer's terminal
task_events row — and emits a RESULTS report describing what actually happened,
cross-checked against ground truth.

Crucially this module is a TRANSCRIBER, not a checker. It reads the finished
swarm (the synthesizer leaf + the chain up to the Jira-linked root) and the
pre-swarm HEAD snapshot from autonomous-knockout.jsonl, then emits a
human-readable report carrying a WORK_MANIFEST of CLAIMS. The inline cron
verifier (tools.report_verifier, Part 1/P272) is the SOLE checker — it runs the
manifest against git + live Jira and prepends the ✅/❌ banner. Keeping
verification out of here closes skeptic #4 (no grading-your-own-homework) and
means a no-op swarm cannot manufacture a green result: the collector transcribes
whatever the synthesizer claimed, and the verifier fails it against reality.

Defensive-parse contract: the synthesizer's completion metadata is LLM-authored
free-form convention (kanban-synthesizer SKILL.md mandates only a prose recap;
the structured keys are not code-enforced). So every claim is OMITTED rather
than fabricated when its source field is missing or non-derivable — never a
false ✅ and never a false ❌.

Read-only: opens the kanban DB in SQLite ro mode and never touches git/Jira.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Optional

import yaml

# Default runtime locations. Overridable for tests / manual replay.
DEFAULT_KANBAN_DB = "~/.hermes/kanban.db"
DEFAULT_KNOCKOUT_LOG = "~/.hermes/logs/autonomous-knockout.jsonl"
DEFAULT_REPO = "~/.hermes/hermes-agent"

# Metadata keys that may carry the ON-FORK cherry-pick SHA, in priority order.
# Deliberately EXCLUDES upstream_commit / merge_commit: those are different
# objects (the fix inside the upstream PR, and the merge node) — verifying them
# against our fork's HEAD would false-FAIL. Forensic lesson from MOL-708:
# local_commit a12ea7674 (on-fork, the SHA to verify) vs upstream_commit
# a61420952 (inside upstream PR #28914).
_SHA_KEYS = ("local_commit", "commit", "sha")

_MAX_CHAIN_DEPTH = 64  # cycle / runaway guard on the link walk


def _connect_ro(db_path: str) -> sqlite3.Connection:
    """Open the kanban DB strictly read-only. expanduser first so ~ resolves."""
    resolved = os.path.expanduser(db_path)
    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _walk_to_root(conn: sqlite3.Connection, leaf_id: str) -> list[str]:
    """Walk task_links child→parent from the synthesizer leaf to the swarm root.

    The swarm is a single-parent chain (root→builder→verifier→synthesizer), so
    each child has at most one parent. Returns [leaf, ..., root]. Depth-guarded
    against a malformed cyclic link table.
    """
    chain = [leaf_id]
    seen = {leaf_id}
    current = leaf_id
    for _ in range(_MAX_CHAIN_DEPTH):
        row = conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ? LIMIT 1",
            (current,),
        ).fetchone()
        if row is None:
            break
        parent = row["parent_id"]
        if parent in seen:  # defensive: cycle
            break
        chain.append(parent)
        seen.add(parent)
        current = parent
    return chain


def _root_ticket_key(conn: sqlite3.Connection, root_id: str) -> Optional[str]:
    """The root task's idempotency_key IS the Jira ticket key (only the root
    carries it). Returns None when absent (e.g. an ad-hoc, non-ticketed swarm)."""
    row = conn.execute(
        "SELECT idempotency_key FROM tasks WHERE id = ? LIMIT 1", (root_id,)
    ).fetchone()
    if row is None:
        return None
    key = row["idempotency_key"]
    return key.strip() if isinstance(key, str) and key.strip() else None


def _latest_run(conn: sqlite3.Connection, task_id: str) -> Optional[sqlite3.Row]:
    """Most recent task_run for a task (outcome / summary / metadata)."""
    return conn.execute(
        "SELECT status, outcome, summary, metadata FROM task_runs "
        "WHERE task_id = ? ORDER BY started_at DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()


def _task_title(conn: sqlite3.Connection, task_id: str) -> str:
    row = conn.execute(
        "SELECT title FROM tasks WHERE id = ? LIMIT 1", (task_id,)
    ).fetchone()
    return (row["title"] if row else "") or ""


def _parse_metadata(raw: object) -> dict:
    """task_runs.metadata is a JSON string (or NULL). Parse defensively —
    anything that isn't a JSON object becomes {}."""
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extract_sha(metadata: dict) -> Optional[str]:
    """Pull the on-fork cherry-pick SHA from metadata by priority. Returns None
    if no recognised key holds a non-empty string — caller omits the claim."""
    for key in _SHA_KEYS:
        val = metadata.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _lookup_head_before(log_path: str, ticket_key: Optional[str]) -> Optional[str]:
    """Find the pre-swarm HEAD baseline that the 22:00 run snapshotted into
    autonomous-knockout.jsonl (Part 1.5). Joins on ticket_key — both sides carry
    it, and it survives the next-morning / date-rollover trap that
    last-jsonl-entry guessing does not. Most-recent matching line wins.

    Normalises null-ish snapshots (absent / JSON null / "" / the string "null")
    to None so the verifier degrades to a present-only PASS instead of choking
    on a bogus baseline.
    """
    if not ticket_key:
        return None
    resolved = os.path.expanduser(log_path)
    if not os.path.exists(resolved):
        return None
    found: Optional[str] = None
    with open(resolved, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("ticket_key") != ticket_key:
                continue
            hb = entry.get("head_before")
            if isinstance(hb, str) and hb.strip() and hb.strip().lower() != "null":
                found = hb.strip()  # later lines override → most-recent wins
            else:
                found = None  # explicit null-ish snapshot for this run
    return found


def _coerce_tests(metadata: dict) -> Optional[str]:
    """Worker-claimed test result, surfaced in prose ONLY (never a verifier
    claim — it isn't cheaply cross-checkable without re-running)."""
    val = metadata.get("tests_result") or metadata.get("tests")
    return str(val).strip() if val not in (None, "") else None


def _coerce_ac(metadata: dict) -> Optional[str]:
    met = metadata.get("acceptance_criteria_met")
    total = metadata.get("acceptance_criteria_total")
    if met is None and total is None:
        return None
    return f"{met if met is not None else '?'}/{total if total is not None else '?'}"


# Event kinds that mark a task as settled (the swarm-done signal the gateway
# notifier already watches). A leaf carrying one of these is finished — its
# upstream chain (builder/verifier) necessarily completed first, because the
# synthesizer is dependency-gated on them.
TERMINAL_EVENT_KINDS = ("completed", "blocked", "gave_up", "crashed", "timed_out")

# A synthesizer leaf's title contains one of these (case-insensitive). Used to
# pick the synth among a swarm's leaves; falls back to the sole leaf when no
# title matches (covers a swarm blocked before the synth task was created).
_SYNTH_TITLE_HINTS = ("synthe", "synthesiz")


def _child_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    return [
        r["child_id"]
        for r in conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ?", (task_id,)
        ).fetchall()
    ]


def _swarm_leaf(conn: sqlite3.Connection, root_id: str) -> Optional[str]:
    """Find the synthesizer leaf by walking DOWN from a swarm root.

    A leaf is a descendant with no children. The swarm's synthesizer is the
    final leaf. Prefers a leaf whose title names synthesis; otherwise, when a
    single leaf exists, returns it (a swarm blocked before the synth task was
    created has the builder/verifier as its deepest leaf — we still report it
    honestly). Returns None only for a degenerate/empty swarm.
    """
    leaves: list[str] = []
    seen = {root_id}
    frontier = [root_id]
    for _ in range(_MAX_CHAIN_DEPTH):
        if not frontier:
            break
        nxt: list[str] = []
        for node in frontier:
            children = _child_ids(conn, node)
            if not children:
                leaves.append(node)
                continue
            for c in children:
                if c not in seen:
                    seen.add(c)
                    nxt.append(c)
        frontier = nxt
    if not leaves:
        return None
    for leaf in leaves:
        title = _task_title(conn, leaf).lower()
        if any(hint in title for hint in _SYNTH_TITLE_HINTS):
            return leaf
    # No synth-titled leaf (e.g. swarm blocked pre-synth): the deepest/last leaf
    # discovered is the furthest the swarm progressed — report it honestly.
    return leaves[-1]


def _latest_terminal_event(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Return the kind of the task's most-recent event IF it is terminal, else
    None (the swarm is still running and the watcher should re-check later)."""
    row = conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    kind = row["kind"]
    return kind if kind in TERMINAL_EVENT_KINDS else None


# P275/MOL-2219 Finding A: statuses that mean a task will still advance on a
# future dispatcher tick. "in_progress" is accepted alongside the canonical
# "running" defensively (older runs / test fixtures use it).
_LIVE_STATUSES = ("ready", "running", "in_progress", "triage")


def _task_status(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT status FROM tasks WHERE id = ? LIMIT 1", (task_id,)
    ).fetchone()
    return row["status"] if row else None


def _parent_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    return [
        r["parent_id"]
        for r in conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?", (task_id,)
        ).fetchall()
    ]


def _all_parents_done(conn: sqlite3.Connection, task_id: str) -> bool:
    """True when every gating parent is ``done`` (so recompute_ready will promote
    this task next tick). A parentless task is trivially unblocked."""
    parents = _parent_ids(conn, task_id)
    if not parents:
        return True
    return all(_task_status(conn, p) == "done" for p in parents)


def _has_gave_up_event(conn: sqlite3.Connection, task_id: str) -> bool:
    """True if the task ever emitted a ``gave_up`` event — the failure circuit
    breaker tripped at its limit (status → blocked + run closed gave_up). This is
    DEFINITIVE: unlike a bare ``blocked`` dependency-block (reclaimable) or a
    ``crashed``/``timed_out`` that bounces back to ``ready`` for retry, a gave_up
    task will not auto-recover."""
    row = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'gave_up' LIMIT 1",
        (task_id,),
    ).fetchone()
    return row is not None


def _swarm_can_progress(conn: sqlite3.Connection, chain: list[str]) -> bool:
    """Can the swarm still make forward progress toward its synthesizer?

    P275/MOL-2219 Finding A. ``recompute_ready`` only promotes a todo task once
    ALL its parents are ``done``; a ``gave_up`` upstream worker never becomes
    ``done``, so the synthesizer leaf is stranded in ``todo`` and never emits a
    terminal event — the swarm-results watcher would wait for it forever (silent
    no-op). This predicate detects that terminal-rest state.

    A swarm is LIVE iff some chain task will advance on a future tick:
      - status in {ready, running, triage} → actively dispatchable
      - blocked WITHOUT a gave_up event → a dependency block that may be reclaimed
      - todo whose parents are ALL done → recompute_ready promotes it next tick
    Otherwise (every non-done task is gave_up-blocked or todo-stranded behind a
    non-done parent) the swarm is at terminal rest → returns False.
    """
    for tid in chain:
        status = _task_status(conn, tid)
        if status in _LIVE_STATUSES:
            return True
        if status == "blocked" and not _has_gave_up_event(conn, tid):
            return True
        if status == "todo" and _all_parents_done(conn, tid):
            return True
    return False


def _load_pending_knockout_swarms(
    log_path: str, *, max_age_hours: float, now_ts: float
) -> list[dict]:
    """Parse autonomous-knockout.jsonl into the swarms still awaiting a results
    report: those with a ``swarm_created`` line, recent enough, and NOT yet
    carrying a matching ``results_delivered`` marker.

    Both the gate (is this a knockout swarm?) and the dedup (already reported?)
    read this single file — no separate cursor state. ``max_age_hours`` keeps a
    lost marker file from re-firing reports for ancient swarms.
    """
    resolved = os.path.expanduser(log_path)
    if not os.path.exists(resolved):
        return []
    created: dict[str, dict] = {}   # swarm_id -> {ticket_key, ts}
    delivered: set[str] = set()
    with open(resolved, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            swarm_id = entry.get("swarm_id")
            if not isinstance(swarm_id, str) or not swarm_id:
                continue
            action = entry.get("action")
            if action == "swarm_created":
                ts = _parse_iso_ts(entry.get("ts"))
                created[swarm_id] = {
                    "ticket_key": entry.get("ticket_key"),
                    "ts": ts,
                }
            elif action == "results_delivered":
                delivered.add(swarm_id)
    pending = []
    cutoff = now_ts - max_age_hours * 3600.0
    for swarm_id, info in created.items():
        if swarm_id in delivered:
            continue
        ts = info["ts"]
        if ts is not None and ts < cutoff:
            continue  # too old — don't backfill ancient swarms
        pending.append({"swarm_id": swarm_id, "ticket_key": info["ticket_key"]})
    return pending


def _parse_iso_ts(ts: object) -> Optional[float]:
    """Best-effort ISO-8601 → epoch seconds; None when unparseable."""
    if not isinstance(ts, str) or not ts.strip():
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts.strip()).timestamp()
    except (ValueError, TypeError):
        return None


def find_pending_knockout_results(
    *,
    kanban_db: str = DEFAULT_KANBAN_DB,
    knockout_log: str = DEFAULT_KNOCKOUT_LOG,
    max_age_hours: float = 18.0,
    now_ts: float,
) -> list[dict]:
    """The watcher's per-tick query: knockout swarms that have reached a
    terminal synthesizer event and have not yet had a results report delivered.

    Returns a list of ``{synth_id, root_id, ticket_key, terminal_kind}`` —
    empty on a quiet night or while swarms are still running. Read-only.
    """
    pending = _load_pending_knockout_swarms(
        knockout_log, max_age_hours=max_age_hours, now_ts=now_ts
    )
    if not pending:
        return []
    out: list[dict] = []
    conn = _connect_ro(kanban_db)
    try:
        for swarm in pending:
            root_id = swarm["swarm_id"]
            leaf = _swarm_leaf(conn, root_id)
            if leaf is None:
                continue
            kind = _latest_terminal_event(conn, leaf)
            if kind is None:
                # P275/MOL-2219 Finding A: the synth leaf has no terminal event.
                # Either the swarm is genuinely still running, OR an upstream
                # worker gave_up and stranded the leaf in todo forever (a gave_up
                # parent never becomes done, so recompute_ready never promotes
                # the synth). Distinguish by forward-progress: if the swarm can
                # no longer advance, it is at terminal REST by upstream stranding
                # — fire honestly with a synthetic 'stranded' kind (collect_results
                # then reports the dead frontier: empty manifest, no false ✅).
                chain = _walk_to_root(conn, leaf)
                if _swarm_can_progress(conn, chain):
                    continue  # genuinely still running — re-check next tick
                kind = "stranded"
            out.append({
                "synth_id": leaf,
                "root_id": root_id,
                "ticket_key": swarm["ticket_key"],
                "terminal_kind": kind,
            })
    finally:
        conn.close()
    return out


def mark_results_delivered(
    log_path: str, *, swarm_id: str, synth_id: str, now_iso: str
) -> None:
    """Append the dedup marker so this swarm is never re-reported. Same file the
    gate reads. Best-effort append (a short single-line write is atomic on a
    POSIX append-mode fd, so it won't tear the cron's swarm_created lines)."""
    resolved = os.path.expanduser(log_path)
    rec = {
        "ts": now_iso,
        "action": "results_delivered",
        "swarm_id": swarm_id,
        "synth_id": synth_id,
    }
    with open(resolved, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def collect_results(
    synth_task_id: str,
    *,
    kanban_db: str = DEFAULT_KANBAN_DB,
    knockout_log: str = DEFAULT_KNOCKOUT_LOG,
    repo: str = DEFAULT_REPO,
) -> str:
    """Build the knockout RESULTS report (prose + WORK_MANIFEST of claims).

    Pure read: walks the swarm chain from the synthesizer to the ticketed root,
    transcribes the synthesizer's claimed results, looks up the pre-swarm HEAD
    baseline, and emits a manifest the cron verifier will independently check.
    Emits CLAIMS only — never self-verifies.
    """
    conn = _connect_ro(kanban_db)
    try:
        chain = _walk_to_root(conn, synth_task_id)
        root_id = chain[-1]
        ticket_key = _root_ticket_key(conn, root_id)

        synth_run = _latest_run(conn, synth_task_id)
        synth_outcome = (synth_run["outcome"] if synth_run else None) or "unknown"
        metadata = _parse_metadata(synth_run["metadata"] if synth_run else None)

        sha = _extract_sha(metadata)
        head_before = _lookup_head_before(knockout_log, ticket_key)
        claimed_status = metadata.get("jira_status")
        claimed_status = (
            str(claimed_status).strip()
            if isinstance(claimed_status, str) and claimed_status.strip()
            else None
        )
        tests = _coerce_tests(metadata)
        ac = _coerce_ac(metadata)

        # Per-worker outcomes (root → ... → synth), shown leaf-last for readability.
        worker_lines = []
        for tid in reversed(chain):  # root first
            run = _latest_run(conn, tid)
            outcome = (run["outcome"] if run else None) or "(no run)"
            worker_lines.append(f"- `{tid}` {_task_title(conn, tid)} — {outcome}")
    finally:
        conn.close()

    # ---- Assemble the WORK_MANIFEST (claims only) ----
    manifest: dict = {}
    if sha:
        commit_entry = {"repo": repo, "sha": sha}
        if head_before:
            commit_entry["head_before"] = head_before
        manifest["git_commits"] = [commit_entry]
    if ticket_key and claimed_status:
        manifest["jira_status"] = [{"issue": ticket_key, "status": claimed_status}]

    manifest_yaml = yaml.safe_dump(manifest, sort_keys=False).strip() if manifest else "{}"

    # ---- Assemble the human report ----
    title_ticket = ticket_key or "(no ticket)"
    lines = [
        f"# Knockout Results — {title_ticket}",
        "",
        f"**Swarm:** `{root_id}` → `{synth_task_id}` ({len(chain)} tasks)",
        f"**Synthesizer outcome:** {synth_outcome}",
        "",
        "## Per-worker outcomes",
        *worker_lines,
        "",
        "## Claimed results (independently verified in the banner above)",
    ]
    if sha:
        landed = f" (baseline `{head_before}`)" if head_before else " (no baseline snapshot)"
        lines.append(f"- Cherry-pick: `{sha}` on `{repo}`{landed}")
    else:
        lines.append("- Cherry-pick: none claimed")
    if ticket_key and claimed_status:
        lines.append(f"- Jira status: {ticket_key} → {claimed_status}")
    else:
        lines.append("- Jira status: none claimed")
    if tests:
        lines.append(f"- Tests: {tests}  _(worker-claimed, not re-run)_")
    if ac:
        lines.append(f"- Acceptance criteria: {ac}  _(worker-claimed)_")
    if synth_outcome not in ("completed",):
        lines.append("")
        lines.append(
            f"⚠️ Synthesizer outcome was **{synth_outcome}**, not `completed` — "
            "claims below may be partial; trust the verifier banner."
        )

    lines += [
        "",
        f"<!-- WORK_MANIFEST P274/MOL-2219 knockout-results synth={synth_task_id}",
        manifest_yaml,
        "WORK_MANIFEST -->",
    ]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    """CLI for manual replay: `python -m tools.knockout_results <synth_task_id>`."""
    import argparse

    parser = argparse.ArgumentParser(description="Knockout swarm results collector (claims only).")
    parser.add_argument("synth_task_id", help="The synthesizer task id (swarm leaf).")
    parser.add_argument("--kanban-db", default=DEFAULT_KANBAN_DB)
    parser.add_argument("--knockout-log", default=DEFAULT_KNOCKOUT_LOG)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    args = parser.parse_args(argv)
    print(collect_results(
        args.synth_task_id,
        kanban_db=args.kanban_db,
        knockout_log=args.knockout_log,
        repo=args.repo,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
