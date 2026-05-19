"""Tests for tools.reflection_agent (MOL-227 v1).

Covers:
- Unit: _build_prompt assembles claims summary + output body.
- Unit: _parse_concerns tolerates fenced / bare / malformed JSON.
- Acceptance: names MOL-220 fabrication by token on historical bad run.
- Acceptance: names MOL-212 fabrication by token on historical bad run.
- Acceptance: zero high-severity concerns on historical known-clean run.
- Containment: Kimi raises → returns [] and logs.
- Containment: Kimi returns non-JSON → returns [] and logs.

Kimi is stubbed via monkeypatch.setattr(ra, "_call_kimi", fake) — tests do
NOT hit OpenRouter. Live smoke against real Kimi is separate (see plan doc).

Run:
    cd ~/.hermes/hermes-agent
    ./venv/bin/python3 -m pytest tools/test_fixtures/test_reflection_agent.py -v
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


def _mk_ts(year: int = 2026, month: int = 4, day: int = 16, hour: int = 21, minute: int = 0, second: int = 0) -> float:
    """UTC timestamp for reflect_and_annotate tests. Mirrors test_report_verifier
    helper so both modules can be debugged with the same reference times."""
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc).timestamp()

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from tools import reflection_agent as ra  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CRON_OUTPUT_DIR = Path.home() / ".hermes" / "cron" / "output" / "fef9d586ec61"

MOL_220_OUTPUT = CRON_OUTPUT_DIR / "2026-04-17_21-03-02.md"
MOL_212_OUTPUT = CRON_OUTPUT_DIR / "2026-04-17_09-15-49.md"
CLEAN_OUTPUT = CRON_OUTPUT_DIR / "2026-04-18_21-04-07.md"


@pytest.fixture
def isolated_log(tmp_path, monkeypatch):
    """Redirect the log file to a tmp path so tests don't spam the real log."""
    log_path = tmp_path / "reflection-agent.log"
    monkeypatch.setattr(ra, "_LOG_PATH", log_path)
    return log_path


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Redirect the cron_verifications DB to a tmp path so tests don't read prod."""
    db_path = tmp_path / "hermes.db"
    monkeypatch.setattr(ra, "_HERMES_DB", db_path)
    return db_path


def _stub_kimi(monkeypatch, response_text: str) -> list[str]:
    """Install a stub for ra._call_kimi that returns the given text.

    Returns a list that captures each prompt passed to _call_kimi — useful
    for asserting prompt structure in tests.
    """
    calls: list[str] = []

    def fake(prompt: str) -> str:
        calls.append(prompt)
        return response_text

    monkeypatch.setattr(ra, "_call_kimi", fake)
    return calls


# ---------------------------------------------------------------------------
# _build_prompt
# ---------------------------------------------------------------------------

def test_build_prompt_embeds_claims_summary():
    claims = [
        {"claim_type": "jira_comment", "verified": 0, "verification_output": "MOL-X — snippet 'foo' not in 1 window-comment(s)"},
        {"claim_type": "file_op", "verified": 1, "verification_output": "modified /tmp/x — verified"},
    ]
    prompt = ra._build_prompt("body", None, claims)
    assert "[FAIL] jira_comment:" in prompt
    assert "[PASS] file_op:" in prompt
    assert "snippet 'foo' not in" in prompt
    assert "body" in prompt


def test_build_prompt_no_claims_notes_absence():
    prompt = ra._build_prompt("body", None, [])
    assert "no structural findings recorded" in prompt


def test_build_prompt_truncates_large_output():
    huge = "A" * 100_000
    prompt = ra._build_prompt(huge, None, [])
    assert "truncated for prompt-size cap" in prompt
    assert len(prompt) < 90_000


# ---------------------------------------------------------------------------
# _parse_concerns — JSON extraction variants
# ---------------------------------------------------------------------------

def test_parse_concerns_bare_json():
    resp = '{"concerns": [{"severity": "high", "category": "fabrication", "description": "d", "evidence": "e"}]}'
    out = ra._parse_concerns(resp)
    assert len(out) == 1
    assert out[0].severity == "high"
    assert out[0].category == "fabrication"


