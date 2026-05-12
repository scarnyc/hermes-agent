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

Observability
-------------
Calls emit structured log lines on the dedicated ``hermes.lint.lsp``
logger so live agent sessions can answer "did the LSP path fire on
that edit?" with a single ``rg``. Levels are tuned for a 1000-write
session — steady state is silent at the default INFO threshold, only
state changes and action-required failures escape:

* ``DEBUG`` (default agent.log threshold = INFO, so invisible) for
  every per-call event that has no novel signal: ``clean``, ``feature
  off``, ``extension not mapped``, ``backend not local``, repeated
  ``no project root`` for an already-announced file, repeated ``server
  unavailable`` for an already-announced binary.
* ``INFO`` for state transitions worth surfacing exactly once:
  ``active for <root>`` the first time a (language, project_root)
  client starts, ``no project root for <path>`` the first time we see
  that file. Plus every ``N diags`` event (the failure signal users
  actually want — these are inherently rare and per-edit).
* ``WARNING`` for action-required failures the first time they happen
  per (language, binary): ``server unavailable`` (binary not on PATH),
  ``no server configured``. Per-call ``WARNING`` for timeouts, server
  errors, and unexpected bridge exceptions — these are inherently
  novel events, not steady state.

Net result: an agent that writes 1000 clean ``.ts`` files in one
project session emits ONE INFO line (``active for <root>``) and 1000
DEBUG lines that never reach ``agent.log`` at default level.

Grep recipe::

    tail -f ~/.hermes/logs/agent.log | rg 'lsp\\['
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING, Any, Optional

from tools.lsp_client import (
    Diagnostic,
    find_project_root,
    format_diagnostics,
    get_or_start_client,
)

if TYPE_CHECKING:
    from tools.file_operations import LintResult

logger = logging.getLogger(__name__)
# Dedicated logger name so it survives a ``logging.getLogger(__name__)``
# rename of the module without breaking the documented grep recipe.
_event_log = logging.getLogger("hermes.lint.lsp")


def _short_path(file_path: str) -> str:
    """Render *file_path* relative to the cwd when sensible, else absolute.

    Keeps the log line readable for the common case (the user is inside
    the project they're editing) without emitting brittle ``../../..``
    chains for the cross-tree case.
    """
    try:
        rel = os.path.relpath(file_path)
    except ValueError:
        return file_path
    if rel.startswith(".." + os.sep) or rel == "..":
        return file_path
    return rel


def _log(language_id: str, level: int, message: str) -> None:
    _event_log.log(level, "lsp[%s] %s", language_id, message)


# ---------------------------------------------------------------------------
# Once-per-X dedup for steady-state events
# ---------------------------------------------------------------------------
#
# Why these aren't bounded LRU caches: each set grows at most by the number
# of distinct (language, project_root) and (language, file_path) pairs the
# user touches in a single Python process. Even an aggressive monorepo
# session is well under a few hundred entries — bytes of memory, not MB.
# Bounded eviction would risk re-firing INFO/WARNING lines for the same
# event, which is exactly what these caches exist to prevent.

_announce_lock = threading.Lock()
_announced_active: set[tuple[str, str]] = set()
_announced_unavailable: set[tuple[str, str]] = set()
_announced_no_root: set[tuple[str, str]] = set()


def _reset_announce_caches() -> None:
    """Clear the dedup caches. Test-only — production code never calls this."""
    with _announce_lock:
        _announced_active.clear()
        _announced_unavailable.clear()
        _announced_no_root.clear()


