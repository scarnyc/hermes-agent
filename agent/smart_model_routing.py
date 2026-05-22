"""Helpers for optional cheap-vs-strong model routing.

Supports three classifier modes (selected via routing_config["classifier"]):

* ``"keyword"`` (default, back-compat): route to the legacy ``cheap_model``
  on short, low-signal turns; otherwise stay on primary.
* ``"intent"`` (MOL-30, P34): route by intent — ``[chief-of-staff]`` pin /
  image marker keep primary, browser keywords select ``browser`` route,
  jira/coding signals select ``jira_or_coding`` route. Routes live under
  ``routing_config["routes"]``.
* ``"arch-router"``: reserved for the deferred Arch-Router-1.5B classifier
  (Phase 2 of MOL-30, currently archived).
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

from utils import is_truthy_value

# ── Intent classifier (MOL-30 P34) ──
_COS_PIN_RE = re.compile(r"\[chief-of-staff\]", re.IGNORECASE)
_IMAGE_MARKER_RE = re.compile(r"\[User sent an image:")
_JIRA_SUBSTRINGS = ("jira", "atlassian", "ticket")
_JIRA_TICKET_RE = re.compile(r"\b(?:MOL|PROJ)-\d+\b")
_CODE_PATH_RE = re.compile(r"(?:~|/Users/[^/\s]+)/Code/")
_CODE_EXT_TOKENS = (
    ".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go",
    ".sh", ".sql", ".yaml", ".yml", ".json",
)
_EDIT_VERBS = (
    "edit", "fix", "implement", "refactor", "patch", "add",
    "write", "update", "create", "delete", "remove", "modify",
)
_BROWSER_KEYWORDS = (
    "playwright", "screenshot", "headless", "browse to",
    "navigate to", "fill the form", "fill form", "click the",
)


def classify_with_intent_keywords(
    user_message: str, routing_config: Optional[Dict[str, Any]]
) -> Optional[str]:
    """Classify a turn by intent keywords.

    Precedence: ``[chief-of-staff]`` pin → image marker → browser keywords
    → jira/coding signals → ``None`` (stay on primary).

    Returns a route name (``"browser"`` or ``"jira_or_coding"``) or ``None``.
    The caller is responsible for looking the route name up in
    ``routing_config["routes"]`` and falling through to primary if absent
    (the browser route is intentionally dormant on the live config).
    """
    del routing_config  # reserved for future per-route gating

    text = (user_message or "").strip()
    if not text:
        return None

    # Hard-override: explicit Chief-of-Staff pin keeps work on primary.
    if _COS_PIN_RE.search(text):
        return None

    # P120/MOL-455: image attachments route to jira_or_coding (Kimi K2.6 native multimodal).
    # Pre-MOL-455 primary was Gemini (vision-capable); post-swap primary is
    # DeepSeek V4 (text-only), so images would silently drop content. K2.6
    # carries the vision load via the existing bucket.
    if _IMAGE_MARKER_RE.search(text):
        return "jira_or_coding"  # P120/MOL-455

    lowered = text.lower()

    # Browser: interactive automation only. Bare "browser" is deliberately
    # NOT keyed (matches "browser agent benchmarks" in prose).
    for kw in _BROWSER_KEYWORDS:
        if kw in lowered:
            return "browser"

    # Jira / coding: jira substring, ticket id, ~/Code/ path, or
    # file-extension + edit-verb combo.
    for sub in _JIRA_SUBSTRINGS:
        if sub in lowered:
            return "jira_or_coding"
    if _JIRA_TICKET_RE.search(text):
        return "jira_or_coding"
    if _CODE_PATH_RE.search(text):
        return "jira_or_coding"

    has_ext = any(tok in lowered for tok in _CODE_EXT_TOKENS)
    tokens = {token.strip(".,:;!?()[]{}\"'`") for token in lowered.split()}
    has_verb = bool(tokens & set(_EDIT_VERBS))
    if has_ext and has_verb:
        return "jira_or_coding"

    return None


_COMPLEX_KEYWORDS = {
    "debug",
    "debugging",
    "implement",
    "implementation",
    "refactor",
    "patch",
    "traceback",
    "stacktrace",
    "exception",
    "error",
    "analyze",
    "analysis",
    "investigate",
    "architecture",
    "design",
    "compare",
    "benchmark",
    "optimize",
    "optimise",
    "review",
    "terminal",
    "shell",
    "tool",
    "tools",
    "pytest",
    "test",
    "tests",
    "plan",
    "planning",
    "delegate",
    "subagent",
    "cron",
    "docker",
    "kubernetes",
}

_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)


def _coerce_bool(value: Any, default: bool = False) -> bool:
    return is_truthy_value(value, default=default)


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def choose_cheap_model_route(user_message: str, routing_config: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the configured cheap-model route when a message looks simple.

    Dispatches on ``cfg["classifier"]``:

    * ``"intent"`` (MOL-30 P34) — keyword/regex-based intent classifier with
      precedence CoS pin → image → browser → jira_or_coding → None.
    * ``"keyword"`` (default, back-compat) — short low-signal turns route to
      the legacy ``cheap_model``; otherwise stay on primary.
    """
    cfg = routing_config or {}
    if not _coerce_bool(cfg.get("enabled"), False):
        return None

    classifier = str(cfg.get("classifier") or "keyword").strip().lower()
    routes_cfg = cfg.get("routes")
    routes = routes_cfg if isinstance(routes_cfg, dict) else {}

    if classifier == "intent" and routes:
        route_name = classify_with_intent_keywords(user_message, cfg)
        if not route_name:
            return None
        route_entry = routes.get(route_name)
        if not isinstance(route_entry, dict):
            return None
        provider = str(route_entry.get("provider") or "").strip().lower()
        model = str(route_entry.get("model") or "").strip()
        if not provider or not model:
            return None
        route = dict(route_entry)
        route["provider"] = provider
        route["model"] = model
        route["routing_reason"] = f"intent:{route_name}"
        return route

    cheap_model = cfg.get("cheap_model") or {}
    if not isinstance(cheap_model, dict):
        return None
    provider = str(cheap_model.get("provider") or "").strip().lower()
    model = str(cheap_model.get("model") or "").strip()
    if not provider or not model:
        return None

    text = (user_message or "").strip()
    if not text:
        return None

    max_chars = _coerce_int(cfg.get("max_simple_chars"), 160)
    max_words = _coerce_int(cfg.get("max_simple_words"), 28)

    if len(text) > max_chars:
        return None
    if len(text.split()) > max_words:
        return None
    if text.count("\n") > 1:
        return None
    if "```" in text or "`" in text:
        return None
    if _URL_RE.search(text):
        return None

    lowered = text.lower()
    words = {token.strip(".,:;!?()[]{}\"'`") for token in lowered.split()}
    if words & _COMPLEX_KEYWORDS:
        return None

    route = dict(cheap_model)
    route["provider"] = provider
    route["model"] = model
    route["routing_reason"] = "simple_turn"
    return route


