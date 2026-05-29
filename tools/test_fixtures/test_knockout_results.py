"""Tests for tools.knockout_results — P274/MOL-2219 deterministic results collector.

The collector reads a finished kanban swarm (the synthesizer leaf + the chain
up to the Jira-linked root) plus the pre-swarm HEAD snapshot from
autonomous-knockout.jsonl, and emits a human-readable results report with a
WORK_MANIFEST of CLAIMS. It performs NO verification itself — the inline cron
verifier (report_verifier.py, Part 1/P272) is the sole checker. These tests lock
two things: (1) the collector↔verifier contract (the manifest it emits parses
cleanly via report_verifier._parse_manifest into the right claim entries), and
(2) its defensive behaviour (omit, never fabricate, a claim it cannot derive).

Run:
    cd ~/.hermes/hermes-agent
    ./venv/bin/python3 -m pytest tools/test_fixtures/test_knockout_results.py -v
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from tools import knockout_results as kr  # noqa: E402
from tools import report_verifier as rv  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures: build a minimal kanban DB with the real swarm chain topology
#   root(MOL-708) -> builder -> verifier -> synthesizer
# and a matching autonomous-knockout.jsonl entry.
# --------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, status TEXT NOT NULL,
    created_at INTEGER NOT NULL, result TEXT, idempotency_key TEXT
);
CREATE TABLE task_links (
    parent_id TEXT NOT NULL, child_id TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);
CREATE TABLE task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
    status TEXT NOT NULL, started_at INTEGER NOT NULL, ended_at INTEGER,
    outcome TEXT, summary TEXT, metadata TEXT, error TEXT
);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
    kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL
);
"""

ROOT = "t_root0001"
BUILDER = "t_bld00002"
VERIFIER = "t_vfy00003"
SYNTH = "t_syn00004"

# The two SHAs from the MOL-708 forensic verdict: local_commit is the on-fork
# landed object (what "landed this run" means); upstream_commit is the fix
# inside the upstream PR. The collector MUST pick local_commit.
LOCAL_SHA = "a12ea7674256b32b843e308909f010e9d0fa5930"
UPSTREAM_SHA = "a61420952000000000000000000000000000aaaa"


def _make_db(tmp_path, *, synth_metadata, synth_outcome="completed",
             synth_status="done", root_idem="MOL-708"):
    db = tmp_path / "kanban.db"
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    rows = [
        (ROOT, "Swarm root", "done", root_idem),
        (BUILDER, "Implement MOL-708", "done", None),
        (VERIFIER, "Verify swarm outputs", "done", None),
        (SYNTH, "Synthesize swarm outputs", synth_status, None),
    ]
    for tid, title, status, idem in rows:
        conn.execute(
            "INSERT INTO tasks(id,title,status,created_at,idempotency_key) "
            "VALUES (?,?,?,?,?)", (tid, title, status, 1000, idem))
    for parent, child in [(ROOT, BUILDER), (BUILDER, VERIFIER), (VERIFIER, SYNTH)]:
        conn.execute("INSERT INTO task_links(parent_id,child_id) VALUES (?,?)",
                     (parent, child))
    # builder + verifier completed; synth carries the structured claim metadata
    conn.execute("INSERT INTO task_runs(task_id,status,started_at,outcome,summary,metadata) "
                 "VALUES (?,?,?,?,?,?)",
                 (BUILDER, "done", 1100, "completed", "Implemented fix", None))
    conn.execute("INSERT INTO task_runs(task_id,status,started_at,outcome,summary,metadata) "
                 "VALUES (?,?,?,?,?,?)",
                 (VERIFIER, "done", 1200, "completed", "gate pass",
                  json.dumps({"gate": "pass", "gaps": []})))
    conn.execute("INSERT INTO task_runs(task_id,status,started_at,outcome,summary,metadata) "
                 "VALUES (?,?,?,?,?,?)",
                 (SYNTH, synth_status, 1300, synth_outcome, "Synthesized + transitioned",
                  json.dumps(synth_metadata) if synth_metadata is not None else None))
    conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,?,?,?)",
                 (SYNTH, synth_outcome, None, 1300))
    conn.commit()
    conn.close()
    return str(db)


def _make_log(tmp_path, *, ticket="MOL-708", head_before=LOCAL_SHA, include=True):
    log = tmp_path / "autonomous-knockout.jsonl"
    entry = {"ts": "2026-05-28T22:00:00-04:00", "ticket_key": ticket,
             "action": "swarm_created", "outcome": "dispatched",
             "swarm_id": ROOT, "ac_source": "original"}
    if include:
        entry["head_before"] = head_before
    log.write_text(json.dumps(entry) + "\n")
    return str(log)


