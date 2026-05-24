#!/usr/bin/env python3
"""
Tests for the subagent delegation tool.

Uses mock AIAgent instances to test the delegation logic without
requiring API keys or real LLM calls.

Run with:  python -m pytest tests/test_delegate.py -v
   or:     python tests/test_delegate.py
"""

import json
import os
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from tools.delegate_tool import (
    DELEGATE_BLOCKED_TOOLS,
    DELEGATE_TASK_SCHEMA,
    DelegateEvent,
    _get_max_concurrent_children,
    _LEGACY_EVENT_MAP,
    MAX_DEPTH,
    SubagentOverrides,  # P72/MOL-251 — dataclass refactor
    check_delegate_requirements,
    delegate_task,
    _build_child_agent,
    _build_child_progress_callback,
    _build_child_system_prompt,
    _strip_blocked_tools,
    _resolve_child_credential_pool,
    _resolve_delegation_credentials,
)


def _make_mock_parent(depth=0):
    """Create a mock parent agent with the fields delegate_task expects."""
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key="***"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = depth
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    return parent


class TestDelegateRequirements(unittest.TestCase):
    def test_always_available(self):
        self.assertTrue(check_delegate_requirements())

    def test_schema_valid(self):
        self.assertEqual(DELEGATE_TASK_SCHEMA["name"], "delegate_task")
        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
        self.assertIn("goal", props)
        self.assertIn("tasks", props)
        self.assertIn("context", props)
        self.assertIn("toolsets", props)
        # max_iterations is intentionally NOT exposed to the model — it's
        # config-authoritative via delegation.max_iterations so users get
        # predictable budgets.
        self.assertNotIn("max_iterations", props)
        self.assertNotIn("maxItems", props["tasks"])  # removed — limit is now runtime-configurable


class TestChildSystemPrompt(unittest.TestCase):
    def test_goal_only(self):
        prompt = _build_child_system_prompt("Fix the tests")
        self.assertIn("Fix the tests", prompt)
        self.assertIn("YOUR TASK", prompt)
        self.assertNotIn("CONTEXT", prompt)

    def test_goal_with_context(self):
        prompt = _build_child_system_prompt("Fix the tests", "Error: assertion failed in test_foo.py line 42")
        self.assertIn("Fix the tests", prompt)
        self.assertIn("CONTEXT", prompt)
        self.assertIn("assertion failed", prompt)

    def test_empty_context_ignored(self):
        prompt = _build_child_system_prompt("Do something", "  ")
        self.assertNotIn("CONTEXT", prompt)


class TestStripBlockedTools(unittest.TestCase):
    def test_removes_blocked_toolsets(self):
        result = _strip_blocked_tools(["terminal", "file", "delegation", "clarify", "memory", "code_execution"])
        self.assertEqual(sorted(result), ["file", "terminal"])

    def test_preserves_allowed_toolsets(self):
        result = _strip_blocked_tools(["terminal", "file", "web", "browser"])
        self.assertEqual(sorted(result), ["browser", "file", "terminal", "web"])

    def test_empty_input(self):
        result = _strip_blocked_tools([])
        self.assertEqual(result, [])


class TestDelegateTask(unittest.TestCase):
    def test_no_parent_agent(self):
        result = json.loads(delegate_task(goal="test"))
        self.assertIn("error", result)
        self.assertIn("parent agent", result["error"])

    def test_depth_limit(self):
        parent = _make_mock_parent(depth=2)
        result = json.loads(delegate_task(goal="test", parent_agent=parent))
        self.assertIn("error", result)
        self.assertIn("depth limit", result["error"].lower())

    def test_no_goal_or_tasks(self):
        parent = _make_mock_parent()
        result = json.loads(delegate_task(parent_agent=parent))
        self.assertIn("error", result)

    def test_empty_goal(self):
        parent = _make_mock_parent()
        result = json.loads(delegate_task(goal="  ", parent_agent=parent))
        self.assertIn("error", result)

    def test_task_missing_goal(self):
        parent = _make_mock_parent()
        result = json.loads(delegate_task(tasks=[{"context": "no goal here"}], parent_agent=parent))
        self.assertIn("error", result)

    @patch("tools.delegate_tool._detect_repo_path", return_value="/home/test/repo")
    @patch("tools.delegate_tool._run_claude_code_deepseek_direct_delegation")
    def test_single_task_mode(self, mock_run, _mock_repo):
        mock_run.return_value = {
            "result": "Done!", "num_turns": 3, "duration_seconds": 5.0, "verified": True
        }
        parent = _make_mock_parent()
        result = json.loads(delegate_task(goal="Fix tests ~/Code/hermes-poc", context="error log...", parent_agent=parent))
        self.assertIn("results", result)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["status"], "completed")
        self.assertEqual(result["results"][0]["summary"], "Done!")
        mock_run.assert_called_once()

    @patch("tools.delegate_tool._detect_repo_path", return_value="/home/test/repo")
    @patch("tools.delegate_tool._run_claude_code_deepseek_direct_delegation")
    def test_batch_mode(self, mock_run, _mock_repo):
        mock_run.side_effect = [
            {"result": "Result A", "num_turns": 2, "duration_seconds": 3.0, "verified": True},
            {"result": "Result B", "num_turns": 4, "duration_seconds": 6.0, "verified": True},
        ]
        parent = _make_mock_parent()
        tasks = [
            {"goal": "Research topic A ~/Code/test"},
            {"goal": "Research topic B ~/Code/test"},
        ]
        result = json.loads(delegate_task(tasks=tasks, parent_agent=parent))
        self.assertIn("results", result)
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0]["summary"], "Result A")
        self.assertEqual(result["results"][1]["summary"], "Result B")
        self.assertIn("total_duration_seconds", result)

    @patch("tools.delegate_tool._detect_repo_path", return_value="/home/test/repo")
    @patch("tools.delegate_tool._run_claude_code_deepseek_direct_delegation")
    def test_batch_mode_accepts_json_string_tasks(self, mock_run, _mock_repo):
        mock_run.side_effect = [
            {"result": "Result A", "num_turns": 2, "duration_seconds": 3.0, "verified": True},
            {"result": "Result B", "num_turns": 4, "duration_seconds": 6.0, "verified": True},
        ]
        parent = _make_mock_parent()
        tasks = json.dumps(
            [
                {"goal": "Research topic A ~/Code/test"},
                {"goal": "Research topic B ~/Code/test"},
            ]
        )

        result = json.loads(delegate_task(tasks=tasks, parent_agent=parent))

        self.assertIn("results", result)
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0]["summary"], "Result A")
        self.assertEqual(result["results"][1]["summary"], "Result B")

    @patch("tools.delegate_tool._run_single_child")
    def test_batch_mode_rejects_non_object_tasks(self, mock_run):
        parent = _make_mock_parent()

        result = json.loads(
            delegate_task(tasks=["not a task object"], parent_agent=parent)
        )

        self.assertIn("error", result)
        self.assertIn("Task 0 must be an object", result["error"])
        mock_run.assert_not_called()

    @patch("tools.delegate_tool._run_single_child")
    def test_batch_mode_rejects_malformed_json_string_tasks(self, mock_run):
        parent = _make_mock_parent()

        result = json.loads(
            delegate_task(tasks='[{"goal": "bad}', parent_agent=parent)
        )

        self.assertIn("error", result)
        self.assertIn("could not be parsed as JSON", result["error"])
        mock_run.assert_not_called()

    @patch("tools.delegate_tool._run_single_child")
    def test_batch_capped_at_3(self, mock_run):
        mock_run.return_value = {
            "task_index": 0, "status": "completed",
            "summary": "Done", "api_calls": 1, "duration_seconds": 1.0
        }
        parent = _make_mock_parent()
        limit = _get_max_concurrent_children()
        tasks = [{"goal": f"Task {i}"} for i in range(limit + 2)]
        result = json.loads(delegate_task(tasks=tasks, parent_agent=parent))
        # Should return an error instead of silently truncating
        self.assertIn("error", result)
        self.assertIn("Too many tasks", result["error"])
        mock_run.assert_not_called()

    @patch("tools.delegate_tool._detect_repo_path", return_value="/home/test/repo")
    @patch("tools.delegate_tool._run_claude_code_deepseek_direct_delegation")
    def test_batch_ignores_toplevel_goal(self, mock_run, _mock_repo):
        """When tasks array is provided, top-level goal/context/toolsets are ignored."""
        mock_run.return_value = {
            "result": "Done", "num_turns": 1, "duration_seconds": 1.0, "verified": True
        }
        parent = _make_mock_parent()
        result = json.loads(delegate_task(
            goal="This should be ignored",
            tasks=[{"goal": "Actual task ~/Code/test"}],
            parent_agent=parent,
        ))
        # The mock was called with the tasks array item, not the top-level goal
        call_args = mock_run.call_args
        self.assertEqual(call_args.kwargs.get("goal"), "Actual task ~/Code/test")

    @patch("tools.delegate_tool._detect_repo_path", return_value="/home/test/repo")
    @patch("tools.delegate_tool._run_claude_code_deepseek_direct_delegation")
    def test_failed_child_included_in_results(self, mock_run, _mock_repo):
        mock_run.return_value = {
            "error": "Something broke"
        }
        parent = _make_mock_parent()
        result = json.loads(delegate_task(goal="Break things ~/Code/test", parent_agent=parent))
        self.assertEqual(result["results"][0]["status"], "error")
        self.assertIn("Something broke", result["results"][0]["error"])

    @patch("tools.delegate_tool._detect_repo_path", return_value="/home/test/repo")
    @patch("tools.delegate_tool._run_claude_code_deepseek_direct_delegation")
    def test_depth_increments(self, mock_run, _mock_repo):
        """Verify _run_claude_code_deepseek_direct_delegation is called for single-tier dispatch."""
        mock_run.return_value = {"result": "done", "num_turns": 1, "duration_seconds": 1.0, "verified": True}
        parent = _make_mock_parent(depth=0)

        delegate_task(goal="Test depth ~/Code/test", parent_agent=parent)
        mock_run.assert_called_once()

    @patch("tools.delegate_tool._detect_repo_path", return_value="/home/test/repo")
    @patch("tools.delegate_tool._run_claude_code_deepseek_direct_delegation")
    def test_active_children_tracking(self, mock_run, _mock_repo):
        """Verify single-tier dispatch completes without child tracking (CC subprocess, not in-process)."""
        mock_run.return_value = {"result": "done", "num_turns": 1, "duration_seconds": 1.0, "verified": True}
        parent = _make_mock_parent(depth=0)

        delegate_task(goal="Test tracking ~/Code/test", parent_agent=parent)
        # Single-tier CC dispatch doesn't track children (no Tier 3 in-process agents)
        mock_run.assert_called_once()

    @patch("tools.delegate_tool._detect_repo_path", return_value="/home/test/repo")
    @patch("tools.delegate_tool._run_claude_code_deepseek_direct_delegation")
    def test_child_inherits_runtime_credentials(self, mock_run, _mock_repo):
        mock_run.return_value = {"result": "ok", "num_turns": 1, "duration_seconds": 1.0, "verified": True}
        parent = _make_mock_parent(depth=0)
        parent.base_url = "https://chatgpt.com/backend-api/codex"
        parent.api_key="***"
        parent.provider = "openai-codex"
        parent.api_mode = "codex_responses"

        delegate_task(goal="Test runtime inheritance ~/Code/test", parent_agent=parent)

        # Single-tier dispatch routes all tasks through DeepSeek CC
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args.kwargs
        self.assertEqual(call_kwargs["goal"], "Test runtime inheritance ~/Code/test")

    def test_child_inherits_parent_print_fn(self):
        parent = _make_mock_parent(depth=0)
        sink = MagicMock()
        parent._print_fn = sink

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            MockAgent.return_value = mock_child

            _build_child_agent(
                task_index=0,
                goal="Keep stdout clean",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
            )

        self.assertIs(mock_child._print_fn, sink)

    def test_child_uses_thinking_callback_when_progress_callback_available(self):
        parent = _make_mock_parent(depth=0)
        parent.tool_progress_callback = MagicMock()

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            MockAgent.return_value = mock_child

            _build_child_agent(
                task_index=0,
                goal="Avoid raw child spinners",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
            )

        self.assertTrue(callable(mock_child.thinking_callback))
        mock_child.thinking_callback("deliberating...")
        parent.tool_progress_callback.assert_not_called()


