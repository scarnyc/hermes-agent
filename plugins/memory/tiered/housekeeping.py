"""Memory housekeeping — noise deletion, stale archival, stats recomputation.

Runs after nightly consolidation (chained, not a separate cron).
"""

import logging
from dataclasses import dataclass, field

from .store import TieredMemoryDB

logger = logging.getLogger(__name__)


@dataclass
class HousekeepingResult:
    deleted: int = 0
    archived: int = 0
    deduped: int = 0
    errors: list[str] = field(default_factory=list)


def run_housekeeping(db: TieredMemoryDB) -> HousekeepingResult:
    """Noise deletion -> stale archival -> stats recomputation -> PRAGMA optimize.

    Each phase is independent — partial progress preserved on failure.
    """
    result = HousekeepingResult()

    # Phase 1: Noise deletion
    try:
        result.deleted = db.delete_noise()
        if result.deleted > 0:
            logger.info("Housekeeping: deleted %d noise entries", result.deleted)
    except Exception as e:
        logger.warning("Housekeeping noise deletion failed: %s", e)
        result.errors.append(f"noise_deletion: {e}")

    # Phase 2: Stale archival
    try:
        result.archived = db.archive_stale()
        if result.archived > 0:
            logger.info("Housekeeping: archived %d stale entries", result.archived)
    except Exception as e:
        logger.warning("Housekeeping stale archival failed: %s", e)
        result.errors.append(f"stale_archival: {e}")

    # Phase 2.5: Semantic deduplication
    try:
        result.deduped = db.deduplicate_similar()
        if result.deduped > 0:
            logger.info("Housekeeping: deduped %d similar entries", result.deduped)
    except Exception as e:
        logger.warning("Housekeeping semantic dedup failed: %s", e)
        result.errors.append(f"semantic_dedup: {e}")

    # Phase 3: Stats recomputation (already done by delete_noise/archive_stale, but ensure fresh)
    try:
        db.update_stats()
    except Exception as e:
        logger.warning("Housekeeping stats recomputation failed: %s", e)
        result.errors.append(f"stats_recomputation: {e}")

    # Phase 4: PRAGMA optimize
    try:
        db._conn.execute("PRAGMA optimize")
    except Exception as e:
        logger.warning("Housekeeping PRAGMA optimize failed: %s", e)
        result.errors.append(f"pragma_optimize: {e}")

    return result
