"""Bridge between ``ShellFileOperations._check_lint`` and ``tools.lsp_client``.

Kept in its own module so the change to ``file_operations.py`` is a single
import + one branch in ``_check_lint`` — easy to audit, easy to revert.

Behavioural contract for ``maybe_lint_via_lsp``:
    * Returns ``None`` whenever LSP cannot or should not handle this file
      (feature off, language not configured, env not local, no project
      root, server missing, request timed out). The caller falls back to
      the existing in-process / shell linter exactly as before.
    * Returns a ``LintResult`` only when the LSP path produced a real
      verdict — clean or with diagnostics.

We never raise. The lint hook is on the hot edit path and any exception
here would surface as a write failure rather than a degraded warning.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, Optional

from tools.lsp_client import (
    Diagnostic,
    LspClient,
    find_project_root,
    format_diagnostics,
    get_or_start_client,
)

if TYPE_CHECKING:
    from tools.file_operations import LintResult

logger = logging.getLogger(__name__)


# Map file extension → LSP language id. Mirrors the ``LINTERS`` table in
# file_operations.py but only covers languages where we have an opinion
# about which server to use. New extensions added here MUST also appear
# in the ``servers`` config block below or in the user override.
_EXT_TO_LANGUAGE: dict[str, str] = {
    ".ts": "typescript",
    ".tsx": "typescriptreact",
    ".mts": "typescript",
    ".cts": "typescript",
    ".js": "javascript",
    ".jsx": "javascriptreact",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".rs": "rust",
    ".go": "go",
}


# Default server command per language. Every entry can be overridden via
# ``lint.lsp.servers.<language>`` in config.yaml. Keep these to the
# upstream-recommended invocation; users who want overlay configurations
# (extra args, alternate binaries) live in their own config.
_DEFAULT_SERVERS: dict[str, list[str]] = {
    "typescript": ["typescript-language-server", "--stdio"],
    "typescriptreact": ["typescript-language-server", "--stdio"],
    "javascript": ["typescript-language-server", "--stdio"],
    "javascriptreact": ["typescript-language-server", "--stdio"],
    "rust": ["rust-analyzer"],
    "go": ["gopls"],
}


def _load_lsp_config() -> dict[str, Any]:
    """Read ``lint.lsp`` from config.yaml, with deep defaults baked in.

    Cached imports are deliberately avoided here: the lint hook runs
    inside long-lived agent loops and a stale cache would prevent users
    from toggling the flag mid-session via ``/config reload``. The yaml
    parse cost is negligible compared to the actual lint subprocess.
    """
    try:
        from hermes_cli.config import load_config
    except Exception:
        return {}
    try:
        cfg = load_config() or {}
    except Exception:
        return {}
    lint_cfg = cfg.get("lint")
    if not isinstance(lint_cfg, dict):
        return {}
    lsp_cfg = lint_cfg.get("lsp")
    return lsp_cfg if isinstance(lsp_cfg, dict) else {}


def _resolve_server_cmd(language_id: str, lsp_cfg: dict[str, Any]) -> Optional[list[str]]:
    overrides = lsp_cfg.get("servers")
    if isinstance(overrides, dict):
        candidate = overrides.get(language_id)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.split()
        if isinstance(candidate, list) and candidate:
            return [str(part) for part in candidate]
    return list(_DEFAULT_SERVERS.get(language_id, []))


def _is_local_env(env: object) -> bool:
    """LSP only routes to local files in this PR.

    Rationale lives in lsp_client.py: container/SSH backends need the
    server inside the sandbox, which is a separate engineering effort.
    Until that lands, anything non-local falls through to the shell
    linter so we don't pretend to lint a file the host process can't
    even open.
    """
    try:
        from tools.environments.local import LocalEnvironment
    except Exception:
        return False
    return isinstance(env, LocalEnvironment)


def maybe_lint_via_lsp(
    file_path: str,
    content: Optional[str],
    env: object,
) -> Optional["LintResult"]:
    """Try to produce a LintResult via LSP. Returns ``None`` when caller
    should fall through to the in-process / shell linter.

    Args:
        file_path: Absolute path of the just-written file.
        content: Post-write file content. Required — we send it as the
            ``didOpen`` text payload so the server lints the bytes the
            agent intended to land on disk, not whatever was there
            before the write completed.
        env: Terminal environment from ``ShellFileOperations.env``.
            We currently only handle ``LocalEnvironment``.
    """
    if content is None:
        return None
    if not _is_local_env(env):
        return None

    lsp_cfg = _load_lsp_config()
    if not lsp_cfg.get("enabled"):
        return None

    ext = os.path.splitext(file_path)[1].lower()
    language_id = _EXT_TO_LANGUAGE.get(ext)
    if language_id is None:
        return None

    project_root = find_project_root(file_path, language_id)
    if project_root is None:
        # No tsconfig.json / Cargo.toml / go.mod in the ancestor chain.
        # Linting an orphan file via LSP would reproduce the very
        # phantom-error problem we are trying to escape, so abort.
        return None

    server_cmd = _resolve_server_cmd(language_id, lsp_cfg)
    if not server_cmd:
        return None

    idle_seconds = float(lsp_cfg.get("idle_shutdown") or 600.0)
    try:
        client = get_or_start_client(
            language_id,
            project_root,
            server_cmd,
            idle_seconds=idle_seconds,
        )
    except Exception as exc:
        logger.debug("LSP get_or_start_client failed: %s", exc)
        return None
    if client is None:
        return None

    timeout = float(lsp_cfg.get("diagnostic_timeout") or 10.0)
    settle_ms = int(lsp_cfg.get("settle_ms") or 400)
    try:
        diagnostics: list[Diagnostic] = client.diagnostics(
            file_path,
            content,
            timeout=timeout,
            settle_ms=settle_ms,
        )
    except (TimeoutError, RuntimeError) as exc:
        logger.debug("LSP diagnostics(%s) failed: %s", file_path, exc)
        return None
    except Exception as exc:  # noqa: BLE001 — never raise from the lint hot path
        logger.warning("LSP diagnostics(%s) raised unexpectedly: %s", file_path, exc)
        return None

    # Filter to errors + warnings. Hints/info are skipped because the
    # existing shell linters never surface them, and surfacing them here
    # would change the agent's "lint clean / lint dirty" verdict mid-PR.
    blocking = [d for d in diagnostics if d.severity in (1, 2)]

    # Lazy import to avoid a circular import at module load time.
    from tools.file_operations import LintResult

    if not blocking:
        return LintResult(success=True, output="")
    return LintResult(success=False, output=format_diagnostics(file_path, blocking))


__all__ = [
    "maybe_lint_via_lsp",
]