def _announce_once(bucket: set[tuple[str, str]], key: tuple[str, str]) -> bool:
    """Return True if *key* has not been announced for *bucket* yet.

    Atomically marks the key as announced as a side effect, so concurrent
    callers cannot both win the race and double-log.
    """
    with _announce_lock:
        if key in bucket:
            return False
        bucket.add(key)
        return True


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
        # No content means the caller (delta refinement against an absent
        # pre-write state) has nothing for didOpen — silent skip.
        return None

    ext = os.path.splitext(file_path)[1].lower()
    language_id = _EXT_TO_LANGUAGE.get(ext)
    if language_id is None:
        # Most writes are .py / .json / .yaml — DEBUG so steady state stays
        # invisible at the default INFO threshold.
        _event_log.debug("lsp[?] skipped: extension %s not mapped (%s)", ext or "<none>", _short_path(file_path))
        return None

    if not _is_local_env(env):
        _log(language_id, logging.DEBUG, f"skipped: backend not local ({_short_path(file_path)})")
        return None

    lsp_cfg = _load_lsp_config()
    if not lsp_cfg.get("enabled"):
        _log(language_id, logging.DEBUG, f"skipped: feature off ({_short_path(file_path)})")
        return None

    short = _short_path(file_path)

    project_root = find_project_root(file_path, language_id)
    if project_root is None:
        # First time we see this orphan file → INFO so an opt-in user
        # learns about the coverage gap. Repeated writes to the same
        # orphan stay silent at DEBUG.
        if _announce_once(_announced_no_root, (language_id, file_path)):
            _log(language_id, logging.INFO, f"skipped: no project root for {short}")
        else:
            _log(language_id, logging.DEBUG, f"skipped: no project root for {short}")
        return None

    server_cmd = _resolve_server_cmd(language_id, lsp_cfg)
    if not server_cmd:
        # Configuration error, not a runtime fluke. Once-per-language is
        # plenty — the missing block won't fix itself on the next write.
        if _announce_once(_announced_unavailable, (language_id, "<no-cmd>")):
            _log(language_id, logging.WARNING, f"skipped: no server configured (set lint.lsp.servers.{language_id})")
        return None
    binary_name = server_cmd[0]

    idle_seconds = float(lsp_cfg.get("idle_shutdown") or 600.0)
    try:
        client = get_or_start_client(
            language_id,
            project_root,
            server_cmd,
            idle_seconds=idle_seconds,
        )
    except Exception as exc:
        if _announce_once(_announced_unavailable, (language_id, binary_name)):
            _log(language_id, logging.WARNING, f"skipped: spawn failed for {binary_name!r}: {exc}")
        else:
            _log(language_id, logging.DEBUG, f"skipped: spawn failed again for {binary_name!r}: {exc}")
        return None
    if client is None:
        # Most likely cause: binary not on PATH. WARNING the first time so
        # the opt-in user notices and installs it; DEBUG after so a session
        # of 1000 edits doesn't carpet ``errors.log`` with the same line.
        if _announce_once(_announced_unavailable, (language_id, binary_name)):
            _log(language_id, logging.WARNING, f"skipped: server unavailable ({binary_name!r} not on PATH)")
        else:
            _log(language_id, logging.DEBUG, f"skipped: server still unavailable ({binary_name!r})")
        return None

    # First successful client for this (language, root) → one INFO line.
    # That's the opt-in user's "yes, LSP is wired up" confirmation.
    if _announce_once(_announced_active, (language_id, project_root)):
        _log(language_id, logging.INFO, f"active for {_short_path(project_root)} (server: {binary_name})")

    timeout = float(lsp_cfg.get("diagnostic_timeout") or 10.0)
    settle_ms = int(lsp_cfg.get("settle_ms") or 400)
    try:
        diagnostics: list[Diagnostic] = client.diagnostics(
            file_path,
            content,
            timeout=timeout,
            settle_ms=settle_ms,
        )
    except TimeoutError as exc:
        # Timeouts are inherently distinct events (a hung server today is
        # not the same signal as a hung server yesterday), so each one
        # gets its own WARNING.
        _log(language_id, logging.WARNING, f"skipped: timeout after {timeout:.1f}s ({short}): {exc}")
        return None
    except RuntimeError as exc:
        _log(language_id, logging.WARNING, f"skipped: server error for {short}: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001 — never raise from the lint hot path
        _log(language_id, logging.WARNING, f"skipped: unexpected bridge error for {short}: {exc}")
        return None

    # Filter to errors + warnings. Hints/info are skipped because the
    # existing shell linters never surface them, and surfacing them here
    # would change the agent's "lint clean / lint dirty" verdict mid-PR.
    blocking = [d for d in diagnostics if d.severity in (1, 2)]

    # Lazy import to avoid a circular import at module load time.
    from tools.file_operations import LintResult

    if not blocking:
        # Clean writes are the steady state on a healthy project — DEBUG
        # so a 1000-write session stays silent at the default threshold.
        _log(language_id, logging.DEBUG, f"clean ({short})")
        return LintResult(success=True, output="")
    # Diagnostics are inherently rare and per-edit, so each one is its
    # own INFO event — this is what the user actually wants to grep for.
    _log(language_id, logging.INFO, f"{len(blocking)} diag{'s' if len(blocking) != 1 else ''} ({short})")
    return LintResult(success=False, output=format_diagnostics(file_path, blocking))


__all__ = [
    "maybe_lint_via_lsp",
]
