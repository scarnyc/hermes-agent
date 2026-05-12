"""Tests for ``tools.lsp_lint`` — the bridge between ``_check_lint`` and the
LSP client.

We do not spin up a real language server here. Instead we monkeypatch
``get_or_start_client`` with a fake that returns whatever diagnostics the
test wants. This keeps the bridge logic — feature flag, language map,
project-root gating, error containment, observability — under tight
unit-test control while leaving the real LSP wire-protocol coverage to
test_lsp_client.py.
"""

from __future__ import annotations

import logging
from pathlib import Path
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


# ---------------------------------------------------------------------------
# Observability — log levels and once-per-X dedup
# ---------------------------------------------------------------------------
#
# The default agent.log threshold is INFO. These tests pin down that
# *steady state* (1000 clean writes, 1000 feature-off writes, etc.) emits
# zero records visible to the agent.log handler, and that *novel* events
# get exactly one WARNING/INFO line that survives.


@pytest.fixture(autouse=True)
def _reset_dedup_caches():
    """Each test sees a fresh dedup state so prior runs cannot mask
    a missing announcement (or hide a duplicate one)."""
    lsp_lint._reset_announce_caches()
    yield
    lsp_lint._reset_announce_caches()


def _records_at(caplog, level: int) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= level]


class TestLogLevelsSteadyState:
    """Anything that fires every write must stay at DEBUG so 1000 writes
    cannot flood agent.log at the default INFO threshold."""

    def test_feature_off_is_debug(self, ts_project, local_env, caplog) -> None:
        _, src = ts_project
        with caplog.at_level(logging.DEBUG, logger="hermes.lint.lsp"), \
             patch.object(lsp_lint, "_load_lsp_config", return_value={"enabled": False}):
            for _ in range(5):
                lsp_lint.maybe_lint_via_lsp(str(src), "export {}\n", local_env)
        assert _records_at(caplog, logging.INFO) == []

    def test_unmapped_extension_is_debug(self, tmp_path: Path, local_env, caplog) -> None:
        path = tmp_path / "x.txt"
        path.write_text("hi")
        with caplog.at_level(logging.DEBUG, logger="hermes.lint.lsp"), \
             patch.object(lsp_lint, "_load_lsp_config", return_value={"enabled": True}):
            for _ in range(5):
                lsp_lint.maybe_lint_via_lsp(str(path), "hi", local_env)
        assert _records_at(caplog, logging.INFO) == []

    def test_non_local_backend_is_debug(self, ts_project, caplog) -> None:
        _, src = ts_project

        class _FakeRemote:
            cwd = "/remote"

        with caplog.at_level(logging.DEBUG, logger="hermes.lint.lsp"), \
             patch.object(lsp_lint, "_load_lsp_config", return_value={"enabled": True}):
            for _ in range(5):
                lsp_lint.maybe_lint_via_lsp(str(src), "export {}\n", _FakeRemote())
        assert _records_at(caplog, logging.INFO) == []

    def test_clean_write_is_debug(self, ts_project, local_env, caplog) -> None:
        _, src = ts_project
        with caplog.at_level(logging.DEBUG, logger="hermes.lint.lsp"), \
             patch.object(lsp_lint, "_load_lsp_config", return_value={"enabled": True}), \
             patch.object(lsp_lint, "get_or_start_client", return_value=_FakeClient([])):
            # First write produces ONE INFO ("active for ...") for the
            # state transition. Subsequent clean writes must be DEBUG so
            # 1000 clean writes don't carpet agent.log.
            for _ in range(5):
                lsp_lint.maybe_lint_via_lsp(str(src), "export {}\n", local_env)
        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(infos) == 1, f"expected exactly one 'active' INFO, got {[r.getMessage() for r in infos]}"
        assert "active for" in infos[0].getMessage()


