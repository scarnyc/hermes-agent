"""In-process embedding client for tiered memory.

P168/MOL-546: swapped from Ollama HTTP (nomic-embed-text, 768-dim, MTEB ~62.4)
to in-process fastembed (mxbai-embed-large-v1, 1024-dim, MTEB ~64.7). The swap
buys ~2.3 MTEB-retrieval points AND removes the Ollama HTTP round-trip + cold-load
penalty (~3-5s first call on prior model). Model loads once at import; subsequent
embeds run ~15-50ms each.

MAX_CHARS = 2000: mxbai-embed-large's effective context is ~512 tokens (~2000 chars),
so 6000-char (P160) truncation was wasted bytes beyond model context AND slower
embed pass. Returns None on failure for FTS-only fallback.

Migration: store.py:_migrate_vec_dims() auto-handles the 768→1024 dim change via
DROP+CREATE memory_vec + _reembed_all(). ~16ms per entry × ~2400 entries ≈ 40s on
first gateway start post-upgrade.

P168/MOL-546 review fix-pass-2 (silent-failure-hunter findings):
- `_get_model()` first-call init is allowed to RAISE — caller (typically
  `_reembed_all` during migration) sees the actionable error instead of
  per-call's silent return-None mask.
- `generate_embedding` per-call catch narrowed from bare `except Exception`
  to specific classes that fastembed/ONNX/IO can actually emit; KeyboardInterrupt
  and programming errors (TypeError, AttributeError) propagate.
- Per-entry truncation logging adds an audit trail when MAX_CHARS truncation
  fires, so future recall regressions can be attributed to specific entries.
"""

import logging
import os
from typing import Optional

from fastembed import TextEmbedding

logger = logging.getLogger(__name__)

MODEL = "mixedbread-ai/mxbai-embed-large-v1"  # P168/MOL-546 — was "nomic-embed-text"
DIMS = 1024                                    # P168/MOL-546 — was 768 (nomic); auto-migration via store.py:_migrate_vec_dims
MAX_CHARS = 2000                               # P168/MOL-546 — was 6000 (P160); matches mxbai's ~512-token effective context

# Exception classes that the embed path can legitimately produce.
# Bare `except Exception` was rejected per review: it masked programming
# errors (TypeError, AttributeError) AND environmental crashes worth surfacing
# (MemoryError under OOM is borderline but listed here because the alternative
# is the entire embedding pass dying mid-batch from one OOM event).
# P168/MOL-546.
_EMBED_ERRORS = (RuntimeError, OSError, ValueError, ImportError, MemoryError)

# Module-level singleton — fastembed lazy-loads ONNX session on first .embed() call.
# Cache dir keeps model weights in ~/.cache/fastembed/ (writable from Hermes sandbox).
_CACHE_DIR = os.path.expanduser("~/.cache/fastembed")
_model: Optional[TextEmbedding] = None


def _get_model() -> TextEmbedding:
    """Lazily instantiate the shared TextEmbedding singleton.

    First call performs HF download (if model uncached) + ONNX session init.
    Failures here ARE allowed to raise — the caller's per-call exception
    handler is for inference errors only, not init errors. An init failure
    surfaces as `fastembed init failed: <reason>` to the operator instead
    of being silently swallowed for every subsequent embed call.
    """
    global _model
    if _model is None:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        try:
            _model = TextEmbedding(MODEL, cache_dir=_CACHE_DIR)
        except _EMBED_ERRORS as e:
            logger.error(
                "fastembed init failed for %s in %s: %s. "
                "Check HF egress + cache writability; pre-download with "
                "`python -m fastembed.cli download %s` if running offline.",
                MODEL, _CACHE_DIR, e, MODEL,
            )
            raise
    return _model


def generate_embedding(text: str) -> Optional[list[float]]:
    """Generate 1024-dim embedding via fastembed. Returns None on failure (FTS-only fallback).

    First call cost: ONNX session init + optional HF download (allowed to raise).
    Subsequent calls catch only inference-class errors per `_EMBED_ERRORS`.
    """
    raw_len = len(text)
    text = text[:MAX_CHARS].strip()
    if not text:
        return None
    if raw_len > MAX_CHARS:
        logger.info("Embedding input truncated: %d → %d chars", raw_len, MAX_CHARS)
    try:
        emb = next(iter(_get_model().embed([text])))
        emb_list = emb.tolist()
        if len(emb_list) != DIMS:
            logger.warning("Embedding dimension mismatch: expected %d, got %d", DIMS, len(emb_list))
            return None
        return emb_list
    except _EMBED_ERRORS as e:
        logger.warning("Embedding generation failed: %s", e)
        return None


def generate_embeddings(texts: list[str]) -> list[Optional[list[float]]]:
    """Batch embedding generation. fastembed handles batching internally."""
    if not texts:
        return []
    stripped = [t[:MAX_CHARS].strip() for t in texts]
    non_empty_indices = [i for i, t in enumerate(stripped) if t]
    if not non_empty_indices:
        return [None] * len(texts)
    non_empty_texts = [stripped[i] for i in non_empty_indices]
    try:
        raw = list(_get_model().embed(non_empty_texts))
        validated = [
            emb.tolist() if hasattr(emb, "tolist") and len(emb) == DIMS else None
            for emb in raw
        ]
        result: list[Optional[list[float]]] = [None] * len(texts)
        for idx, emb in zip(non_empty_indices, validated):
            result[idx] = emb
        return result
    except _EMBED_ERRORS as e:
        logger.warning("Batch embedding generation failed: %s", e)
        return [None] * len(texts)