_FULL_META = {
    "ticket": "MOL-708", "jira_status": "Done",
    "upstream_commit": UPSTREAM_SHA, "merge_commit": "2b41f9d8" + "0" * 32,
    "local_commit": LOCAL_SHA, "integration_method": "cherry-pick",
    "tests_result": "212 passed in 2.32s",
    "acceptance_criteria_met": 3, "acceptance_criteria_total": 3,
}


def _parse(report):
    """Extract + parse the WORK_MANIFEST from a collector report, via the
    real verifier — this is the cross-module contract under test."""
    body = rv._extract_manifest(report)
    assert body is not None, "collector emitted no WORK_MANIFEST"
    return rv._parse_manifest(body)


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def test_walk_to_root_resolves_ticket_key(tmp_path):
    db = _make_db(tmp_path, synth_metadata=_FULL_META)
    conn = kr._connect_ro(db)
    chain = kr._walk_to_root(conn, SYNTH)
    assert chain[0] == SYNTH and chain[-1] == ROOT
    assert kr._root_ticket_key(conn, ROOT) == "MOL-708"


def test_manifest_parses_via_report_verifier(tmp_path):
    db = _make_db(tmp_path, synth_metadata=_FULL_META)
    log = _make_log(tmp_path)
    report = kr.collect_results(SYNTH, kanban_db=db, knockout_log=log,
                                repo="~/.hermes/hermes-agent")
    manifest = _parse(report)
    assert len(manifest["git_commits"]) == 1
    gc = manifest["git_commits"][0]
    assert gc["sha"] == LOCAL_SHA
    assert gc["head_before"] == LOCAL_SHA  # from the jsonl snapshot
    assert manifest["jira_status"] == [{"issue": "MOL-708", "status": "Done"}]


def test_sha_is_local_commit_not_upstream(tmp_path):
    # Encodes the forensic lesson: never verify the upstream/merge SHA.
    db = _make_db(tmp_path, synth_metadata=_FULL_META)
    log = _make_log(tmp_path)
    report = kr.collect_results(SYNTH, kanban_db=db, knockout_log=log)
    manifest = _parse(report)
    sha = manifest["git_commits"][0]["sha"]
    assert sha == LOCAL_SHA
    assert sha != UPSTREAM_SHA


def test_missing_sha_omits_git_commits_claim(tmp_path):
    meta = {k: v for k, v in _FULL_META.items()
            if k not in ("local_commit", "commit", "sha")}
    db = _make_db(tmp_path, synth_metadata=meta)
    log = _make_log(tmp_path)
    report = kr.collect_results(SYNTH, kanban_db=db, knockout_log=log)
    manifest = _parse(report)
    assert manifest["git_commits"] == []          # omitted, never fabricated
    assert manifest["jira_status"]                 # jira_status still emitted


def test_head_before_absent_omits_field(tmp_path):
    db = _make_db(tmp_path, synth_metadata=_FULL_META)
    log = _make_log(tmp_path, include=False)       # jsonl predates Part 1.5
    report = kr.collect_results(SYNTH, kanban_db=db, knockout_log=log)
    gc = _parse(report)["git_commits"][0]
    assert gc["sha"] == LOCAL_SHA
    assert "head_before" not in gc                 # → verifier present-only PASS


@pytest.mark.parametrize("nullish", ["null", "", None])
def test_head_before_nullish_normalised_out(tmp_path, nullish):
    db = _make_db(tmp_path, synth_metadata=_FULL_META)
    log = _make_log(tmp_path, head_before=nullish)
    report = kr.collect_results(SYNTH, kanban_db=db, knockout_log=log)
    gc = _parse(report)["git_commits"][0]
    assert "head_before" not in gc


def test_noncompleted_outcome_reported_no_false_done(tmp_path):
    # Blocked swarm: synth never claimed Done. No jira_status=Done fabricated.
    meta = {"ticket": "MOL-708", "tests_result": "blocked"}
    db = _make_db(tmp_path, synth_metadata=meta,
                  synth_outcome="blocked", synth_status="blocked")
    log = _make_log(tmp_path)
    report = kr.collect_results(SYNTH, kanban_db=db, knockout_log=log)
    manifest = _parse(report)
    assert manifest["git_commits"] == []
    assert manifest["jira_status"] == []
    assert "blocked" in report.lower()