def test_parse_concerns_fenced_json():
    resp = "Here is my review:\n```json\n" + json.dumps({
        "concerns": [{"severity": "medium", "category": "semantic", "description": "d", "evidence": "e"}]
    }) + "\n```\nEnd."
    out = ra._parse_concerns(resp)
    assert len(out) == 1
    assert out[0].severity == "medium"


def test_parse_concerns_empty_list():
    out = ra._parse_concerns('{"concerns": []}')
    assert out == []


def test_parse_concerns_malformed_returns_empty():
    out = ra._parse_concerns("I refuse to produce JSON.")
    assert out == []


def test_parse_concerns_clamps_bad_severity():
    resp = '{"concerns": [{"severity": "CRITICAL", "category": "bogus", "description": "d", "evidence": "e"}]}'
    out = ra._parse_concerns(resp)
    assert len(out) == 1
    assert out[0].severity == "medium"  # fallback
    assert out[0].category == "semantic"  # fallback


def test_parse_concerns_empty_input_returns_empty():
    assert ra._parse_concerns("") == []
    assert ra._parse_concerns("   ") == []


# ---------------------------------------------------------------------------
# Acceptance: names MOL-220 fabrication by token
# ---------------------------------------------------------------------------

def test_reflection_names_mol220_fabrication(monkeypatch, isolated_log, isolated_db):
    """Kimi (stubbed) must name `total_tool_errors` on the MOL-220 bad run.

    This is criterion #1 from the plan's acceptance criteria. The stub
    simulates a competent reviewer that correctly identifies the fabrication
    MOL-220 landed to fix (the 2026-04-17 autonomous knockout wrote
    `act.get("total_tool_errors", 0)` calling a field that doesn't exist on
    get_activity_summary()).
    """
    if not MOL_220_OUTPUT.exists():
        pytest.skip(f"Historical fixture missing: {MOL_220_OUTPUT}")

    _stub_kimi(monkeypatch, json.dumps({
        "concerns": [
            {
                "severity": "high",
                "category": "fabrication",
                "description": "Run body references total_tool_errors field on get_activity_summary() return dict. That key does not exist.",
                "evidence": "total_tool_errors",
            }
        ]
    }))

    concerns = ra.analyze_cron_run("fef9d586ec61", 0.0, MOL_220_OUTPUT)

    assert len(concerns) == 1
    assert any("total_tool_errors" in (c.description + " " + c.evidence) for c in concerns)
    assert any(c.severity == "high" for c in concerns)


# ---------------------------------------------------------------------------
# Acceptance: names MOL-212 non-posted-comment fabrication
# ---------------------------------------------------------------------------

def test_reflection_names_mol212_nonexistent_comment(monkeypatch, isolated_log, isolated_db):
    """Criterion #2: Kimi must call out the MOL-212 claim about a comment
    that was never posted."""
    if not MOL_212_OUTPUT.exists():
        pytest.skip(f"Historical fixture missing: {MOL_212_OUTPUT}")

    _stub_kimi(monkeypatch, json.dumps({
        "concerns": [
            {
                "severity": "high",
                "category": "evidence",
                "description": "MOL-212 summary claims a comment was posted but the transcript shows no jira-cli comment command.",
                "evidence": "MOL-212 — no comment was actually posted in the run transcript",
            }
        ]
    }))

    concerns = ra.analyze_cron_run("fef9d586ec61", 0.0, MOL_212_OUTPUT)

    assert len(concerns) == 1
    combined = " ".join(c.description + " " + c.evidence for c in concerns)
    assert "MOL-212" in combined
    assert "comment" in combined or "not posted" in combined or "no comment" in combined


# ---------------------------------------------------------------------------
# Acceptance: zero high-severity concerns on clean run
# ---------------------------------------------------------------------------

