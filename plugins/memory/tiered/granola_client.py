"""Granola MCP Streamable HTTP client — session lifecycle, JSON/SSE parsing, auth.

Ported from moltworker src/services/granola-mcp-client.ts.
Uses httpx.Client (sync) to match project conventions (embeddings.py, consolidation.py).
No validateEgressUrl() — Rampart handles egress policy in Hermes.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GRANOLA_MCP_URL = "https://mcp.granola.ai/mcp"
REQUEST_TIMEOUT = 30.0


class GranolaMcpClient:
    """Minimal MCP Streamable HTTP client for Granola.

    Handles session lifecycle, JSON/SSE response parsing, and auth
    with optional token refresh on 401.
    """

    def __init__(
        self,
        get_token: Callable[[], str],
        on_token_refresh: Callable[[], str] | None = None,
    ) -> None:
        self._get_token = get_token
        self._on_token_refresh = on_token_refresh
        self._session_id: str | None = None
        self._init_retry_count = 0
        self._refresh_attempted = False
        self._max_retries = 1
        self._http = httpx.Client(timeout=REQUEST_TIMEOUT)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        """Call a tool on the Granola MCP server.

        Handles session init, expiry (404), and response parsing.
        """
        if not self._session_id:
            self._initialize()

        token = self._get_token()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        resp = self._http.post(
            GRANOLA_MCP_URL,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": uuid.uuid4().hex,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            },
        )

        # Session expired — re-initialize and retry once
        if resp.status_code == 404 and self._init_retry_count < self._max_retries:
            self._session_id = None
            self._init_retry_count += 1
            try:
                result = self.call_tool(name, args)
            finally:
                self._init_retry_count = 0
            return result

        # Auth failure — attempt token refresh if callback provided
        if resp.status_code == 401:
            if self._on_token_refresh and not self._refresh_attempted:
                self._refresh_attempted = True
                try:
                    self._session_id = None
                    self._on_token_refresh()
                    result = self.call_tool(name, args)
                    return result
                finally:
                    self._refresh_attempted = False
            self._refresh_attempted = False
            body = resp.text[:200]
            raise RuntimeError(f"Granola MCP auth failed (401): {body}")

        # Other HTTP errors
        if not resp.is_success:
            body = resp.text[:200]
            raise RuntimeError(f"Granola MCP error: {resp.status_code} — {body}")

        return self._parse_response(resp)

    def reset_session(self) -> None:
        """Reset session (for testing or manual recovery)."""
        self._session_id = None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _initialize(self) -> None:
        """MCP initialize handshake — required before tools/call."""
        token = self._get_token()
        resp = self._http.post(
            GRANOLA_MCP_URL,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {token}",
            },
            json={
                "jsonrpc": "2.0",
                "id": uuid.uuid4().hex,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "hermes", "version": "1.0.0"},
                },
            },
        )

        if not resp.is_success:
            if resp.status_code == 401 and self._on_token_refresh and not self._refresh_attempted:
                self._refresh_attempted = True
                try:
                    self._on_token_refresh()
                    self._initialize()
                    return
                finally:
                    self._refresh_attempted = False
            body = resp.text[:200]
            if resp.status_code == 401:
                raise RuntimeError(f"Granola MCP auth failed (401): {body}")
            raise RuntimeError(f"Granola MCP initialize failed: {resp.status_code} — {body}")

        # Capture session ID from response header
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid

        # Parse and discard the initialize result
        self._parse_response(resp)

        # Send initialized notification (MCP spec requires this after initialize)
        if self._session_id:
            self._http.post(
                GRANOLA_MCP_URL,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                    "Mcp-Session-Id": self._session_id,
                },
                json={
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                },
            )

    def _parse_response(self, response: httpx.Response) -> Any:
        """Parse response — handles both JSON and SSE formats."""
        content_type = response.headers.get("content-type", "")

        if "text/event-stream" in content_type:
            return self._parse_sse(response.text)

        # Default: JSON response
        data = response.json()

        # JSON-RPC error
        if "error" in data and data["error"]:
            err = data["error"]
            raise RuntimeError(
                f"Granola MCP JSON-RPC error {err['code']}: {err['message']}"
            )

        return data.get("result")

    def _parse_sse(self, text: str) -> Any:
        """Parse SSE response — extract JSON-RPC result from event stream."""
        events = text.split("\n\n")
        for event in events:
            lines = event.split("\n")
            for line in lines:
                if line.startswith("data: "):
                    try:
                        data = json.loads(line[6:])
                        if "result" in data and data["result"] is not None:
                            return data["result"]
                        if "error" in data and data["error"]:
                            err = data["error"]
                            raise RuntimeError(
                                f"Granola MCP JSON-RPC error {err['code']}: {err['message']}"
                            )
                    except json.JSONDecodeError:
                        continue  # Skip non-JSON SSE events
        raise RuntimeError("Granola MCP: no JSON-RPC result found in SSE stream")