def test_claims_only_no_verification(tmp_path):
    # A garbage SHA must still appear verbatim in the manifest — proving the
    # collector does NOT resolve git/Jira (that's the verifier's job).
    meta = {"ticket": "MOL-708", "jira_status": "Done",
            "local_commit": "deadbeef" * 5}
    db = _make_db(tmp_path, synth_metadata=meta)
    log = _make_log(tmp_path, head_before="cafef00d" * 5)
    report = kr.collect_results(SYNTH, kanban_db=db, knockout_log=log)
    gc = _parse(report)["git_commits"][0]
    assert gc["sha"] == "deadbeef" * 5


def test_root_without_idempotency_key_omits_jira(tmp_path):
    db = _make_db(tmp_path, synth_metadata=_FULL_META, root_idem=None)
    log = _make_log(tmp_path)
    report = kr.collect_results(SYNTH, kanban_db=db, knockout_log=log)
    manifest = _parse(report)
    assert manifest["jira_status"] == []           # no ticket → no jira claim


# --------------------------------------------------------------------------
# Watcher-support helpers (P273) — leaf detection, terminal-event gate,
# pending-swarm discovery, and the once-only delivered marker.
# --------------------------------------------------------------------------

# The log fixture stamps swarm_created at this instant; tests derive now_ts
# relative to it so the max-age gate is exercised deterministically.
_LOG_BASE_TS = kr._parse_iso_ts("2026-05-28T22:00:00-04:00")


def _add_event(db, task_id, kind, created_at):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) "
                 "VALUES (?,?,?,?)", (task_id, kind, None, created_at))
    conn.commit()
    conn.close()


def test_swarm_leaf_finds_synthesizer(tmp_path):
    db = _make_db(tmp_path, synth_metadata=_FULL_META)
    conn = kr._connect_ro(db)
    assert kr._swarm_leaf(conn, ROOT) == SYNTH   # title contains "synthe"


def test_swarm_leaf_blocked_pre_synth_returns_deepest(tmp_path):
    # A swarm blocked at the builder never created a synth task. The deepest
    # leaf (verifier here) is the furthest progress — report it, don't crash.
    db = tmp_path / "k.db"
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    for tid, title in [(ROOT, "Swarm root"), (BUILDER, "Implement"),
                       (VERIFIER, "Verify outputs")]:
        conn.execute("INSERT INTO tasks(id,title,status,created_at) "
                     "VALUES (?,?,?,?)", (tid, title, "done", 1000))
    for p, c in [(ROOT, BUILDER), (BUILDER, VERIFIER)]:
        conn.execute("INSERT INTO task_links(parent_id,child_id) VALUES (?,?)", (p, c))
    conn.commit(); conn.close()
    ro = kr._connect_ro(str(db))
    assert kr._swarm_leaf(ro, ROOT) == VERIFIER


def test_latest_terminal_event_gate(tmp_path):
    db = _make_db(tmp_path, synth_metadata=_FULL_META)  # synth has "completed"
    conn = kr._connect_ro(db)
    assert kr._latest_terminal_event(conn, SYNTH) == "completed"
    assert kr._latest_terminal_event(conn, ROOT) is None   # no events on root


def test_latest_terminal_event_nonterminal_is_none(tmp_path):
    db = _make_db(tmp_path, synth_metadata=_FULL_META,
                  synth_outcome="started", synth_status="in_progress")
    conn = kr._connect_ro(db)
    assert kr._latest_terminal_event(conn, SYNTH) is None   # still running


def test_latest_event_wins_over_earlier(tmp_path):
    # reclaim path: a blocked event then a later completed event → terminal.
    db = _make_db(tmp_path, synth_metadata=_FULL_META,
                  synth_outcome="blocked", synth_status="done")
    _add_event(db, SYNTH, "completed", 1400)   # later than the 1300 blocked
    conn = kr._connect_ro(db)
    assert kr._latest_terminal_event(conn, SYNTH) == "completed"


def test_find_pending_returns_terminal_swarm(tmp_path):
    db = _make_db(tmp_path, synth_metadata=_FULL_META)
    log = _make_log(tmp_path)
    pending = kr.find_pending_knockout_results(
        kanban_db=db, knockout_log=log, now_ts=_LOG_BASE_TS + 600)
    assert len(pending) == 1
    assert pending[0] == {"synth_id": SYNTH, "root_id": ROOT,
                          "ticket_key": "MOL-708", "terminal_kind": "completed"}


def test_find_pending_excludes_still_running(tmp_path):
    db = _make_db(tmp_path, synth_metadata=_FULL_META,
                  synth_outcome="started", synth_status="in_progress")
    log = _make_log(tmp_path)
    assert kr.find_pending_knockout_results(
        kanban_db=db, knockout_log=log, now_ts=_LOG_BASE_TS + 600) == []