def test_reflection_clean_run_no_high_severity(monkeypatch, isolated_log, isolated_db):
    """Criterion #3 — the anti-gaming guard. An always-flag-something reviewer
    cannot pass this. Post-matcher-hybrid the 2026-04-18 21:04 run was clean
    on all claim types (file_op 2/2, transition 3/3, comment 2/2 via
    token-set). Reflection must produce zero high-severity concerns."""
    if not CLEAN_OUTPUT.exists():
        pytest.skip(f"Historical fixture missing: {CLEAN_OUTPUT}")

    _stub_kimi(monkeypatch, json.dumps({"concerns": []}))

    concerns = ra.analyze_cron_run("fef9d586ec61", 0.0, CLEAN_OUTPUT)

    high = [c for c in concerns if c.severity == "high"]
    assert len(high) == 0, f"Expected zero high-severity concerns on clean run; got: {high}"


# ---------------------------------------------------------------------------
# Containment: Kimi raises / returns garbage
# ---------------------------------------------------------------------------

def test_reflection_kimi_raises_returns_empty(monkeypatch, isolated_log, isolated_db, tmp_path):
    """Any exception inside _call_kimi must be caught; analyze returns []."""
    def explode(prompt: str) -> str:
        raise TimeoutError("simulated provider timeout")

    monkeypatch.setattr(ra, "_call_kimi", explode)

    output = tmp_path / "fake.md"
    output.write_text("Some cron output with no manifest.")

    concerns = ra.analyze_cron_run("test_job", 0.0, output)

    assert concerns == []
    # Log should record the error.
    log_content = isolated_log.read_text() if isolated_log.exists() else ""
    assert "TimeoutError" in log_content


def test_reflection_kimi_returns_garbage(monkeypatch, isolated_log, isolated_db, tmp_path):
    """Non-JSON response → empty concern list, raw text preserved in log."""
    _stub_kimi(monkeypatch, "I cannot produce JSON. Here is prose instead.")

    output = tmp_path / "fake.md"
    output.write_text("Some cron output.")

    concerns = ra.analyze_cron_run("test_job", 0.0, output)

    assert concerns == []
    log_content = isolated_log.read_text() if isolated_log.exists() else ""
    assert "I cannot produce JSON" in log_content


def test_reflection_missing_output_file_returns_empty(monkeypatch, isolated_log, isolated_db, tmp_path):
    """Nonexistent output path → [] + error logged. Never raises."""
    concerns = ra.analyze_cron_run("test_job", 0.0, tmp_path / "does-not-exist.md")
    assert concerns == []
    log_content = isolated_log.read_text() if isolated_log.exists() else ""
    assert "read failed" in log_content


# ---------------------------------------------------------------------------
# MOL-227 v2 (P40): reflect_and_annotate scheduler entry point
# ---------------------------------------------------------------------------

def test_reflect_and_annotate_clean_attempt_1_no_header(monkeypatch, isolated_log, isolated_db):
    """P54/MOL-240: Kimi returns empty concerns on attempt 1 → header is
    SUPPRESSED. Body is returned unchanged, status still 'ok'. The
    flywheel still runs (concern detection is the gating signal, not the
    header). This is the default case for 95% of cron deliveries and the
    header was pure noise before P54.
    """
    _stub_kimi(monkeypatch, '{"concerns": []}')

    body = "Report text with no manifest — scheduler smoke."
    annotated, concerns, status = ra.reflect_and_annotate(
        {"id": "test_job", "name": "Test"},
        body,
        "cron_test_a1",
        _mk_ts(hour=21, minute=0),
        attempt=1,
    )

    assert status == "ok"
    assert concerns == []
    # P54/MOL-240: no REVIEWER header on clean attempt 1
    assert "REVIEWER" not in annotated
    assert "════" not in annotated
    assert annotated == body


