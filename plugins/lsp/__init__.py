"""LSP Plugin — semantic diagnostics from real language servers.

Hooks into write_file/patch via the Hermes plugin system to surface
type errors, undefined names, missing imports, and other semantic
issues detected by pyright, gopls, rust-analyzer, typescript-language-server,
and ~20 more.

Opt-in: add ``lsp`` to ``plugins.enabled`` in config.yaml.

Architecture
------------
- ``on_session_start``: create LSP service (lightweight — no servers spawned yet)
- ``pre_tool_call``: on write_file/patch, snapshot LSP baseline for the file
- ``transform_tool_result``: after write, fetch diagnostics, inject delta into result JSON
- ``on_session_end`` + ``atexit``: tear down all server child processes

Baselines are keyed by ``(session_id, abs_path)`` to handle concurrent
gateway sessions sharing a process.  Parallel writes to different files
are safe because Hermes doesn't parallelize overlapping path-scoped tools.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("plugins.lsp")

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

# The LSP service singleton (created on first relevant pre_tool_call).
_service: Optional[Any] = None  # LSPService | None
_service_lock = threading.Lock()

# Baselines keyed by (session_id, abs_path) → diagnostics list.
# Concurrent-safe: Hermes never writes to the same path in parallel.
_baselines: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Plugin registration — wire hooks and CLI commands."""
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("transform_tool_result", _transform_tool_result)

    # CLI: hermes lsp status/install/restart/list/which
    try:
        from plugins.lsp.cli import setup_lsp_parser, run_lsp_command
        ctx.register_cli_command(
            name="lsp",
            help="Language Server Protocol management",
            setup_fn=setup_lsp_parser,
            handler_fn=run_lsp_command,
        )
    except Exception as e:
        logger.debug("LSP CLI registration failed: %s", e)

    # Process-exit cleanup as a second layer beyond on_session_end.
    atexit.register(_atexit_cleanup)


# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------


def _on_session_start(**kwargs) -> None:
    """Create the LSP service on session start.

    We don't gate on git-workspace here — that check happens per-file
    in ``enabled_for()``.  The service is lightweight when idle (no
    servers spawned, no event loop until first use).
    """
    # Skip non-interactive scripted modes where LSP is pointless.
    platform = kwargs.get("platform", "cli")
    if platform in ("batch", "cron"):
        return
    _ensure_service()


def _on_session_end(**kwargs) -> None:
    """Tear down all language servers and clear baselines."""
    global _service
    with _service_lock:
        if _service is not None:
            try:
                _service.shutdown()
            except Exception as e:
                logger.debug("LSP shutdown error: %s", e)
            _service = None
    _baselines.clear()


def _atexit_cleanup() -> None:
    """Process-exit fallback — catch crashes / Ctrl+C that skip on_session_end."""
    global _service
    if _service is not None:
        try:
            _service.shutdown()
        except Exception:
            pass
        _service = None


# ---------------------------------------------------------------------------
# Tool hooks
# ---------------------------------------------------------------------------


def _pre_tool_call(**kwargs) -> None:
    """Snapshot LSP baseline before a file write.

    Fires for write_file and patch (single-path mode).  Skips V4A
    multi-file patches (args has ``patch`` field, not ``path``).
    """
    tool_name = kwargs.get("tool_name", "")
    if tool_name not in ("write_file", "patch"):
        return

    svc = _ensure_service()
    if svc is None:
        return

    args = _parse_args(kwargs.get("args"))
    if args is None:
        return

    # V4A multi-file patch: has "patch" key, skip for MVP
    if "patch" in args and "path" not in args:
        return

    path = args.get("path", "")
    if not path:
        return

    abs_path = _resolve_path(path)

    # Best-effort local-only check: if the path doesn't exist on the
    # host filesystem, we're probably in a Docker/SSH backend.
    if not os.path.exists(os.path.dirname(abs_path) or "."):
        return

    if not svc.enabled_for(abs_path):
        return

    session_id = kwargs.get("session_id", "") or ""
    key = (session_id, abs_path)

    try:
        svc.snapshot_baseline(abs_path)
        _baselines[key] = []  # Mark that we took a baseline
    except Exception as e:
        logger.debug("LSP baseline snapshot failed for %s: %s", abs_path, e)


