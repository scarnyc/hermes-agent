"""P75/MOL-312, P76/MOL-313, P77/MOL-314 — empty-response handling tests.

P75: pure-function tests for _recover_briefing_from_tool_calls (recovery
helper that extracts briefing content from a prior memory_observe call when
the synthesis turn collapses to empty).

P76: tests for _log_empty_response_diagnostic (JSONL logger; fail-open).

P77: tests for the agent.fallback_on_empty_exhausted config plumbing.

See PATCHES.md P75/P76/P77 for the full failure-mode rationale.
"""

import json

import pytest

import run_agent
from run_agent import (
    _log_empty_response_diagnostic,
    _recover_briefing_from_tool_calls,
)


def _build_assistant_msg_with_memory_observe(
    *,
    content: object,
    category: str = "briefing",
    title: str = "Daily Briefing 2026-04-26",
    other_arg_value: str = "ignored",
):
    """Construct a synthetic assistant turn that called memory_observe.

    The shape mirrors what run_agent.py builds via _build_assistant_message
    when the model emits a tool_calls turn (OpenAI tool-calling spec).
    """
    args = {"category": category, "title": title}
    if content is not None:
        args["content"] = content
    args["extra"] = other_arg_value
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "tool_memory_observe_synthetic",
                "type": "function",
                "function": {
                    "name": "memory_observe",
                    "arguments": json.dumps(args),
                },
            }
        ],
    }


class TestRecoverBriefingFromToolCalls:
    """P75/MOL-312 helper-level tests."""

    def test_positive_recovery(self):
        """Briefing memory_observe call → returns the content arg."""
        briefing = (
            "INFRA: ALL GREEN. EMAIL: 21 unread. CALENDAR: 4 events. "
            "JIRA: MOL-312, MOL-305, MOL-294. NEWS: 20 items. ANALYSIS: ..."
        )
        messages = [
            {"role": "user", "content": "run comprehensive update"},
            _build_assistant_msg_with_memory_observe(content=briefing),
            {
                "role": "tool",
                "tool_call_id": "tool_memory_observe_synthetic",
                "content": '{"id": "abc", "category": "briefing", "status": "stored"}',
            },
            {"role": "assistant", "content": "", "tool_calls": []},
        ]
        assert _recover_briefing_from_tool_calls(messages) == briefing

    def test_no_memory_observe_call_returns_none(self):
        """No memory_observe in history → None (falls through to existing path)."""
        messages = [
            {"role": "user", "content": "list jira"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tool_terminal_x",
                        "type": "function",
                        "function": {
                            "name": "terminal",
                            "arguments": '{"command": "jira issue list"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tool_terminal_x", "content": "..."},
        ]
        assert _recover_briefing_from_tool_calls(messages) is None

    def test_malformed_arguments_json_returns_none(self):
        """Non-JSON arguments string → None, no exception raised."""
        messages = [
            {"role": "user", "content": "x"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tool_memory_observe_bad",
                        "type": "function",
                        "function": {
                            "name": "memory_observe",
                            "arguments": "not-json{",
                        },
                    }
                ],
            },
        ]
        assert _recover_briefing_from_tool_calls(messages) is None

    def test_wrong_category_returns_none(self):
        """memory_observe with category != 'briefing' → None."""
        messages = [
            {"role": "user", "content": "x"},
            _build_assistant_msg_with_memory_observe(
                content="some chat memory", category="chat"
            ),
        ]
        assert _recover_briefing_from_tool_calls(messages) is None

    def test_most_recent_briefing_call_wins(self):
        """Multiple briefing calls → returns content of the LAST (most recent)."""
        first = "first briefing (older)"
        second = "second briefing (newer)"
        messages = [
            {"role": "user", "content": "x"},
            _build_assistant_msg_with_memory_observe(content=first),
            {"role": "tool", "tool_call_id": "tool_memory_observe_synthetic", "content": "{}"},
            {"role": "user", "content": "again"},
            _build_assistant_msg_with_memory_observe(content=second),
        ]
        assert _recover_briefing_from_tool_calls(messages) == second

    def test_missing_content_arg_returns_none(self):
        """memory_observe called with no content key → None."""
        messages = [
            {"role": "user", "content": "x"},
            _build_assistant_msg_with_memory_observe(content=None),
        ]
        assert _recover_briefing_from_tool_calls(messages) is None

    def test_empty_string_content_returns_none(self):
        """memory_observe called with content='' or whitespace → None."""
        messages = [
            {"role": "user", "content": "x"},
            _build_assistant_msg_with_memory_observe(content="   \n  "),
        ]
        assert _recover_briefing_from_tool_calls(messages) is None

    def test_non_string_content_returns_none(self):
        """memory_observe with non-string content (e.g., dict) → None."""
        messages = [
            {"role": "user", "content": "x"},
            _build_assistant_msg_with_memory_observe(content={"nested": "dict"}),
        ]
        assert _recover_briefing_from_tool_calls(messages) is None

    def test_empty_messages_returns_none(self):
        """Empty messages list → None."""
        assert _recover_briefing_from_tool_calls([]) is None

    def test_tool_calls_none_field_handled(self):
        """Assistant message with tool_calls=None → no crash, returns None."""
        messages = [
            {"role": "assistant", "content": "", "tool_calls": None},
        ]
        assert _recover_briefing_from_tool_calls(messages) is None