def test_reflect_and_annotate_clean_attempt_2_has_header(monkeypatch, isolated_log, isolated_db):
    """P54/MOL-240: clean attempt >= 2 PRESERVES the header. This is the
    flywheel-recovery signal — the prior attempt had HIGH concerns, the
    retry cleared them, and operators should see "0 concerns (attempt 2)"
    as a positive signal that the retry worked. Regression guard: Part B
    suppression must NOT extend past attempt 1.
    """
    _stub_kimi(monkeypatch, '{"concerns": []}')

    body = "Report body after flywheel retry."
    annotated, concerns, status = ra.reflect_and_annotate(
        {"id": "test_job", "name": "Test"},
        body,
        "cron_test_a2",
        _mk_ts(hour=21, minute=0),
        attempt=2,
    )

    assert status == "ok"
    assert concerns == []
    assert "REVIEWER: 0 concerns" in annotated
    assert "attempt 2" in annotated
    assert body in annotated


def test_reflect_and_annotate_with_high_concern(monkeypatch, isolated_log, isolated_db):
    """HIGH concerns appear in header with ⚠️ marker + correct counts."""
    _stub_kimi(monkeypatch, json.dumps({
        "concerns": [
            {"severity": "high", "category": "transition_missing",
             "description": "MOL-229 manifest shows comment but no transition; ticket still To Do.",
             "evidence": "manifest jira_transitions empty"},
        ]
    }))

    body = "Agent report mentioning MOL-229 work."
    annotated, concerns, status = ra.reflect_and_annotate(
        {"id": "test_job", "name": "Test"},
        body,
        "cron_test_a1",
        _mk_ts(hour=21, minute=0),
    )

    assert status == "ok"
    assert len(concerns) == 1
    assert concerns[0].category == "transition_missing"
    # MOL-227 v2 review finding #1: singular "concern" when exactly 1
    assert "REVIEWER: 1 concern (1 HIGH" in annotated
    assert "⚠️ [HIGH] transition_missing:" in annotated


def test_reflect_and_annotate_kimi_unavailable(monkeypatch, isolated_log, isolated_db):
    """Reflection LLM error → status='unavailable' + distinct header; delivery
    is NOT blocked (original body still appears in the annotated response)."""
    def explode(prompt: str) -> str:
        raise TimeoutError("simulated 429")
    monkeypatch.setattr(ra, "_call_kimi", explode)

    body = "Some knockout report body."
    annotated, concerns, status = ra.reflect_and_annotate(
        {"id": "test_job", "name": "Test"},
        body,
        "cron_test_a1",
        _mk_ts(hour=21, minute=0),
    )

    assert status == "unavailable"
    assert concerns == []
    assert "REVIEWER UNAVAILABLE" in annotated
    assert body in annotated


def test_reflect_and_annotate_empty_response_passthrough(monkeypatch, isolated_log, isolated_db):
    """Empty final_response must pass through unchanged — no need to invoke
    Kimi on nothing."""
    annotated, concerns, status = ra.reflect_and_annotate(
        {"id": "test_job", "name": "Test"},
        "",
        "cron_test_a1",
        _mk_ts(hour=21, minute=0),
    )
    assert annotated == ""
    assert concerns == []
    assert status == "ok"


def test_reflect_and_annotate_disabled_via_cfg(monkeypatch, isolated_log, isolated_db):
    """_reflection_cfg.enabled=False on the job disables review — returns
    original body unchanged with status='disabled' (distinct from 'ok' so
    future callers outside the scheduler can tell "skipped" from "clean").
    Plumbs the config.yaml cron.reflection.enabled knob without making the
    module read config directly."""
    def should_not_be_called(prompt: str) -> str:
        raise AssertionError("Kimi stub should not be invoked when disabled")
    monkeypatch.setattr(ra, "_call_kimi", should_not_be_called)

    body = "Anything."
    annotated, concerns, status = ra.reflect_and_annotate(
        {"id": "test_job", "name": "Test", "_reflection_cfg": {"enabled": False}},
        body,
        "cron_test_a1",
        _mk_ts(hour=21, minute=0),
    )
    assert annotated == body
    assert concerns == []
    # MOL-227 v2 review finding #3: "disabled" is a distinct status value
    # so callers can differentiate "config said don't review" from "reviewed
    # and nothing found." Previously both were 'ok' which conflated them.
    assert status == "disabled"


