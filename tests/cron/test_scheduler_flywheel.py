"""Tests for MOL-227 v2 (P40) flywheel retry in cron/scheduler.py.

Separate from test_scheduler.py so the flywheel's retry-loop + retry-context
plumbing has room to breathe. Tests cover:

- _build_retry_feedback_block renders the three-section prompt correctly.
- _summarize_completed_actions pulls PASS rows by session_id.
- tick() flywheel: attempt 1 HIGH → retry fires with context → attempt 2 clean.
- tick() exhaustion: all retries fail → REVIEW INCOMPLETE banner.
- tick() unavailable: reflection returns "unavailable" → NO retry fired.

Run:
    cd ~/.hermes/hermes-agent
    ./venv/bin/python3 -m pytest tests/cron/test_scheduler_flywheel.py -n 0 -v
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from cron import scheduler as sch
from tools import reflection_agent as ra


# ---------------------------------------------------------------------------
# P69/MOL-277: lock isolation
# ---------------------------------------------------------------------------
# The scheduler's tick() takes an exclusive flock on cron.scheduler._LOCK_FILE
# (default: ~/.hermes/cron/.tick.lock). On a developer box where the live
# gateway is running, that lock is already held, so bare `pytest` against the
# flywheel tests silently no-ops (run_job mocks don't fire → every "expected 2
# calls, got 0" assertion in this file fails) and the suite required a manual
# `HERMES_HOME=/tmp/...` override to pass. P58 (MOL-268) shipped flagging this
# as a follow-up. This autouse fixture redirects _LOCK_DIR / _LOCK_FILE into a
# per-test tmp_path so the flywheel tests run cleanly alongside the production
# gateway with no env override.
#
# The hasattr asserts are a defensive tripwire: if a future scheduler refactor
# moves _LOCK_DIR / _LOCK_FILE off the module namespace (onto a class, into a
# function, etc.), monkeypatch.setattr would silently no-op and this fixture
# would fail open — reintroducing the lock collision without any signal.
# Failing loudly at fixture setup ensures the regression surfaces on the next
# test run.
@pytest.fixture(autouse=True)
def isolated_tick_lock(tmp_path, monkeypatch):
    assert hasattr(sch, "_LOCK_DIR"), (
        "cron.scheduler._LOCK_DIR missing — scheduler may have been refactored. "
        "Update isolated_tick_lock fixture to monkeypatch the new location."
    )
    assert hasattr(sch, "_LOCK_FILE"), (
        "cron.scheduler._LOCK_FILE missing — scheduler may have been refactored. "
        "Update isolated_tick_lock fixture to monkeypatch the new location."
    )
    lock_dir = tmp_path / "cron_lock"
    lock_dir.mkdir()
    monkeypatch.setattr(sch, "_LOCK_DIR", lock_dir)
    monkeypatch.setattr(sch, "_LOCK_FILE", lock_dir / ".tick.lock")
    yield


# ---------------------------------------------------------------------------
# _build_retry_feedback_block — retry prompt rendering
# ---------------------------------------------------------------------------

def test_retry_feedback_block_has_three_sections():
    """The retry-feedback block must include ALREADY COMPLETED, YOUR PREVIOUS
    RESPONSE, and OPEN CONCERNS in that order. Prevents the retry from
    double-writing actions the structural verifier already confirmed."""
    ctx = {
        "attempt_number": 2,
        "completed_actions": "- [jira_comment] MOL-229 — snippet matched",
        "prior_response_truncated": "Previous summary of work on MOL-229.",
        "concerns_to_address": [
            {
                "severity": "high",
                "category": "transition_missing",
                "description": "MOL-229 still To Do despite work completion.",
                "evidence": "jira_transitions empty",
            },
        ],
    }
    out = sch._build_retry_feedback_block(ctx)
    assert "REVIEWER FEEDBACK — ATTEMPT 2" in out
    assert "ALREADY COMPLETED" in out
    assert "MOL-229 — snippet matched" in out
    assert "YOUR PREVIOUS RESPONSE" in out
    assert "Previous summary of work on MOL-229." in out
    assert "OPEN CONCERNS" in out
    assert "[transition_missing]" in out
    assert "ORIGINAL PROMPT FOLLOWS" in out
    # Section order check — ALREADY COMPLETED before OPEN CONCERNS
    assert out.index("ALREADY COMPLETED") < out.index("OPEN CONCERNS")


def test_retry_feedback_block_truncates_prior_response():
    """Prior response is truncated to 2000 chars to prevent prompt bloat when
    the previous attempt was huge (e.g. long code review output).

    MOL-227 v2 review finding #4: tighten the tolerance. Original assertion
    `< 3000` allowed 50% drift before failing; the documented contract is
    2000 chars. Allow only ~200 chars of overhead for the section headers
    and two-space indentation the block adds around the truncated content.
    """
    ctx = {
        "attempt_number": 2,
        "completed_actions": "",
        "prior_response_truncated": "A" * 5000,  # oversized on purpose
        "concerns_to_address": [],
    }
    out = sch._build_retry_feedback_block(ctx)
    prior_section = out.split("YOUR PREVIOUS RESPONSE")[1].split("OPEN CONCERNS")[0]
    # Contract: 2000 chars of content + ~200 chars of block chrome
    # (header line, indentation, section breaks). Fail if we drift past ~2200.
    assert len(prior_section) < 2200, (
        f"prior_section grew to {len(prior_section)} chars; expected < 2200 "
        "(2000 content cap + ~200 chrome). Contract may have silently drifted."
    )
    # Also guard the floor: the block should contain SUBSTANTIALLY all of the
    # 2000 chars, not silently truncate to something much smaller.
    assert len(prior_section) > 1900


def test_retry_feedback_block_empty_concerns_is_safe():
    """Degenerate case: empty concerns list. Block still renders without crash."""
    ctx = {
        "attempt_number": 2,
        "completed_actions": "",
        "prior_response_truncated": "",
        "concerns_to_address": [],
    }
    out = sch._build_retry_feedback_block(ctx)
    assert "REVIEWER FEEDBACK — ATTEMPT 2" in out
    assert "OPEN CONCERNS" in out


# ---------------------------------------------------------------------------
# _build_job_prompt integration — retry_context prepends correctly
# ---------------------------------------------------------------------------

def test_build_job_prompt_prepends_retry_feedback():
    """When retry_context is passed, _build_job_prompt prepends the feedback
    block BEFORE the cron SYSTEM hint + base prompt."""
    job = {"id": "test-x", "prompt": "hello world", "skills": []}
    ctx = {
        "attempt_number": 2,
        "completed_actions": "- [jira_comment] OK",
        "prior_response_truncated": "prev",
        "concerns_to_address": [
            {"severity": "high", "category": "evidence", "description": "x", "evidence": "y"}
        ],
    }
    out_with_ctx = sch._build_job_prompt(job, retry_context=ctx)
    out_plain = sch._build_job_prompt(job, retry_context=None)

    assert "REVIEWER FEEDBACK — ATTEMPT 2" in out_with_ctx
    assert "REVIEWER FEEDBACK" not in out_plain
    # Retry block appears BEFORE the cron system hint in the final prompt
    assert out_with_ctx.index("REVIEWER FEEDBACK") < out_with_ctx.index("SYSTEM: You are running as a scheduled cron")


# ---------------------------------------------------------------------------
# _summarize_completed_actions — queries cron_verifications by session_id
# ---------------------------------------------------------------------------

def test_summarize_completed_actions_returns_pass_rows(tmp_path, monkeypatch):
    """Query returns only verified=1 rows for the given session_id, formatted
    as dash-bulleted lines suitable for the retry-context ALREADY COMPLETED
    section."""
    db = tmp_path / "hermes.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE cron_verifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            run_timestamp TEXT NOT NULL,
            session_id TEXT,
            claim_type TEXT NOT NULL,
            claim_payload_json TEXT NOT NULL,
            verified INTEGER NOT NULL,
            verification_output TEXT,
            created_at TEXT
        )
    """)
    # Two PASS rows for target session
    conn.execute(
        "INSERT INTO cron_verifications (job_id, run_timestamp, session_id, claim_type, "
        "claim_payload_json, verified, verification_output) VALUES (?,?,?,?,?,?,?)",
        ("job1", "2026-04-20T21:00:00", "sess_a1", "jira_comment", "{}", 1,
         "MOL-229 — snippet matched"),
    )
    conn.execute(
        "INSERT INTO cron_verifications (job_id, run_timestamp, session_id, claim_type, "
        "claim_payload_json, verified, verification_output) VALUES (?,?,?,?,?,?,?)",
        ("job1", "2026-04-20T21:00:00", "sess_a1", "file_op", "{}", 1,
         "modified /tmp/x — verified"),
    )
    # One FAIL row — should be excluded
    conn.execute(
        "INSERT INTO cron_verifications (job_id, run_timestamp, session_id, claim_type, "
        "claim_payload_json, verified, verification_output) VALUES (?,?,?,?,?,?,?)",
        ("job1", "2026-04-20T21:00:00", "sess_a1", "jira_transition", "{}", 0,
         "MOL-229 — no matching transition"),
    )
    # One PASS row from a different session — should be excluded
    conn.execute(
        "INSERT INTO cron_verifications (job_id, run_timestamp, session_id, claim_type, "
        "claim_payload_json, verified, verification_output) VALUES (?,?,?,?,?,?,?)",
        ("job1", "2026-04-20T21:00:00", "sess_other", "file_op", "{}", 1,
         "modified /tmp/y — verified"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(sch, "_hermes_home", tmp_path)
    # The helper looks up <home>/memory/hermes.db — set up that structure
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "hermes.db").write_bytes(db.read_bytes())

    summary = sch._summarize_completed_actions("sess_a1")

    assert "MOL-229 — snippet matched" in summary
    assert "modified /tmp/x" in summary
    assert "no matching transition" not in summary  # FAIL excluded
    assert "/tmp/y" not in summary  # other session excluded


def test_summarize_completed_actions_no_db_returns_empty(tmp_path, monkeypatch):
    """DB absent → returns empty string. Retry still proceeds, just without
    the completed-actions context (caller must be robust to empty)."""
    monkeypatch.setattr(sch, "_hermes_home", tmp_path / "doesnotexist")
    summary = sch._summarize_completed_actions("any_session")
    assert summary == ""


# ---------------------------------------------------------------------------
# Flywheel integration — attempt 1 HIGH → retry → attempt 2 clean
# ---------------------------------------------------------------------------

class _FakeConcern:
    def __init__(self, severity, category, description, evidence=""):
        self.severity = severity
        self.category = category
        self.description = description
        self.evidence = evidence


def _make_flywheel_job():
    return {
        "id": "test-flywheel",
        "name": "test flywheel",
        "prompt": "do the thing",
        "enabled": True,
        "schedule": {"kind": "cron", "expr": "0 21 * * *"},
        "claims_expected": True,
    }


def test_flywheel_retries_on_high_concerns_then_passes(tmp_path):
    """Attempt 1 returns HIGH; scheduler retries with retry_context; attempt
    2 returns clean; delivery happens with attempt=2 header."""
    run_calls = []
    reflect_calls = []

    def fake_run_job(job, retry_context=None):
        run_calls.append({"attempt": len(run_calls) + 1, "retry_context": retry_context})
        if len(run_calls) == 1:
            return (True, "# out", "Attempt 1 response", None, [])
        return (True, "# out", "Attempt 2 response", None, [])

    def fake_reflect(job, final_response, cron_session_id, job_start_ts_utc, attempt=1):
        # P65/MOL-271: reflect_and_annotate returns (concerns, status, banner)
        # — test stubs updated from the pre-P65 (annotated, concerns, status)
        # contract when the P69/MOL-277 lock-isolation fixture started letting
        # these tests actually execute.
        reflect_calls.append(attempt)
        if attempt == 1:
            return (
                [_FakeConcern("high", "transition_missing", "ticket still To Do")],
                "ok",
                "🔍 REVIEWER: 1 concerns (1 HIGH)",
            )
        return (
            [],
            "ok",
            "🔍 REVIEWER: 0 concerns",
        )

    captured = {}
    def capture_deliver(job, content, adapters=None, loop=None):
        captured["content"] = content
        return None

    with patch("cron.scheduler.get_due_jobs", return_value=[_make_flywheel_job()]), \
         patch("cron.scheduler.advance_next_run", return_value=True), \
         patch("cron.scheduler.run_job", side_effect=fake_run_job), \
         patch("cron.scheduler.save_job_output", return_value=str(tmp_path / "out.md")), \
         patch("cron.scheduler._deliver_result", side_effect=capture_deliver), \
         patch("cron.scheduler.mark_job_run"), \
         patch("cron.scheduler.load_config", return_value={"cron": {"reflection": {"enabled": True, "max_retries": 1}}}), \
         patch("tools.reflection_agent.reflect_and_annotate", side_effect=fake_reflect), \
         patch("tools.report_verifier.verify_and_annotate", side_effect=lambda **kw: kw["final_response"]):
        from cron.scheduler import tick
        tick(verbose=False)

    assert len(run_calls) == 2, f"Expected 2 run_job calls; got {len(run_calls)}"
    assert run_calls[0]["retry_context"] is None, "First attempt should have no retry_context"
    assert run_calls[1]["retry_context"] is not None, "Second attempt must have retry_context"
    rc = run_calls[1]["retry_context"]
    assert rc["attempt_number"] == 2
    assert len(rc["concerns_to_address"]) == 1
    assert rc["concerns_to_address"][0]["category"] == "transition_missing"
    # Delivered content is the clean (attempt 2) response
    assert "Attempt 2 response" in captured["content"]
    assert "REVIEW INCOMPLETE" not in captured["content"]


def test_flywheel_exhausted_prepends_incomplete_banner(tmp_path):
    """max_retries=1 + both attempts HIGH → REVIEW INCOMPLETE banner
    prepended to the attempt-2 response; delivery still proceeds."""
    def fake_run_job(job, retry_context=None):
        return (True, "# out", "persistent failure", None, [])

    def fake_reflect(job, final_response, cron_session_id, job_start_ts_utc, attempt=1):
        # P65/MOL-271 contract: (concerns, status, banner). Banner returned
        # empty — the scheduler prepends its own REVIEW INCOMPLETE banner
        # after exhaustion, which is what this test asserts on.
        return (
            [_FakeConcern("high", "evidence", "still missing evidence")],
            "ok",
            "",
        )

    captured = {}
    def capture_deliver(job, content, adapters=None, loop=None):
        captured["content"] = content
        return None

    with patch("cron.scheduler.get_due_jobs", return_value=[_make_flywheel_job()]), \
         patch("cron.scheduler.advance_next_run", return_value=True), \
         patch("cron.scheduler.run_job", side_effect=fake_run_job), \
         patch("cron.scheduler.save_job_output", return_value=str(tmp_path / "out.md")), \
         patch("cron.scheduler._deliver_result", side_effect=capture_deliver), \
         patch("cron.scheduler.mark_job_run"), \
         patch("cron.scheduler.load_config", return_value={"cron": {"reflection": {"enabled": True, "max_retries": 1}}}), \
         patch("tools.reflection_agent.reflect_and_annotate", side_effect=fake_reflect), \
         patch("tools.report_verifier.verify_and_annotate", side_effect=lambda **kw: kw["final_response"]):
        from cron.scheduler import tick
        tick(verbose=False)

    assert "REVIEW INCOMPLETE after 2 attempts" in captured["content"]
    assert "persistent failure" in captured["content"]  # original still shipped


def test_flywheel_no_retry_when_review_unavailable(tmp_path):
    """If reflect_and_annotate returns status='unavailable', scheduler must
    NOT retry — reflection didn't actually run, so there's no signal about
    HIGH concerns. Silent-fail here would loop infinitely on every 429."""
    run_count = {"n": 0}

    def fake_run_job(job, retry_context=None):
        run_count["n"] += 1
        return (True, "# out", "some response", None, [])

    def fake_reflect_unavail(job, final_response, cron_session_id, job_start_ts_utc, attempt=1):
        return (
            "⚠️ REVIEWER UNAVAILABLE\n" + final_response,
            [],
            "unavailable",
        )

    with patch("cron.scheduler.get_due_jobs", return_value=[_make_flywheel_job()]), \
         patch("cron.scheduler.advance_next_run", return_value=True), \
         patch("cron.scheduler.run_job", side_effect=fake_run_job), \
         patch("cron.scheduler.save_job_output", return_value=str(tmp_path / "out.md")), \
         patch("cron.scheduler._deliver_result"), \
         patch("cron.scheduler.mark_job_run"), \
         patch("cron.scheduler.load_config", return_value={"cron": {"reflection": {"enabled": True, "max_retries": 1}}}), \
         patch("tools.reflection_agent.reflect_and_annotate", side_effect=fake_reflect_unavail), \
         patch("tools.report_verifier.verify_and_annotate", side_effect=lambda **kw: kw["final_response"]):
        from cron.scheduler import tick
        tick(verbose=False)

    assert run_count["n"] == 1, f"Expected exactly 1 run_job call; got {run_count['n']}"


def test_flywheel_disabled_by_config_skips_retry_entirely(tmp_path):
    """cron.reflection.enabled=false → no reflect_and_annotate call, no retry.
    Single run_job invocation, clean delivery."""
    run_count = {"n": 0}

    def fake_run_job(job, retry_context=None):
        run_count["n"] += 1
        return (True, "# out", "clean output", None, [])

    reflect_called = {"n": 0}
    def fake_reflect(*args, **kw):
        reflect_called["n"] += 1
        raise AssertionError("reflect_and_annotate must not be called when enabled=False")

    with patch("cron.scheduler.get_due_jobs", return_value=[_make_flywheel_job()]), \
         patch("cron.scheduler.advance_next_run", return_value=True), \
         patch("cron.scheduler.run_job", side_effect=fake_run_job), \
         patch("cron.scheduler.save_job_output", return_value=str(tmp_path / "out.md")), \
         patch("cron.scheduler._deliver_result"), \
         patch("cron.scheduler.mark_job_run"), \
         patch("cron.scheduler.load_config", return_value={"cron": {"reflection": {"enabled": False}}}), \
         patch("tools.reflection_agent.reflect_and_annotate", side_effect=fake_reflect), \
         patch("tools.report_verifier.verify_and_annotate", side_effect=lambda **kw: kw["final_response"]):
        from cron.scheduler import tick
        tick(verbose=False)

    assert run_count["n"] == 1
    assert reflect_called["n"] == 0


def test_flywheel_max_retries_hard_capped_at_3(tmp_path):
    """Misconfigured max_retries=99 gets clamped to 3 to prevent runaway cost.
    Count run_job calls: must be <= 4 (3 retries + 1 initial)."""
    run_count = {"n": 0}

    def fake_run_job(job, retry_context=None):
        run_count["n"] += 1
        return (True, "# out", "x", None, [])

    def fake_reflect_always_high(job, final_response, cron_session_id, job_start_ts_utc, attempt=1):
        # P65/MOL-271 contract: (concerns, status, banner)
        return (
            [_FakeConcern("high", "evidence", "persistent")],
            "ok",
            "",
        )

    with patch("cron.scheduler.get_due_jobs", return_value=[_make_flywheel_job()]), \
         patch("cron.scheduler.advance_next_run", return_value=True), \
         patch("cron.scheduler.run_job", side_effect=fake_run_job), \
         patch("cron.scheduler.save_job_output", return_value=str(tmp_path / "out.md")), \
         patch("cron.scheduler._deliver_result"), \
         patch("cron.scheduler.mark_job_run"), \
         patch("cron.scheduler.load_config", return_value={"cron": {"reflection": {"enabled": True, "max_retries": 99}}}), \
         patch("tools.reflection_agent.reflect_and_annotate", side_effect=fake_reflect_always_high), \
         patch("tools.report_verifier.verify_and_annotate", side_effect=lambda **kw: kw["final_response"]):
        from cron.scheduler import tick
        tick(verbose=False)

    assert run_count["n"] == 4, f"Hard cap should allow 1 + 3 retries = 4 runs; got {run_count['n']}"


def test_skip_reflection_job_flag_bypasses_flywheel(tmp_path):
    """P54/MOL-240: per-job `skip_reflection: true` in jobs.json suppresses
    the reflection flywheel for that job only — symmetric with skip_memory.
    reflect_and_annotate must NOT be called, AND max_retries is forced to 0
    so the run_job loop only fires once. Global cron.reflection.enabled
    stays True (this is the key distinction from
    test_flywheel_disabled_by_config_skips_retry_entirely — that test
    disables reflection globally; this one disables it for one job while
    keeping it enabled for others).
    """
    run_count = {"n": 0}

    def fake_run_job(job, retry_context=None):
        run_count["n"] += 1
        return (True, "# out", "clean output", None, [])

    reflect_called = {"n": 0}
    def fake_reflect(*args, **kw):
        reflect_called["n"] += 1
        raise AssertionError("reflect_and_annotate must not be called when skip_reflection=True")

    # Job-level skip_reflection on top of the standard flywheel fixture.
    job = _make_flywheel_job()
    job["skip_reflection"] = True

    with patch("cron.scheduler.get_due_jobs", return_value=[job]), \
         patch("cron.scheduler.advance_next_run", return_value=True), \
         patch("cron.scheduler.run_job", side_effect=fake_run_job), \
         patch("cron.scheduler.save_job_output", return_value=str(tmp_path / "out.md")), \
         patch("cron.scheduler._deliver_result"), \
         patch("cron.scheduler.mark_job_run"), \
         patch("cron.scheduler.load_config", return_value={"cron": {"reflection": {"enabled": True, "max_retries": 3}}}), \
         patch("tools.reflection_agent.reflect_and_annotate", side_effect=fake_reflect), \
         patch("tools.report_verifier.verify_and_annotate", side_effect=lambda **kw: kw["final_response"]):
        from cron.scheduler import tick
        tick(verbose=False)

    assert run_count["n"] == 1, f"skip_reflection must force max_retries=0; got {run_count['n']} run_job calls"
    assert reflect_called["n"] == 0, "reflect_and_annotate must be skipped for jobs with skip_reflection=True"


# ---------------------------------------------------------------------------
# P58/MOL-268: anti-fabrication counter-instruction in retry prompt
# ---------------------------------------------------------------------------

def test_retry_feedback_block_contains_anti_fabrication_clause():
    """P58/MOL-268 — when the flywheel retries with an EVIDENCE concern, the
    retry prompt must offer the agent a delete-the-claim escape hatch, not only
    the "produce the tool output" instruction. Without this, an agent facing an
    evidence-concern for a claim it cannot substantiate (e.g. a personal
    reminder with no real action) fabricates a plausible-looking action list.
    Dentist-cron 73fdb5394faf (2026-04-22) is the reference incident."""
    ctx = {
        "attempt_number": 2,
        "completed_actions": "",
        "prior_response_truncated": "Chief, it's time to call the dentist.",
        "concerns_to_address": [
            {
                "severity": "high",
                "category": "evidence",
                "description": "Output provides zero textual evidence of technical actions.",
                "evidence": "Chief, it's time to call the dentist.",
            },
        ],
    }
    out = sch._build_retry_feedback_block(ctx)
    # Content assertions — not just marker checks. Drift in the prompt text
    # would defeat the fix silently.
    assert "DELETE the unsubstantiated claim" in out
    assert "Never fabricate checkmark" in out
    assert "A short honest response with no action bullets is strictly better" in out
    assert "Fabrication is a severe review failure" in out
    # The original "produce the tool output" branch must remain — the new
    # clause adds a counter-option, it doesn't replace the happy path.
    assert "produce the tool output that proves the action" in out