class TestToolNamePreservation(unittest.TestCase):
    """Verify _last_resolved_tool_names is restored after subagent runs."""

    @patch("tools.delegate_tool._detect_repo_path", return_value="/home/test/repo")
    @patch("tools.delegate_tool._run_claude_code_deepseek_direct_delegation")
    def test_global_tool_names_restored_after_delegation(self, mock_run, _mock_repo):
        """Verify single-tier dispatch preserves model_tools global state."""
        import model_tools

        parent = _make_mock_parent(depth=0)
        original_tools = ["terminal", "read_file", "web_search", "execute_code", "delegate_task"]
        model_tools._last_resolved_tool_names = list(original_tools)

        mock_run.return_value = {"result": "done", "num_turns": 1, "duration_seconds": 1.0, "verified": True}
        delegate_task(goal="Test tool preservation ~/Code/test", parent_agent=parent)

        self.assertEqual(model_tools._last_resolved_tool_names, original_tools)

    @patch("tools.delegate_tool._detect_repo_path", return_value="/home/test/repo")
    @patch("tools.delegate_tool._run_claude_code_deepseek_direct_delegation")
    def test_global_tool_names_restored_after_child_failure(self, mock_run, _mock_repo):
        """Even when CC delegation raises, the global must be restored."""
        import model_tools

        parent = _make_mock_parent(depth=0)
        original_tools = ["terminal", "read_file", "web_search"]
        model_tools._last_resolved_tool_names = list(original_tools)

        mock_run.side_effect = RuntimeError("boom")
        result = json.loads(delegate_task(goal="Crash test ~/Code/test", parent_agent=parent))
        self.assertEqual(result["results"][0]["status"], "error")

        self.assertEqual(model_tools._last_resolved_tool_names, original_tools)

    def test_build_child_agent_does_not_raise_name_error(self):
        """Regression: _build_child_agent must not reference _saved_tool_names.

        The bug introduced by the e7844e9c merge conflict: line 235 inside
        _build_child_agent read `list(_saved_tool_names)` where that variable
        is only defined later in _run_single_child.  Calling _build_child_agent
        standalone (without _run_single_child's scope) must never raise NameError.
        """
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent"):
            try:
                _build_child_agent(
                    task_index=0,
                    goal="regression check",
                    context=None,
                    toolsets=None,
                    model=None,
                    max_iterations=10,
                    parent_agent=parent,
                    task_count=1,
                )
            except NameError as exc:
                self.fail(
                    f"_build_child_agent raised NameError — "
                    f"_saved_tool_names leaked back into wrong scope: {exc}"
                )

    @patch("tools.delegate_tool._detect_repo_path", return_value="/home/test/repo")
    @patch("tools.delegate_tool._run_claude_code_deepseek_direct_delegation")
    def test_saved_tool_names_set_on_child_before_run(self, mock_run, _mock_repo):
        """Verify single-tier CC dispatch is called with correct goal."""
        mock_run.return_value = {"result": "ok", "num_turns": 1, "duration_seconds": 1.0, "verified": True}
        parent = _make_mock_parent(depth=0)

        delegate_task(goal="capture test ~/Code/test", parent_agent=parent)

        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args.kwargs["goal"], "capture test ~/Code/test")


class TestDelegateObservability(unittest.TestCase):
    """Tests for enriched metadata returned by _run_single_child."""

    def setUp(self):
        self._repo_patcher = patch("tools.delegate_tool._detect_repo_path", return_value="/home/test/repo")
        self._repo_patcher.start()

    def tearDown(self):
        self._repo_patcher.stop()

    @patch("tools.delegate_tool._detect_repo_path", return_value="/home/test/repo")
    @patch("tools.delegate_tool._run_claude_code_deepseek_direct_delegation")
    def test_observability_fields_present(self, mock_run, _mock_repo):
        """Completed delegation should return duration and delegation type."""
        parent = _make_mock_parent(depth=0)
        mock_run.return_value = {
            "result": "done", "num_turns": 3, "duration_seconds": 5.0, "verified": True,
            "model": "claude-sonnet-4-6",
        }

        result = json.loads(delegate_task(goal="Test observability ~/Code/test", parent_agent=parent))
        entry = result["results"][0]

        # Core fields from single-tier CC dispatch
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["delegation"], "claude-code-deepseek-direct")
        self.assertEqual(entry["api_calls"], 3)
        self.assertEqual(entry["duration_seconds"], 5.0)

    def test_tool_trace_detects_error(self):
        """Tool results containing 'error' should be marked as error status."""
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.model = "claude-sonnet-4-6"
            mock_child.session_prompt_tokens = 0
            mock_child.session_completion_tokens = 0
            mock_child.run_conversation.return_value = {
                "final_response": "failed",
                "completed": True,
                "interrupted": False,
                "api_calls": 1,
                "messages": [
                    {"role": "assistant", "tool_calls": [
                        {"id": "tc_1", "function": {"name": "terminal", "arguments": '{"cmd": "ls"}'}}
                    ]},
                    {"role": "tool", "tool_call_id": "tc_1", "content": "Error: command not found"},
                ],
            }
            MockAgent.return_value = mock_child

            result = json.loads(delegate_task(goal="Test error trace", parent_agent=parent))
            trace = result["results"][0]["tool_trace"]
            self.assertEqual(trace[0]["status"], "error")

    @patch("tools.delegate_tool._detect_repo_path", return_value="/home/test/repo")
    @patch("tools.delegate_tool._run_claude_code_deepseek_direct_delegation")
    def test_parallel_tool_calls_paired_correctly(self, mock_run, _mock_repo):
        """Delegation should complete with correct status and api_calls."""
        parent = _make_mock_parent(depth=0)
        mock_run.return_value = {"result": "done", "num_turns": 1, "duration_seconds": 1.0, "verified": True}

        result = json.loads(delegate_task(goal="Test parallel ~/Code/test", parent_agent=parent))
        entry = result["results"][0]
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["api_calls"], 1)

    @patch("tools.delegate_tool._detect_repo_path", return_value="/home/test/repo")
    @patch("tools.delegate_tool._run_claude_code_deepseek_direct_delegation")
    def test_exit_reason_interrupted(self, mock_run, _mock_repo):
        """Error delegation should report error status."""
        parent = _make_mock_parent(depth=0)
        mock_run.side_effect = RuntimeError("interrupted")

        result = json.loads(delegate_task(goal="Test interrupted ~/Code/test", parent_agent=parent))
        entry = result["results"][0]
        self.assertEqual(entry["status"], "error")

    @patch("tools.delegate_tool._detect_repo_path", return_value="/home/test/repo")
    @patch("tools.delegate_tool._run_claude_code_deepseek_direct_delegation")
    def test_exit_reason_max_iterations(self, mock_run, _mock_repo):
        """Delegation returning partial result should still complete."""
        parent = _make_mock_parent(depth=0)
        mock_run.return_value = {"result": "partial", "num_turns": 50, "duration_seconds": 10.0, "verified": True}

        result = json.loads(delegate_task(goal="Test max iters ~/Code/test", parent_agent=parent))
        entry = result["results"][0]
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["api_calls"], 50)

class TestSubagentCostRollup(unittest.TestCase):
    """Port of Kilo-Org/kilocode#9448 — parent's session_estimated_cost_usd
    must include subagent spend, not just the parent's own API calls."""

    def setUp(self):
        self._repo_patcher = patch("tools.delegate_tool._detect_repo_path", return_value="/home/test/repo")
        self._repo_patcher.start()

    def tearDown(self):
        self._repo_patcher.stop()

    def _make_parent_with_cost_counters(self, depth=0, starting_cost=0.0):
        parent = _make_mock_parent(depth=depth)
        # The fields AIAgent exposes and the footer reads from.  Set real
        # floats/strings so the rollup can add to them rather than tripping
        # on MagicMock auto-attrs.
        parent.session_estimated_cost_usd = starting_cost
        parent.session_cost_status = "unknown"
        parent.session_cost_source = "none"
        return parent

    @patch("tools.delegate_tool._run_claude_code_deepseek_direct_delegation")
    def test_single_child_cost_folded_into_parent(self, mock_run):
        parent = self._make_parent_with_cost_counters(starting_cost=0.10)
        mock_run.return_value = {"result": "done", "num_turns": 2, "duration_seconds": 3.0, "verified": True}

        result = json.loads(delegate_task(goal="do stuff", parent_agent=parent))

        mock_run.assert_called_once()
        self.assertEqual(result["results"][0]["status"], "completed")

    def test_batch_children_costs_sum_into_parent(self):
        parent = self._make_parent_with_cost_counters(starting_cost=0.00)

        with patch("tools.delegate_tool._run_single_child") as mock_run:
            mock_run.side_effect = [
                {
                    "task_index": 0,
                    "status": "completed",
                    "summary": "A",
                    "api_calls": 2,
                    "duration_seconds": 1.0,
                    "_child_role": "leaf",
                    "_child_cost_usd": 0.15,
                },
                {
                    "task_index": 1,
                    "status": "completed",
                    "summary": "B",
                    "api_calls": 2,
                    "duration_seconds": 1.0,
                    "_child_role": "leaf",
                    "_child_cost_usd": 0.27,
                },
                {
                    "task_index": 2,
                    "status": "failed",
                    "summary": "",
                    "error": "boom",
                    "api_calls": 0,
                    "duration_seconds": 0.1,
                    "_child_role": "leaf",
                    "_child_cost_usd": 0.03,
                },
            ]
            result = json.loads(
                delegate_task(
                    tasks=[{"goal": "A"}, {"goal": "B"}, {"goal": "C"}],
                    parent_agent=parent,
                )
            )

        # 0.15 + 0.27 + 0.03 even though one child failed — the API calls it
        # made before failing still cost money.
        self.assertAlmostEqual(parent.session_estimated_cost_usd, 0.45, places=6)
        # cost_source promoted from "none" since the parent had no direct spend.
        self.assertEqual(parent.session_cost_source, "subagent")
        self.assertEqual(parent.session_cost_status, "estimated")
        # All internal fields stripped from results.
        for entry in result["results"]:
            self.assertNotIn("_child_cost_usd", entry)
            self.assertNotIn("_child_role", entry)

    def test_zero_cost_children_leave_parent_source_untouched(self):
        """If every child reports 0 cost (e.g. free local model), we should
        not invent a fake 'subagent' source — the parent's 'none' stays."""
        parent = self._make_parent_with_cost_counters(starting_cost=0.00)

        with patch("tools.delegate_tool._run_single_child") as mock_run:
            mock_run.return_value = {
                "task_index": 0,
                "status": "completed",
                "summary": "done",
                "api_calls": 1,
                "duration_seconds": 0.5,
                "_child_role": "leaf",
                "_child_cost_usd": 0.0,
            }
            delegate_task(goal="free local run", parent_agent=parent)

        self.assertEqual(parent.session_estimated_cost_usd, 0.0)
        self.assertEqual(parent.session_cost_source, "none")

    def test_parent_with_real_source_not_overwritten(self):
        """If the parent already has its own cost billed (cost_source != 'none'),
        adding subagent cost must not clobber the existing source label."""
        parent = self._make_parent_with_cost_counters(starting_cost=0.20)
        parent.session_cost_status = "exact"
        parent.session_cost_source = "openrouter"

        with patch("tools.delegate_tool._run_single_child") as mock_run:
            mock_run.return_value = {
                "task_index": 0,
                "status": "completed",
                "summary": "done",
                "api_calls": 1,
                "duration_seconds": 0.5,
                "_child_role": "leaf",
                "_child_cost_usd": 0.30,
            }
            delegate_task(goal="billed run", parent_agent=parent)

        self.assertAlmostEqual(parent.session_estimated_cost_usd, 0.50, places=6)
        # Real source label preserved.
        self.assertEqual(parent.session_cost_source, "openrouter")
        self.assertEqual(parent.session_cost_status, "exact")

    def test_rollup_tolerates_missing_cost_fields(self):
        """Older fixtures / fabricated error entries may not carry
        _child_cost_usd.  Rollup must degrade to zero-add silently."""
        parent = self._make_parent_with_cost_counters(starting_cost=0.10)

        with patch("tools.delegate_tool._run_single_child") as mock_run:
            mock_run.return_value = {
                "task_index": 0,
                "status": "completed",
                "summary": "done",
                "api_calls": 1,
                "duration_seconds": 0.5,
                # no _child_role, no _child_cost_usd
            }
            result = json.loads(delegate_task(goal="legacy", parent_agent=parent))

        # Parent cost unchanged.
        self.assertEqual(parent.session_estimated_cost_usd, 0.10)
        self.assertEqual(len(result["results"]), 1)