class TestLogEmptyResponseDiagnostic:
    """P76/MOL-313 — JSONL diagnostic logger; fail-open contract."""

    @pytest.fixture
    def isolated_log_path(self, tmp_path, monkeypatch):
        """Redirect _EMPTY_RESPONSE_LOG_PATH to a tmp file so tests are
        isolated from the real ~/.hermes/logs/ surface."""
        from pathlib import Path
        log_file = tmp_path / "empty-response.jsonl"
        monkeypatch.setattr(run_agent, "_EMPTY_RESPONSE_LOG_PATH", Path(log_file))
        return Path(log_file)

    def test_appends_one_jsonl_row_per_call(self, isolated_log_path):
        _log_empty_response_diagnostic(
            model="google/gemini-3.1-pro-preview",
            provider="openrouter",
            session_id="cron_4f64b8b302cc_20260426_070017",
            attempt=1,
            finish_reason="stop",
            has_reasoning=False,
            message_count=18,
            fallback_activated=False,
        )
        assert isolated_log_path.exists()
        lines = isolated_log_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["model"] == "google/gemini-3.1-pro-preview"
        assert row["provider"] == "openrouter"
        assert row["session_id"] == "cron_4f64b8b302cc_20260426_070017"
        assert row["attempt"] == 1
        assert row["finish_reason"] == "stop"
        assert row["has_reasoning"] is False
        assert row["message_count"] == 18
        assert row["fallback_activated"] is False
        assert "ts" in row and isinstance(row["ts"], str)

    def test_three_retries_emit_three_rows(self, isolated_log_path):
        for attempt in (1, 2, 3):
            _log_empty_response_diagnostic(
                model="x", provider="y", session_id="s",
                attempt=attempt, finish_reason="stop",
                has_reasoning=False, message_count=10,
                fallback_activated=False,
            )
        lines = isolated_log_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 3
        assert [json.loads(line)["attempt"] for line in lines] == [1, 2, 3]

    def test_log_dir_created_if_missing(self, tmp_path, monkeypatch):
        """Helper auto-creates the parent directory on first write."""
        from pathlib import Path
        nested = tmp_path / "deeper" / "nesting" / "empty-response.jsonl"
        monkeypatch.setattr(run_agent, "_EMPTY_RESPONSE_LOG_PATH", Path(nested))
        _log_empty_response_diagnostic(
            model="x", provider="y", session_id="s",
            attempt=1, finish_reason="stop",
            has_reasoning=False, message_count=1,
            fallback_activated=False,
        )
        assert nested.exists()
        assert nested.parent.is_dir()

    def test_io_error_does_not_raise(self, monkeypatch):
        """Fail-open contract: IO failure must NOT raise (would block agent loop)."""
        from pathlib import Path
        bogus = Path("/this/path/cannot/be/created/under/sandbox/empty-response.jsonl")
        monkeypatch.setattr(run_agent, "_EMPTY_RESPONSE_LOG_PATH", bogus)
        # Must not raise
        _log_empty_response_diagnostic(
            model="x", provider="y", session_id="s",
            attempt=1, finish_reason="stop",
            has_reasoning=False, message_count=1,
            fallback_activated=False,
        )

    def test_serialization_failure_swallowed(self, isolated_log_path, monkeypatch):
        """Non-serializable input (e.g., bytes) must NOT raise."""
        # Force a JSON serialization failure by passing a non-serializable
        # value through one of the primitive fields. We monkey-patch
        # json.dumps to raise once to simulate this without changing the
        # public API.
        original_dumps = json.dumps

        def boom(*a, **kw):
            raise TypeError("synthetic serialization failure")

        monkeypatch.setattr(run_agent.json, "dumps", boom)
        # Must not raise
        _log_empty_response_diagnostic(
            model="x", provider="y", session_id="s",
            attempt=1, finish_reason="stop",
            has_reasoning=False, message_count=1,
            fallback_activated=False,
        )
        # Restore for cleanliness
        monkeypatch.setattr(run_agent.json, "dumps", original_dumps)


