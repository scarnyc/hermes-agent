"""SQLite + sqlite-vec + FTS5 database store for tiered memory."""

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import sqlite_vec
from sqlite_vec import serialize_float32

from .embeddings import generate_embedding
from .scrub import scrub_content

logger = logging.getLogger(__name__)

CATEGORY_TTL = {
    "briefing": 30,
    "chat": 14,
    "project": 90,
    "principle": None,
}

STALENESS_THRESHOLDS = {
    "briefing": 7,
    "chat": 7,  # Tightened from 14 — chats are ephemeral context, not long-term knowledge
    "project": 30,
    # principle: never archived
}

# Short acknowledgment titles that provide zero retrieval value.
# Applied to chat category only; principles and projects are never touched.
NOISE_CHAT_TITLES = {
    "", "hi", "hey", "hello", "yo", "sup",
    "yes", "yep", "yup", "y", "yeah", "yea",
    "no", "nope", "nah", "n",
    "sure", "done", "ok", "okay", "k", "kk",
    "thanks", "thx", "ty", "thank you",
    "cool", "nice", "got it", "gotcha", "understood",
    "great", "perfect", "awesome",
    "go", "go ahead", "proceed", "continue",
}

_SCHEMA = """
-- NOTE: PRAGMA journal_mode=WAL is executed SEPARATELY before this script

CREATE TABLE IF NOT EXISTS memory_entries (
  id          TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  source      TEXT NOT NULL DEFAULT 'hermes',
  title       TEXT NOT NULL,
  content     TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  category    TEXT NOT NULL DEFAULT 'project'
                CHECK (category IN ('briefing','chat','project','principle')),
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
  archived_at TEXT,
  expires_at  TEXT,
  superseded_by TEXT REFERENCES memory_entries(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_content_hash ON memory_entries(content_hash);
CREATE INDEX IF NOT EXISTS idx_category ON memory_entries(category);
CREATE INDEX IF NOT EXISTS idx_archived ON memory_entries(archived_at);
CREATE INDEX IF NOT EXISTS idx_expires ON memory_entries(expires_at);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  title, content, content=memory_entries, content_rowid=rowid
);

CREATE TRIGGER IF NOT EXISTS memory_fts_i AFTER INSERT ON memory_entries BEGIN
  INSERT INTO memory_fts(rowid, title, content) VALUES (new.rowid, new.title, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memory_fts_d AFTER DELETE ON memory_entries BEGIN
  INSERT INTO memory_fts(memory_fts, rowid, title, content) VALUES ('delete', old.rowid, old.title, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memory_fts_u AFTER UPDATE ON memory_entries BEGIN
  INSERT INTO memory_fts(memory_fts, rowid, title, content) VALUES ('delete', old.rowid, old.title, old.content);
  INSERT INTO memory_fts(rowid, title, content) VALUES (new.rowid, new.title, new.content);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(
  entry_rowid integer primary key,
  embedding float[1024] distance_metric=cosine,
  category text
);
-- P151/MOL-502: dim was 1024 (bge-m3); briefly 768 (nomic-embed-text).
-- P168/MOL-546: back to 1024 (mxbai-embed-large-v1).
-- Existing DBs auto-migrate via _migrate_vec_dims() in __init__. Fresh DBs
-- skip the migration (no prior memory_vec data → migration probe returns
-- empty), so the SCHEMA literal MUST match the current model's DIMS or
-- fresh installs ship with a broken vec table.

CREATE TABLE IF NOT EXISTS memory_stats (
  source      TEXT PRIMARY KEY DEFAULT 'hermes',
  entry_count INTEGER NOT NULL DEFAULT 0,
  total_bytes INTEGER NOT NULL DEFAULT 0,
  updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT OR IGNORE INTO memory_stats (source) VALUES ('hermes');

-- Wing/room hierarchical partitioning (MemPalace-inspired, MOL-176)
-- Wing: source-level grouping (diary, obsidian, chat, default)
-- Room: topic-level grouping (security, memory, integrations, etc.)

CREATE TABLE IF NOT EXISTS memory_facts (
  id              TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  subject         TEXT NOT NULL,
  predicate       TEXT NOT NULL,
  object          TEXT NOT NULL,
  source_entry_id TEXT REFERENCES memory_entries(id),
  valid_from      TEXT NOT NULL DEFAULT (datetime('now')),
  valid_to        TEXT,
  confidence      REAL DEFAULT 1.0,
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fact_subject ON memory_facts(subject);
CREATE INDEX IF NOT EXISTS idx_fact_valid ON memory_facts(valid_to);
"""