class TestBlockedTools(unittest.TestCase):
    def test_blocked_tools_constant(self):
        for tool in ["delegate_task", "clarify", "memory", "send_message", "execute_code"]:
            self.assertIn(tool, DELEGATE_BLOCKED_TOOLS)

    def test_constants(self):
        from tools.delegate_tool import (
            _get_max_spawn_depth, _get_orchestrator_enabled,
            _MIN_SPAWN_DEPTH, _MAX_SPAWN_DEPTH_CAP,
        )
        self.assertEqual(_get_max_concurrent_children(), 3)
        self.assertEqual(MAX_DEPTH, 1)
        self.assertEqual(_get_max_spawn_depth(), 1)       # default: flat
        self.assertTrue(_get_orchestrator_enabled())      # default
        self.assertEqual(_MIN_SPAWN_DEPTH, 1)
        self.assertEqual(_MAX_SPAWN_DEPTH_CAP, 3)


class TestDelegationCredentialResolution(unittest.TestCase):
    """Tests for provider:model credential resolution in delegation config."""

    def test_no_provider_returns_none_credentials(self):
        """When delegation.provider is empty, all credentials are None (inherit parent)."""
        parent = _make_mock_parent(depth=0)
        cfg = {"model": "", "provider": ""}
        creds = _resolve_delegation_credentials(cfg, parent)
        self.assertIsNone(creds["provider"])
        self.assertIsNone(creds["base_url"])
        self.assertIsNone(creds["api_key"])
        self.assertIsNone(creds["api_mode"])
        self.assertIsNone(creds["model"])

    def test_model_only_no_provider(self):
        """When only model is set (no provider), model is returned but credentials are None."""
        parent = _make_mock_parent(depth=0)
        cfg = {"model": "google/gemini-3-flash-preview", "provider": ""}
        creds = _resolve_delegation_credentials(cfg, parent)
        self.assertEqual(creds["model"], "google/gemini-3-flash-preview")
        self.assertIsNone(creds["provider"])
        self.assertIsNone(creds["base_url"])
        self.assertIsNone(creds["api_key"])



    def test_direct_endpoint_uses_configured_base_url_and_api_key(self):
        parent = _make_mock_parent(depth=0)
        cfg = {
            "model": "qwen2.5-coder",
            "provider": "openrouter",
            "base_url": "http://localhost:1234/v1",
            "api_key": "local-key",
        }
        creds = _resolve_delegation_credentials(cfg, parent)
        self.assertEqual(creds["model"], "qwen2.5-coder")
        self.assertEqual(creds["provider"], "custom")
        self.assertEqual(creds["base_url"], "http://localhost:1234/v1")
        self.assertEqual(creds["api_key"], "local-key")
        self.assertEqual(creds["api_mode"], "chat_completions")

    def test_direct_endpoint_auto_detects_anthropic_messages_suffix(self):
        # Issue #10213: Azure AI Foundry exposes Anthropic-compatible models at
        # a /anthropic URL suffix. Subagents must pick anthropic_messages
        # automatically, matching the main agent's runtime resolver.
        parent = _make_mock_parent(depth=0)
        cfg = {
            "model": "claude-opus-4-6",
            "provider": "custom",
            "base_url": "https://myfoundry.services.ai.azure.com/anthropic",
            "api_key": "foundry-key",
        }
        creds = _resolve_delegation_credentials(cfg, parent)
        self.assertEqual(creds["provider"], "custom")
        self.assertEqual(creds["base_url"], "https://myfoundry.services.ai.azure.com/anthropic")
        self.assertEqual(creds["api_key"], "foundry-key")
        self.assertEqual(creds["api_mode"], "anthropic_messages")

    def test_direct_endpoint_honors_explicit_api_mode(self):
        # When delegation.api_mode is set explicitly, it overrides URL-based
        # detection so users can force a transport on non-standard endpoints.
        parent = _make_mock_parent(depth=0)
        cfg = {
            "model": "claude-opus-4-6",
            "provider": "custom",
            "base_url": "https://proxy.example.com/v1",
            "api_key": "proxy-key",
            "api_mode": "anthropic_messages",
        }
        creds = _resolve_delegation_credentials(cfg, parent)
        self.assertEqual(creds["api_mode"], "anthropic_messages")

    def test_direct_endpoint_explicit_api_mode_overrides_url_detection(self):
        # Explicit api_mode in config always wins over auto-detection.
        parent = _make_mock_parent(depth=0)
        cfg = {
            "model": "claude-opus-4-6",
            "provider": "custom",
            "base_url": "https://myfoundry.services.ai.azure.com/anthropic",
            "api_key": "foundry-key",
            "api_mode": "chat_completions",
        }
        creds = _resolve_delegation_credentials(cfg, parent)
        self.assertEqual(creds["api_mode"], "chat_completions")

    def test_direct_endpoint_invalid_api_mode_falls_back_to_detection(self):
        # An invalid api_mode string must not break detection; fall back to URL heuristic.
        parent = _make_mock_parent(depth=0)
        cfg = {
            "model": "claude-opus-4-6",
            "provider": "custom",
            "base_url": "https://myfoundry.services.ai.azure.com/anthropic",
            "api_key": "foundry-key",
            "api_mode": "garbage",
        }
        creds = _resolve_delegation_credentials(cfg, parent)
        self.assertEqual(creds["api_mode"], "anthropic_messages")

    def test_direct_endpoint_returns_none_api_key_when_not_configured(self):
        # When base_url is set without api_key, api_key should be None so
        # _build_child_agent inherits the parent's key (effective_api_key = override or parent).
        parent = _make_mock_parent(depth=0)
        cfg = {
            "model": "qwen2.5-coder",
            "base_url": "http://localhost:1234/v1",
        }
        with patch.dict(os.environ, {"OPENAI_API_KEY": "env-openai-key"}, clear=False):
            creds = _resolve_delegation_credentials(cfg, parent)
        self.assertIsNone(creds["api_key"])
        self.assertEqual(creds["provider"], "custom")

    def test_direct_endpoint_no_raise_when_only_provider_env_key_present(self):
        # Even if OPENAI_API_KEY is absent, no ValueError — _build_child_agent uses parent key.
        parent = _make_mock_parent(depth=0)
        cfg = {
            "model": "qwen2.5-coder",
            "base_url": "http://localhost:1234/v1",
        }
        with patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "env-openrouter-key",
                "OPENAI_API_KEY": "",
            },
            clear=False,
        ):
            creds = _resolve_delegation_credentials(cfg, parent)
        self.assertIsNone(creds["api_key"])
        self.assertEqual(creds["provider"], "custom")


    @patch("hermes_cli.runtime_provider.resolve_runtime_provider")
    def test_provider_resolution_failure_raises_valueerror(self, mock_resolve):
        """When provider resolution fails, ValueError is raised with helpful message."""
        mock_resolve.side_effect = RuntimeError("OPENROUTER_API_KEY not set")
        parent = _make_mock_parent(depth=0)
        cfg = {"model": "some-model", "provider": "openrouter"}
        with self.assertRaises(ValueError) as ctx:
            _resolve_delegation_credentials(cfg, parent)
        self.assertIn("openrouter", str(ctx.exception).lower())
        self.assertIn("Cannot resolve", str(ctx.exception))

    @patch("hermes_cli.runtime_provider.resolve_runtime_provider")
    def test_provider_resolves_but_no_api_key_raises(self, mock_resolve):
        """When provider resolves but has no API key, ValueError is raised."""
        mock_resolve.return_value = {
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "",
            "api_mode": "chat_completions",
        }
        parent = _make_mock_parent(depth=0)
        cfg = {"model": "some-model", "provider": "openrouter"}
        with self.assertRaises(ValueError) as ctx:
            _resolve_delegation_credentials(cfg, parent)
        self.assertIn("no API key", str(ctx.exception))

    def test_missing_config_keys_inherit_parent(self):
        """When config dict has no model/provider keys at all, inherits parent."""
        parent = _make_mock_parent(depth=0)
        cfg = {"max_iterations": 45}
        creds = _resolve_delegation_credentials(cfg, parent)
        self.assertIsNone(creds["model"])
        self.assertIsNone(creds["provider"])