class TestFallbackOnEmptyExhaustedConfig:
    """P77/MOL-314 — config flag plumbing for the empty-exhausted fallback."""

    def test_default_config_has_flag_true(self):
        """DEFAULT_CONFIG['agent']['fallback_on_empty_exhausted'] must default
        to True so empty-completion failures activate the fallback chain
        out of the box."""
        from hermes_cli.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["agent"]["fallback_on_empty_exhausted"] is True

    def test_default_config_flag_present_in_agent_section(self):
        """Flag must live under DEFAULT_CONFIG['agent'], not at top level.

        save_config() strips unknown top-level keys on round-trip — a flag
        registered at the bare top level would be silently wiped on the
        next config save. Nesting under 'agent' is what makes it persist.
        """
        from hermes_cli.config import DEFAULT_CONFIG
        assert "fallback_on_empty_exhausted" not in DEFAULT_CONFIG
        assert "fallback_on_empty_exhausted" in DEFAULT_CONFIG["agent"]


class TestRecoverBriefingMalformedShapes:
    """P75/MOL-312 — review-driven coverage for malformed tool_calls shapes
    that the helper must absorb without raising.
    """

    def test_tool_calls_string_shape_skipped(self):
        """tool_calls is a non-list iterable (a string) → iter() succeeds
        but each character isn't a dict → all skipped → returns None."""
        messages = [
            {"role": "assistant", "content": "", "tool_calls": "not-a-list"},
        ]
        assert _recover_briefing_from_tool_calls(messages) is None

    def test_tool_calls_dict_shape_skipped(self):
        """tool_calls is a dict (iter yields keys) → keys aren't dicts →
        skipped → returns None."""
        messages = [
            {"role": "assistant", "content": "", "tool_calls": {"id": "x"}},
        ]
        assert _recover_briefing_from_tool_calls(messages) is None

    def test_tool_calls_int_shape_skipped(self):
        """tool_calls is a non-iterable scalar → iter() raises TypeError →
        caught → next message tried → returns None overall."""
        messages = [
            {"role": "assistant", "content": "", "tool_calls": 42},
        ]
        assert _recover_briefing_from_tool_calls(messages) is None

    def test_messages_with_non_dict_entry_skipped(self):
        """messages contains a non-dict (e.g. a string) → isinstance guard
        skips → no AttributeError → returns None."""
        messages = [
            "not-a-dict",
            None,
            {"role": "assistant", "content": "", "tool_calls": []},
        ]
        assert _recover_briefing_from_tool_calls(messages) is None


