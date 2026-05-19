"""Tiered Memory Provider — hybrid FTS5 + sqlite-vec with RRF ranking.

Implements the MemoryProvider ABC for Hermes. Activated via config:
  memory:
    provider: tiered

Gates activation on sqlite-vec only (Ollama is optional — FTS fallback).
"""

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider

from .enrichment import (
    compose_enrichment_block,
    get_active_principles,
    read_enrichment_cache,
    refresh_enrichment_async,
)
from .granola_tools import (
    get_granola_meeting,
    list_granola_meetings,
    query_granola_meetings,
)
from .hot_cache import maybe_update_hot_cache
from .search import hybrid_search
from .store import TieredMemoryDB, _classify_room

logger = logging.getLogger(__name__)

MEMORY_SEARCH_SCHEMA = {
    "name": "memory_search",
    "description": "Search long-term memory using hybrid full-text + semantic search with RRF ranking.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query (1-1000 chars)"},
            "category": {
                "type": "string",
                "enum": ["briefing", "chat", "project", "principle"],
                "description": "Optional category filter",
            },
            # P13 / MOL-168 item 3: maximum reduced from 20 → 10 to cap
            # worst-case context injection. With the per-entry 500-char slice
            # applied below, 10 × 500 = ~5KB max tool output.
            "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
        },
        "required": ["query"],
    },
}

# P13 / MOL-168 item 2: per-entry content slice returned by memory_search.
# Matches the prefetch() path's 500-char truncation (line ~282). Prevents a
# ballooning corpus (Obsidian/diary/Granola ingest) from overflowing context
# when the agent calls memory_search with large entries in the result set.
_MEMORY_SEARCH_CONTENT_SLICE = 500

MEMORY_OBSERVE_SCHEMA = {
    "name": "memory_observe",
    "description": "Store an observation in long-term memory with automatic embedding for semantic search.",
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short title (1-200 chars)"},
            "content": {"type": "string", "description": "Observation content (1-10000 chars)"},
            "category": {
                "type": "string",
                "enum": ["briefing", "chat", "project", "principle"],
                "default": "project",
            },
        },
        "required": ["title", "content"],
    },
}


GRANOLA_LIST_SCHEMA = {
    "name": "list_granola_meetings",
    "description": "List Granola meeting notes with time range filtering.",
    "parameters": {
        "type": "object",
        "properties": {
            "time_range": {
                "type": "string",
                "enum": ["this_week", "last_week", "last_30_days", "custom"],
                "default": "last_30_days",
                "description": "Time range filter",
            },
            "custom_start": {"type": "string", "description": "ISO datetime for custom range start"},
            "custom_end": {"type": "string", "description": "ISO datetime for custom range end"},
        },
    },
}

GRANOLA_GET_SCHEMA = {
    "name": "get_granola_meeting",
    "description": "Get a single Granola meeting. Set include_transcript=true for full transcript.",
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Meeting ID"},
            "include_transcript": {"type": "boolean", "default": False},
        },
        "required": ["id"],
    },
}

GRANOLA_QUERY_SCHEMA = {
    "name": "query_granola_meetings",
    "description": "Search Granola meetings with natural language query.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query (min 2 chars)"},
        },
        "required": ["query"],
    },
}


