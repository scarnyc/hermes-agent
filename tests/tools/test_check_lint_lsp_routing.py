"""Tests for ``ShellFileOperations._check_lint`` LSP routing.

Two regression guarantees:

1. With LSP disabled (the default), behaviour is unchanged for every
   extension — ``_check_lint`` still hits the in-process linter for
   .py/.json/.yaml/.toml and the shell linter table for .ts/.go/.rs.

2. With LSP enabled and the bridge returning a verdict, ``_check_lint``
   surfaces that verdict directly and skips the shell linter.

Both sides matter: a regression in (1) would break every existing user
who has not opted in; a regression in (2) would silently mean the
opt-in flag does nothing.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools import file_operations
from tools.file_operations import LintResult, ShellFileOperations
from tools.environments.local import LocalEnvironment


@pytest.fixture
def ops(tmp_path: Path) -> ShellFileOperations:
    env = LocalEnvironment(cwd=str(tmp_path), timeout=5)
    return ShellFileOperations(env, cwd=str(tmp_path))


# ---------------------------------------------------------------------------
# Default-off path
# ---------------------------------------------------------------------------


class TestDefaultOff:
    def test_python_still_uses_in_process_linter(self, ops, tmp_path: Path) -> None:
        path = tmp_path / "a.py"
        # Don't actually call the LSP bridge; the in-process linter should
        # short-circuit before it is even consulted.
        with patch("tools.lsp_lint.maybe_lint_via_lsp", return_value=None) as bridge:
            result = ops._check_lint(str(path), content="x = 1\n")
        assert result.success is True
        # Bridge is consulted (returning None) — that's the contract — but
        # the in-process linter still runs and produces the verdict.
        bridge.assert_called_once()

    def test_typescript_falls_through_to_shell_when_bridge_returns_none(
        self, ops, tmp_path: Path
    ) -> None:
        path = tmp_path / "x.ts"
        path.write_text("export {}\n")
        # Bridge says "not handling this" → the existing shell-linter path
        # must run. We don't actually exec tsc; we mock _has_command to
        # report it absent so the shell branch produces a deterministic
        # "skipped" result we can assert on.
        with patch("tools.lsp_lint.maybe_lint_via_lsp", return_value=None), \
             patch.object(ops, "_has_command", return_value=False):
            result = ops._check_lint(str(path), content="export {}\n")
        assert result.skipped is True
        assert "not available" in result.message


# ---------------------------------------------------------------------------
# LSP-on path
# ---------------------------------------------------------------------------


class TestLspRouting:
    def test_lsp_clean_short_circuits_shell_linter(self, ops, tmp_path: Path) -> None:
        path = tmp_path / "x.ts"
        path.write_text("export {}\n")
        clean = LintResult(success=True, output="")
        with patch("tools.lsp_lint.maybe_lint_via_lsp", return_value=clean), \
             patch.object(ops, "_has_command") as has_command:
            result = ops._check_lint(str(path), content="export {}\n")
        assert result is clean
        # Shell linter must not even probe for `tsc` when LSP returned a
        # verdict; otherwise we'd be paying for both subprocess startups.
        has_command.assert_not_called()

    def test_lsp_errors_short_circuit_shell_linter(self, ops, tmp_path: Path) -> None:
        path = tmp_path / "x.ts"
        path.write_text("export {}\n")
        dirty = LintResult(success=False, output=str(path) + ":1:1: error: boom")
        with patch("tools.lsp_lint.maybe_lint_via_lsp", return_value=dirty), \
             patch.object(ops, "_has_command") as has_command:
            result = ops._check_lint(str(path), content="export {}\n")
        assert result is dirty
        has_command.assert_not_called()

    def test_lsp_bridge_exception_falls_through_safely(self, ops, tmp_path: Path) -> None:
        # Even if the bridge module itself throws (broken import, config
        # parse error, etc.) the lint hook must never propagate — the
        # write_file caller would otherwise see a synthetic error for an
        # otherwise-successful write.
        path = tmp_path / "x.ts"
        path.write_text("export {}\n")
        with patch("tools.lsp_lint.maybe_lint_via_lsp", side_effect=RuntimeError("oops")), \
             patch.object(ops, "_has_command", return_value=False):
            result = ops._check_lint(str(path), content="export {}\n")
        assert result.skipped is True