def test_find_pending_excludes_too_old(tmp_path):
    db = _make_db(tmp_path, synth_metadata=_FULL_META)
    log = _make_log(tmp_path)
    # 19h after dispatch, default gate is 18h → ancient swarm is not backfilled
    assert kr.find_pending_knockout_results(
        kanban_db=db, knockout_log=log, now_ts=_LOG_BASE_TS + 19 * 3600) == []


def test_mark_delivered_dedups(tmp_path):
    db = _make_db(tmp_path, synth_metadata=_FULL_META)
    log = _make_log(tmp_path)
    now = _LOG_BASE_TS + 600
    assert len(kr.find_pending_knockout_results(
        kanban_db=db, knockout_log=log, now_ts=now)) == 1
    kr.mark_results_delivered(log, swarm_id=ROOT, synth_id=SYNTH,
                              now_iso="2026-05-28T22:08:00-04:00")
    # second pass: the marker suppresses re-delivery
    assert kr.find_pending_knockout_results(
        kanban_db=db, knockout_log=log, now_ts=now) == []


def test_find_pending_no_log_is_empty(tmp_path):
    db = _make_db(tmp_path, synth_metadata=_FULL_META)
    assert kr.find_pending_knockout_results(
        kanban_db=db, knockout_log=str(tmp_path / "nope.jsonl"),
        now_ts=_LOG_BASE_TS + 600) == []


# --------------------------------------------------------------------------
# Integration: collector → verifier banner (the cross-module seam end-to-end).
# Unit tests prove each half (collector emits the SHA verbatim; the verifier
# rejects a bogus SHA). These lock that the EXACT manifest string the collector
# emits flows through verify_and_annotate and flips the ✅/⚠️ banner — i.e. the
# `<!-- WORK_MANIFEST <freeform>` opener the verifier regex tolerates, and the
# head_before baseline plumbing, actually compose. Git-only (no live Jira).
# --------------------------------------------------------------------------

import subprocess  # noqa: E402


def _make_git_repo(tmp_path):
    """Hermetic two-commit repo → (repo_path, head_before, new_sha).
    Mirrors the report_verifier git_repo fixture; local so this file is
    self-contained (the fixture lives in test_report_verifier.py)."""
    repo = tmp_path / "gitrepo"
    repo.mkdir()

    def _git(*args):
        return subprocess.run(["git", "-C", str(repo), *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    _git("init", "-q")
    _git("config", "user.email", "t@example.com")
    _git("config", "user.name", "Tester")
    (repo / "a.txt").write_text("1\n")
    _git("add", "a.txt"); _git("commit", "-q", "-m", "first commit")
    head_before = _git("rev-parse", "HEAD")
    (repo / "b.txt").write_text("2\n")
    _git("add", "b.txt"); _git("commit", "-q", "-m", "fix(agent): cherry-pick landed")
    new_sha = _git("rev-parse", "HEAD")
    return repo, head_before, new_sha


def test_integration_landed_sha_passes_banner(tmp_path, monkeypatch):
    monkeypatch.setattr(rv, "_persist_results", lambda *a, **k: None)
    repo, head_before, new_sha = _make_git_repo(tmp_path)
    # Synth claims the genuinely-new on-fork SHA; jsonl snapshot is pre-swarm HEAD.
    db = _make_db(tmp_path, synth_metadata={"ticket": "MOL-708", "local_commit": new_sha})
    log = _make_log(tmp_path, head_before=head_before)
    report = kr.collect_results(SYNTH, kanban_db=db, knockout_log=log, repo=str(repo))
    annotated = rv.verify_and_annotate(
        {"id": "knockout-results-test", "claims_expected": True}, report, "sess", 0.0)
    assert "✅ VERIFICATION: 1/1 claims passed" in annotated
    assert "landed this run" in annotated


def test_integration_bogus_sha_fails_banner(tmp_path, monkeypatch):
    # The whole point of the redesign: a no-op/garbage cherry-pick must NOT get
    # a green banner. Collector passes the bogus SHA through; verifier flips ⚠️.
    monkeypatch.setattr(rv, "_persist_results", lambda *a, **k: None)
    repo, head_before, _new_sha = _make_git_repo(tmp_path)
    db = _make_db(tmp_path, synth_metadata={"ticket": "MOL-708",
                                            "local_commit": "deadbeef" * 5})
    log = _make_log(tmp_path, head_before=head_before)
    report = kr.collect_results(SYNTH, kanban_db=db, knockout_log=log, repo=str(repo))
    annotated = rv.verify_and_annotate(
        {"id": "knockout-results-test", "claims_expected": True}, report, "sess", 0.0)
    assert "⚠️ VERIFICATION: 1/1 claims failed" in annotated
    assert "unknown commit" in annotated