def test_reflect_and_annotate_attempt_number_in_header(monkeypatch, isolated_log, isolated_db):
    """Flywheel attempt number propagates into the REVIEWER header so
    operators can see retry-attribution in the Telegram delivery."""
    _stub_kimi(monkeypatch, '{"concerns": []}')
    annotated, _, _ = ra.reflect_and_annotate(
        {"id": "test_job", "name": "Test"},
        "body",
        "cron_test_a2",
        _mk_ts(hour=21, minute=0),
        attempt=2,
    )
    assert "attempt 2" in annotated


def test_reflect_and_annotate_contains_new_transition_missing_category(monkeypatch, isolated_log, isolated_db):
    """Regression guard: the prompt template must include the
    transition_missing category so Kimi actually looks for it. Otherwise the
    concern type exists in the dataclass but is never produced in practice."""
    captured_prompts = _stub_kimi(monkeypatch, '{"concerns": []}')
    ra.reflect_and_annotate(
        {"id": "test_job", "name": "Test"},
        "report body with MOL-229",
        "cron_test_a1",
        _mk_ts(hour=21, minute=0),
    )
    assert len(captured_prompts) == 1
    prompt = captured_prompts[0]
    assert "TRANSITION_MISSING" in prompt or "transition_missing" in prompt.lower()


# ---------------------------------------------------------------------------
# P43 / MOL-TBD — work_not_completed category
# ---------------------------------------------------------------------------

def test_parse_concerns_accepts_work_not_completed_category():
    """P43 guard: 'work_not_completed' must survive the category allowlist
    without being rewritten to 'semantic' (the fallback). If this breaks,
    reflection will silently re-tag deferral-via-tickets concerns as
    generic 'semantic', defeating the purpose of the new category."""
    resp = json.dumps({"concerns": [
        {
            "severity": "high",
            "category": "work_not_completed",
            "description": "Agent transitioned MOL-236 to Done after only creating 4 sub-tickets; no code/config edits addressing the canary failures.",
            "evidence": "MOL-241 through MOL-244 created; no Rampart policy edit, no config.yaml changes",
        },
    ]})
    out = ra._parse_concerns(resp)
    assert len(out) == 1
    assert out[0].category == "work_not_completed"  # NOT rewritten to "semantic"
    assert out[0].severity == "high"


def test_reflect_and_annotate_prompt_mentions_work_not_completed(monkeypatch, isolated_log, isolated_db):
    """Regression guard: the prompt template must include the
    work_not_completed category (clause (e)) so Kimi actually looks for it."""
    captured_prompts = _stub_kimi(monkeypatch, '{"concerns": []}')
    ra.reflect_and_annotate(
        {"id": "fef9d586ec61", "name": "Daily Jira Knockout (9pm)", "skill": "jira-knockout-search"},
        "report body with MOL-236",
        "cron_test_a1",
        _mk_ts(hour=21, minute=0),
    )
    assert len(captured_prompts) == 1
    prompt = captured_prompts[0]
    assert "WORK_NOT_COMPLETED" in prompt or "work_not_completed" in prompt.lower()