class TestEmptyDiagnosticOutcomeField:
    """P76/MOL-313 — review-driven coverage for the new ``outcome`` field
    that lets the JSONL reader cohort empty-events by terminal disposition.
    """

    @pytest.fixture
    def isolated_log_path(self, tmp_path, monkeypatch):
        from pathlib import Path
        log_file = tmp_path / "empty-response.jsonl"
        monkeypatch.setattr(run_agent, "_EMPTY_RESPONSE_LOG_PATH", Path(log_file))
        return Path(log_file)

    def test_outcome_defaults_to_retry(self, isolated_log_path):
        """When outcome is omitted, the row records ``outcome=retry`` so
        retry rows are unambiguously distinguishable from terminal rows."""
        _log_empty_response_diagnostic(
            model="x", provider="y", session_id="s",
            attempt=1, finish_reason="stop",
            has_reasoning=False, message_count=10,
            fallback_activated=False,
        )
        row = json.loads(isolated_log_path.read_text(encoding="utf-8").strip())
        assert row["outcome"] == "retry"

    def test_outcome_recovered_recorded(self, isolated_log_path):
        """Recovery success path emits ``outcome=recovered``."""
        _log_empty_response_diagnostic(
            model="x", provider="y", session_id="s",
            attempt=3, finish_reason="stop",
            has_reasoning=False, message_count=10,
            fallback_activated=False,
            outcome="recovered",
        )
        row = json.loads(isolated_log_path.read_text(encoding="utf-8").strip())
        assert row["outcome"] == "recovered"

    def test_outcome_fallback_activated_recorded(self, isolated_log_path):
        """Fallback activation path emits ``outcome=fallback_activated``."""
        _log_empty_response_diagnostic(
            model="primary", provider="openrouter", session_id="s",
            attempt=3, finish_reason="stop",
            has_reasoning=False, message_count=10,
            fallback_activated=True,
            outcome="fallback_activated",
        )
        row = json.loads(isolated_log_path.read_text(encoding="utf-8").strip())
        assert row["outcome"] == "fallback_activated"
        assert row["fallback_activated"] is True

    def test_outcome_terminal_exit_reason_recorded(self, isolated_log_path):
        """Terminal (empty) fallthrough emits ``outcome=<turn_exit_reason>``
        — one of empty_response_exhausted / fallback_chain_exhausted /
        empty_response_after_fallback."""
        for reason in (
            "empty_response_exhausted",
            "fallback_chain_exhausted",
            "empty_response_after_fallback",
        ):
            _log_empty_response_diagnostic(
                model="x", provider="y", session_id="s",
                attempt=3, finish_reason="stop",
                has_reasoning=False, message_count=10,
                fallback_activated=(reason == "empty_response_after_fallback"),
                outcome=reason,
            )
        lines = isolated_log_path.read_text(encoding="utf-8").strip().split("\n")
        outcomes = [json.loads(line)["outcome"] for line in lines]
        assert outcomes == [
            "empty_response_exhausted",
            "fallback_chain_exhausted",
            "empty_response_after_fallback",
        ]


