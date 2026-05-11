"""Shared LLM helper for tiered memory composition.

Primary: qwen3:1.7b via local Ollama (thinking on, /think soft-switch).
P151/MOL-502: downsized from qwen3:8b (~8.4 GB resident) to qwen3:1.7b
(~2.5 GB) for nightly consolidation/composition. Nightly batch text — modest
quality drop accepted. Composition dry-run gate (composition_review side
log) wraps the first cron run before in-place rewrites are enabled.

Fallback: moonshotai/kimi-k2.6 via OpenRouter (P61/MOL-268 — cost-control
flip 2026-04-23; was google/gemini-3.1-pro-preview, flipped after reviewer-
feedback contagion drove 4am memory cron to 151 tool calls × Gemini pricing).

Ollama 0.20+ separates thinking from content — reasoning lands in
message.reasoning (ignored by openai SDK); content comes back clean.
No <think> tag stripping needed.
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# ── Primary: Ollama qwen3:1.7b (local, $0) ───────────────────────────
# P151/MOL-502: was qwen3:8b (8.4 GB resident); qwen3:1.7b is ~2.5 GB and
# cold-loads ~5× faster. keep_alive=90 (was Ollama default 5min) evicts
# faster between cron bursts.
PRIMARY_MODEL = "qwen3:1.7b"
PRIMARY_BASE_URL = "http://localhost:11434/v1"
# Ollama ignores the key; the openai SDK requires a non-empty string.
PRIMARY_API_KEY = "ollama"
PRIMARY_KEEP_ALIVE = 90  # seconds; passed as extra_body to OpenAI-compat endpoint

# ── Fallback: OpenRouter Kimi K2.6 (P61/MOL-268) ─────────────────────
FALLBACK_MODEL = "moonshotai/kimi-k2.6"
# P81/MOL-294: HERMES_MOCK_LLM_URL routes FALLBACK to aimock (PRIMARY=local Ollama left untouched — free).
# .strip() defends against empty-string masking.
FALLBACK_BASE_URL = os.environ.get("HERMES_MOCK_LLM_URL", "").strip() or "https://openrouter.ai/api/v1"
FALLBACK_API_KEY_ENV = "OPENROUTER_API_KEY"

MAX_TOKENS = 4096
TEMPERATURE = 0.3

# Hard char budget — see MOL-168. Kimi K2.6 fallback has 200k context;
# qwen3:1.7b has 32k. The 300k cap is a cost guard for fallback, not a
# qwen overflow guard (qwen would truncate naturally before then).
MAX_PROMPT_CHARS = 300_000

# qwen3 soft-switch — belt+suspenders; thinking is on by default.
_THINK_PREFIX = "/think\n\n"


def _call_primary(prompt: str) -> Optional[str]:
    """Call qwen3:1.7b via local Ollama OpenAI-compat endpoint.

    P151/MOL-502: model downsized from qwen3:8b. keep_alive passed via
    extra_body — Ollama's OpenAI-compat endpoint honors it.

    Raises on any error — caller is expected to catch and fall through.
    """
    from openai import OpenAI

    client = OpenAI(base_url=PRIMARY_BASE_URL, api_key=PRIMARY_API_KEY)
    response = client.chat.completions.create(
        model=PRIMARY_MODEL,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        messages=[{"role": "user", "content": _THINK_PREFIX + prompt}],
        extra_body={"keep_alive": PRIMARY_KEEP_ALIVE},
    )
    return response.choices[0].message.content


def _call_fallback(prompt: str) -> Optional[str]:
    """Call Kimi K2.6 via OpenRouter with reasoning: high."""
    from openai import OpenAI

    api_key = os.environ.get(FALLBACK_API_KEY_ENV, "").strip()
    if not api_key:
        logger.warning(
            "Memory LLM fallback unavailable — %s not set", FALLBACK_API_KEY_ENV
        )
        return None

    client = OpenAI(base_url=FALLBACK_BASE_URL, api_key=api_key)
    response = client.chat.completions.create(
        model=FALLBACK_MODEL,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        messages=[{"role": "user", "content": prompt}],
        extra_body={
            "reasoning": {"enabled": True, "effort": "high"},
        },
    )
    return response.choices[0].message.content


def llm_compose(prompt: str, context: str) -> Optional[str]:
    """Compose memory via local Qwen primary; fall back to Kimi K2.6 on failure.

    Returns text or None on total failure.
    """
    full = f"{prompt}\n\n{context}"
    if len(full) > MAX_PROMPT_CHARS:
        logger.error(
            "llm_compose input too large (%d chars, cap %d); truncating tail. "
            "See MOL-168.",
            len(full),
            MAX_PROMPT_CHARS,
        )
        full = full[:MAX_PROMPT_CHARS] + "\n\n[...truncated for size cap — see MOL-168...]"

    # Try local Qwen first
    try:
        result = _call_primary(full)
        if result:
            logger.info("memory LLM: %s (local, primary)", PRIMARY_MODEL)
            return result
        logger.warning("memory LLM primary returned empty content; falling back")
    except Exception as e:
        logger.info(
            "memory LLM primary failed (%s: %s); falling back to %s",
            type(e).__name__, str(e)[:200], FALLBACK_MODEL,
        )

    # Fall back to Pro 3.1
    try:
        result = _call_fallback(full)
        if result:
            logger.info("memory LLM: %s (fallback)", FALLBACK_MODEL)
            return result
        logger.warning("memory LLM fallback returned empty content")
        return None
    except Exception as e:
        logger.warning(
            "memory LLM fallback failed (%s: %s)",
            type(e).__name__, str(e)[:200],
        )
        return None