class TestLogLevelsNovelEvents:
    """Things that change state or require action must escape DEBUG."""

    def test_diagnostics_are_info_per_call(self, ts_project, local_env, caplog) -> None:
        _, src = ts_project
        diags = [Diagnostic(line=1, column=1, severity=1, message="boom")]
        with caplog.at_level(logging.INFO, logger="hermes.lint.lsp"), \
             patch.object(lsp_lint, "_load_lsp_config", return_value={"enabled": True}), \
             patch.object(lsp_lint, "get_or_start_client", return_value=_FakeClient(diags)):
            for _ in range(3):
                lsp_lint.maybe_lint_via_lsp(str(src), "export {}\n", local_env)
        # Anchor on the diagnostic message shape — temp paths can contain
        # "diag" as a substring (e.g. test_diagnostics_are_info_per_0/), so
        # a bare ``"diag" in msg`` filter would over-match the active line.
        diag_lines = [r.getMessage() for r in caplog.records if "] 1 diag (" in r.getMessage()]
        assert len(diag_lines) == 3

    def test_active_announcement_fires_once_per_root(self, ts_project, local_env, caplog) -> None:
        _, src = ts_project
        with caplog.at_level(logging.INFO, logger="hermes.lint.lsp"), \
             patch.object(lsp_lint, "_load_lsp_config", return_value={"enabled": True}), \
             patch.object(lsp_lint, "get_or_start_client", return_value=_FakeClient([])):
            for _ in range(10):
                lsp_lint.maybe_lint_via_lsp(str(src), "export {}\n", local_env)
        actives = [r for r in caplog.records if "active for" in r.getMessage()]
        assert len(actives) == 1


class TestLogLevelsActionRequired:
    """First occurrence WARNING, subsequent same-event DEBUG."""

    def test_server_unavailable_warns_once(self, ts_project, local_env, caplog) -> None:
        _, src = ts_project
        with caplog.at_level(logging.DEBUG, logger="hermes.lint.lsp"), \
             patch.object(lsp_lint, "_load_lsp_config", return_value={"enabled": True}), \
             patch.object(lsp_lint, "get_or_start_client", return_value=None):
            for _ in range(5):
                lsp_lint.maybe_lint_via_lsp(str(src), "export {}\n", local_env)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        # Exactly one WARNING — the other 4 calls demoted to DEBUG so
        # errors.log (WARNING+) doesn't get the same line 1000 times.
        assert len(warnings) == 1
        assert "server unavailable" in warnings[0].getMessage()

    def test_no_project_root_info_once_per_path(self, tmp_path: Path, local_env, caplog) -> None:
        # Two distinct orphan files → two INFO lines. Same orphan twice → one.
        orphan_a = tmp_path / "a.ts"
        orphan_a.write_text("")
        orphan_b = tmp_path / "b.ts"
        orphan_b.write_text("")
        with caplog.at_level(logging.DEBUG, logger="hermes.lint.lsp"), \
             patch.object(lsp_lint, "_load_lsp_config", return_value={"enabled": True}):
            lsp_lint.maybe_lint_via_lsp(str(orphan_a), "", local_env)
            lsp_lint.maybe_lint_via_lsp(str(orphan_a), "", local_env)
            lsp_lint.maybe_lint_via_lsp(str(orphan_b), "", local_env)
            lsp_lint.maybe_lint_via_lsp(str(orphan_b), "", local_env)
        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(infos) == 2, [r.getMessage() for r in infos]

    def test_timeout_warns_every_time(self, ts_project, local_env, caplog) -> None:
        # Timeouts are inherently novel events (a hang now isn't the same
        # signal as a hang yesterday), so every one must escape DEBUG.
        _, src = ts_project
        with caplog.at_level(logging.WARNING, logger="hermes.lint.lsp"), \
             patch.object(lsp_lint, "_load_lsp_config", return_value={"enabled": True}), \
             patch.object(lsp_lint, "get_or_start_client",
                          return_value=_FakeClient(TimeoutError("slow"))):
            for _ in range(3):
                lsp_lint.maybe_lint_via_lsp(str(src), "export {}\n", local_env)
        timeouts = [r for r in caplog.records if "timeout" in r.getMessage()]
        assert len(timeouts) == 3