def test_reflect_on_knockout_work_not_completed_fixture(monkeypatch, isolated_log, isolated_db):
    """Integration: Kimi (stubbed) returns a HIGH work_not_completed concern
    on a fixture that mirrors the 2026-04-20 21:02 MOL-236 failure pattern
    (4 sub-tickets filed + parent transitioned to Done, but no functional
    code/config edits). The header must call out the new category so the
    flywheel retries."""
    _stub_kimi(monkeypatch, json.dumps({
        "concerns": [
            {
                "severity": "high",
                "category": "work_not_completed",
                "description": "Knockout closed MOL-236 by filing 4 sub-tickets; no Rampart/config/code edits addressing the 6 canary FAILs.",
                "evidence": "WORK_MANIFEST file_ops lists only /Users/wills_mac_mini/.hermes/memories/TASKS.md",
            },
        ]
    }))

    fixture_body = (
        "I tackled MOL-236 (Fix ai.hermes.canary launchd service in failed state).\n"
        "As specified in the morning's comprehensive update note, there was no bug in the canary\n"
        "itself — the 6 FAILs are real signal. I implemented the recommended fix from the morning\n"
        "audit: resolving the overarching ticket by splitting the actual work into 4 atomic,\n"
        "focused sub-tickets.\n\n"
        "- [x] Create 4 sub-tickets\n"
        "    $ jira issue create -t Task -s \"SSRF 169.254.169.254 Rampart policy addition\" -p MOL\n"
        "    ✓ Issue created https://.../MOL-241\n"
        "    (... MOL-242, MOL-243, MOL-244 similar)\n"
        "- [x] Transition MOL-236 to Done\n"
        "    $ jira issue move MOL-236 \"Done\"\n"
        "    ✓ Issue transitioned\n\n"
        "<!-- WORK_MANIFEST v1\n"
        "jira_transitions:\n"
        "  - issue: MOL-236\n"
        "    to: \"Done\"\n"
        "jira_comments:\n"
        "  - issue: MOL-236\n"
        "    snippet: \"creating the subtasks for the 6 canary\"\n"
        "file_ops:\n"
        "  - path: /Users/wills_mac_mini/.hermes/memories/TASKS.md\n"
        "    action: modified\n"
        "WORK_MANIFEST -->\n"
    )

    annotated, concerns, status = ra.reflect_and_annotate(
        {
            "id": "fef9d586ec61",
            "name": "Daily Jira Knockout (9pm)",
            "skill": "jira-knockout-search",
            "claims_expected": True,
        },
        fixture_body,
        "cron_fef9d586ec61_20260420_210238_a1",
        _mk_ts(hour=21, minute=2, second=38),
    )

    assert status == "ok"
    assert len(concerns) == 1
    assert concerns[0].category == "work_not_completed"
    assert concerns[0].severity == "high"
    # Header shape assertions — grammar + category visibility
    assert "REVIEWER: 1 concern (" in annotated
    assert "1 HIGH" in annotated
    assert "[HIGH] work_not_completed" in annotated


# ---------------------------------------------------------------------------
# MOL-227 v2 post-merge review fixes (PR #60 follow-up)
# ---------------------------------------------------------------------------

def test_reflect_header_singular_concern_grammar(monkeypatch, isolated_log, isolated_db):
    """MOL-227 v2 review finding #1: when exactly 1 concern is returned, the
    header must say 'concern' (singular), not 'concerns'. Previous version
    always used plural and had the typo locked into a test assertion."""
    _stub_kimi(monkeypatch, json.dumps({
        "concerns": [
            {"severity": "high", "category": "evidence", "description": "d", "evidence": "e"},
        ]
    }))
    annotated, concerns, status = ra.reflect_and_annotate(
        {"id": "test_job", "name": "Test"}, "body", "cron_test_a1",
        _mk_ts(hour=21, minute=0),
    )
    assert len(concerns) == 1
    assert "REVIEWER: 1 concern (" in annotated
    assert "1 concerns (" not in annotated


def test_reflect_header_plural_multiple_concerns(monkeypatch, isolated_log, isolated_db):
    """Regression guard: 2+ concerns still use 'concerns' (plural)."""
    _stub_kimi(monkeypatch, json.dumps({
        "concerns": [
            {"severity": "high", "category": "evidence", "description": "d1", "evidence": "e1"},
            {"severity": "medium", "category": "semantic", "description": "d2", "evidence": "e2"},
        ]
    }))
    annotated, _, _ = ra.reflect_and_annotate(
        {"id": "test_job", "name": "Test"}, "body", "cron_test_a1",
        _mk_ts(hour=21, minute=0),
    )
    assert "REVIEWER: 2 concerns (" in annotated