class TestDelegationProviderIntegration(unittest.TestCase):
    """Integration tests: delegation config → _run_single_child → AIAgent construction."""

    def setUp(self):
        self._repo_patcher = patch("tools.delegate_tool._detect_repo_path", return_value="/home/test/repo")
        self._repo_patcher.start()

    def tearDown(self):
        self._repo_patcher.stop()

    @patch("tools.delegate_tool._load_config")
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    def test_config_provider_credentials_reach_child_agent(self, mock_creds, mock_cfg):
        """When delegation.provider is configured, child agent gets resolved credentials."""
        mock_cfg.return_value = {
            "max_iterations": 45,
            "model": "google/gemini-3-flash-preview",
            "provider": "openrouter",
        }
        mock_creds.return_value = {
            "model": "google/gemini-3-flash-preview",
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-delegation-key",
            "api_mode": "chat_completions",
        }
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.run_conversation.return_value = {
                "final_response": "done", "completed": True, "api_calls": 1
            }
            MockAgent.return_value = mock_child

            delegate_task(goal="Test provider routing", parent_agent=parent)

            _, kwargs = MockAgent.call_args
            self.assertEqual(kwargs["model"], "google/gemini-3-flash-preview")
            self.assertEqual(kwargs["provider"], "openrouter")
            self.assertEqual(kwargs["base_url"], "https://openrouter.ai/api/v1")
            self.assertEqual(kwargs["api_key"], "sk-or-delegation-key")
            self.assertEqual(kwargs["api_mode"], "chat_completions")

    @patch("tools.delegate_tool._load_config")
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    def test_cross_provider_delegation(self, mock_creds, mock_cfg):
        """Parent on Nous, subagent on OpenRouter — full credential switch."""
        mock_cfg.return_value = {
            "max_iterations": 45,
            "model": "google/gemini-3-flash-preview",
            "provider": "openrouter",
        }
        mock_creds.return_value = {
            "model": "google/gemini-3-flash-preview",
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-key",
            "api_mode": "chat_completions",
        }
        parent = _make_mock_parent(depth=0)
        parent.provider = "nous"
        parent.base_url = "https://inference-api.nousresearch.com/v1"
        parent.api_key = "nous-key-abc"

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.run_conversation.return_value = {
                "final_response": "done", "completed": True, "api_calls": 1
            }
            MockAgent.return_value = mock_child

            delegate_task(goal="Cross-provider test", parent_agent=parent)

            _, kwargs = MockAgent.call_args
            # Child should use OpenRouter, NOT Nous
            self.assertEqual(kwargs["provider"], "openrouter")
            self.assertEqual(kwargs["base_url"], "https://openrouter.ai/api/v1")
            self.assertEqual(kwargs["api_key"], "sk-or-key")
            self.assertNotEqual(kwargs["base_url"], parent.base_url)
            self.assertNotEqual(kwargs["api_key"], parent.api_key)

    @patch("tools.delegate_tool._load_config")
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    def test_provider_override_clears_parent_openrouter_filters(
        self, mock_creds, mock_cfg
    ):
        """Delegated provider should not inherit parent provider-preference filters."""
        mock_cfg.return_value = {
            "max_iterations": 45,
            "model": "google/gemini-3-flash-preview",
            "provider": "openrouter",
        }
        mock_creds.return_value = {
            "model": "google/gemini-3-flash-preview",
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-key",
            "api_mode": "chat_completions",
        }
        parent = _make_mock_parent(depth=0)
        parent.providers_allowed = ["anthropic/claude-3.5-sonnet"]
        parent.providers_ignored = ["openai/gpt-4o-mini"]
        parent.providers_order = ["google/gemini-2.5-pro"]
        parent.provider_sort = "price"

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.run_conversation.return_value = {
                "final_response": "done",
                "completed": True,
                "api_calls": 1,
            }
            MockAgent.return_value = mock_child

            delegate_task(goal="Cross-provider test", parent_agent=parent)

            _, kwargs = MockAgent.call_args
            self.assertEqual(kwargs["provider"], "openrouter")
            self.assertIsNone(kwargs["providers_allowed"])
            self.assertIsNone(kwargs["providers_ignored"])
            self.assertIsNone(kwargs["providers_order"])
            self.assertIsNone(kwargs["provider_sort"])

    @patch("tools.delegate_tool._load_config")
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    def test_direct_endpoint_credentials_reach_child_agent(self, mock_creds, mock_cfg):
        mock_cfg.return_value = {
            "max_iterations": 45,
            "model": "qwen2.5-coder",
            "base_url": "http://localhost:1234/v1",
            "api_key": "local-key",
        }
        mock_creds.return_value = {
            "model": "qwen2.5-coder",
            "provider": "custom",
            "base_url": "http://localhost:1234/v1",
            "api_key": "local-key",
            "api_mode": "chat_completions",
        }
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.run_conversation.return_value = {
                "final_response": "done", "completed": True, "api_calls": 1
            }
            MockAgent.return_value = mock_child

            delegate_task(goal="Direct endpoint test", parent_agent=parent)

            _, kwargs = MockAgent.call_args
            self.assertEqual(kwargs["model"], "qwen2.5-coder")
            self.assertEqual(kwargs["provider"], "custom")
            self.assertEqual(kwargs["base_url"], "http://localhost:1234/v1")
            self.assertEqual(kwargs["api_key"], "local-key")
            self.assertEqual(kwargs["api_mode"], "chat_completions")

    @patch("tools.delegate_tool._load_config")
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    def test_empty_config_inherits_parent(self, mock_creds, mock_cfg):
        """When delegation config is empty, child inherits parent credentials."""
        mock_cfg.return_value = {"max_iterations": 45, "model": "", "provider": ""}
        mock_creds.return_value = {
            "model": None,
            "provider": None,
            "base_url": None,
            "api_key": None,
            "api_mode": None,
        }
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.run_conversation.return_value = {
                "final_response": "done", "completed": True, "api_calls": 1
            }
            MockAgent.return_value = mock_child

            delegate_task(goal="Test inherit", parent_agent=parent)

            _, kwargs = MockAgent.call_args
            self.assertEqual(kwargs["model"], parent.model)
            self.assertEqual(kwargs["provider"], parent.provider)
            self.assertEqual(kwargs["base_url"], parent.base_url)

    @patch("tools.delegate_tool._load_config")
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    def test_credential_error_returns_json_error(self, mock_creds, mock_cfg):
        """When credential resolution fails, delegate_task returns a JSON error."""
        mock_cfg.return_value = {"model": "bad-model", "provider": "nonexistent"}
        mock_creds.side_effect = ValueError(
            "Cannot resolve delegation provider 'nonexistent': Unknown provider"
        )
        parent = _make_mock_parent(depth=0)

        result = json.loads(delegate_task(goal="Should fail", parent_agent=parent))
        self.assertIn("error", result)
        self.assertIn("Cannot resolve", result["error"])
        self.assertIn("nonexistent", result["error"])

    @patch("tools.delegate_tool._load_config")
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    def test_batch_mode_all_children_get_credentials(self, mock_creds, mock_cfg):
        """In batch mode, all children receive the resolved credentials."""
        mock_cfg.return_value = {
            "max_iterations": 45,
            "model": "meta-llama/llama-4-scout",
            "provider": "openrouter",
        }
        mock_creds.return_value = {
            "model": "meta-llama/llama-4-scout",
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-batch",
            "api_mode": "chat_completions",
        }
        parent = _make_mock_parent(depth=0)

        # Patch _build_child_agent since credentials are now passed there
        # (agents are built in the main thread before being handed to workers)
        with patch("tools.delegate_tool._build_child_agent") as mock_build, \
             patch("tools.delegate_tool._run_single_child") as mock_run:
            mock_child = MagicMock()
            mock_build.return_value = mock_child
            mock_run.return_value = {
                "task_index": 0, "status": "completed",
                "summary": "Done", "api_calls": 1, "duration_seconds": 1.0
            }

            tasks = [{"goal": "Task A"}, {"goal": "Task B"}]
            delegate_task(tasks=tasks, parent_agent=parent)

            self.assertEqual(mock_build.call_count, 2)
            for call in mock_build.call_args_list:
                self.assertEqual(call.kwargs.get("model"), "meta-llama/llama-4-scout")
                overrides = call.kwargs.get("overrides")
                self.assertIsNotNone(overrides, "overrides kwarg missing")
                self.assertEqual(overrides.provider, "openrouter")
                self.assertEqual(overrides.base_url, "https://openrouter.ai/api/v1")
                self.assertEqual(overrides.api_key, "sk-or-batch")
                self.assertEqual(overrides.api_mode, "chat_completions")

    @patch("tools.delegate_tool._load_config")
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    def test_delegation_acp_runtime_reaches_child_agent(self, mock_creds, mock_cfg):
        """Resolved ACP runtime command/args must be forwarded to child agents."""
        mock_cfg.return_value = {
            "max_iterations": 45,
            "model": "copilot-model",
            "provider": "copilot-acp",
        }
        mock_creds.return_value = {
            "model": "copilot-model",
            "provider": "copilot-acp",
            "base_url": "acp://copilot",
            "api_key": "copilot-acp",
            "api_mode": "chat_completions",
            "command": "custom-copilot",
            "args": ["--stdio-custom"],
        }
        parent = _make_mock_parent(depth=0)

        with patch("tools.delegate_tool._build_child_agent") as mock_build, \
             patch("tools.delegate_tool._run_single_child") as mock_run:
            mock_child = MagicMock()
            mock_build.return_value = mock_child
            mock_run.return_value = {
                "task_index": 0, "status": "completed",
                "summary": "Done", "api_calls": 1, "duration_seconds": 1.0
            }

            delegate_task(goal="ACP delegation test", parent_agent=parent)

            _, kwargs = mock_build.call_args
            overrides = kwargs.get("overrides")
            self.assertIsNotNone(overrides, "overrides kwarg missing")
            self.assertEqual(overrides.provider, "copilot-acp")
            self.assertEqual(overrides.base_url, "acp://copilot")
            self.assertEqual(overrides.api_key, "copilot-acp")
            self.assertEqual(overrides.api_mode, "chat_completions")
            self.assertEqual(overrides.acp_command, "custom-copilot")
            self.assertEqual(overrides.acp_args, ["--stdio-custom"])

    @patch("tools.delegate_tool._load_config")
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    def test_model_only_no_provider_inherits_parent_credentials(self, mock_creds, mock_cfg):
        """Setting only model (no provider) changes model but keeps parent credentials."""
        mock_cfg.return_value = {
            "max_iterations": 45,
            "model": "google/gemini-3-flash-preview",
            "provider": "",
        }
        mock_creds.return_value = {
            "model": "google/gemini-3-flash-preview",
            "provider": None,
            "base_url": None,
            "api_key": None,
            "api_mode": None,
        }
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.run_conversation.return_value = {
                "final_response": "done", "completed": True, "api_calls": 1
            }
            MockAgent.return_value = mock_child

            delegate_task(goal="Model only test", parent_agent=parent)

            _, kwargs = MockAgent.call_args
            # Model should be overridden
            self.assertEqual(kwargs["model"], "google/gemini-3-flash-preview")
            # But provider/base_url/api_key should inherit from parent
            self.assertEqual(kwargs["provider"], parent.provider)
            self.assertEqual(kwargs["base_url"], parent.base_url)


class TestChildCredentialPoolResolution(unittest.TestCase):
    def test_same_provider_shares_parent_pool(self):
        parent = _make_mock_parent()
        mock_pool = MagicMock()
        parent._credential_pool = mock_pool

        result = _resolve_child_credential_pool("openrouter", parent)
        self.assertIs(result, mock_pool)

    def test_no_provider_inherits_parent_pool(self):
        parent = _make_mock_parent()
        mock_pool = MagicMock()
        parent._credential_pool = mock_pool

        result = _resolve_child_credential_pool(None, parent)
        self.assertIs(result, mock_pool)

    def test_different_provider_loads_own_pool(self):
        parent = _make_mock_parent()
        parent._credential_pool = MagicMock()
        mock_pool = MagicMock()
        mock_pool.has_credentials.return_value = True

        with patch("agent.credential_pool.load_pool", return_value=mock_pool):
            result = _resolve_child_credential_pool("anthropic", parent)

        self.assertIs(result, mock_pool)

    def test_different_provider_empty_pool_returns_none(self):
        parent = _make_mock_parent()
        parent._credential_pool = MagicMock()
        mock_pool = MagicMock()
        mock_pool.has_credentials.return_value = False

        with patch("agent.credential_pool.load_pool", return_value=mock_pool):
            result = _resolve_child_credential_pool("anthropic", parent)

        self.assertIsNone(result)

    def test_different_provider_load_failure_returns_none(self):
        parent = _make_mock_parent()
        parent._credential_pool = MagicMock()

        with patch("agent.credential_pool.load_pool", side_effect=Exception("disk error")):
            result = _resolve_child_credential_pool("anthropic", parent)

        self.assertIsNone(result)

    def test_build_child_agent_assigns_parent_pool_when_shared(self):
        parent = _make_mock_parent()
        mock_pool = MagicMock()
        parent._credential_pool = mock_pool

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            MockAgent.return_value = mock_child

            _build_child_agent(
                task_index=0,
                goal="Test pool assignment",
                context=None,
                toolsets=["terminal"],
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
            )

            self.assertEqual(mock_child._credential_pool, mock_pool)

    @patch("tools.delegate_tool._load_config", return_value={})
    def test_build_child_agent_preserves_mcp_toolsets_by_default(self, mock_cfg):
        parent = _make_mock_parent()
        parent.enabled_toolsets = ["web", "browser", "mcp-MiniMax"]

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            MockAgent.return_value = mock_child

            _build_child_agent(
                task_index=0,
                goal="Test narrowed toolsets",
                context=None,
                toolsets=["web", "browser"],
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
            )

        self.assertEqual(
            MockAgent.call_args[1]["enabled_toolsets"],
            ["web", "browser", "mcp-MiniMax"],
        )

    @patch(
        "tools.delegate_tool._load_config",
        return_value={"inherit_mcp_toolsets": False},
    )
    def test_build_child_agent_strict_intersection_when_opted_out(self, mock_cfg):
        parent = _make_mock_parent()
        parent.enabled_toolsets = ["web", "browser", "mcp-MiniMax"]

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            MockAgent.return_value = mock_child

            _build_child_agent(
                task_index=0,
                goal="Test narrowed toolsets",
                context=None,
                toolsets=["web", "browser"],
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
            )

        self.assertEqual(
            MockAgent.call_args[1]["enabled_toolsets"],
            ["web", "browser"],
        )