# Wing/room classifier — heuristic keyword matching (no LLM calls)
# Loaded from config/memory/room-taxonomy.yaml at runtime; hardcoded fallback below.

_DEFAULT_TAXONOMY = {
    "wings": {
        "diary":    {"match": "title_prefix:[diary]",    "weight": 0.5},
        "obsidian": {"match": "title_prefix:[obsidian]", "weight": 0.7},
        "chat":     {"match": "category:chat",           "weight": 0.8},
        "default":  {"weight": 1.0},
    },
    "rooms": {
        "security":       {"keywords": ["sandbox", "rampart", "firewall", "tcc", "pf", "invariant", "sbpl", "deny", "allow", "credential", "injection", "ssrf", "pii"]},
        "memory":         {"keywords": ["memory", "recall", "embedding", "fts", "vector", "dedup", "consolidation", "hot_cache", "prefetch", "mempalace", "search", "rrf"]},
        "infrastructure": {"keywords": ["gateway", "launchd", "flock", "envchain", "sandbox-exec", "docker", "ollama", "plist", "kickstart"]},
        "integrations":   {"keywords": ["jira", "gws", "google", "gmail", "calendar", "granola", "telegram", "obsidian", "slack", "mcp"]},
        "skills":         {"keywords": ["skill", "hermes skills", "install", "scanner", "cisco", "hub", "tap", "wondelai"]},
        "patches":        {"keywords": ["patch", "verify_patches", "update_hermes", "hermes-patches"]},
        "context":        {"keywords": ["context", "enrichment", "prefetch", "briefing", "comprehensive-update", "cron"]},
        "general":        {"keywords": []},
    },
}

_TAXONOMY_PATHS = [
    Path.home() / ".hermes" / "config" / "memory" / "room-taxonomy.yaml",
    Path(__file__).resolve().parents[4] / "config" / "memory" / "room-taxonomy.yaml",
]


def _load_taxonomy() -> dict:
    """Load room taxonomy from YAML config. Falls back to hardcoded defaults."""
    for path in _TAXONOMY_PATHS:
        if path.exists():
            try:
                import yaml
                with open(path, encoding="utf-8") as f:
                    return yaml.safe_load(f)
            except Exception as e:
                logger.warning("Failed to load taxonomy from %s: %s", path, e)
    return _DEFAULT_TAXONOMY


def _classify_wing(title: str, category: str) -> str:
    """Classify entry into a wing based on title prefix and category."""
    title_lower = title.lower()
    if title_lower.startswith("[diary]"):
        return "diary"
    if title_lower.startswith("[obsidian]"):
        return "obsidian"
    if category == "chat":
        return "chat"
    return "default"


def _classify_room(title: str, content: str, taxonomy: dict | None = None) -> str:
    """Classify entry into a room based on keyword matching in title + content."""
    if taxonomy is None:
        taxonomy = _DEFAULT_TAXONOMY
    rooms = taxonomy.get("rooms", {})
    text = f"{title} {content}".lower()
    best_room = "general"
    best_count = 0
    for room_name, room_def in rooms.items():
        if room_name == "general":
            continue
        keywords = room_def.get("keywords", [])
        count = sum(1 for kw in keywords if kw.lower() in text)
        if count > best_count:
            best_count = count
            best_room = room_name
    return best_room


