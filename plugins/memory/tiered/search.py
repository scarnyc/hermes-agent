"""Hybrid FTS5 + vector search with Reciprocal Rank Fusion.

Ranking formula:
    score = RRF_base * category_weight * recency_multiplier * wing_weight

RRF_base combines BM25 (FTS) and cosine (vector) rankings with tunable weights.
Category weights favor durable knowledge (principle > project > briefing > chat).
Recency multiplier gently boosts recent entries without crowding out older signal.
Wing weights down-rank broad source classes (diary, obsidian) that match widely
but contribute low specific recall value.

P151/MOL-502: learned reranker (qwen3:8b via /api/generate) removed. MOL-176's
"+34% recall came from data organization, not algorithm" finding is the design
license; the reranker was also failing cold (10s call vs >10s qwen3:8b load)
and silently falling back to RRF order anyway. Hybrid RRF + the four
weight multipliers above carry the full recall load.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from .embeddings import generate_embedding
from .store import TieredMemoryDB

logger = logging.getLogger(__name__)

# RRF tuning
RRF_K = 60
FTS_WEIGHT = 0.4
VECTOR_WEIGHT = 0.6

# Category weights — durable knowledge ranks above conversational noise
CATEGORY_WEIGHTS = {
    "principle": 1.3,  # Learned rules are the most valuable
    "project":   1.0,  # Baseline
    "briefing":  0.9,  # Time-bounded summaries
    "chat":      0.7,  # Noisiest tier
}

# Recency multiplier parameters
# Entries in the last 7 days get a boost; older entries decay gracefully.
RECENCY_BOOST_MAX = 1.25  # Multiplier for entries created today
RECENCY_HALFLIFE_DAYS = 14  # Days until boost decays to ~1.0

# Score threshold — entries below this are noise (calibrated from benchmark: noise floor ~0.008)
MIN_SCORE_THRESHOLD = 0.010

# Wing weights — source-level quality signal (MemPalace-inspired, MOL-176)
# Diary entries match everything broadly but provide low specific recall value
WING_WEIGHTS = {
    "diary":   0.5,   # Broad session summaries — low specificity
    "obsidian": 0.7,  # Vault notes — moderate specificity
    "chat":    0.8,   # Conversations — some noise
    "default": 1.0,   # Project/principle entries — full weight
}

def _parse_created_at(created_at: str) -> Optional[datetime]:
    """Parse SQLite datetime('now') output. Returns None on failure."""
    if not created_at:
        return None
    try:
        # SQLite stores 'YYYY-MM-DD HH:MM:SS' (UTC, no tz)
        return datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _recency_multiplier(created_at: str) -> float:
    """Exponential decay from RECENCY_BOOST_MAX → 1.0 over RECENCY_HALFLIFE_DAYS.

    Clamped to [1.0, RECENCY_BOOST_MAX]. Missing/invalid timestamps return 1.0.
    """
    dt = _parse_created_at(created_at)
    if dt is None:
        return 1.0
    age_days = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)
    # Decay: boost starts at RECENCY_BOOST_MAX, halves every RECENCY_HALFLIFE_DAYS
    boost = 1.0 + (RECENCY_BOOST_MAX - 1.0) * (0.5 ** (age_days / RECENCY_HALFLIFE_DAYS))
    return max(1.0, min(RECENCY_BOOST_MAX, boost))


def _category_weight(category: str) -> float:
    return CATEGORY_WEIGHTS.get(category, 1.0)


def _wing_weight(wing: str) -> float:
    return WING_WEIGHTS.get(wing, 1.0)


def _get_superseded_tokens(query: str, entries: list[dict]) -> set[tuple[str, Optional[datetime]]]:
    """Find MOL tokens that are marked as completed in project entries."""
    mol_tokens = re.findall(r'MOL-\d+', query)
    superseded_tokens = set()
    completion_keywords = {"done", "shipped", "closed", "completed", "superseded", "landed", "merged", "archived", "n/a"}

    if not mol_tokens:
        return superseded_tokens

    for r in entries:
        if r.get("category") == "project":
            content_lower = r.get("content", "").lower()
            if any(kw in content_lower for kw in completion_keywords):
                for token in mol_tokens:
                    if token.lower() in content_lower:
                        superseded_tokens.add((token, _parse_created_at(r.get("created_at", ""))))
    return superseded_tokens


def reciprocal_rank_fusion(
    fts_results: list[dict],
    vec_results: list[dict],
    limit: int,
    query: str = "",
) -> list[dict]:
    """Merge FTS and vector results using Reciprocal Rank Fusion with
    category + recency weighting."""
    scores: dict[str, float] = {}
    entries: dict[str, dict] = {}

    for rank, r in enumerate(fts_results, 1):
        rid = r["id"]
        scores[rid] = scores.get(rid, 0) + FTS_WEIGHT / (RRF_K + rank)
        entries[rid] = r

    for rank, r in enumerate(vec_results, 1):
        rid = r["id"]
        scores[rid] = scores.get(rid, 0) + VECTOR_WEIGHT / (RRF_K + rank)
        entries.setdefault(rid, r)

    # Apply category, recency, and wing multipliers
    superseded_tokens = _get_superseded_tokens(query, list(entries.values()))

    for rid, base_score in list(scores.items()):
        entry = entries[rid]
        cat_w = _category_weight(entry.get("category", "project"))
        rec_w = _recency_multiplier(entry.get("created_at", ""))
        wing_w = _wing_weight(entry.get("wing", "default"))

        final_score = base_score * cat_w * rec_w * wing_w

        # MOL-177 Ranker supersession logic
        if entry.get("category") in ("chat", "briefing"):
            entry_content = entry.get("content", "").lower()
            entry_dt = _parse_created_at(entry.get("created_at", ""))
            for token, comp_dt in superseded_tokens:
                if token.lower() in entry_content:
                    if entry_dt and comp_dt and entry_dt < comp_dt:
                        final_score *= 0.2
                        break

        scores[rid] = final_score

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    # Filter below minimum score threshold (noise gate)
    ranked = [(eid, score) for eid, score in ranked if score >= MIN_SCORE_THRESHOLD]
    ranked = ranked[:limit]
    return [{**entries[eid], "score": score} for eid, score in ranked]


def hybrid_search(
    db: TieredMemoryDB,
    query: str,
    limit: int = 5,
    category: Optional[str] = None,
    wing: Optional[str] = None,
    room: Optional[str] = None,
    include_superseded: bool = False,
    **_compat: object,  # P151/MOL-502: swallow legacy `rerank=` from old callers
) -> list[dict]:
    """Full hybrid search: FTS5 + sqlite-vec + RRF merge.
    Falls back to FTS-only if Ollama is unreachable.

    P151/MOL-502: the optional `rerank=` parameter is gone; the **_compat
    catch-all preserves backward compatibility for any out-of-tree caller
    that still passes it.
    """
    fetch_limit = limit * 4

    fts_results = db.search_fts(query, limit=fetch_limit, category=category, wing=wing, room=room, include_superseded=include_superseded)

    embedding = generate_embedding(query)
    if embedding:
        vec_results = db.search_vec(embedding, limit=fetch_limit, category=category, wing=wing, room=room, include_superseded=include_superseded)
        results = reciprocal_rank_fusion(fts_results, vec_results, limit, query=query)
        for r in results:
            r["search_mode"] = "hybrid"
        return results
    else:
        logger.info("Embedding unavailable, falling back to FTS-only search")
        # FTS-only fallback also gets category + recency weighting so ranking stays consistent
        superseded_tokens = _get_superseded_tokens(query, fts_results[:limit])
        results = []
        for i, r in enumerate(fts_results[:limit]):
            base = 1.0 / (i + 1)
            cat_w = _category_weight(r.get("category", "project"))
            rec_w = _recency_multiplier(r.get("created_at", ""))
            wing_w = _wing_weight(r.get("wing", "default"))
            score = base * cat_w * rec_w * wing_w

            # MOL-177 Ranker supersession logic
            if r.get("category") in ("chat", "briefing"):
                entry_content = r.get("content", "").lower()
                entry_dt = _parse_created_at(r.get("created_at", ""))
                for token, comp_dt in superseded_tokens:
                    if token.lower() in entry_content:
                        if entry_dt and comp_dt and entry_dt < comp_dt:
                            score *= 0.2
                            break

            results.append({**r, "score": score, "search_mode": "fts_only"})
        # Re-sort since weights may have changed order
        results.sort(key=lambda x: x["score"], reverse=True)
        return results