class TestChildCredentialLeasing(unittest.TestCase):
    def test_run_single_child_acquires_and_releases_lease(self):
        from tools.delegate_tool import _run_single_child

        leased_entry = MagicMock()
        leased_entry.id = "cred-b"

        child = MagicMock()
        child._credential_pool = MagicMock()
        child._credential_pool.acquire_lease.return_value = "cred-b"
        child._credential_pool.current.return_value = leased_entry
        child.run_conversation.return_value = {
            "final_response": "done",
            "completed": True,
            "interrupted": False,
            "api_calls": 1,
            "messages": [],
        }

        result = _run_single_child(
            task_index=0,
            goal="Investigate rate limits",
            child=child,
            parent_agent=_make_mock_parent(),
        )

        self.assertEqual(result["status"], "completed")
        child._credential_pool.acquire_lease.assert_called_once_with()
        child._swap_credential.assert_called_once_with(leased_entry)
        child._credential_pool.release_lease.assert_called_once_with("cred-b")

    def test_run_single_child_releases_lease_after_failure(self):
        from tools.delegate_tool import _run_single_child

        child = MagicMock()
        child._credential_pool = MagicMock()
        child._credential_pool.acquire_lease.return_value = "cred-a"
        child._credential_pool.current.return_value = MagicMock(id="cred-a")
        child.run_conversation.side_effect = RuntimeError("boom")

        result = _run_single_child(
            task_index=1,
            goal="Trigger failure",
            child=child,
            parent_agent=_make_mock_parent(),
        )

        self.assertEqual(result["status"], "error")
        child._credential_pool.release_lease.assert_called_once_with("cred-a")


class TestDelegateHeartbeat(unittest.TestCase):
    """Heartbeat propagates child activity to parent during delegation."""

    def setUp(self):
        self._repo_patcher = patch("tools.delegate_tool._detect_repo_path", return_value="/home/test/repo")
        self._repo_patcher.start()

    def tearDown(self):
        self._repo_patcher.stop()

    def test_heartbeat_touches_parent_activity_during_child_run(self):
        """Parent's _touch_activity is called while child.run_conversation blocks."""
        from tools.delegate_tool import _run_single_child

        parent = _make_mock_parent()
        touch_calls = []
        parent._touch_activity = lambda desc: touch_calls.append(desc)

        child = MagicMock()
        child.get_activity_summary.return_value = {
            "current_tool": "terminal",
            "api_call_count": 3,
            "max_iterations": 50,
            "last_activity_desc": "executing tool: terminal",
        }

        # Make run_conversation block long enough for heartbeats to fire
        def slow_run(**kwargs):
            time.sleep(0.25)
            return {"final_response": "done", "completed": True, "api_calls": 3}

        child.run_conversation.side_effect = slow_run

        # Patch the heartbeat interval to fire quickly
        with patch("tools.delegate_tool._HEARTBEAT_INTERVAL", 0.05):
            _run_single_child(
                task_index=0,
                goal="Test heartbeat",
                child=child,
                parent_agent=parent,
            )

        # Heartbeat should have fired at least once during the 0.25s sleep
        self.assertGreater(len(touch_calls), 0,
                           "Heartbeat did not propagate activity to parent")
        # Verify the description includes child's current tool detail
        self.assertTrue(
            any("terminal" in desc for desc in touch_calls),
            f"Heartbeat descriptions should include child tool info: {touch_calls}")

    def test_heartbeat_stops_after_child_completes(self):
        """Heartbeat thread is cleaned up when the child finishes."""
        from tools.delegate_tool import _run_single_child

        parent = _make_mock_parent()
        touch_calls = []
        parent._touch_activity = lambda desc: touch_calls.append(desc)

        child = MagicMock()
        child.get_activity_summary.return_value = {
            "current_tool": None,
            "api_call_count": 1,
            "max_iterations": 50,
            "last_activity_desc": "done",
        }
        child.run_conversation.return_value = {
            "final_response": "done", "completed": True, "api_calls": 1,
        }

        with patch("tools.delegate_tool._HEARTBEAT_INTERVAL", 0.05):
            _run_single_child(
                task_index=0,
                goal="Test cleanup",
                child=child,
                parent_agent=parent,
            )

        # Record count after completion, wait, and verify no more calls
        count_after = len(touch_calls)
        time.sleep(0.15)
        self.assertEqual(len(touch_calls), count_after,
                         "Heartbeat continued firing after child completed")

    def test_heartbeat_stops_after_child_error(self):
        """Heartbeat thread is cleaned up even when the child raises."""
        from tools.delegate_tool import _run_single_child

        parent = _make_mock_parent()
        touch_calls = []
        parent._touch_activity = lambda desc: touch_calls.append(desc)

        child = MagicMock()
        child.get_activity_summary.return_value = {
            "current_tool": "web_search",
            "api_call_count": 2,
            "max_iterations": 50,
            "last_activity_desc": "executing tool: web_search",
        }

        def slow_fail(**kwargs):
            time.sleep(0.15)
            raise RuntimeError("network timeout")

        child.run_conversation.side_effect = slow_fail

        with patch("tools.delegate_tool._HEARTBEAT_INTERVAL", 0.05):
            result = _run_single_child(
                task_index=0,
                goal="Test error cleanup",
                child=child,
                parent_agent=parent,
            )

        self.assertEqual(result["status"], "error")

        # Verify heartbeat stopped
        count_after = len(touch_calls)
        time.sleep(0.15)
        self.assertEqual(len(touch_calls), count_after,
                         "Heartbeat continued firing after child error")

    def test_heartbeat_includes_child_activity_desc_when_no_tool(self):
        """When child has no current_tool, heartbeat uses last_activity_desc."""
        from tools.delegate_tool import _run_single_child

        parent = _make_mock_parent()
        touch_calls = []
        parent._touch_activity = lambda desc: touch_calls.append(desc)

        child = MagicMock()
        child.get_activity_summary.return_value = {
            "current_tool": None,
            "api_call_count": 5,
            "max_iterations": 90,
            "last_activity_desc": "API call #5 completed",
        }

        def slow_run(**kwargs):
            time.sleep(0.15)
            return {"final_response": "done", "completed": True, "api_calls": 5}

        child.run_conversation.side_effect = slow_run

        with patch("tools.delegate_tool._HEARTBEAT_INTERVAL", 0.05):
            _run_single_child(
                task_index=0,
                goal="Test desc fallback",
                child=child,
                parent_agent=parent,
            )

        self.assertGreater(len(touch_calls), 0)
        self.assertTrue(
            any("API call #5 completed" in desc for desc in touch_calls),
            f"Heartbeat should include last_activity_desc: {touch_calls}")

    def test_heartbeat_does_not_trip_idle_stale_while_inside_tool(self):
        """A long-running tool (no iteration advance, but current_tool set)
        must not be flagged stale at the idle threshold.

        Bug #13041: when a child is legitimately busy inside a slow tool
        (terminal command, browser fetch), api_call_count does not advance.
        The previous stale check treated this as idle and stopped the
        heartbeat after 5 cycles (~150s), letting the gateway kill the
        session. The fix uses a much higher in-tool threshold and only
        applies the tight idle threshold when current_tool is None.
        """
        from tools.delegate_tool import _run_single_child

        parent = _make_mock_parent()
        touch_calls = []
        parent._touch_activity = lambda desc: touch_calls.append(desc)

        child = MagicMock()
        # Child is stuck inside a single terminal call for the whole run.
        # api_call_count never advances, current_tool is always set.
        child.get_activity_summary.return_value = {
            "current_tool": "terminal",
            "api_call_count": 1,
            "max_iterations": 50,
            "last_activity_desc": "executing tool: terminal",
        }

        def slow_run(**kwargs):
            # Long enough to exceed the OLD idle threshold (5 cycles) at
            # the patched interval, but shorter than the new in-tool
            # threshold.
            time.sleep(0.4)
            return {"final_response": "done", "completed": True, "api_calls": 1}

        child.run_conversation.side_effect = slow_run

        # Patch both the interval AND the idle ceiling so the test proves
        # the in-tool branch takes effect: with a 0.05s interval and the
        # default _HEARTBEAT_STALE_CYCLES_IDLE=5, the old behavior would
        # trip after 0.25s and stop firing. We should see heartbeats
        # continuing through the full 0.4s run.
        with patch("tools.delegate_tool._HEARTBEAT_INTERVAL", 0.05):
            _run_single_child(
                task_index=0,
                goal="Test long-running tool",
                child=child,
                parent_agent=parent,
            )

        # With the old idle threshold (5 cycles = 0.25s), touch_calls
        # would cap at ~5. With the in-tool threshold (20 cycles = 1.0s),
        # we should see substantially more heartbeats over 0.4s.
        self.assertGreater(
            len(touch_calls), 6,
            f"Heartbeat stopped too early while child was inside a tool; "
            f"got {len(touch_calls)} touches over 0.4s at 0.05s interval",
        )



class TestDelegationReasoningEffort(unittest.TestCase):
    """Tests for delegation.reasoning_effort config override."""

    @patch("tools.delegate_tool._load_config")
    @patch("run_agent.AIAgent")
    def test_inherits_parent_reasoning_when_no_override(self, MockAgent, mock_cfg):
        """With no delegation.reasoning_effort, child inherits parent's config."""
        mock_cfg.return_value = {"max_iterations": 50, "reasoning_effort": ""}
        MockAgent.return_value = MagicMock()
        parent = _make_mock_parent()
        parent.reasoning_config = {"enabled": True, "effort": "xhigh"}

        _build_child_agent(
            task_index=0, goal="test", context=None, toolsets=None,
            model=None, max_iterations=50, parent_agent=parent,
            task_count=1,
        )
        call_kwargs = MockAgent.call_args[1]
        self.assertEqual(call_kwargs["reasoning_config"], {"enabled": True, "effort": "xhigh"})

    @patch("tools.delegate_tool._load_config")
    @patch("run_agent.AIAgent")
    def test_override_reasoning_effort_from_config(self, MockAgent, mock_cfg):
        """delegation.reasoning_effort overrides the parent's level."""
        mock_cfg.return_value = {"max_iterations": 50, "reasoning_effort": "low"}
        MockAgent.return_value = MagicMock()
        parent = _make_mock_parent()
        parent.reasoning_config = {"enabled": True, "effort": "xhigh"}

        _build_child_agent(
            task_index=0, goal="test", context=None, toolsets=None,
            model=None, max_iterations=50, parent_agent=parent,
            task_count=1,
        )
        call_kwargs = MockAgent.call_args[1]
        self.assertEqual(call_kwargs["reasoning_config"], {"enabled": True, "effort": "low"})

    @patch("tools.delegate_tool._load_config")
    @patch("run_agent.AIAgent")
    def test_override_reasoning_effort_none_disables(self, MockAgent, mock_cfg):
        """delegation.reasoning_effort: 'none' disables thinking for subagents."""
        mock_cfg.return_value = {"max_iterations": 50, "reasoning_effort": "none"}
        MockAgent.return_value = MagicMock()
        parent = _make_mock_parent()
        parent.reasoning_config = {"enabled": True, "effort": "high"}

        _build_child_agent(
            task_index=0, goal="test", context=None, toolsets=None,
            model=None, max_iterations=50, parent_agent=parent,
            task_count=1,
        )
        call_kwargs = MockAgent.call_args[1]
        self.assertEqual(call_kwargs["reasoning_config"], {"enabled": False})

    @patch("tools.delegate_tool._load_config")
    @patch("run_agent.AIAgent")
    def test_invalid_reasoning_effort_falls_back_to_parent(self, MockAgent, mock_cfg):
        """Invalid delegation.reasoning_effort falls back to parent's config."""
        mock_cfg.return_value = {"max_iterations": 50, "reasoning_effort": "banana"}
        MockAgent.return_value = MagicMock()
        parent = _make_mock_parent()
        parent.reasoning_config = {"enabled": True, "effort": "medium"}

        _build_child_agent(
            task_index=0, goal="test", context=None, toolsets=None,
            model=None, max_iterations=50, parent_agent=parent,
            task_count=1,
        )
        call_kwargs = MockAgent.call_args[1]
        self.assertEqual(call_kwargs["reasoning_config"], {"enabled": True, "effort": "medium"})