class TieredMemoryDB:
    """SQLite-backed memory store with FTS5 and sqlite-vec vector search."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        # Verify extension loaded correctly
        vec_version = self._conn.execute("SELECT vec_version()").fetchone()[0]
        logger.info("sqlite-vec loaded: %s", vec_version)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._migrate_vec_dims()
        self._migrate_wing_room()
        self._migrate_superseded()
        self._migrate_tombstone_audit()
        self._taxonomy = _load_taxonomy()

    def _migrate_superseded(self) -> None:
        """Add superseded_by column if missing."""
        try:
            self._conn.execute("SELECT superseded_by FROM memory_entries LIMIT 1")
        except sqlite3.OperationalError:
            logger.info("Adding superseded_by column to memory_entries")
            self._conn.execute("ALTER TABLE memory_entries ADD COLUMN superseded_by TEXT REFERENCES memory_entries(id)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_superseded ON memory_entries(superseded_by)")
            self._conn.commit()

    def _migrate_tombstone_audit(self) -> None:
        """P50/MOL-177 Phase 2: Add tombstone_audit table if missing.

        Records every tombstone decision the consolidation job's supersession
        pass makes, in dry-run (applied=0) and live (applied=1) modes alike.
        Serves the ticket's 7-day audit-before-flip gate and permanent forensics.
        """
        try:
            self._conn.execute("SELECT 1 FROM tombstone_audit LIMIT 1")
        except sqlite3.OperationalError:
            logger.info("Creating tombstone_audit table (P50/MOL-177)")
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS tombstone_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_ts TEXT NOT NULL DEFAULT (datetime('now')),
                    entry_id TEXT NOT NULL REFERENCES memory_entries(id),
                    superseded_by_id TEXT NOT NULL REFERENCES memory_entries(id),
                    mol_token TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    applied INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_tombstone_run_ts ON tombstone_audit(run_ts);
                CREATE INDEX IF NOT EXISTS idx_tombstone_entry ON tombstone_audit(entry_id);
            """)
            self._conn.commit()

    def _migrate_wing_room(self) -> None:
        """Add wing/room columns if missing (one-time migration)."""
        try:
            self._conn.execute("SELECT wing FROM memory_entries LIMIT 1")
        except sqlite3.OperationalError:
            logger.info("Adding wing/room columns to memory_entries")
            self._conn.execute("ALTER TABLE memory_entries ADD COLUMN wing TEXT NOT NULL DEFAULT 'default'")
            self._conn.execute("ALTER TABLE memory_entries ADD COLUMN room TEXT NOT NULL DEFAULT 'general'")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_wing ON memory_entries(wing)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_room ON memory_entries(room)")
            self._conn.commit()

    def _migrate_vec_dims(self) -> None:
        """Recreate memory_vec if embedding dimensions changed (model upgrade).

        P168/MOL-546 fix-pass-2: previously swallowed all exceptions to a warning,
        leaving a partial-state vec table (old dim) in place after a failed
        DROP+CREATE. That made every subsequent insert into the new-dim schema
        silently mismatch the search-path joins. Now: pre-flight read errors stay
        soft (no prior memory_vec is OK), but a failed DROP+CREATE+_reembed_all
        raises so the operator sees the migration failure on first gateway boot
        instead of running with a broken vec index.
        """
        from .embeddings import DIMS
        try:
            row = self._conn.execute(
                "SELECT length(embedding) as emb_bytes FROM memory_vec LIMIT 1"
            ).fetchone()
        except Exception as e:
            # Reading the vec table failed (table missing on fresh DB is normal).
            logger.warning("Vec migration probe failed (no prior vec table?): %s", e)
            return
        if not row or not row["emb_bytes"]:
            return
        current_dims = row["emb_bytes"] // 4  # float32 = 4 bytes
        if current_dims == DIMS:
            return
        logger.warning(
            "Vec dimension mismatch: DB has %d, model expects %d — rebuilding",
            current_dims, DIMS,
        )
        # Fail-closed: any error in the DROP+CREATE+reembed sequence escalates
        # to the caller instead of leaving a partial vec table.
        self._conn.execute("DROP TABLE memory_vec")
        self._conn.execute(f"""
            CREATE VIRTUAL TABLE memory_vec USING vec0(
                entry_rowid integer primary key,
                embedding float[{DIMS}] distance_metric=cosine,
                category text
            )
        """)
        self._conn.commit()
        self._reembed_all()

    def _reembed_all(self) -> None:
        """Re-embed all non-archived entries after a dimension change.

        P168/MOL-546 fix-pass-2 (silent-failure-hunter): on a fastembed model
        load failure (HF download blocked, ONNX init crash) the prior version
        silently logged `Re-embedded 0/N` at INFO level and the gateway booted
        with an EMPTY memory_vec — undetectable until queries returned no
        vector hits. Now: if rows existed but zero embedded successfully, the
        migration writes a tripwire file (read by integrity-check on next boot)
        and RAISES, refusing to commit the empty vec table.
        """
        rows = self._conn.execute(
            "SELECT rowid, title, content, category FROM memory_entries WHERE archived_at IS NULL"
        ).fetchall()
        count = 0
        for r in rows:
            # P168/MOL-546: title appears 2x so it contributes meaningfully even on long content
            emb = generate_embedding(f"{r['title']}. {r['title']}. {r['content']}")
            if emb:
                self._conn.execute(
                    "INSERT OR REPLACE INTO memory_vec (entry_rowid, embedding, category) VALUES (?, ?, ?)",
                    (r["rowid"], serialize_float32(emb), r["category"]),
                )
                count += 1
        if len(rows) > 0 and count == 0:
            self._conn.rollback()
            logger.error(
                "P168/MOL-546: re-embed produced zero embeddings for %d non-archived rows; "
                "fastembed init likely failed. Refusing to commit empty memory_vec.",
                len(rows),
            )
            tripwire_dir = os.path.expanduser("~/.hermes/state")
            os.makedirs(tripwire_dir, exist_ok=True)
            tripwire = os.path.join(
                tripwire_dir, f"reembed-failure-{int(time.time())}.json"
            )
            try:
                with open(tripwire, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "ts": time.time(),
                            "rows_seen": len(rows),
                            "embedded": count,
                            "marker": "P168/MOL-546 re-embed produced zero embeddings",
                        },
                        f,
                    )
            except OSError as e:
                logger.warning("Tripwire write failed: %s", e)
            raise RuntimeError(
                f"re-embed produced zero embeddings; vec table is empty (rows={len(rows)})"
            )
        self._conn.commit()
        logger.info("Re-embedded %d/%d entries with new model", count, len(rows))

    def insert_entry(self, title: str, content: str, category: str = "project") -> str:
        """Insert a memory entry with dedup, scrubbing, FTS indexing, and vector embedding.
        Returns the entry id (text UUID). Returns existing id on duplicate."""
        with self._lock:
            # Hash ORIGINAL content for dedup (before scrub, so different originals don't collide)
            content_hash = hashlib.sha256(content.encode()).hexdigest()

            existing = self._conn.execute(
                "SELECT id FROM memory_entries WHERE content_hash = ?", (content_hash,)
            ).fetchone()
            if existing:
                return existing["id"]

            # Scrub AFTER dedup check
            content = scrub_content(content)
            title = scrub_content(title)

            # Classify wing and room
            wing = _classify_wing(title, category)
            room = _classify_room(title, content, self._taxonomy)

            ttl_days = CATEGORY_TTL.get(category)
            expires_at = (
                (datetime.now(timezone.utc) + timedelta(days=ttl_days)).strftime("%Y-%m-%d %H:%M:%S")
                if ttl_days else None
            )

            try:
                cursor = self._conn.execute(
                    "INSERT INTO memory_entries (title, content, content_hash, category, expires_at, wing, room) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (title, content, content_hash, category, expires_at, wing, room),
                )
                entry_rowid = cursor.lastrowid
                entry_id = self._conn.execute(
                    "SELECT id FROM memory_entries WHERE rowid = ?", (entry_rowid,)
                ).fetchone()["id"]

                # P168/MOL-546: title appears 2x so it contributes meaningfully even on long content
                embedding = generate_embedding(f"{title}. {title}. {content}")
                if embedding:
                    self._conn.execute(
                        "INSERT INTO memory_vec (entry_rowid, embedding, category) VALUES (?, ?, ?)",
                        (entry_rowid, serialize_float32(embedding), category),
                    )

                self._update_stats()

                self._conn.commit()
                return entry_id
            except Exception:
                self._conn.rollback()
                raise

    def search_fts(self, query: str, limit: int = 10, category: Optional[str] = None,
                   wing: Optional[str] = None, room: Optional[str] = None,
                   include_superseded: bool = False) -> list[dict]:
        """Full-text search with BM25 ranking. Excludes archived entries."""
        with self._lock:
            escaped = _escape_fts_query(query)
            if not escaped:
                return []

            superseded_clause = "AND me.superseded_by IS NULL" if not include_superseded else ""
            rows = self._conn.execute(f"""
                SELECT me.id, me.title, me.content, me.category, me.created_at,
                       me.wing, me.room, me.superseded_by,
                       bm25(memory_fts, 5.0, 1.0) AS fts_rank
                FROM memory_fts
                JOIN memory_entries me ON me.rowid = memory_fts.rowid
                WHERE memory_fts MATCH ?
                  AND me.archived_at IS NULL
                  {superseded_clause}
                  AND (? IS NULL OR me.category = ?)
                  AND (? IS NULL OR me.wing = ?)
                  AND (? IS NULL OR me.room = ?)
                ORDER BY bm25(memory_fts, 5.0, 1.0)
                LIMIT ?
            """, (escaped, category, category, wing, wing, room, room, limit)).fetchall()  # nosec B608 — superseded_clause is one of two hardcoded literals

            return [dict(row) for row in rows]

    def search_vec(self, embedding: list[float], limit: int = 10, category: Optional[str] = None,
                   wing: Optional[str] = None, room: Optional[str] = None,
                   include_superseded: bool = False) -> list[dict]:
        """Vector KNN search with cosine distance. Excludes archived entries."""
        with self._lock:
            if category:
                vec_rows = self._conn.execute("""
                    SELECT entry_rowid, distance
                    FROM memory_vec
                    WHERE embedding MATCH ? AND k = ? AND category = ?
                """, (serialize_float32(embedding), limit * 4, category)).fetchall()
            else:
                vec_rows = self._conn.execute("""
                    SELECT entry_rowid, distance
                    FROM memory_vec
                    WHERE embedding MATCH ? AND k = ?
                """, (serialize_float32(embedding), limit * 4)).fetchall()

            results = []
            for vr in vec_rows:
                superseded_clause = "AND superseded_by IS NULL" if not include_superseded else ""
                me = self._conn.execute(f"""
                    SELECT id, title, content, category, created_at, wing, room, superseded_by
                    FROM memory_entries
                    WHERE rowid = ? AND archived_at IS NULL
                    {superseded_clause}
                """, (vr["entry_rowid"],)).fetchone()  # nosec B608 — superseded_clause is one of two hardcoded literals
                if me:
                    if wing and me["wing"] != wing:
                        continue
                    if room and me["room"] != room:
                        continue
                    row = dict(me)
                    row["distance"] = vr["distance"]
                    results.append(row)
                if len(results) >= limit:
                    break

            return results

    def archive_entry(self, entry_id: str) -> bool:
        """Soft-archive an entry. Returns True if found and archived."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE memory_entries SET archived_at = datetime('now') WHERE id = ? AND archived_at IS NULL",
                (entry_id,),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def delete_expired(self) -> int:
        """Delete entries past their expires_at. Returns count deleted."""
        with self._lock:
            # First delete from vec (need rowids before deleting entries)
            expired_rowids = self._conn.execute(
                "SELECT rowid FROM memory_entries WHERE expires_at IS NOT NULL AND expires_at < datetime('now')"
            ).fetchall()
            for row in expired_rowids:
                self._conn.execute("DELETE FROM memory_vec WHERE entry_rowid = ?", (row["rowid"],))

            cursor = self._conn.execute(
                "DELETE FROM memory_entries WHERE expires_at IS NOT NULL AND expires_at < datetime('now')"
            )
            count = cursor.rowcount
            if count > 0:
                self._update_stats()
            self._conn.commit()
            return count

    def get_stats(self) -> dict:
        """Return entry_count, total_bytes from memory_stats."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM memory_stats WHERE source = 'hermes'").fetchone()
            return dict(row) if row else {"entry_count": 0, "total_bytes": 0}

    # ------------------------------------------------------------------
    # Stats helpers
    # ------------------------------------------------------------------

    def _update_stats(self) -> None:
        """Recompute memory_stats. Call within a lock context."""
        self._conn.execute("""
            UPDATE memory_stats SET
              entry_count = (SELECT COUNT(*) FROM memory_entries WHERE archived_at IS NULL),
              total_bytes = (SELECT COALESCE(SUM(LENGTH(content)), 0) FROM memory_entries WHERE archived_at IS NULL),
              updated_at = datetime('now')
            WHERE source = 'hermes'
        """)

    def update_stats(self) -> None:
        """Public wrapper — recompute memory_stats under lock."""
        with self._lock:
            self._update_stats()
            self._conn.commit()

    # ------------------------------------------------------------------
    # Recent entries & pattern detection
    # ------------------------------------------------------------------

    def get_recent_entries(
        self,
        hours: int = 48,
        exclude_category: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Get non-archived entries from the last N hours.

        If ``limit`` is provided, caps the number of rows returned (ordered by
        ``created_at DESC``). Default ``None`` preserves unbounded behavior for
        existing callers. See MOL-168 / P10 for context.
        """
        with self._lock:
            if exclude_category:
                sql = (
                    "SELECT id, title, content, category, created_at "
                    "FROM memory_entries "
                    "WHERE archived_at IS NULL "
                    "  AND created_at > datetime('now', ? || ' hours') "
                    "  AND category != ? "
                    "ORDER BY created_at DESC"
                )
                params: tuple = (f"-{hours}", exclude_category)
            else:
                sql = (
                    "SELECT id, title, content, category, created_at "
                    "FROM memory_entries "
                    "WHERE archived_at IS NULL "
                    "  AND created_at > datetime('now', ? || ' hours') "
                    "ORDER BY created_at DESC"
                )
                params = (f"-{hours}",)
            if limit is not None:
                sql += " LIMIT ?"
                params = (*params, int(limit))
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def query_active_principles(self, limit: int = 10) -> list[str]:
        """Return titles of non-archived principle entries, most recent first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT title FROM memory_entries "
                "WHERE category = 'principle' AND archived_at IS NULL "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [row["title"] for row in rows]

    def detect_patterns(self, days: int = 7, min_count: int = 3) -> list[dict]:
        """Find repeated topics over N days. Groups by title prefix (50 chars)."""
        with self._lock:
            rows = self._conn.execute("""
                SELECT
                    SUBSTR(title, 1, 50) AS topic,
                    COUNT(*) AS count,
                    MAX(created_at) AS last_seen
                FROM memory_entries
                WHERE archived_at IS NULL
                  AND created_at > datetime('now', ? || ' days')
                GROUP BY SUBSTR(title, 1, 50)
                HAVING COUNT(*) >= ?
                ORDER BY COUNT(*) DESC
            """, (f"-{days}", min_count)).fetchall()
            return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Housekeeping: noise deletion & stale archival
    # ------------------------------------------------------------------

    def delete_noise(self, dry_run: bool = False) -> int:
        """Delete known noise entries (greetings, fallback diaries, short chat acks).

        Noise criteria:
        - Legacy greeting/diary patterns (any category).
        - Chat entries whose normalized title is in NOISE_CHAT_TITLES.
        - Chat entries with content shorter than 30 chars (pure acks).

        Principles and projects are never touched. If dry_run=True, returns the
        count that would be deleted without mutating anything.
        """
        with self._lock:
            # Build placeholders for the noise title set
            noise_titles = list(NOISE_CHAT_TITLES)
            title_placeholders = ",".join("?" * len(noise_titles))

            select_sql = f"""
                SELECT rowid FROM memory_entries WHERE
                    title LIKE 'User Greeted%'
                    OR title LIKE 'User greeted%'
                    OR title = 'Handled simple greeting after loading workspace context'
                    OR title LIKE 'Fallback diary:%'
                    OR (category = 'chat' AND LOWER(TRIM(title)) IN ({title_placeholders}))
                    OR (category = 'chat' AND LENGTH(content) < 30)
            """  # nosec B608 — title_placeholders is structural "?,?,..."
            noise_rowids = self._conn.execute(select_sql, noise_titles).fetchall()

            if dry_run:
                return len(noise_rowids)

            # Clean up vec entries first
            for row in noise_rowids:
                self._conn.execute("DELETE FROM memory_vec WHERE entry_rowid = ?", (row["rowid"],))

            delete_sql = f"""
                DELETE FROM memory_entries WHERE
                    title LIKE 'User Greeted%'
                    OR title LIKE 'User greeted%'
                    OR title = 'Handled simple greeting after loading workspace context'
                    OR title LIKE 'Fallback diary:%'
                    OR (category = 'chat' AND LOWER(TRIM(title)) IN ({title_placeholders}))
                    OR (category = 'chat' AND LENGTH(content) < 30)
            """  # nosec B608 — title_placeholders is structural "?,?,..."
            cursor = self._conn.execute(delete_sql, noise_titles)
            count = cursor.rowcount
            if count > 0:
                self._update_stats()
            self._conn.commit()
            return count

    def deduplicate_chat_titles(self, dry_run: bool = False) -> int:
        """Collapse chat entries sharing the same normalized title.

        For each group of chat entries with the same LOWER(TRIM(title)),
        keep the most recent entry and delete the rest.
        Only applies to 'chat' category. Returns count deleted (or to-be-deleted).
        """
        with self._lock:
            dup_groups = self._conn.execute("""
                SELECT LOWER(TRIM(title)) AS norm_title, COUNT(*) AS n
                FROM memory_entries
                WHERE category = 'chat' AND archived_at IS NULL
                GROUP BY LOWER(TRIM(title))
                HAVING COUNT(*) > 1
            """).fetchall()

            to_delete: list[int] = []
            for group in dup_groups:
                rows = self._conn.execute("""
                    SELECT rowid FROM memory_entries
                    WHERE category = 'chat'
                      AND archived_at IS NULL
                      AND LOWER(TRIM(title)) = ?
                    ORDER BY created_at DESC
                """, (group["norm_title"],)).fetchall()
                # Keep [0] (most recent), delete the rest
                to_delete.extend(r["rowid"] for r in rows[1:])

            if dry_run:
                return len(to_delete)

            for rowid in to_delete:
                self._conn.execute("DELETE FROM memory_vec WHERE entry_rowid = ?", (rowid,))
                self._conn.execute("DELETE FROM memory_entries WHERE rowid = ?", (rowid,))

            if to_delete:
                self._update_stats()
            self._conn.commit()
            return len(to_delete)

    def archive_stale(self) -> int:
        """Soft-archive entries past their category staleness threshold. Returns count."""
        with self._lock:
            total = 0
            for category, days in STALENESS_THRESHOLDS.items():
                cursor = self._conn.execute("""
                    UPDATE memory_entries
                    SET archived_at = datetime('now')
                    WHERE category = ?
                      AND archived_at IS NULL
                      AND created_at < datetime('now', ? || ' days')
                """, (category, f"-{days}"))
                total += cursor.rowcount
            if total > 0:
                self._update_stats()
            self._conn.commit()
            return total

    # ------------------------------------------------------------------
    # Housekeeping: semantic deduplication
    # ------------------------------------------------------------------

    def deduplicate_similar(self, threshold: float = 0.05, category: str | None = None,
                            keep_strategy: str = "recent", dry_run: bool = False) -> int:
        """Find entries with similar embeddings and mark them as superseded.

        Logic:
          - For each category (briefing, chat, project), find vectors with distance < threshold.
          - Identify groups of duplicates.
          - Pick one 'keeper' (based on keep_strategy: "recent" or "longest").
          - Mark others as superseded_by keeper_id (Phase 2 Tombstones).
          - Principles are NEVER deduplicated.
        """
        with self._lock:
            if category:
                categories = [category] if category != "principle" else []
            else:
                # All categories except principle
                categories = [c for c in CATEGORY_TTL if c != "principle"]

            if not categories:
                return 0

            total_superseded = 0

            for cat in categories:
                # Get all non-archived, non-superseded entries with embeddings in this category
                entries = self._conn.execute("""
                    SELECT me.rowid, me.id, me.title, me.content, me.created_at
                    FROM memory_entries me
                    JOIN memory_vec mv ON mv.entry_rowid = me.rowid
                    WHERE me.archived_at IS NULL
                      AND me.superseded_by IS NULL
                      AND me.category = ?
                    ORDER BY me.created_at ASC
                """, (cat,)).fetchall()

                if len(entries) < 2:
                    continue

                # Track which rowids have been marked for supersession in this run
                to_supersede: dict[int, str] = {} # rowid -> keeper_id

                for entry in entries:
                    rowid = entry["rowid"]
                    if rowid in to_supersede:
                        continue

                    # Get this entry's embedding
                    vec_row = self._conn.execute(
                        "SELECT embedding FROM memory_vec WHERE entry_rowid = ?",
                        (rowid,),
                    ).fetchone()
                    if not vec_row:
                        continue

                    embedding = vec_row["embedding"]

                    # Find neighbors within threshold
                    neighbors = self._conn.execute("""
                        SELECT entry_rowid, distance
                        FROM memory_vec
                        WHERE embedding MATCH ? AND k = ? AND category = ?
                    """, (embedding, len(entries), cat)).fetchall()

                    # Collect the duplicate group
                    group_rowids = [rowid]
                    for nb in neighbors:
                        nb_rowid = nb["entry_rowid"]
                        if nb_rowid == rowid:
                            continue
                        if nb_rowid in to_supersede:
                            continue
                        if nb["distance"] < threshold:
                            group_rowids.append(nb_rowid)

                    if len(group_rowids) < 2:
                        continue

                    # Find the keeper
                    placeholders = ",".join("?" * len(group_rowids))
                    if keep_strategy == "longest":
                        keeper = self._conn.execute(f"""
                            SELECT rowid, id FROM memory_entries
                            WHERE rowid IN ({placeholders})
                            ORDER BY LENGTH(content) DESC
                            LIMIT 1
                        """, group_rowids).fetchone()  # nosec B608 — placeholders is structural "?,?,..."
                    else:
                        keeper = self._conn.execute(f"""
                            SELECT rowid, id FROM memory_entries
                            WHERE rowid IN ({placeholders})
                            ORDER BY created_at DESC
                            LIMIT 1
                        """, group_rowids).fetchone()  # nosec B608 — placeholders is structural "?,?,..."

                    keeper_rowid = keeper["rowid"]
                    keeper_id = keeper["id"]
                    for rid in group_rowids:
                        if rid != keeper_rowid:
                            to_supersede[rid] = keeper_id

                # Perform updates
                if not dry_run:
                    for rid, k_id in to_supersede.items():
                        self._conn.execute(
                            "UPDATE memory_entries SET superseded_by = ?, updated_at = datetime('now') WHERE rowid = ?",
                            (k_id, rid)
                        )
                    total_superseded += len(to_supersede)
                else:
                    total_superseded += len(to_supersede)

            if total_superseded > 0 and not dry_run:
                self._update_stats()
                self._conn.commit()
            return total_superseded

    # ------------------------------------------------------------------
    # Temporal Knowledge Graph (MOL-177)
    # ------------------------------------------------------------------

    def insert_fact(self, subject: str, predicate: str, object_val: str,
                    source_entry_id: str | None = None,
                    confidence: float = 1.0) -> str:
        """Insert a fact, auto-invalidating any existing fact with same subject+predicate."""
        with self._lock:
            self.invalidate_fact(subject, predicate)
            cursor = self._conn.execute(
                "INSERT INTO memory_facts (subject, predicate, object, source_entry_id, confidence) "
                "VALUES (?, ?, ?, ?, ?)",
                (subject, predicate, object_val, source_entry_id, confidence),
            )
            self._conn.commit()
            return self._conn.execute(
                "SELECT id FROM memory_facts WHERE rowid = ?", (cursor.lastrowid,)
            ).fetchone()["id"]

    def invalidate_fact(self, subject: str, predicate: str) -> int:
        """Mark all current facts matching subject+predicate as expired."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE memory_facts SET valid_to = datetime('now') "
                "WHERE subject = ? AND predicate = ? AND valid_to IS NULL",
                (subject, predicate),
            )
            self._conn.commit()
            return cursor.rowcount

    def get_active_facts(self, subject: str | None = None, limit: int = 50) -> list[dict]:
        """Query currently valid facts, optionally filtered by subject."""
        with self._lock:
            if subject:
                rows = self._conn.execute(
                    "SELECT * FROM memory_facts WHERE valid_to IS NULL AND subject = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (subject, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM memory_facts WHERE valid_to IS NULL "
                    "ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # P50/MOL-177 Phase 2 — tombstone audit + bulk supersession helpers
    # ------------------------------------------------------------------

    def record_tombstone_audit(self, rows: list[dict]) -> int:
        """Batch-insert tombstone audit rows.

        Each row dict must carry: entry_id, superseded_by_id, mol_token,
        reason, applied (0 or 1). Returns number of rows inserted.
        """
        if not rows:
            return 0
        with self._lock:
            cursor = self._conn.executemany(
                "INSERT INTO tombstone_audit "
                "(entry_id, superseded_by_id, mol_token, reason, applied) "
                "VALUES (:entry_id, :superseded_by_id, :mol_token, :reason, :applied)",
                rows,
            )
            self._conn.commit()
            return cursor.rowcount

    def apply_tombstones(self, entry_to_superseder: dict[str, str]) -> int:
        """Set superseded_by on entries listed in entry_to_superseder.

        Only updates rows where superseded_by IS NULL so re-runs are
        idempotent and don't overwrite earlier supersession provenance
        (e.g. from deduplicate_similar). Returns total rows mutated.
        """
        if not entry_to_superseder:
            return 0
        with self._lock:
            total = 0
            for entry_id, superseder_id in entry_to_superseder.items():
                cursor = self._conn.execute(
                    "UPDATE memory_entries "
                    "SET superseded_by = ?, updated_at = datetime('now') "
                    "WHERE id = ? AND superseded_by IS NULL",
                    (superseder_id, entry_id),
                )
                total += cursor.rowcount
            if total > 0:
                self._conn.commit()
            return total

    def apply_tombstones_and_audit(
        self,
        apply_map: dict[str, str],
        audit_rows: list[dict],
    ) -> tuple[int, int]:
        """P50/MOL-177 — live-mode atomic tombstone + audit write.

        Wraps both the memory_entries UPDATE and the tombstone_audit INSERT
        in a single BEGIN IMMEDIATE / COMMIT so a mid-run crash can't leave
        audit rows claiming applied=1 against memory_entries rows that
        never actually got superseded.

        audit_rows must carry applied=1; dry-run callers should use
        record_tombstone_audit directly (no UPDATE needed).

        Returns (rows_updated, audit_rows_inserted).
        """
        if not apply_map and not audit_rows:
            return (0, 0)
        with self._lock:
            # IMMEDIATE acquires a reserved lock up-front so other writers
            # can't race between the UPDATE and the INSERTs.
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                updated = 0
                for entry_id, superseder_id in apply_map.items():
                    cursor = self._conn.execute(
                        "UPDATE memory_entries "
                        "SET superseded_by = ?, updated_at = datetime('now') "
                        "WHERE id = ? AND superseded_by IS NULL",
                        (superseder_id, entry_id),
                    )
                    updated += cursor.rowcount
                inserted = 0
                if audit_rows:
                    cursor = self._conn.executemany(
                        "INSERT INTO tombstone_audit "
                        "(entry_id, superseded_by_id, mol_token, reason, applied) "
                        "VALUES (:entry_id, :superseded_by_id, :mol_token, :reason, :applied)",
                        audit_rows,
                    )
                    inserted = cursor.rowcount
                self._conn.commit()
                return (updated, inserted)
            except Exception:
                self._conn.rollback()
                raise

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._conn.close()


def _escape_fts_query(query: str) -> str:
    """Escape an FTS5 query: wrap each token in double quotes for safety."""
    tokens = query.strip().split()
    if not tokens:
        return ""
    escaped = " ".join(f'"{t.replace(chr(34), chr(34)+chr(34))}"' for t in tokens)
    return escaped