class TieredMemoryProvider(MemoryProvider):
    """Hybrid FTS5 + sqlite-vec memory with RRF ranking."""

    @property
    def name(self) -> str:
        return "tiered"

    def is_available(self) -> bool:
        try:
            import sqlite_vec  # noqa: F401
            return True
        except ImportError:
            return False

    def initialize(self, session_id: str, **kwargs) -> None:
        hermes_home = kwargs.get("hermes_home", str(Path.home() / ".hermes"))
        self._hermes_home = hermes_home
        db_path = Path(hermes_home) / "memory" / "hermes.db"
        self._db = TieredMemoryDB(db_path)
        self._session_id = session_id
        self._sync_thread: threading.Thread | None = None
        self._memory_dir = Path(hermes_home) / "memories"
        self._ensure_cron_registered()

    def _ensure_cron_registered(self) -> None:
        """Register consolidation and comprehensive-update cron jobs if not already present."""
        try:
            from cron.jobs import create_job, list_jobs
        except ImportError:
            logger.debug("cron.jobs not available — skipping cron registration")
            return
        try:
            jobs = list_jobs()

            existing_consol = [j for j in jobs if j.get("name") == "Tiered Memory Consolidation"]
            if not existing_consol:
                create_job(
                    prompt="Run tiered memory consolidation",
                    schedule="0 4 * * *",
                    name="Tiered Memory Consolidation",
                    deliver="local",
                )
                logger.info("Registered tiered memory consolidation cron (0 4 * * * UTC)")

            existing_cu = [j for j in jobs if j.get("name") == "Comprehensive Update"]
            if not existing_cu:
                create_job(
                    prompt="Run comprehensive update briefing",
                    schedule="0 11 * * 1-5",
                    name="Comprehensive Update",
                    deliver="telegram",
                    skill="comprehensive-update",
                )
                logger.info("Registered comprehensive update cron (0 11 * * 1-5 UTC, weekdays)")
        except Exception:
            logger.warning("Could not register cron jobs", exc_info=True)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            MEMORY_SEARCH_SCHEMA,
            MEMORY_OBSERVE_SCHEMA,
            GRANOLA_LIST_SCHEMA,
            GRANOLA_GET_SCHEMA,
            GRANOLA_QUERY_SCHEMA,
        ]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if tool_name == "memory_search":
            query = args["query"][:1000]
            try:
                results = hybrid_search(
                    self._db,
                    query,
                    limit=args.get("limit", 5),
                    category=args.get("category"),
                )  # P151/MOL-502: rerank= removed
                # P13 / MOL-168 item 2: truncate per-entry content before
                # returning. Without this, large ingested entries (Obsidian,
                # diary, Granola) balloon the tool output and ultimately
                # overflow the main agent's 200k context. Prefetch uses the
                # same 500-char slice at line ~282.
                truncated = []
                for r in results:
                    content = r.get("content", "") or ""
                    if len(content) > _MEMORY_SEARCH_CONTENT_SLICE:
                        content = content[:_MEMORY_SEARCH_CONTENT_SLICE] + "...[truncated]"
                    truncated.append({**r, "content": content})
                return json.dumps({"results": truncated, "total": len(truncated)})
            except Exception as e:
                logger.warning("memory_search failed: %s", e)
                return json.dumps({"results": [], "total": 0, "error": "Search temporarily unavailable"})
        elif tool_name == "memory_observe":
            title = args["title"][:200]
            content = args["content"][:10000]
            try:
                entry_id = self._db.insert_entry(title, content, args.get("category", "project"))
                return json.dumps({"id": entry_id, "category": args.get("category", "project"), "status": "stored"})
            except RuntimeError as e:
                logger.error("memory_observe scrub failure: %s", e)
                return json.dumps({"error": "Content scrubbing unavailable — observation rejected", "status": "rejected"})
            except Exception as e:
                logger.warning("memory_observe failed: %s", e)
                return json.dumps({"error": "Storage temporarily unavailable", "status": "failed"})
        elif tool_name == "list_granola_meetings":
            try:
                result = list_granola_meetings(
                    time_range=args.get("time_range", "last_30_days"),
                    custom_start=args.get("custom_start"),
                    custom_end=args.get("custom_end"),
                )
                return result["content"]
            except Exception as e:
                logger.warning("list_granola_meetings failed: %s", e)
                return json.dumps({"error": "Granola temporarily unavailable", "details": str(e)})
        elif tool_name == "get_granola_meeting":
            try:
                result = get_granola_meeting(
                    meeting_id=args.get("id", ""),
                    include_transcript=args.get("include_transcript", False),
                )
                return result["content"]
            except Exception as e:
                logger.warning("get_granola_meeting failed: %s", e)
                return json.dumps({"error": "Granola temporarily unavailable", "details": str(e)})
        elif tool_name == "query_granola_meetings":
            try:
                result = query_granola_meetings(query=args.get("query", ""))
                return result["content"]
            except Exception as e:
                logger.warning("query_granola_meetings failed: %s", e)
                return json.dumps({"error": "Granola temporarily unavailable", "details": str(e)})
        raise NotImplementedError(f"Unknown tool: {tool_name}")

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        # Update hot cache if stale (best-effort)
        try:
            maybe_update_hot_cache(self._db, self._memory_dir)
        except Exception:
            logger.warning("Hot cache update failed", exc_info=True)

        # Context enrichment (best-effort, never breaks prefetch)
        enrichment = ""
        if hasattr(self, "_hermes_home"):
            cache_path = Path(self._hermes_home) / "cache" / "enrichment.json"
            cache = None
            try:
                cache = read_enrichment_cache(cache_path)
                if cache is None:
                    # Non-blocking: refresh in background, use stale/empty for this turn
                    refresh_enrichment_async(cache_path)
            except OSError:
                logger.warning("Enrichment cache I/O failed for %s", cache_path, exc_info=True)

            principles: list[str] = []
            try:
                principles = get_active_principles(self._db)
            except Exception:
                logger.warning("Failed to query active principles", exc_info=True)

            try:
                enrichment = compose_enrichment_block(cache, principles)
            except Exception:
                logger.warning("Failed to compose enrichment block", exc_info=True)

        # Infer room from query for partitioned search (MemPalace-inspired)
        # If classifier returns "general", skip room filter (search full corpus)
        inferred_room = _classify_room(query, query)
        room_filter = inferred_room if inferred_room != "general" else None

        # Hybrid search with room partitioning
        # P151/MOL-502: rerank= argument removed; hybrid RRF + wing/room/category/recency
        # weighting carries the recall load. The learned qwen3:8b reranker was failing
        # cold (10s timeout < model load time) and silently falling back to RRF anyway.
        try:
            results = hybrid_search(self._db, query, limit=5, room=room_filter)
            # If room-filtered search returns too few results, fall back to full corpus
            if len(results) < 2 and room_filter:
                results = hybrid_search(self._db, query, limit=5)
        except Exception:
            logger.warning("prefetch failed, returning empty context", exc_info=True)
            results = []

        parts = []
        if enrichment:
            parts.append(enrichment)
        if results:
            lines = ["[Recalled from memory]"]
            for r in results:
                lines.append(f"- **{r['title']}** (score: {r['score']:.4f}): {r['content'][:500]}")
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        # Join previous sync thread if still running (prevent thread pileup)
        if hasattr(self, '_sync_thread') and self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)

        def _bg():
            try:
                title = user_content[:100].strip()
                content = f"User: {user_content}\n\nAssistant: {assistant_content[:2000]}"
                self._db.insert_entry(title, content, category="chat")
            except RuntimeError:
                logger.error("sync_turn scrub failure — memory persistence disabled", exc_info=True)
            except Exception:
                logger.warning("sync_turn background insert failed", exc_info=True)

        self._sync_thread = threading.Thread(target=_bg, daemon=True)
        self._sync_thread.start()

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Extract session summary and store as briefing entry."""
        try:
            from datetime import datetime, timezone
            for msg in reversed(messages):
                if msg.get("role") == "assistant":
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            c.get("text", "") for c in content if c.get("type") == "text"
                        )
                    if content:
                        title = f"Session {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
                        self._db.insert_entry(title, content[:2000], category="briefing")
                        break
        except Exception:
            logger.warning("on_session_end extraction failed", exc_info=True)

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        if action == "add" and content:
            title = content[:100].strip()
            try:
                self._db.insert_entry(title, content, category="project")
            except RuntimeError:
                logger.error("on_memory_write scrub failure — write not mirrored to tiered store", exc_info=True)
            except Exception:
                logger.warning("on_memory_write failed — write not mirrored to tiered store", exc_info=True)

    def shutdown(self) -> None:
        if hasattr(self, "_sync_thread") and self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=2.0)
        if hasattr(self, "_db"):
            self._db.close()
