"""Tests that the background review agent is restricted to memory+skills toolsets.

Regression coverage for issue #15204: the background skill-review agent
inherited the full default toolset, allowing it to perform non-skill side
effects (terminal, send_message, delegate_task, etc.).
"""

import threading
from unittest.mock import patch


def _make_agent_stub(agent_cls):
    """Create a minimal AIAgent-like object with just enough state for _spawn_background_review."""
    agent = object.__new__(agent_cls)
    agent.model = "test-model"
    agent.platform = "test"
    agent.provider = "openai"
    agent.base_url = ""
    agent.api_key = ""
    agent.api_mode = ""
    agent.session_id = "sess-123"
    agent.session_start = "2026-05-19T00:00:00Z"
    agent._parent_session_id = ""
    agent._credential_pool = None
    agent.quiet_mode = True
    agent._memory_store = None
    agent._memory_enabled = True
    agent._user_profile_enabled = False
    agent._memory_nudge_interval = 5
    agent._skill_nudge_interval = 5
    agent.background_review_callback = None
    agent.status_callback = None
    agent._MEMORY_REVIEW_PROMPT = "review memory"
    agent._SKILL_REVIEW_PROMPT = "review skills"
    agent._COMBINED_REVIEW_PROMPT = "review both"
    # MOL-597: upstream's agent/background_review.py references these on the
    # parent agent. See test_background_review.py _bare_agent() for the
    # equivalent fixture-completion.
    agent._current_main_runtime = lambda: {
        "base_url": "",
        "api_key": "",
        "api_mode": "",
    }
    agent._cached_system_prompt = None
    agent._safe_print = lambda *_a, **_kw: None
    agent._emit_auxiliary_failure = lambda *_a, **_kw: None
    return agent


class _SyncThread:
    """Drop-in replacement for threading.Thread that runs the target inline."""

    def __init__(self, *, target=None, daemon=None, name=None):
        self._target = target

    def start(self):
        if self._target:
            self._target()


def test_background_review_agent_uses_restricted_toolsets():
    """The review agent must only have access to 'memory' and 'skills' toolsets.

    Upstream commits 1f6eb1738 + 5fe067226 (absorbed via MOL-597) shifted
    the restriction from `AIAgent(enabled_toolsets=[...])` to a thread-local
    tool whitelist (set_thread_tool_whitelist) so the review fork can inherit
    the parent's full toolset schema for prefix-cache parity while still
    blocking non-memory/non-skill tools at the pre_tool_call gate.
    """
    import run_agent
    import hermes_cli.plugins as plugins_mod

    agent = _make_agent_stub(run_agent.AIAgent)
    captured = {}

    def _capture_whitelist(allowed, deny_msg_fmt=None):
        captured["whitelist"] = set(allowed) if allowed else None
        raise RuntimeError("stop after capturing whitelist")

    def _noop_init(self, *_args, **_kwargs):
        # Skip real provider resolution; we only care about the whitelist call.
        self._session_messages = []

    def _noop(self, *_args, **_kwargs):
        return None

    with patch.object(plugins_mod, "set_thread_tool_whitelist", _capture_whitelist), \
         patch.object(run_agent.AIAgent, "__init__", _noop_init), \
         patch.object(run_agent.AIAgent, "run_conversation", _noop), \
         patch.object(run_agent.AIAgent, "shutdown_memory_provider", _noop), \
         patch.object(run_agent.AIAgent, "close", _noop), \
         patch("threading.Thread", _SyncThread):
        agent._spawn_background_review(
            messages_snapshot=[],
            review_memory=True,
            review_skills=False,
        )

    assert "whitelist" in captured, "set_thread_tool_whitelist was not called"
    assert "memory" in captured["whitelist"]
    assert "skill_manage" in captured["whitelist"]
    assert "terminal" not in captured["whitelist"]
    assert "delegate_task" not in captured["whitelist"]


def test_background_review_agent_tools_are_limited():
    """Verify the resolved memory+skills toolsets only contain memory and skill tools."""
    from toolsets import resolve_multiple_toolsets

    expected_tools = set(resolve_multiple_toolsets(["memory", "skills"]))

    assert "memory" in expected_tools
    assert "skill_manage" in expected_tools
    assert "skill_view" in expected_tools
    assert "skills_list" in expected_tools

    assert "terminal" not in expected_tools
    assert "send_message" not in expected_tools
    assert "delegate_task" not in expected_tools
    assert "web_search" not in expected_tools
    assert "execute_code" not in expected_tools