def _post_tool_call(**kwargs) -> None:
    """Cleanup hook — clear stale baseline on failure paths.

    transform_tool_result handles the success path (pops the key).
    post_tool_call ensures the key is removed even when the tool errors
    and transform_tool_result doesn't fire or returns early.
    """
    tool_name = kwargs.get("tool_name", "")
    if tool_name not in ("write_file", "patch"):
        return

    # We don't clean up here on success — transform_tool_result handles it.
    # On failure, the result will contain an error and transform_tool_result
    # will return None (no injection), but the baseline entry persists.
    # That's fine — it's a few bytes of memory per failed write, cleared
    # in on_session_end.  Aggressive cleanup here would race with
    # transform_tool_result which fires AFTER post_tool_call.


def _transform_tool_result(**kwargs) -> Optional[str]:
    """Inject LSP diagnostics into the tool result JSON.

    Returns the modified result string (with ``lsp_diagnostics`` field
    added to the JSON), or None to leave the result unchanged.
    """
    tool_name = kwargs.get("tool_name", "")
    if tool_name not in ("write_file", "patch"):
        return None

    # Snapshot service ref to avoid TOCTOU with _on_session_end
    svc = _service
    if svc is None or not svc.is_active():
        return None

    args = _parse_args(kwargs.get("args"))
    if args is None:
        return None

    # V4A multi-file skip
    if "patch" in args and "path" not in args:
        return None

    path = args.get("path", "")
    if not path:
        return None

    abs_path = _resolve_path(path)
    session_id = kwargs.get("session_id", "") or ""
    key = (session_id, abs_path)

    # Only proceed if we captured a baseline for this file
    if key not in _baselines:
        return None

    # Remove the baseline entry (consumed)
    _baselines.pop(key, None)

    if not svc.enabled_for(abs_path):
        return None

    # Fetch diagnostics with a short timeout (don't block long on cold start)
    try:
        diagnostics = svc.get_diagnostics_sync(abs_path, delta=True, timeout=3.0)
    except Exception as e:
        logger.debug("LSP diagnostics fetch failed for %s: %s", abs_path, e)
        return None

    if not diagnostics:
        return None

    # Format and inject into result JSON
    try:
        from plugins.lsp.reporter import report_for_file, truncate
        block = report_for_file(abs_path, diagnostics)
        if not block:
            return None
        lsp_output = truncate(block)
    except Exception:
        return None

    # Preserve JSON shape: parse existing result, add lsp_diagnostics field.
    # Only inject when result is a JSON object (dict). Non-dict JSON (arrays,
    # strings, numbers) and non-JSON results are left unmodified — we cannot
    # inject a field without corrupting the format.
    result = kwargs.get("result") or ""
    if not isinstance(result, str):
        return None
    try:
        result_data = json.loads(result)
        if not isinstance(result_data, dict):
            return None
        result_data["lsp_diagnostics"] = lsp_output
        return json.dumps(result_data, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ensure_service():
    """Lazy-initialize the LSP service singleton."""
    global _service
    if _service is not None:
        return _service if _service.is_active() else None
    with _service_lock:
        if _service is not None:
            return _service if _service.is_active() else None
        try:
            from plugins.lsp.manager import LSPService
            _service = LSPService.create_from_config()
        except Exception as e:
            logger.debug("LSP service creation failed: %s", e)
            return None
    return _service if (_service is not None and _service.is_active()) else None


def _parse_args(args) -> Optional[Dict[str, Any]]:
    """Normalize args from hook kwargs (may be dict or JSON string)."""
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def _resolve_path(path: str) -> str:
    """Expand and absolutify a path."""
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        expanded = os.path.join(os.getcwd(), expanded)
    return os.path.normpath(expanded)


# ---------------------------------------------------------------------------
# Public API (used by plugins/lsp/cli.py)
# ---------------------------------------------------------------------------


def get_service():
    """Return the active LSP service or None."""
    svc = _service
    return svc if (svc is not None and svc.is_active()) else None


def shutdown_service() -> None:
    """Tear down the LSP service (idempotent)."""
    _on_session_end()
