"""Granola OAuth token refresh — PKCE public client (no client_secret).

Ported from moltworker src/services/granola-oauth.ts.
No validateEgressUrl() — Rampart handles egress policy in Hermes.
"""

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

GRANOLA_TOKEN_URL = "https://mcp-auth.granola.ai/oauth2/token"


@dataclass
class GranolaTokenResult:
    """Result of a successful token refresh."""

    access_token: str
    expires_in: int
    new_refresh_token: str | None = None


def refresh_granola_token(client_id: str, refresh_token: str) -> GranolaTokenResult:
    """Refresh a Granola OAuth access token using a stored refresh token.

    Granola uses PKCE (public client) — no client_secret is sent.

    Args:
        client_id: OAuth client ID.
        refresh_token: Stored refresh token.

    Returns:
        GranolaTokenResult with the new access token and optional rotated refresh token.

    Raises:
        RuntimeError: On non-2xx response from the token endpoint.
    """
    resp = httpx.post(
        GRANOLA_TOKEN_URL,
        data={
            "client_id": client_id,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )

    if not resp.is_success:
        error_code = f"HTTP {resp.status_code}"
        try:
            parsed = resp.json()
            if "error" in parsed:
                error_code = parsed["error"]
        except (ValueError, KeyError):
            pass
        raise RuntimeError(f"Granola OAuth refresh failed: {error_code}")

    data = resp.json()

    return GranolaTokenResult(
        access_token=data["access_token"],
        expires_in=data["expires_in"],
        new_refresh_token=data.get("refresh_token"),
    )
