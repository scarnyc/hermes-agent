"""LSP Plugin — semantic diagnostics from real language servers.

Hooks into write_file/patch via the Hermes plugin system to surface
type errors, undefined names, missing imports, and other semantic
issues detected by pyright, gopls, rust-analyzer, typescript-language-server,
and ~20 more.

Opt-in: add ``lsp`` to ``plugins.enabled`` in config.yaml.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import threading
from typing import Any

logger = logging.getLogger("plugins.lsp")

# Module-level state
_service: Any = None  # LSPService | None
_service_lock = threading.Lock()
# Presence set: (session_id, abs_path) entries where a baseline was captured.
_baselines: set[tuple[str, str]] = set()


def register(ctx) -> None:
    """Plugin registration — wire hooks and CLI commands."""
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("transform_tool_result", _transform_tool_result)

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

    atexit.register(_on_session_end)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Tool hooks
# ---------------------------------------------------------------------------


def _pre_tool_call(**kwargs) -> None:
    """Snapshot LSP baseline before a file write."""
    tool_name = kwargs.get("tool_name", "")
    if tool_name not in ("write_file", "patch"):
        return

    svc = _ensure_service()
    if svc is None:
        return

    args = _parse_args(kwargs.get("args"))
    if args is None:
        return

    path = args.get("path", "")
    if not path:
        return

    abs_path = _resolve_path(path)

    # Best-effort local-only check: skip if parent dir doesn't exist on host
    if not os.path.exists(os.path.dirname(abs_path) or "."):
        return

    if not svc.enabled_for(abs_path):
        return

    session_id = kwargs.get("session_id") or ""
    key = (session_id, abs_path)

    try:
        svc.snapshot_baseline(abs_path)
        _baselines.add(key)
    except Exception as e:
        logger.debug("LSP baseline snapshot failed for %s: %s", abs_path, e)


def _transform_tool_result(**kwargs) -> str | None:
    """Inject LSP diagnostics into the tool result JSON.

    Returns modified result string with ``lsp_diagnostics`` field,
    or None to leave unchanged.
    """
    tool_name = kwargs.get("tool_name", "")
    if tool_name not in ("write_file", "patch"):
        return None

    svc = _service
    if svc is None or not svc.is_active():
        return None

    args = _parse_args(kwargs.get("args"))
    if args is None:
        return None

    path = args.get("path", "")
    if not path:
        return None

    abs_path = _resolve_path(path)
    session_id = kwargs.get("session_id") or ""
    key = (session_id, abs_path)

    if key not in _baselines:
        return None
    _baselines.discard(key)

    # Fetch diagnostics with short timeout
    try:
        diagnostics = svc.get_diagnostics_sync(abs_path, delta=True, timeout=3.0)
    except Exception as e:
        logger.debug("LSP diagnostics fetch failed for %s: %s", abs_path, e)
        return None

    if not diagnostics:
        return None

    # Format
    try:
        from plugins.lsp.reporter import report_for_file, truncate
        block = report_for_file(abs_path, diagnostics)
        if not block:
            return None
        lsp_output = truncate(block)
    except Exception:
        return None

    # Inject into result JSON (only when result is a JSON dict)
    result = kwargs.get("result")
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
# Helpers
# ---------------------------------------------------------------------------


def _ensure_service():
    """Lazy-initialize the LSP service singleton."""
    global _service
    svc = _service
    if svc is not None:
        return svc if svc.is_active() else None
    with _service_lock:
        if _service is not None:
            return _service if _service.is_active() else None
        try:
            from plugins.lsp.manager import LSPService
            _service = LSPService.create_from_config()
        except Exception as e:
            logger.debug("LSP service creation failed: %s", e)
            return None
        return _service if (_service and _service.is_active()) else None


def _parse_args(args) -> dict[str, Any] | None:
    """Normalize args (may be dict or JSON string)."""
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
