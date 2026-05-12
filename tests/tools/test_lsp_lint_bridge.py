"""Tests for ``tools.lsp_lint`` — the bridge between ``_check_lint`` and the
LSP client.

We do not spin up a real language server here. Instead we monkeypatch
``get_or_start_client`` with a fake that returns whatever diagnostics the
test wants. This keeps the bridge logic — feature flag, language map,
project-root gating, error containment — under tight unit-test control
while leaving the real LSP wire-protocol coverage to test_lsp_client.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tools import lsp_lint
from tools.lsp_client import Diagnostic
from tools.environments.local import LocalEnvironment


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, diags: list[Diagnostic] | Exception | None = None) -> None:
        self._diags = diags

    def diagnostics(self, path: str, content: str, *, timeout: float, settle_ms: int) -> list[Diagnostic]:
        if isinstance(self._diags, Exception):
            raise self._diags
        return list(self._diags or [])


@pytest.fixture
def ts_project(tmp_path: Path) -> tuple[Path, Path]:
    """A tsconfig-rooted project with one source file."""
    (tmp_path / "tsconfig.json").write_text("{}")
    src = tmp_path / "src" / "app.ts"
    src.parent.mkdir(parents=True)
    src.write_text("export {}\n")
    return tmp_path, src


@pytest.fixture
def local_env(tmp_path: Path) -> LocalEnvironment:
    return LocalEnvironment(cwd=str(tmp_path), timeout=5)


# ---------------------------------------------------------------------------
# Feature-flag gating
# ---------------------------------------------------------------------------


class TestFeatureFlag:
    def test_returns_none_when_disabled(self, ts_project, local_env) -> None:
        _, src = ts_project
        with patch.object(lsp_lint, "_load_lsp_config", return_value={"enabled": False}):
            result = lsp_lint.maybe_lint_via_lsp(str(src), "export {}\n", local_env)
        assert result is None

    def test_returns_none_when_config_missing(self, ts_project, local_env) -> None:
        _, src = ts_project
        with patch.object(lsp_lint, "_load_lsp_config", return_value={}):
            result = lsp_lint.maybe_lint_via_lsp(str(src), "export {}\n", local_env)
        assert result is None

    def test_returns_none_when_content_missing(self, ts_project, local_env) -> None:
        _, src = ts_project
        with patch.object(lsp_lint, "_load_lsp_config", return_value={"enabled": True}):
            result = lsp_lint.maybe_lint_via_lsp(str(src), None, local_env)
        # We never lint without content — there is no point sending an empty
        # didOpen text payload.
        assert result is None


# ---------------------------------------------------------------------------
# Backend gating
# ---------------------------------------------------------------------------


class TestBackendGating:
    def test_returns_none_for_non_local_env(self, ts_project) -> None:
        _, src = ts_project

        class _FakeRemoteEnv:
            cwd = "/remote/workspace"

        with patch.object(lsp_lint, "_load_lsp_config", return_value={"enabled": True}):
            result = lsp_lint.maybe_lint_via_lsp(str(src), "export {}\n", _FakeRemoteEnv())
        # Container/SSH/Modal backends are deliberately excluded in this PR.
        assert result is None


# ---------------------------------------------------------------------------
# Project-root gating
# ---------------------------------------------------------------------------


class TestProjectRootGating:
    def test_orphan_file_returns_none(self, tmp_path: Path, local_env) -> None:
        # No tsconfig anywhere on the way up — the bridge must abort
        # rather than reproduce the phantom-error problem.
        orphan_dir = tmp_path / "orphan"
        orphan_dir.mkdir()
        orphan = orphan_dir / "loose.ts"
        orphan.write_text("export {}\n")
        with patch.object(lsp_lint, "_load_lsp_config", return_value={"enabled": True}):
            result = lsp_lint.maybe_lint_via_lsp(str(orphan), "export {}\n", local_env)
        assert result is None


# ---------------------------------------------------------------------------
# Language map
# ---------------------------------------------------------------------------


class TestLanguageMap:
    def test_unsupported_extension_returns_none(self, tmp_path: Path, local_env) -> None:
        path = tmp_path / "file.txt"
        path.write_text("hello")
        with patch.object(lsp_lint, "_load_lsp_config", return_value={"enabled": True}):
            result = lsp_lint.maybe_lint_via_lsp(str(path), "hello", local_env)
        assert result is None


# ---------------------------------------------------------------------------
# Happy path + error containment
# ---------------------------------------------------------------------------


class TestRouting:
    def test_clean_run_returns_success_lint_result(self, ts_project, local_env) -> None:
        _, src = ts_project
        with patch.object(lsp_lint, "_load_lsp_config", return_value={"enabled": True}), \
             patch.object(lsp_lint, "get_or_start_client", return_value=_FakeClient([])):
            result = lsp_lint.maybe_lint_via_lsp(str(src), "export {}\n", local_env)
        assert result is not None
        assert result.success is True
        assert result.output == ""

    def test_errors_become_failure_with_formatted_output(self, ts_project, local_env) -> None:
        _, src = ts_project
        diags = [
            Diagnostic(line=12, column=4, severity=1, message="Cannot find name 'foo'", source="ts", code="2304"),
            Diagnostic(line=15, column=1, severity=2, message="Unused import", source="ts"),
        ]
        with patch.object(lsp_lint, "_load_lsp_config", return_value={"enabled": True}), \
             patch.object(lsp_lint, "get_or_start_client", return_value=_FakeClient(diags)):
            result = lsp_lint.maybe_lint_via_lsp(str(src), "export {}\n", local_env)
        assert result is not None
        assert result.success is False
        assert ":12:4: error: Cannot find name 'foo'" in result.output
        assert ":15:1: warning: Unused import" in result.output

    def test_hint_severity_is_filtered_out(self, ts_project, local_env) -> None:
        _, src = ts_project
        diags = [
            Diagnostic(line=1, column=1, severity=4, message="hint", source="ts"),
            Diagnostic(line=2, column=1, severity=3, message="info", source="ts"),
        ]
        with patch.object(lsp_lint, "_load_lsp_config", return_value={"enabled": True}), \
             patch.object(lsp_lint, "get_or_start_client", return_value=_FakeClient(diags)):
            result = lsp_lint.maybe_lint_via_lsp(str(src), "export {}\n", local_env)
        # No errors or warnings → treated as clean. Otherwise we'd be
        # changing the agent's verdict mid-edit when shell linters never
        # surfaced these severities.
        assert result is not None
        assert result.success is True
        assert result.output == ""

    def test_client_unavailable_returns_none(self, ts_project, local_env) -> None:
        _, src = ts_project
        with patch.object(lsp_lint, "_load_lsp_config", return_value={"enabled": True}), \
             patch.object(lsp_lint, "get_or_start_client", return_value=None):
            result = lsp_lint.maybe_lint_via_lsp(str(src), "export {}\n", local_env)
        # Server binary missing or failed to spawn: caller must fall through
        # to the legacy shell linter rather than report a phantom-clean.
        assert result is None

    def test_diagnostics_timeout_returns_none(self, ts_project, local_env) -> None:
        _, src = ts_project
        with patch.object(lsp_lint, "_load_lsp_config", return_value={"enabled": True}), \
             patch.object(lsp_lint, "get_or_start_client",
                          return_value=_FakeClient(TimeoutError("slow"))):
            result = lsp_lint.maybe_lint_via_lsp(str(src), "export {}\n", local_env)
        assert result is None

    def test_unexpected_exception_returns_none(self, ts_project, local_env) -> None:
        _, src = ts_project
        with patch.object(lsp_lint, "_load_lsp_config", return_value={"enabled": True}), \
             patch.object(lsp_lint, "get_or_start_client",
                          return_value=_FakeClient(RuntimeError("server crash"))):
            result = lsp_lint.maybe_lint_via_lsp(str(src), "export {}\n", local_env)
        # The lint hook is on the hot edit path — bridge must never raise.
        assert result is None