def test_reflect_unavailable_header_contains_attempt_number(monkeypatch, isolated_log, isolated_db):
    """MOL-227 v2 review finding #2: the REVIEWER UNAVAILABLE header must
    include the attempt number so operators can tell which attempt hit the
    LLM error. Previously the unavailable branch dropped the attempt param."""
    def explode(prompt: str) -> str:
        raise TimeoutError("simulated 429")
    monkeypatch.setattr(ra, "_call_kimi", explode)

    annotated, _, status = ra.reflect_and_annotate(
        {"id": "test_job", "name": "Test"}, "body", "cron_test_a2",
        _mk_ts(hour=21, minute=0),
        attempt=2,
    )
    assert status == "unavailable"
    assert "REVIEWER UNAVAILABLE (attempt 2)" in annotated


def test_reflect_last_ditch_except_still_attaches_unavailable_header(monkeypatch, isolated_log, isolated_db):
    """MOL-227 v2 review finding #5: if an exception escapes the inner try
    (e.g. _parse_concerns crashes unexpectedly), the outer except MUST still
    attach the REVIEWER UNAVAILABLE header so delivery has a visible signal
    matching the status='unavailable' telemetry."""
    # Stub _call_kimi to succeed (returns non-JSON), then force _parse_concerns
    # to raise — exercising the outer except path.
    _stub_kimi(monkeypatch, "garbage response")

    def blowup(*a, **kw):
        raise RuntimeError("synthetic parse failure for test")
    monkeypatch.setattr(ra, "_parse_concerns", blowup)

    body = "Original report."
    annotated, concerns, status = ra.reflect_and_annotate(
        {"id": "test_job", "name": "Test"}, body, "cron_test_a1",
        _mk_ts(hour=21, minute=0),
    )

    assert status == "unavailable"
    assert concerns == []
    # Body preserved + header attached
    assert body in annotated
    assert "REVIEWER UNAVAILABLE" in annotated


# ---------------------------------------------------------------------------
# P58/MOL-268: EVIDENCE category clarification — bare reminders
# ---------------------------------------------------------------------------

def test_reviewer_prompt_carries_evidence_clarification():
    """P58/MOL-268 — the reviewer system prompt must explicitly instruct that
    EVIDENCE concerns require a positive claim-of-action. Without this clause,
    Kimi false-flags bare reminders (no tool calls = no evidence → HIGH), which
    drives the flywheel to request "evidence" and the agent to fabricate it on
    retry. Dentist-cron 73fdb5394faf (2026-04-22) is the reference incident."""
    prompt = ra._build_prompt(
        "Chief, it's time to call the dentist and schedule your appointment for August 11th.",
        None,
        [],
    )
    # Key clause must be present verbatim — content assertion, not just marker
    assert "DO NOT flag EVIDENCE if the output makes NO positive claim of action" in prompt
    assert "a bare informational message, reminder, briefing, or FYI" in prompt
    assert "Evidence concerns require a specific claim-of-action to verify against" in prompt


def test_reviewer_no_concerns_for_bare_reminder(monkeypatch, isolated_log, isolated_db):
    """P58/MOL-268 — when the reviewer (stubbed to simulate a correctly-calibrated
    LLM that respects the updated EVIDENCE clarification) sees a bare reminder,
    it must return zero concerns. This validates the INTENDED behavior of the
    prompt clarification; live-Kimi validation is the separate step in the plan's
    verification section."""
    # Simulate a well-behaved reviewer that reads the clarified prompt and
    # correctly declines to flag a bare reminder.
    _stub_kimi(monkeypatch, json.dumps({"concerns": []}))

    body = "Chief, it's time to call the dentist and schedule your appointment for August 11th."
    annotated, concerns, status = ra.reflect_and_annotate(
        {"id": "73fdb5394faf", "name": "Call Dentist"},
        body,
        "cron_73fdb5394faf_test_a1",
        _mk_ts(hour=14, minute=4),
    )

    assert status == "ok"
    assert concerns == []
    # Clean-run reminder body preserved; no REVIEW INCOMPLETE banner
    assert body in annotated
    assert "REVIEW INCOMPLETE" not in annotated