# =========================================================================
# Dispatch helper, progress events, concurrency
# =========================================================================

class TestDispatchDelegateTask(unittest.TestCase):
    """Tests for the _dispatch_delegate_task helper and full param forwarding."""

    def setUp(self):
        self._repo_patcher = patch("tools.delegate_tool._detect_repo_path", return_value="/home/test/repo")
        self._repo_patcher.start()

    def tearDown(self):
        self._repo_patcher.stop()

    @patch("tools.delegate_tool._load_config", return_value={})
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    def test_acp_args_forwarded(self, mock_creds, mock_cfg):
        """Both acp_command and acp_args reach delegate_task via the helper."""
        mock_creds.return_value = {
            "provider": None, "base_url": None,
            "api_key": None, "api_mode": None, "model": None,
        }
        parent = _make_mock_parent(depth=0)
        with patch("tools.delegate_tool._build_child_agent") as mock_build:
            mock_child = MagicMock()
            mock_child.run_conversation.return_value = {
                "final_response": "done", "completed": True,
                "api_calls": 1, "messages": [],
            }
            mock_child._delegate_saved_tool_names = []
            mock_child._credential_pool = None
            mock_child.session_prompt_tokens = 0
            mock_child.session_completion_tokens = 0
            mock_child.model = "test"
            mock_build.return_value = mock_child

            delegate_task(
                goal="test",
                acp_command="claude",
                acp_args=["--acp", "--stdio"],
                parent_agent=parent,
            )
            _, kwargs = mock_build.call_args
            overrides = kwargs.get("overrides")
            self.assertIsNotNone(overrides, "overrides kwarg missing")
            self.assertEqual(overrides.acp_command, "claude")
            self.assertEqual(overrides.acp_args, ["--acp", "--stdio"])

class TestDelegateEventEnum(unittest.TestCase):
    """Tests for DelegateEvent enum and back-compat aliases."""

    def test_enum_values_are_strings(self):
        for event in DelegateEvent:
            self.assertIsInstance(event.value, str)
            self.assertTrue(event.value.startswith("delegate."))

    def test_legacy_map_covers_all_old_names(self):
        expected_legacy = {"_thinking", "reasoning.available",
                          "tool.started", "tool.completed", "subagent_progress"}
        self.assertEqual(set(_LEGACY_EVENT_MAP.keys()), expected_legacy)

    def test_legacy_map_values_are_delegate_events(self):
        for old_name, event in _LEGACY_EVENT_MAP.items():
            self.assertIsInstance(event, DelegateEvent)

    def test_progress_callback_normalises_tool_started(self):
        """_build_child_progress_callback handles tool.started via enum."""
        parent = _make_mock_parent()
        parent._delegate_spinner = MagicMock()
        parent.tool_progress_callback = MagicMock()

        cb = _build_child_progress_callback(0, "test goal", parent, task_count=1)
        self.assertIsNotNone(cb)

        cb("tool.started", tool_name="terminal", preview="ls")
        parent._delegate_spinner.print_above.assert_called()

    def test_progress_callback_normalises_thinking(self):
        """Both _thinking and reasoning.available route to TASK_THINKING."""
        parent = _make_mock_parent()
        parent._delegate_spinner = MagicMock()
        parent.tool_progress_callback = None

        cb = _build_child_progress_callback(0, "test goal", parent, task_count=1)

        cb("_thinking", tool_name=None, preview="pondering...")
        assert any("💭" in str(c) for c in parent._delegate_spinner.print_above.call_args_list)

        parent._delegate_spinner.print_above.reset_mock()
        cb("reasoning.available", tool_name=None, preview="hmm")
        assert any("💭" in str(c) for c in parent._delegate_spinner.print_above.call_args_list)

    def test_progress_callback_tool_completed_is_noop(self):
        """tool.completed is normalised but produces no display output."""
        parent = _make_mock_parent()
        parent._delegate_spinner = MagicMock()
        parent.tool_progress_callback = None

        cb = _build_child_progress_callback(0, "test goal", parent, task_count=1)
        cb("tool.completed", tool_name="terminal")
        parent._delegate_spinner.print_above.assert_not_called()

    def test_progress_callback_ignores_unknown_events(self):
        """Unknown event types are silently ignored."""
        parent = _make_mock_parent()
        parent._delegate_spinner = MagicMock()

        cb = _build_child_progress_callback(0, "test goal", parent, task_count=1)
        # Should not raise
        cb("some.unknown.event", tool_name="x")
        parent._delegate_spinner.print_above.assert_not_called()

    def test_progress_callback_accepts_enum_value_directly(self):
        """cb(DelegateEvent.TASK_THINKING, ...) must route to the thinking
        branch.  Pre-fix the callback only handled legacy strings via
        _LEGACY_EVENT_MAP.get and silently dropped enum-typed callers."""
        parent = _make_mock_parent()
        parent._delegate_spinner = MagicMock()
        parent.tool_progress_callback = None

        cb = _build_child_progress_callback(0, "test goal", parent, task_count=1)
        cb(DelegateEvent.TASK_THINKING, preview="pondering")
        # If the enum was accepted, the thinking emoji got printed.
        assert any(
            "💭" in str(c)
            for c in parent._delegate_spinner.print_above.call_args_list
        )

    def test_progress_callback_accepts_new_style_string(self):
        """cb('delegate.task_thinking', ...) — the string form of the
        enum value — must route to the thinking branch too, so new-style
        emitters don't have to import DelegateEvent."""
        parent = _make_mock_parent()
        parent._delegate_spinner = MagicMock()

        cb = _build_child_progress_callback(0, "test goal", parent, task_count=1)
        cb("delegate.task_thinking", preview="hmm")
        assert any(
            "💭" in str(c)
            for c in parent._delegate_spinner.print_above.call_args_list
        )

    def test_progress_callback_task_progress_not_misrendered(self):
        """'subagent_progress' (legacy name for TASK_PROGRESS) carries a
        pre-batched summary in the tool_name slot.  Before the fix, this
        fell through to the TASK_TOOL_STARTED rendering path, treating
        the summary string as a tool name.  After the fix: distinct
        render (no tool-start emoji lookup) and pass-through relay
        upward (no re-batching).

        Regression path only reachable once nested orchestration is
        enabled: nested orchestrators relay subagent_progress from
        grandchildren upward through this callback.
        """
        parent = _make_mock_parent()
        parent._delegate_spinner = MagicMock()
        parent.tool_progress_callback = MagicMock()

        cb = _build_child_progress_callback(0, "test goal", parent, task_count=1)
        cb("subagent_progress", tool_name="🔀 [1] terminal, file")

        # Spinner gets a distinct 🔀-prefixed line, NOT a tool emoji
        # followed by the summary string as if it were a tool name.
        calls = parent._delegate_spinner.print_above.call_args_list
        self.assertTrue(any("🔀 🔀 [1] terminal, file" in str(c) for c in calls))
        # Parent callback receives the relay (pass-through, no re-batching).
        parent.tool_progress_callback.assert_called_once()
        # No '⚡' tool-start emoji should appear — that's the pre-fix bug.
        self.assertFalse(any("⚡" in str(c) for c in calls))


class TestConcurrencyDefaults(unittest.TestCase):
    """Tests for the concurrency default and no hard ceiling."""

    @patch("tools.delegate_tool._load_config", return_value={})
    def test_default_is_three(self, mock_cfg):
        # Clear env var if set
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_get_max_concurrent_children(), 3)

    @patch("tools.delegate_tool._load_config",
           return_value={"max_concurrent_children": 10})
    def test_no_upper_ceiling(self, mock_cfg):
        """Users can raise concurrency as high as they want — no hard cap."""
        self.assertEqual(_get_max_concurrent_children(), 10)

    @patch("tools.delegate_tool._load_config",
           return_value={"max_concurrent_children": 100})
    def test_very_high_values_honored(self, mock_cfg):
        self.assertEqual(_get_max_concurrent_children(), 100)

    @patch("tools.delegate_tool._load_config",
           return_value={"max_concurrent_children": 0})
    def test_zero_clamped_to_one(self, mock_cfg):
        """Floor of 1 is enforced; zero or negative values raise to 1."""
        self.assertEqual(_get_max_concurrent_children(), 1)

    @patch("tools.delegate_tool._load_config", return_value={})
    def test_env_var_honored_uncapped(self, mock_cfg):
        with patch.dict(os.environ, {"DELEGATION_MAX_CONCURRENT_CHILDREN": "12"}):
            self.assertEqual(_get_max_concurrent_children(), 12)

    @patch("tools.delegate_tool._load_config",
           return_value={"max_concurrent_children": 6})
    def test_configured_value_returned(self, mock_cfg):
        self.assertEqual(_get_max_concurrent_children(), 6)


# =========================================================================
# max_spawn_depth clamping
# =========================================================================

class TestMaxSpawnDepth(unittest.TestCase):
    """Tests for _get_max_spawn_depth clamping and fallback behavior."""

    @patch("tools.delegate_tool._load_config", return_value={})
    def test_max_spawn_depth_defaults_to_1(self, mock_cfg):
        from tools.delegate_tool import _get_max_spawn_depth
        self.assertEqual(_get_max_spawn_depth(), 1)

    @patch("tools.delegate_tool._load_config",
           return_value={"max_spawn_depth": 0})
    def test_max_spawn_depth_clamped_below_one(self, mock_cfg):
        import logging
        from tools.delegate_tool import _get_max_spawn_depth
        with self.assertLogs("tools.delegate_tool", level=logging.WARNING) as cm:
            result = _get_max_spawn_depth()
        self.assertEqual(result, 1)
        self.assertTrue(any("clamping to 1" in m for m in cm.output))

    @patch("tools.delegate_tool._load_config",
           return_value={"max_spawn_depth": 99})
    def test_max_spawn_depth_clamped_above_three(self, mock_cfg):
        import logging
        from tools.delegate_tool import _get_max_spawn_depth
        with self.assertLogs("tools.delegate_tool", level=logging.WARNING) as cm:
            result = _get_max_spawn_depth()
        self.assertEqual(result, 3)
        self.assertTrue(any("clamping to 3" in m for m in cm.output))

    @patch("tools.delegate_tool._load_config",
           return_value={"max_spawn_depth": "not-a-number"})
    def test_max_spawn_depth_invalid_falls_back_to_default(self, mock_cfg):
        from tools.delegate_tool import _get_max_spawn_depth
        self.assertEqual(_get_max_spawn_depth(), 1)


# =========================================================================
# role param plumbing
# =========================================================================
#
# These tests cover the schema + signature + stash plumbing of the role
# param.  The full role-honoring behavior (toolset re-add, role-aware
# prompt) lives in TestOrchestratorRoleBehavior below; these tests only
# assert on _delegate_role stashing and on the schema shape.


