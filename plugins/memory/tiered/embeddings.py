"""Ollama embedding client for tiered memory.

Generates 768-dim embeddings via the local Ollama API using nomic-embed-text
(P151/MOL-502: was bge-m3 @ 1024-dim / 1.3 GB resident; nomic-embed-text is
274 MB and cold-loads in ~3-5s instead of ~30s). Returns None on failure to
enable FTS-only fallback.

`keep_alive: 90s` (was Ollama default 5min) — evicts faster between bursts,
leaves headroom for cron clusters spaced 60-90s apart.
"""

import logging
from typing import Optional

import httpx  # Available in Hermes venv (httpx>=0.28.1)

logger = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434/api/embed"
MODEL = "nomic-embed-text"  # P151/MOL-502 — was "bge-m3"
MAX_CHARS = 8000
DIMS = 768                  # P151/MOL-502 — was 1024 (bge-m3); migration re-embeds existing entries
TIMEOUT = 30.0  # First-call cold-load is ~3-5s; 30s keeps slack for rare slow loads
KEEP_ALIVE = 90  # seconds; faster eviction than Ollama default 5min


def generate_embedding(text: str) -> Optional[list[float]]:
    """Generate 768-dim embedding via Ollama. Returns None on failure (FTS-only fallback)."""
    text = text[:MAX_CHARS].strip()
    if not text:
        return None
    try:
        resp = httpx.post(
            OLLAMA_URL,
            json={"model": MODEL, "input": text, "keep_alive": KEEP_ALIVE},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        emb = data["embeddings"][0]
        if len(emb) != DIMS:
            logger.warning("Embedding dimension mismatch: expected %d, got %d", DIMS, len(emb))
            return None
        return emb
    except (httpx.HTTPError, KeyError, IndexError, httpx.TimeoutException) as e:
        logger.warning("Embedding generation failed: %s", e)
        return None


def generate_embeddings(texts: list[str]) -> list[Optional[list[float]]]:
    """Batch embedding generation. Ollama supports input as list."""
    if not texts:
        return []
    stripped = [t[:MAX_CHARS].strip() for t in texts]
    # Track which positions are empty
    non_empty_indices = [i for i, t in enumerate(stripped) if t]
    if not non_empty_indices:
        return [None] * len(texts)
    non_empty_texts = [stripped[i] for i in non_empty_indices]
    try:
        resp = httpx.post(
            OLLAMA_URL,
            json={"model": MODEL, "input": non_empty_texts, "keep_alive": KEEP_ALIVE},
            timeout=TIMEOUT * 2,
        )
        resp.raise_for_status()
        raw_embeddings = resp.json()["embeddings"]
        # Validate dimensions
        validated = [
            emb if isinstance(emb, list) and len(emb) == DIMS else None
            for emb in raw_embeddings
        ]
        # Reconstruct full result list with None for empty inputs
        result: list[Optional[list[float]]] = [None] * len(texts)
        for idx, emb in zip(non_empty_indices, validated):
            result[idx] = emb
        return result
    except (httpx.HTTPError, KeyError, httpx.TimeoutException) as e:
        logger.warning("Batch embedding generation failed: %s", e)
        return [None] * len(texts)