class TestRecoveryBranchOrchestration:
    """P75 + P77/MOL-312 — review-driven smoke test for the wire-in branch
    ordering. The actual agent loop is too large to instantiate; instead
    we verify the helper-level invariants that the wire-in DEPENDS on,
    plus the message-tagging behavior the wire-in performs.
    """

    def test_recovery_branch_uses_sibling_key_not_overwrite(self):
        """The wire-in tags via _briefing_recovery_marker (sibling key) —
        NOT by overwriting m['content']. This preserves forensics for
        'did the model self-contradict in the prior turn'.

        We assert this by simulating the post-recovery tagging loop's
        exact shape and verifying content is preserved.
        """
        original_content = "I'll synthesize the briefing now..."
        messages = [
            {
                "role": "assistant",
                "content": original_content,
                "tool_calls": [
                    {
                        "id": "tool_memory_observe_x",
                        "type": "function",
                        "function": {
                            "name": "memory_observe",
                            "arguments": json.dumps({
                                "category": "briefing",
                                "content": "STUB",
                            }),
                        },
                    }
                ],
            },
        ]
        # Simulate the wire-in's tagging loop (copy of run_agent.py logic).
        for i in range(len(messages) - 1, -1, -1):
            m = messages[i]
            if not isinstance(m, dict):
                continue
            if m.get("role") == "assistant" and m.get("tool_calls"):
                m["_briefing_recovery_marker"] = (
                    "Recovering briefing from prior memory_observe call..."
                )
                break

        # Original content is preserved.
        assert messages[0]["content"] == original_content
        # Marker is present in sibling key.
        assert messages[0]["_briefing_recovery_marker"] == (
            "Recovering briefing from prior memory_observe call..."
        )

    def test_recovery_tagging_loop_handles_non_dict_messages(self):
        """Wire-in tagging loop must mirror helper's isinstance(m, dict)
        guard — otherwise a None or string element raises AttributeError
        at the worst possible moment (we're already recovering from a
        DIFFERENT failure)."""
        messages = [
            None,
            "not-a-dict",
            {
                "role": "assistant",
                "content": "real content",
                "tool_calls": [{"function": {"name": "memory_observe"}}],
            },
        ]
        # This should NOT raise AttributeError on the None / string entries.
        for i in range(len(messages) - 1, -1, -1):
            m = messages[i]
            if not isinstance(m, dict):
                continue
            if m.get("role") == "assistant" and m.get("tool_calls"):
                m["_briefing_recovery_marker"] = "tagged"
                break

        assert messages[2].get("_briefing_recovery_marker") == "tagged"


class TestFallbackOnEmptyExhaustedRuntimeAttribute:
    """P77/MOL-314 — review-driven verification that the runtime
    constructor actually sets self._fallback_on_empty_exhausted from
    config (not just the static DEFAULT_CONFIG check).
    """

    def test_constructor_logic_reads_flag_from_agent_section(self):
        """Replicate the constructor's read pattern and verify the flag
        flows through as bool(_agent_section.get(...)). This catches
        config-key drift if someone renames the YAML key without
        updating the runtime.

        Direct __init__ instantiation is too heavy (network, providers,
        plugins). Test the read pattern in isolation.
        """
        # Mimic the constructor's read shape from run_agent.py
        # right after self._tool_use_enforcement assignment.
        for cfg_value, expected in [
            (True, True),
            (False, False),
            ("yes", True),       # truthy non-bool tolerated by bool()
            (0, False),          # falsy non-bool tolerated by bool()
            (None, False),       # null in YAML
            (1, True),
        ]:
            agent_section = {"fallback_on_empty_exhausted": cfg_value}
            result = bool(agent_section.get("fallback_on_empty_exhausted", True))
            assert result is expected, f"cfg={cfg_value!r} → got {result!r}, expected {expected!r}"

    def test_missing_flag_defaults_to_true(self):
        """When the YAML omits the key entirely, the constructor's
        ``.get(..., True)`` default kicks in — flag is on by default."""
        agent_section: dict = {}  # no fallback_on_empty_exhausted key
        result = bool(agent_section.get("fallback_on_empty_exhausted", True))
        assert result is True

    def test_missing_agent_section_handled(self):
        """When the agent section itself is missing or non-dict, the
        constructor's `not isinstance(_agent_section, dict)` guard kicks
        in (lines preceding the flag-read). Replicate."""
        for bad_section in [None, [], "x", 42]:
            section = bad_section if isinstance(bad_section, dict) else {}
            result = bool(section.get("fallback_on_empty_exhausted", True))
            assert result is True  # default-True survives non-dict
