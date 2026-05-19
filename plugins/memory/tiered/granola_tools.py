"""Granola tool functions — list, get, query meetings via MCP proxy.

Ported from moltworker src/tools/granola.ts.
Token management uses a file-based cache (sandbox-writable) with envchain
as the bootstrap source. envchain --set fails inside Hermes's sandbox-exec
profile (mach-lookup to com.apple.SecurityServer denied), so refresh-token
rotation cannot self-recover from envchain alone — file cache makes it
durable across cron processes. See plan / MOL-248 for details.
scrub_content() is NOT called here — Rampart handles response scanning.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

from .granola_client import GranolaMcpClient
from .granola_oauth import refresh_granola_token

logger = logging.getLogger(__name__)

GRANOLA_CONTENT_LIMIT = 2000
MAX_RECURSION_DEPTH = 20

VALID_TIME_RANGES = {"this_week", "last_week", "last_30_days", "custom"}

# Module-level cache for the current access token
_cached_access_token: str | None = None

ENVCHAIN_BIN = "/opt/homebrew/bin/envchain"
ENVCHAIN_NAMESPACE = "hermes-granola"

# File-based token cache. ~/.hermes/state/ is sandbox-writable;
# envchain --set is not. P83/MOL-248 (sandbox-vs-Keychain fix).
_TOKEN_FILE = get_hermes_home() / "state" / "granola-tokens.json"


def _load_tokens_from_file() -> dict[str, Any]:
    """Read the token cache file. Returns {} on missing or corrupt.

    Schema:
        {"access_token": str, "refresh_token": str,
         "expires_at": int (epoch seconds), "rotated_at": ISO-8601 str}
    """
    try:
        if not _TOKEN_FILE.is_file():
            return {}
        with _TOKEN_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}
        return data
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("granola token file unreadable, falling back to env: %s", exc)
        return {}


def _persist_to_file(
    access_token: str,
    refresh_token: str | None,
    expires_in: int,
) -> None:
    """Write tokens to the cache file atomically with mode 0600.

    Raises on failure — file is the primary persistence layer; if it can't
    be written we don't want a silent rotated-token-loss like envchain had.
    """
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "access_token": access_token,
        "expires_at": int(time.time()) + int(expires_in),
        "rotated_at": datetime.now(timezone.utc).isoformat(),
    }
    if refresh_token:
        payload["refresh_token"] = refresh_token
    else:
        existing = _load_tokens_from_file()
        if existing.get("refresh_token"):
            payload["refresh_token"] = existing["refresh_token"]

    # Atomic write: tempfile in same dir + os.replace
    fd, tmp_path = tempfile.mkstemp(
        prefix=".granola-tokens.", suffix=".tmp", dir=str(_TOKEN_FILE.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, str(_TOKEN_FILE))
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _persist_to_envchain(key: str, value: str) -> None:
    """Persist a token to envchain (macOS Keychain) for cross-restart survival.

    Best-effort under sandbox: envchain --set requires mach-lookup to
    com.apple.SecurityServer which sandbox-exec denies in cron processes.
    Failures are logged WARNING but not raised — file cache is the
    primary persistence layer, envchain is the bootstrap source.
    """
    os.environ[key] = value
    try:
        result = subprocess.run(
            [ENVCHAIN_BIN, "--set", ENVCHAIN_NAMESPACE, key],
            input=value,
            text=True,
            capture_output=True,
            timeout=5,
        )
        if result.returncode != 0:
            logger.warning("envchain persist %s failed: %s", key, result.stderr.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("envchain persist %s unavailable: %s", key, exc)


def truncate_granola_content(
    data: Any,
    limit: int = GRANOLA_CONTENT_LIMIT,
    depth: int = 0,
) -> Any:
    """Recursively truncate long string fields in Granola API responses.

    Reduces token usage by capping strings to ``limit`` characters.
    """
    if depth > MAX_RECURSION_DEPTH:
        return data
    if data is None:
        return data
    if isinstance(data, str):
        if len(data) > limit:
            return data[:limit] + " [truncated]"
        return data
    if isinstance(data, (int, float, bool)):
        return data
    if isinstance(data, list):
        return [truncate_granola_content(item, limit, depth + 1) for item in data]
    if isinstance(data, dict):
        result: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, str) and len(value) > limit:
                result[key] = value[:limit] + " [truncated]"
            elif isinstance(value, (dict, list)):
                result[key] = truncate_granola_content(value, limit, depth + 1)
            else:
                result[key] = value
        return result
    return data


def _create_client() -> GranolaMcpClient:
    """Create a GranolaMcpClient wired to file-cached tokens with refresh callback.

    Read precedence: in-process cache > token file > os.environ (envchain
    bootstrap). Refresh writes to the file FIRST (must succeed); envchain
    persistence is best-effort and may fail silently inside the sandbox.
    """
    global _cached_access_token

    def get_token() -> str:
        if _cached_access_token:
            return _cached_access_token
        file_data = _load_tokens_from_file()
        token = file_data.get("access_token") or os.environ.get("GRANOLA_OAUTH_TOKEN", "")
        if not token:
            raise RuntimeError("GRANOLA_OAUTH_TOKEN not set")
        return token

    def _refresh_token() -> str:
        return (
            _load_tokens_from_file().get("refresh_token")
            or os.environ.get("GRANOLA_REFRESH_TOKEN", "")
        )

    def _can_refresh() -> bool:
        return bool(_refresh_token() and os.environ.get("GRANOLA_CLIENT_ID"))

    def _refresh() -> str:
        global _cached_access_token
        _cached_access_token = None

        # Read at call time — not closure creation time — so rotated tokens work
        rt = _refresh_token()
        cid = os.environ.get("GRANOLA_CLIENT_ID", "")
        if not rt or not cid:
            raise RuntimeError("GRANOLA_REFRESH_TOKEN or GRANOLA_CLIENT_ID not set")

        result = refresh_granola_token(client_id=cid, refresh_token=rt)
        _cached_access_token = result.access_token

        # File first — sandbox-safe, must succeed.
        _persist_to_file(
            access_token=result.access_token,
            refresh_token=result.new_refresh_token,
            expires_in=result.expires_in,
        )

        # Envchain best-effort — fails silently under sandbox-exec; file is canonical.
        os.environ["GRANOLA_OAUTH_TOKEN"] = result.access_token
        _persist_to_envchain("GRANOLA_OAUTH_TOKEN", result.access_token)
        if result.new_refresh_token:
            os.environ["GRANOLA_REFRESH_TOKEN"] = result.new_refresh_token
            _persist_to_envchain("GRANOLA_REFRESH_TOKEN", result.new_refresh_token)
            logger.info("Granola refresh token rotated and persisted to file")

        logger.info("Granola access token refreshed (expires in %ds)", result.expires_in)
        return result.access_token

    on_token_refresh = _refresh if _can_refresh() else None
    return GranolaMcpClient(get_token=get_token, on_token_refresh=on_token_refresh)


def list_granola_meetings(
    time_range: str | None = None,
    custom_start: str | None = None,
    custom_end: str | None = None,
) -> dict[str, str]:
    """List Granola meetings with time range filtering.

    Args:
        time_range: One of this_week, last_week, last_30_days, custom. Defaults to last_30_days.
        custom_start: ISO datetime string for custom range start.
        custom_end: ISO datetime string for custom range end.

    Returns:
        {"content": json_string} with the meeting list.

    Raises:
        ValueError: For invalid time_range.
    """
    effective_range = time_range or "last_30_days"

    if effective_range not in VALID_TIME_RANGES:
        raise ValueError(
            f"Invalid time_range: {effective_range!r}. "
            f"Must be one of {sorted(VALID_TIME_RANGES)}"
        )

    client = _create_client()
    mcp_args: dict[str, Any] = {}

    if effective_range == "custom":
        mcp_args["time_range"] = "custom"
        if custom_start:
            mcp_args["custom_start"] = custom_start
        if custom_end:
            mcp_args["custom_end"] = custom_end
    else:
        mcp_args["time_range"] = effective_range

    result = client.call_tool("list_meetings", mcp_args)
    return {"content": json.dumps(truncate_granola_content(result))}


def get_granola_meeting(
    meeting_id: str,
    include_transcript: bool = False,
) -> dict[str, str]:
    """Get a single Granola meeting with notes, or its transcript.

    Args:
        meeting_id: The meeting ID to retrieve.
        include_transcript: If True, fetch the raw transcript instead of notes.

    Returns:
        {"content": json_string} with the meeting data.

    Raises:
        ValueError: If meeting_id is empty.
    """
    if not meeting_id:
        raise ValueError("Meeting ID is required")

    client = _create_client()

    if include_transcript:
        result = client.call_tool("get_meeting_transcript", {"meeting_id": meeting_id})
    else:
        result = client.call_tool("get_meetings", {"meeting_ids": [meeting_id]})

    return {"content": json.dumps(truncate_granola_content(result))}


def query_granola_meetings(query: str) -> dict[str, str]:
    """Search Granola meeting notes with a natural language query.

    Args:
        query: Search query (minimum 2 characters).

    Returns:
        {"content": json_string} with query results.

    Raises:
        ValueError: If query is shorter than 2 characters.
    """
    if len(query) < 2:
        raise ValueError("Query must be at least 2 characters")

    client = _create_client()
    result = client.call_tool("query_granola_meetings", {"query": query})
    return {"content": json.dumps(truncate_granola_content(result))}