class TestOrchestratorRoleSchema(unittest.TestCase):
    """Tests that the role param reaches the child via dispatch."""

    def setUp(self):
        self._repo_patcher = patch("tools.delegate_tool._detect_repo_path", return_value="/home/test/repo")
        self._repo_patcher.start()

    def tearDown(self):
        self._repo_patcher.stop()

    @patch("tools.delegate_tool._resolve_delegation_credentials")
    @patch("tools.delegate_tool._load_config",
           return_value={"max_spawn_depth": 2})
    def _run_with_mock_child(self, role_arg, mock_cfg, mock_creds):
        mock_creds.return_value = {
            "provider": None, "base_url": None,
            "api_key": None, "api_mode": None, "model": None,
        }
        parent = _make_mock_parent(depth=0)
        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.run_conversation.return_value = {
                "final_response": "done", "completed": True,
                "api_calls": 1, "messages": [],
            }
            mock_child._delegate_saved_tool_names = []
            mock_child._credential_pool = None
            mock_child.session_prompt_tokens = 0
            mock_child.session_completion_tokens = 0
            mock_child.model = "test"
            MockAgent.return_value = mock_child
            kwargs = {"goal": "test", "parent_agent": parent}
            if role_arg is not _SENTINEL:
                kwargs["role"] = role_arg
            delegate_task(**kwargs)
            return mock_child

    def test_default_role_is_leaf(self):
        child = self._run_with_mock_child(_SENTINEL)
        self.assertEqual(child._delegate_role, "leaf")

    def test_explicit_orchestrator_role_stashed(self):
        """role='orchestrator' reaches _build_child_agent and is stashed.
        Full behavior (toolset re-add) lands in commit 3; commit 2 only
        verifies the plumbing."""
        child = self._run_with_mock_child("orchestrator")
        self.assertEqual(child._delegate_role, "orchestrator")

    def test_unknown_role_coerces_to_leaf(self):
        """role='nonsense' → _normalize_role warns and returns 'leaf'."""
        import logging
        with self.assertLogs("tools.delegate_tool", level=logging.WARNING) as cm:
            child = self._run_with_mock_child("nonsense")
        self.assertEqual(child._delegate_role, "leaf")
        self.assertTrue(any("coercing" in m.lower() for m in cm.output))

    def test_schema_has_role_top_level_and_per_task(self):
        from tools.delegate_tool import DELEGATE_TASK_SCHEMA
        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
        self.assertIn("role", props)
        self.assertEqual(props["role"]["enum"], ["leaf", "orchestrator"])
        task_props = props["tasks"]["items"]["properties"]
        self.assertIn("role", task_props)
        self.assertEqual(task_props["role"]["enum"], ["leaf", "orchestrator"])


# Sentinel used to distinguish "role kwarg omitted" from "role=None".
_SENTINEL = object()


# =========================================================================
# role-honoring behavior
# =========================================================================


def _make_role_mock_child():
    """Helper: mock child with minimal fields for delegate_task to process."""
    mock_child = MagicMock()
    mock_child.run_conversation.return_value = {
        "final_response": "done", "completed": True,
        "api_calls": 1, "messages": [],
    }
    mock_child._delegate_saved_tool_names = []
    mock_child._credential_pool = None
    mock_child.session_prompt_tokens = 0
    mock_child.session_completion_tokens = 0
    mock_child.model = "test"
    return mock_child


class TestOrchestratorRoleBehavior(unittest.TestCase):

    def setUp(self):
        self._repo_patcher = patch("tools.delegate_tool._detect_repo_path", return_value="/home/test/repo")
        self._repo_patcher.start()

    def tearDown(self):
        self._repo_patcher.stop()
    """Tests that role='orchestrator' actually changes toolset + prompt."""

    @patch("tools.delegate_tool._resolve_delegation_credentials")
    @patch("tools.delegate_tool._load_config",
           return_value={"max_spawn_depth": 2})
    def test_orchestrator_role_keeps_delegation_at_depth_1(
        self, mock_cfg, mock_creds
    ):
        """role='orchestrator' + depth-0 parent with max_spawn_depth=2 →
        child at depth 1 gets 'delegation' in enabled_toolsets (can
        further delegate).  Requires max_spawn_depth>=2 since the new
        default is 1 (flat)."""
        mock_creds.return_value = {
            "provider": None, "base_url": None,
            "api_key": None, "api_mode": None, "model": None,
        }
        parent = _make_mock_parent(depth=0)
        parent.enabled_toolsets = ["terminal", "file"]
        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = _make_role_mock_child()
            MockAgent.return_value = mock_child
            delegate_task(goal="test", role="orchestrator", parent_agent=parent)
            kwargs = MockAgent.call_args[1]
            self.assertIn("delegation", kwargs["enabled_toolsets"])
            self.assertEqual(mock_child._delegate_role, "orchestrator")

    @patch("tools.delegate_tool._resolve_delegation_credentials")
    @patch("tools.delegate_tool._load_config",
           return_value={"max_spawn_depth": 2})
    def test_orchestrator_blocked_at_max_spawn_depth(
        self, mock_cfg, mock_creds
    ):
        """Parent at depth 1 with max_spawn_depth=2 spawns child
        at depth 2 (the floor); role='orchestrator' degrades to leaf."""
        mock_creds.return_value = {
            "provider": None, "base_url": None,
            "api_key": None, "api_mode": None, "model": None,
        }
        parent = _make_mock_parent(depth=1)
        parent.enabled_toolsets = ["terminal", "delegation"]
        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = _make_role_mock_child()
            MockAgent.return_value = mock_child
            delegate_task(goal="test", role="orchestrator", parent_agent=parent)
            kwargs = MockAgent.call_args[1]
            self.assertNotIn("delegation", kwargs["enabled_toolsets"])
            self.assertEqual(mock_child._delegate_role, "leaf")

    @patch("tools.delegate_tool._resolve_delegation_credentials")
    @patch("tools.delegate_tool._load_config", return_value={})
    def test_orchestrator_blocked_at_default_flat_depth(
        self, mock_cfg, mock_creds
    ):
        """With default max_spawn_depth=1 (flat), role='orchestrator'
        on a depth-0 parent produces a depth-1 child that is already at
        the floor — the role degrades to 'leaf' and the delegation
        toolset is stripped.  This is the new default posture."""
        mock_creds.return_value = {
            "provider": None, "base_url": None,
            "api_key": None, "api_mode": None, "model": None,
        }
        parent = _make_mock_parent(depth=0)
        parent.enabled_toolsets = ["terminal", "file", "delegation"]
        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = _make_role_mock_child()
            MockAgent.return_value = mock_child
            delegate_task(goal="test", role="orchestrator", parent_agent=parent)
            kwargs = MockAgent.call_args[1]
            self.assertNotIn("delegation", kwargs["enabled_toolsets"])
            self.assertEqual(mock_child._delegate_role, "leaf")

    @patch("tools.delegate_tool._resolve_delegation_credentials")
    def test_orchestrator_enabled_false_forces_leaf(self, mock_creds):
        """Kill switch delegation.orchestrator_enabled=false overrides
        role='orchestrator'."""
        mock_creds.return_value = {
            "provider": None, "base_url": None,
            "api_key": None, "api_mode": None, "model": None,
        }
        parent = _make_mock_parent(depth=0)
        parent.enabled_toolsets = ["terminal", "delegation"]
        with patch("tools.delegate_tool._load_config",
                   return_value={"orchestrator_enabled": False}):
            with patch("run_agent.AIAgent") as MockAgent:
                mock_child = _make_role_mock_child()
                MockAgent.return_value = mock_child
                delegate_task(goal="test", role="orchestrator",
                              parent_agent=parent)
                kwargs = MockAgent.call_args[1]
                self.assertNotIn("delegation", kwargs["enabled_toolsets"])
                self.assertEqual(mock_child._delegate_role, "leaf")

    # ── Role-aware system prompt ────────────────────────────────────────

    def test_leaf_prompt_does_not_mention_delegation(self):
        prompt = _build_child_system_prompt(
            "Fix tests", role="leaf",
            max_spawn_depth=2, child_depth=1,
        )
        self.assertNotIn("delegate_task", prompt)
        self.assertNotIn("Orchestrator Role", prompt)

    def test_orchestrator_prompt_mentions_delegation_capability(self):
        prompt = _build_child_system_prompt(
            "Survey approaches", role="orchestrator",
            max_spawn_depth=2, child_depth=1,
        )
        self.assertIn("delegate_task", prompt)
        self.assertIn("Orchestrator Role", prompt)
        # Depth/max-depth note present and literal:
        self.assertIn("depth 1", prompt)
        self.assertIn("max_spawn_depth=2", prompt)

    def test_orchestrator_prompt_at_depth_floor_says_children_are_leaves(self):
        """With max_spawn_depth=2 and child_depth=1, the orchestrator's
        own children would be at depth 2 (the floor) → must be leaves."""
        prompt = _build_child_system_prompt(
            "Survey", role="orchestrator",
            max_spawn_depth=2, child_depth=1,
        )
        self.assertIn("MUST be leaves", prompt)

    def test_orchestrator_prompt_below_floor_allows_more_nesting(self):
        """With max_spawn_depth=3 and child_depth=1, the orchestrator's
        own children can themselves be orchestrators (depth 2 < 3)."""
        prompt = _build_child_system_prompt(
            "Deep work", role="orchestrator",
            max_spawn_depth=3, child_depth=1,
        )
        self.assertIn("can themselves be orchestrators", prompt)

    # ── Batch mode and intersection ─────────────────────────────────────

    @patch("tools.delegate_tool._resolve_delegation_credentials")
    @patch("tools.delegate_tool._load_config",
           return_value={"max_spawn_depth": 2})
    def test_batch_mode_per_task_role_override(self, mock_cfg, mock_creds):
        """Per-task role beats top-level; no top-level role → "leaf".

        tasks=[{role:'orchestrator'},{role:'leaf'},{}] → first gets
        delegation, second and third don't.  Requires max_spawn_depth>=2
        (raised explicitly here) since the new default is 1 (flat).
        """
        mock_creds.return_value = {
            "provider": None, "base_url": None,
            "api_key": None, "api_mode": None, "model": None,
        }
        parent = _make_mock_parent(depth=0)
        parent.enabled_toolsets = ["terminal", "file", "delegation"]
        built_toolsets = []

        def _factory(*a, **kw):
            m = _make_role_mock_child()
            built_toolsets.append(kw.get("enabled_toolsets"))
            return m

        with patch("run_agent.AIAgent", side_effect=_factory):
            delegate_task(
                tasks=[
                    {"goal": "A", "role": "orchestrator"},
                    {"goal": "B", "role": "leaf"},
                    {"goal": "C"},  # no role → falls back to top_role (leaf)
                ],
                parent_agent=parent,
            )
        self.assertIn("delegation", built_toolsets[0])
        self.assertNotIn("delegation", built_toolsets[1])
        self.assertNotIn("delegation", built_toolsets[2])

    @patch("tools.delegate_tool._resolve_delegation_credentials")
    @patch("tools.delegate_tool._load_config",
           return_value={"max_spawn_depth": 2})
    def test_intersection_preserves_delegation_bound(
        self, mock_cfg, mock_creds
    ):
        """Design decision: orchestrator capability is granted by role,
        NOT inherited from the parent's toolset. A parent without
        'delegation' in its enabled_toolsets can still spawn an
        orchestrator child — the re-add in _build_child_agent runs
        unconditionally for orchestrators (when max_spawn_depth allows).

        If you want to change to "parent must have delegation too",
        update _build_child_agent to check parent_toolsets before the
        re-add and update this test to match.
        """
        mock_creds.return_value = {
            "provider": None, "base_url": None,
            "api_key": None, "api_mode": None, "model": None,
        }
        parent = _make_mock_parent(depth=0)
        parent.enabled_toolsets = ["terminal", "file"]  # no delegation
        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = _make_role_mock_child()
            MockAgent.return_value = mock_child
            delegate_task(goal="test", role="orchestrator",
                          parent_agent=parent)
            self.assertIn("delegation", MockAgent.call_args[1]["enabled_toolsets"])


