"""Integration test: full hook flow pre_tool_call → write → transform_tool_result.

Verifies that the plugin hook wiring correctly:
1. Captures a baseline in pre_tool_call
2. Passes through a write (no interference)
3. Injects diagnostics in transform_tool_result

Uses a mocked LSP service to avoid requiring pyright/gopls in CI.
"""
from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _isolate():
    """Clear plugin state between tests."""
    from plugins import lsp as lsp_plugin
    lsp_plugin._baselines.clear()
    old_service = lsp_plugin._service
    yield
    lsp_plugin._baselines.clear()
    lsp_plugin._service = old_service


class FakeLSPService:
    """Minimal LSP service mock that returns canned diagnostics."""

    def __init__(self, diagnostics=None):
        self._diagnostics = diagnostics or []

    def is_active(self):
        return True

    def enabled_for(self, path):
        return path.endswith(".py") or path.endswith(".ts")

    def snapshot_baseline(self, path):
        pass  # no-op, just marks that we visited

    def get_diagnostics_sync(self, path, delta=True, timeout=3.0):
        return self._diagnostics

    def shutdown(self):
        pass


def test_full_hook_flow_produces_diagnostics(tmp_path):
    """Exercise pre_tool_call → (write) → transform_tool_result end-to-end."""
    from plugins import lsp as lsp_plugin

    test_file = tmp_path / "broken.py"
    test_file.write_text("x: int = 'oops'\n")
    abs_path = str(test_file)

    fake_service = FakeLSPService(diagnostics=[
        {
            "severity": 1,
            "range": {"start": {"line": 0, "character": 9}},
            "message": 'Expression of type "str" is incompatible with declared type "int"',
            "code": "reportAssignmentType",
            "source": "Pyright",
        }
    ])

    with patch.object(lsp_plugin, "_service", fake_service):
        # Step 1: pre_tool_call captures baseline
        lsp_plugin._pre_tool_call(
            tool_name="write_file",
            args={"path": abs_path, "content": "x: int = 'oops'\n"},
            session_id="test-session",
            tool_call_id="call-001",
        )
        assert ("test-session", abs_path) in lsp_plugin._baselines

        # Step 2: simulate the write completing (tool output)
        tool_result = json.dumps({
            "bytes_written": 16,
            "dirs_created": False,
            "lint": None,
        })

        # Step 3: transform_tool_result injects diagnostics
        transformed = lsp_plugin._transform_tool_result(
            tool_name="write_file",
            args={"path": abs_path, "content": "x: int = 'oops'\n"},
            result=tool_result,
            session_id="test-session",
            tool_call_id="call-001",
        )

    # Verify: result is valid JSON with lsp_diagnostics field
    assert transformed is not None
    data = json.loads(transformed)
    assert "lsp_diagnostics" in data
    assert "reportAssignmentType" in data["lsp_diagnostics"]
    assert "Pyright" in data["lsp_diagnostics"]
    # Original fields preserved
    assert data["bytes_written"] == 16
    assert data["dirs_created"] is False

    # Baseline consumed (removed after use)
    assert ("test-session", abs_path) not in lsp_plugin._baselines


def test_hook_flow_returns_none_when_no_diagnostics(tmp_path):
    """transform_tool_result returns None (no modification) when LSP is clean."""
    from plugins import lsp as lsp_plugin

    test_file = tmp_path / "clean.py"
    test_file.write_text("x: int = 42\n")
    abs_path = str(test_file)

    fake_service = FakeLSPService(diagnostics=[])  # Clean — no errors

    with patch.object(lsp_plugin, "_service", fake_service):
        lsp_plugin._pre_tool_call(
            tool_name="write_file",
            args={"path": abs_path, "content": "x: int = 42\n"},
            session_id="test-session",
            tool_call_id="call-002",
        )

        transformed = lsp_plugin._transform_tool_result(
            tool_name="write_file",
            args={"path": abs_path, "content": "x: int = 42\n"},
            result='{"bytes_written": 12}',
            session_id="test-session",
            tool_call_id="call-002",
        )

    # No diagnostics → return None → result unchanged
    assert transformed is None


def test_hook_flow_no_baseline_means_no_injection(tmp_path):
    """transform_tool_result does nothing if pre_tool_call didn't fire."""
    from plugins import lsp as lsp_plugin

    test_file = tmp_path / "no_baseline.py"
    abs_path = str(test_file)

    fake_service = FakeLSPService(diagnostics=[
        {"severity": 1, "range": {"start": {"line": 0, "character": 0}},
         "message": "error", "code": "E1", "source": "test"}
    ])

    with patch.object(lsp_plugin, "_service", fake_service):
        # Skip pre_tool_call — simulate a case where it didn't fire
        transformed = lsp_plugin._transform_tool_result(
            tool_name="write_file",
            args={"path": abs_path},
            result='{"bytes_written": 5}',
            session_id="test-session",
            tool_call_id="call-003",
        )

    # No baseline was captured, so no injection
    assert transformed is None


def test_hook_flow_patch_tool(tmp_path):
    """Hook flow works for patch tool (single-path mode)."""
    from plugins import lsp as lsp_plugin

    test_file = tmp_path / "patched.py"
    test_file.write_text("def f() -> int:\n    return 'wrong'\n")
    abs_path = str(test_file)

    fake_service = FakeLSPService(diagnostics=[
        {
            "severity": 1,
            "range": {"start": {"line": 1, "character": 11}},
            "message": 'Cannot return "str" from function with return type "int"',
            "code": "reportReturnType",
            "source": "Pyright",
        }
    ])

    with patch.object(lsp_plugin, "_service", fake_service):
        lsp_plugin._pre_tool_call(
            tool_name="patch",
            args={"path": abs_path, "old_string": "return 42", "new_string": "return 'wrong'"},
            session_id="test-session",
            tool_call_id="call-004",
        )

        transformed = lsp_plugin._transform_tool_result(
            tool_name="patch",
            args={"path": abs_path, "old_string": "return 42", "new_string": "return 'wrong'"},
            result='{"success": true, "diff": "..."}',
            session_id="test-session",
            tool_call_id="call-004",
        )

    assert transformed is not None
    data = json.loads(transformed)
    assert "lsp_diagnostics" in data
    assert "reportReturnType" in data["lsp_diagnostics"]