def resolve_turn_route(user_message: str, routing_config: Optional[Dict[str, Any]], primary: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the effective model/runtime for one turn.

    Returns a dict with model/runtime/signature/label fields.
    """
    route = choose_cheap_model_route(user_message, routing_config)
    if not route:
        return {
            "model": primary.get("model"),
            "runtime": {
                "api_key": primary.get("api_key"),
                "base_url": primary.get("base_url"),
                "provider": primary.get("provider"),
                "api_mode": primary.get("api_mode"),
                "command": primary.get("command"),
                "args": list(primary.get("args") or []),
                "credential_pool": primary.get("credential_pool"),
            },
            "label": None,
            "signature": (
                primary.get("model"),
                primary.get("provider"),
                primary.get("base_url"),
                primary.get("api_mode"),
                primary.get("command"),
                tuple(primary.get("args") or ()),
            ),
        }

    from hermes_cli.runtime_provider import resolve_runtime_provider

    explicit_api_key = None
    api_key_env = str(route.get("api_key_env") or "").strip()
    if api_key_env:
        explicit_api_key = os.getenv(api_key_env) or None

    try:
        runtime = resolve_runtime_provider(
            requested=route.get("provider"),
            explicit_api_key=explicit_api_key,
            explicit_base_url=route.get("base_url"),
        )
    except Exception:
        return {
            "model": primary.get("model"),
            "runtime": {
                "api_key": primary.get("api_key"),
                "base_url": primary.get("base_url"),
                "provider": primary.get("provider"),
                "api_mode": primary.get("api_mode"),
                "command": primary.get("command"),
                "args": list(primary.get("args") or []),
                "credential_pool": primary.get("credential_pool"),
            },
            "label": None,
            "signature": (
                primary.get("model"),
                primary.get("provider"),
                primary.get("base_url"),
                primary.get("api_mode"),
                primary.get("command"),
                tuple(primary.get("args") or ()),
            ),
        }

    return {
        "model": route.get("model"),
        "runtime": {
            "api_key": runtime.get("api_key"),
            "base_url": runtime.get("base_url"),
            "provider": runtime.get("provider"),
            "api_mode": runtime.get("api_mode"),
            "command": runtime.get("command"),
            "args": list(runtime.get("args") or []),
        },
        "label": f"smart route → {route.get('model')} ({runtime.get('provider')})",
        "signature": (
            route.get("model"),
            runtime.get("provider"),
            runtime.get("base_url"),
            runtime.get("api_mode"),
            runtime.get("command"),
            tuple(runtime.get("args") or ()),
        ),
    }
