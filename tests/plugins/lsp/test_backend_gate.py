"""Integration test: LSP plugin skips non-local paths.

The host-side LSP server can't see files inside Docker/Modal/SSH
sandboxes.  The plugin's ``_pre_tool_call`` uses ``os.path.exists``
on the parent directory as a heuristic local-only gate.  These tests
verify the plugin hooks skip when the path clearly doesn't exist on
the host filesystem.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_plugin_state():
    """Reset plugin module state between tests."""
    # Import the plugin and clear any service state
    from plugins.lsp import _baselines
    _baselines.clear()
    yield
    _baselines.clear()


def test_pre_tool_call_skips_nonexistent_parent_dir():
    """pre_tool_call returns early when the path's parent dir doesn't exist (Docker/SSH heuristic)."""
    from plugins import lsp as lsp_plugin

    # Simulate a path that doesn't exist on host (e.g., inside Docker)
    fake_path = "/nonexistent-docker-container-fs/app/main.py"

    # Mock _ensure_service to return a mock service
    mock_service = type("MockService", (), {
        "is_active": lambda self: True,
        "enabled_for": lambda self, p: True,
        "snapshot_baseline": lambda self, p: None,
    })()

    with patch.object(lsp_plugin, "_service", mock_service):
        lsp_plugin._pre_tool_call(
            tool_name="write_file",
            args={"path": fake_path},
            session_id="test-session",
            tool_call_id="call-1",
        )

    # Baseline should NOT be captured because parent dir doesn't exist
    assert ("test-session", os.path.normpath(fake_path)) not in lsp_plugin._baselines


def test_pre_tool_call_proceeds_for_local_path(tmp_path):
    """pre_tool_call captures baseline when path exists locally."""
    from plugins import lsp as lsp_plugin

    # Create a real file so the parent-dir check passes
    test_file = tmp_path / "test.py"
    test_file.write_text("x = 1\n")

    mock_service = type("MockService", (), {
        "is_active": lambda self: True,
        "enabled_for": lambda self, p: True,
        "snapshot_baseline": lambda self, p: None,
    })()

    with patch.object(lsp_plugin, "_service", mock_service):
        lsp_plugin._pre_tool_call(
            tool_name="write_file",
            args={"path": str(test_file)},
            session_id="test-session",
            tool_call_id="call-2",
        )

    # Baseline SHOULD be captured because the local path exists
    assert ("test-session", str(test_file)) in lsp_plugin._baselines


def test_pre_tool_call_skips_non_write_tools():
    """pre_tool_call is a no-op for tools other than write_file/patch."""
    from plugins import lsp as lsp_plugin

    lsp_plugin._pre_tool_call(
        tool_name="terminal",
        args={"command": "ls"},
        session_id="test-session",
        tool_call_id="call-3",
    )

    assert len(lsp_plugin._baselines) == 0


def test_pre_tool_call_skips_v4a_patch():
    """pre_tool_call skips V4A multi-file patches (has 'patch' key, no 'path' key)."""
    from plugins import lsp as lsp_plugin

    mock_service = type("MockService", (), {
        "is_active": lambda self: True,
        "enabled_for": lambda self, p: True,
        "snapshot_baseline": lambda self, p: None,
    })()

    with patch.object(lsp_plugin, "_service", mock_service):
        lsp_plugin._pre_tool_call(
            tool_name="patch",
            args={"patch": "*** Begin Patch\n*** Update File: foo.py\n..."},
            session_id="test-session",
            tool_call_id="call-4",
        )

    assert len(lsp_plugin._baselines) == 0


def test_transform_tool_result_injects_diagnostics(tmp_path):
    """transform_tool_result appends lsp_diagnostics field to JSON result."""
    from plugins import lsp as lsp_plugin

    test_file = tmp_path / "test.py"
    abs_path = str(test_file)

    # Pre-populate a baseline entry (simulating pre_tool_call ran)
    lsp_plugin._baselines[("test-session", abs_path)] = []

    # Mock service that returns a diagnostic
    mock_service = type("MockService", (), {
        "is_active": lambda self: True,
        "enabled_for": lambda self, p: True,
        "get_diagnostics_sync": lambda self, p, delta=True, timeout=3.0: [
            {
                "severity": 1,
                "range": {"start": {"line": 1, "character": 4}},
                "message": "Type error: str is not int",
                "code": "reportReturnType",
                "source": "Pyright",
            }
        ],
    })()

    with patch.object(lsp_plugin, "_service", mock_service):
        result = lsp_plugin._transform_tool_result(
            tool_name="write_file",
            args={"path": abs_path},
            result='{"bytes_written": 42, "dirs_created": false}',
            session_id="test-session",
            tool_call_id="call-5",
        )

    assert result is not None
    import json
    data = json.loads(result)
    assert "lsp_diagnostics" in data
    assert "reportReturnType" in data["lsp_diagnostics"]
    assert "bytes_written" in data  # Original fields preserved