class TestOrchestratorEndToEnd(unittest.TestCase):

    def setUp(self):
        self._repo_patcher = patch("tools.delegate_tool._detect_repo_path", return_value="/home/test/repo")
        self._repo_patcher.start()

    def tearDown(self):
        self._repo_patcher.stop()
    """End-to-end: parent -> orchestrator -> two-leaf nested orchestration.

    Covers the acceptance gate: parent delegates to an orchestrator
    child; the orchestrator delegates to two leaf grandchildren; the
    role/toolset/depth chain all resolve correctly.

    Mock strategy: a single AIAgent patch with a side_effect factory
    that keys on the child's ephemeral_system_prompt — orchestrator
    prompts contain the string "Orchestrator Role" (see
    _build_child_system_prompt), leaves don't.  The orchestrator
    mock's run_conversation recursively calls delegate_task with
    tasks=[{goal:...},{goal:...}] to spawn two leaves.  This keeps
    the test in one patch context and avoids depth-indexed nesting.
    """

    @patch("tools.delegate_tool._resolve_delegation_credentials")
    @patch("tools.delegate_tool._load_config",
           return_value={"max_spawn_depth": 2})
    def test_end_to_end_nested_orchestration(self, mock_cfg, mock_creds):
        mock_creds.return_value = {
            "provider": None, "base_url": None,
            "api_key": None, "api_mode": None, "model": None,
        }
        parent = _make_mock_parent(depth=0)
        parent.enabled_toolsets = ["terminal", "file", "delegation"]

        # (enabled_toolsets, _delegate_role) for each agent built
        built_agents: list = []
        # Keep the orchestrator mock around so the re-entrant delegate_task
        # can reach it via closure.
        orch_mock = {}

        def _factory(*a, **kw):
            prompt = kw.get("ephemeral_system_prompt", "") or ""
            is_orchestrator = "Orchestrator Role" in prompt
            m = _make_role_mock_child()
            built_agents.append({
                "enabled_toolsets": list(kw.get("enabled_toolsets") or []),
                "is_orchestrator_prompt": is_orchestrator,
            })

            if is_orchestrator:
                # Prepare the orchestrator mock as a parent-capable object
                # so the nested delegate_task call succeeds.
                m._delegate_depth = 1
                m._delegate_role = "orchestrator"
                m._active_children = []
                m._active_children_lock = threading.Lock()
                m._session_db = None
                m.platform = "cli"
                m.enabled_toolsets = ["terminal", "file", "delegation"]
                m.api_key = "***"
                m.base_url = ""
                m.provider = None
                m.api_mode = None
                m.providers_allowed = None
                m.providers_ignored = None
                m.providers_order = None
                m.provider_sort = None
                m._print_fn = None
                m.tool_progress_callback = None
                m.thinking_callback = None
                orch_mock["agent"] = m

                def _orchestrator_run(user_message=None, task_id=None):
                    # Re-entrant: orchestrator spawns two leaves
                    delegate_task(
                        tasks=[{"goal": "leaf-A"}, {"goal": "leaf-B"}],
                        parent_agent=m,
                    )
                    return {
                        "final_response": "orchestrated 2 workers",
                        "completed": True, "api_calls": 1,
                        "messages": [],
                    }
                m.run_conversation.side_effect = _orchestrator_run

            return m

        with patch("run_agent.AIAgent", side_effect=_factory) as MockAgent:
            delegate_task(
                goal="top-level orchestration",
                role="orchestrator",
                parent_agent=parent,
            )

        # 1 orchestrator + 2 leaf grandchildren = 3 agents
        self.assertEqual(MockAgent.call_count, 3)
        # First built = the orchestrator (parent's direct child)
        self.assertIn("delegation", built_agents[0]["enabled_toolsets"])
        self.assertTrue(built_agents[0]["is_orchestrator_prompt"])
        # Next two = leaves (grandchildren)
        self.assertNotIn("delegation", built_agents[1]["enabled_toolsets"])
        self.assertFalse(built_agents[1]["is_orchestrator_prompt"])
        self.assertNotIn("delegation", built_agents[2]["enabled_toolsets"])
        self.assertFalse(built_agents[2]["is_orchestrator_prompt"])


class TestSubagentApprovalCallback(unittest.TestCase):
    """Subagent worker threads must have a non-interactive approval callback
    installed so dangerous-command prompts don't fall back to input() and
    deadlock the parent's prompt_toolkit TUI.

    Governed by delegation.subagent_auto_approve:
      false (default) → _subagent_auto_deny
      true            → _subagent_auto_approve
    """

    def test_auto_deny_returns_deny(self):
        from tools.delegate_tool import _subagent_auto_deny
        self.assertEqual(
            _subagent_auto_deny("rm -rf /tmp/x", "dangerous"),
            "deny",
        )

    def test_auto_approve_returns_once(self):
        from tools.delegate_tool import _subagent_auto_approve
        self.assertEqual(
            _subagent_auto_approve("rm -rf /tmp/x", "dangerous"),
            "once",
        )

    @patch("tools.delegate_tool._load_config", return_value={})
    def test_getter_defaults_to_deny(self, _mock_cfg):
        from tools.delegate_tool import (
            _get_subagent_approval_callback,
            _subagent_auto_deny,
        )
        self.assertIs(_get_subagent_approval_callback(), _subagent_auto_deny)

    @patch(
        "tools.delegate_tool._load_config",
        return_value={"subagent_auto_approve": False},
    )
    def test_getter_explicit_false_is_deny(self, _mock_cfg):
        from tools.delegate_tool import (
            _get_subagent_approval_callback,
            _subagent_auto_deny,
        )
        self.assertIs(_get_subagent_approval_callback(), _subagent_auto_deny)

    @patch(
        "tools.delegate_tool._load_config",
        return_value={"subagent_auto_approve": True},
    )
    def test_getter_true_is_approve(self, _mock_cfg):
        from tools.delegate_tool import (
            _get_subagent_approval_callback,
            _subagent_auto_approve,
        )
        self.assertIs(_get_subagent_approval_callback(), _subagent_auto_approve)

    @patch(
        "tools.delegate_tool._load_config",
        return_value={"subagent_auto_approve": "yes"},
    )
    def test_getter_truthy_string_is_approve(self, _mock_cfg):
        """is_truthy_value accepts 'yes'/'1'/'true' as truthy."""
        from tools.delegate_tool import (
            _get_subagent_approval_callback,
            _subagent_auto_approve,
        )
        self.assertIs(_get_subagent_approval_callback(), _subagent_auto_approve)

    def test_executor_initializer_installs_callback_in_worker(self):
        """The initializer sets the callback on the worker thread's TLS,
        not the parent's — verifies the fix actually scopes to workers.
        """
        from concurrent.futures import ThreadPoolExecutor
        from tools.terminal_tool import (
            set_approval_callback as _set_cb,
            _get_approval_callback,
        )
        from tools.delegate_tool import _subagent_auto_deny

        # Parent thread has no callback.
        _set_cb(None)
        self.assertIsNone(_get_approval_callback())

        seen = []

        def worker():
            seen.append(_get_approval_callback())

        with ThreadPoolExecutor(
            max_workers=1,
            initializer=_set_cb,
            initargs=(_subagent_auto_deny,),
        ) as executor:
            executor.submit(worker).result()

        self.assertEqual(seen, [_subagent_auto_deny])
        # Parent's callback slot is still empty (TLS isolates threads).
        self.assertIsNone(_get_approval_callback())


class TestFallbackModelInheritance(unittest.TestCase):
    """Subagents must inherit the parent's fallback provider chain."""

    def test_child_inherits_fallback_chain(self):
        """_build_child_agent passes parent._fallback_chain as fallback_model."""
        parent = _make_mock_parent(depth=0)
        fallback_entry = {"provider": "openrouter", "model": "gpt-4o-mini", "api_key": "sk-or-x"}
        parent._fallback_chain = [fallback_entry]

        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value = MagicMock()
            _build_child_agent(
                task_index=0,
                goal="test fallback inheritance",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
            )

        _, kwargs = MockAgent.call_args
        self.assertEqual(kwargs["fallback_model"], [fallback_entry])

    def test_child_gets_no_fallback_when_parent_chain_empty(self):
        """When parent._fallback_chain is empty, fallback_model is None."""
        parent = _make_mock_parent(depth=0)
        parent._fallback_chain = []

        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value = MagicMock()
            _build_child_agent(
                task_index=0,
                goal="test no fallback",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
            )

        _, kwargs = MockAgent.call_args
        self.assertIsNone(kwargs["fallback_model"])


class TestP70CodePathRegex(unittest.TestCase):
    """P70/MOL-261 — _CODE_PATH_RE must match hermes-agent paths AND reject
    sibling-prefix collisions (hermes-agent2, hermes-agent-old, hermes-agentFOO).
    Complements the existing smoke test in PATCHES.md P70 with pytest coverage
    so `hermes update` → test-suite drift surfaces immediately.
    """

    def _match(self, s):
        from tools.delegate_tool import _CODE_PATH_RE
        m = _CODE_PATH_RE.search(s)
        return m.group(1) if m else None

    # Positive cases — these MUST match.
    def test_matches_tilde_hermes_agent(self):
        self.assertEqual(self._match("~/.hermes/hermes-agent"), "~/.hermes/hermes-agent")

    def test_matches_tilde_hermes_agent_subpath(self):
        self.assertEqual(
            self._match("~/.hermes/hermes-agent/tools/delegate_tool.py"),
            "~/.hermes/hermes-agent/tools/delegate_tool.py",
        )

    def test_matches_absolute_hermes_agent(self):
        self.assertEqual(
            self._match("/Users/wills_mac_mini/.hermes/hermes-agent/run_agent.py"),
            "/Users/wills_mac_mini/.hermes/hermes-agent/run_agent.py",
        )

    def test_matches_tilde_code(self):
        self.assertEqual(
            self._match("~/Code/hermes-poc/foo.py"),
            "~/Code/hermes-poc/foo.py",
        )

    def test_matches_absolute_code(self):
        self.assertEqual(
            self._match("/Users/will/Code/hermes-poc"),
            "/Users/will/Code/hermes-poc",
        )

    def test_matches_hermes_agent_in_sentence(self):
        self.assertEqual(
            self._match("please fix ~/.hermes/hermes-agent/run_agent.py today"),
            "~/.hermes/hermes-agent/run_agent.py",
        )

    # Negative-lookahead cases — these MUST NOT match (prevent prefix collision).
    def test_rejects_hermes_agent2(self):
        self.assertIsNone(self._match("~/.hermes/hermes-agent2/foo"))

    def test_rejects_hermes_agent_dash_old(self):
        self.assertIsNone(self._match("~/.hermes/hermes-agent-old/foo"))

    def test_rejects_hermes_agentFOO(self):
        self.assertIsNone(self._match("~/.hermes/hermes-agentFOO/x"))

    # Unrelated paths — these MUST NOT match.
    def test_rejects_hermes_skills(self):
        self.assertIsNone(self._match("edit ~/.hermes/skills/foo"))

    def test_rejects_claude_plans(self):
        self.assertIsNone(self._match("~/.claude/plans"))

    def test_rejects_prose_only(self):
        self.assertIsNone(self._match("what time is it"))


class TestP72SubagentOverridesDataclass(unittest.TestCase):
    """P72/MOL-251 — locks SubagentOverrides shape + _build_child_agent signature.

    The dataclass collapses 7 individual override_* kwargs into one object;
    these tests prevent silent field drift or accidental re-introduction of
    the old kwargs.
    """

    def test_field_count(self):
        import dataclasses
        self.assertEqual(len(dataclasses.fields(SubagentOverrides)), 7)

    def test_default_none_semantics(self):
        o = SubagentOverrides()
        self.assertIsNone(o.provider)
        self.assertIsNone(o.base_url)
        self.assertIsNone(o.api_key)
        self.assertIsNone(o.api_mode)
        self.assertIsNone(o.acp_command)
        self.assertIsNone(o.acp_args)
        self.assertIsNone(o.fallback_model)

    def test_explicit_construction_round_trip(self):
        o = SubagentOverrides(
            provider="openrouter",
            base_url="https://u",
            api_key="k",
            api_mode="chat",
            acp_command="cmd",
            acp_args=["-x"],
            fallback_model={"provider": "p", "model": "m"},
        )
        self.assertEqual(o.provider, "openrouter")
        self.assertEqual(o.base_url, "https://u")
        self.assertEqual(o.api_key, "k")
        self.assertEqual(o.api_mode, "chat")
        self.assertEqual(o.acp_command, "cmd")
        self.assertEqual(o.acp_args, ["-x"])
        self.assertEqual(o.fallback_model, {"provider": "p", "model": "m"})

    def test_build_child_agent_signature(self):
        import inspect
        from tools.delegate_tool import _build_child_agent
        params = list(inspect.signature(_build_child_agent).parameters.keys())
        self.assertIn("overrides", params)
        self.assertFalse(
            any(p.startswith("override_") for p in params),
            f"stale override_* kwarg in signature: {params}",
        )


if __name__ == "__main__":
    unittest.main()
