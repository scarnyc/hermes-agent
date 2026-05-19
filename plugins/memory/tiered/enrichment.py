"""Context enrichment for prefetch — injects situational awareness.

Queries Jira for critical/high tasks and memory DB for active principles,
caches results, and composes a "Situational context" block for the system prompt.
"""

import json
import logging
import os
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .store import TieredMemoryDB

logger = logging.getLogger(__name__)

ENRICHMENT_STALENESS_MINUTES = 30
MAX_ENRICHMENT_CHARS = 1500
SUBPROCESS_TIMEOUT = 8.0

# JQL for context enrichment — critical/high open tasks
_JIRA_JQL = "project = MOL AND priority in (Critical, High) AND status != Done"


def _get_jira_token() -> str | None:
    """Retrieve JIRA_API_TOKEN, bridging from envchain if needed."""
    token = os.environ.get("JIRA_API_TOKEN")
    if token:
        return token

    try:
        result = subprocess.run(
            ["envchain", "hermes-jira", "printenv", "ATLASSIAN_API_TOKEN"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        if result.returncode != 0:
            logger.info(
                "envchain hermes-jira returned exit %d: %s",
                result.returncode, result.stderr.strip()[:200],
            )
    except FileNotFoundError:
        logger.info("envchain not installed — Jira token bridge unavailable")
    except subprocess.TimeoutExpired:
        logger.warning("envchain timed out — Keychain may be locked")

    return None


def _query_jira(timeout: float = SUBPROCESS_TIMEOUT) -> list[dict]:
    """Query Jira for critical/high open tasks via CLI subprocess on host."""
    token = _get_jira_token()
    if not token:
        logger.warning("No Jira token available — skipping Jira enrichment")
        return []

    env = os.environ.copy()
    env["JIRA_API_TOKEN"] = token

    try:
        result = subprocess.run(
            [
                "jira", "issue", "list",
                "-q", _JIRA_JQL,
                "--plain", "--no-headers",
                "--columns", "KEY,SUMMARY,STATUS",
            ],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        logger.warning("Jira query timed out after %.1fs", timeout)
        return []
    except FileNotFoundError:
        logger.warning("jira CLI not found in PATH")
        return []

    # Exit code 1 with "No result found" = empty results, not an error
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "No result found" in stderr:
            return []
        logger.warning("Jira query failed (exit %d): %s", result.returncode, stderr[:200])
        return []

    tasks = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            logger.debug("Skipping malformed Jira output line: %s", line[:100])
            continue
        key = parts[0].strip()
        summary = parts[1].strip()
        # Use last non-empty field as status (handles variable column counts)
        status = parts[-1].strip() if len(parts) >= 3 else ""
        tasks.append({"key": key, "summary": summary, "status": status})

    return tasks


def _query_calendar(timeout: float = SUBPROCESS_TIMEOUT) -> list[dict]:
    """Query calendar for today's agenda via gws CLI on host (JSON output)."""
    env = os.environ.copy()
    env["GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"] = "file"

    try:
        result = subprocess.run(
            ["gws", "calendar", "+agenda", "--today", "--format", "json"],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        logger.warning("Calendar query timed out after %.1fs", timeout)
        return []
    except FileNotFoundError:
        logger.warning("gws CLI not found in PATH")
        return []

    if result.returncode != 0:
        logger.warning("Calendar query failed (exit %d): %s", result.returncode, result.stderr[:200])
        return []

    # gws JSON output may have a "Using keyring backend:" preamble on stderr;
    # stdout contains the JSON object with {"count": N, "events": [...]}
    try:
        data = json.loads(result.stdout)
        raw_events = data.get("events", [])
    except json.JSONDecodeError as e:
        logger.warning(
            "Failed to parse calendar JSON (char %d): %.200s",
            e.pos, result.stdout[:200],
        )
        return []
    except TypeError:
        logger.warning("Unexpected calendar output type: %.200s", str(result.stdout)[:200])
        return []

    events = []
    for e in raw_events:
        title = e.get("summary", "(No title)") or "(No title)"
        start = e.get("start", "")
        # Extract just the time portion for display (HH:MM)
        # All-day events have date-only format (no "T")
        if "T" in start:
            start = start.split("T")[1][:5]
        events.append({"title": title, "start": start, "end": e.get("end", "")})

    return events


def refresh_enrichment_cache(cache_path: Path, timeout: float = SUBPROCESS_TIMEOUT) -> None:
    """Query Jira + Calendar in parallel, write JSON cache. Best-effort."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    jira_tasks: list[dict] = []
    calendar_events: list[dict] = []

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            pool.submit(_query_jira, timeout): "jira",
            pool.submit(_query_calendar, timeout): "calendar",
        }
        try:
            for future in as_completed(futures, timeout=timeout + 2):
                source = futures[future]
                try:
                    result = future.result()
                    if source == "jira":
                        jira_tasks = result
                    else:
                        calendar_events = result
                except Exception:
                    logger.warning("Enrichment query '%s' failed", source, exc_info=True)
        except TimeoutError:
            logger.warning(
                "Enrichment pool timed out after %.1fs — writing partial results",
                timeout + 2,
            )

    # Don't cache if both sources returned empty — allows retry on next prefetch
    if not jira_tasks and not calendar_events:
        logger.info("Both enrichment sources returned empty — not caching")
        return

    cache_data = {
        "refreshed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "jira": jira_tasks,
        "calendar": calendar_events,
    }

    tmp_path = cache_path.with_suffix(".tmp")
    try:
        tmp_path.write_text(json.dumps(cache_data, indent=2), encoding="utf-8")
        tmp_path.rename(cache_path)
    except OSError:
        logger.warning("Failed to write enrichment cache to %s", cache_path, exc_info=True)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def read_enrichment_cache(cache_path: Path, max_age_minutes: int = ENRICHMENT_STALENESS_MINUTES) -> dict | None:
    """Read cached enrichment data. Returns None if stale, missing, or corrupt."""
    if not cache_path.exists():
        return None

    try:
        age_seconds = time.time() - os.path.getmtime(str(cache_path))
        if age_seconds > max_age_minutes * 60:
            logger.debug("Enrichment cache is stale (%.0fs old)", age_seconds)
            return None
    except OSError:
        logger.warning("Cannot stat enrichment cache %s", cache_path, exc_info=True)
        return None

    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "refreshed_at" not in data:
            logger.warning("Enrichment cache has invalid structure — will refresh")
            return None
        return data
    except json.JSONDecodeError:
        logger.warning("Enrichment cache is corrupt JSON — will refresh")
        return None
    except OSError:
        logger.warning("Cannot read enrichment cache %s", cache_path, exc_info=True)
        return None


def get_active_principles(db: TieredMemoryDB) -> list[str]:
    """Query active (non-archived) principles from the memory DB."""
    try:
        return db.query_active_principles()
    except sqlite3.OperationalError:
        logger.warning("Failed to query principles — database may be locked", exc_info=True)
        return []
    except Exception:
        logger.warning("Failed to query principles", exc_info=True)
        return []


def refresh_enrichment_async(cache_path: Path, timeout: float = SUBPROCESS_TIMEOUT) -> None:
    """Fire-and-forget enrichment refresh in a background thread."""
    thread = threading.Thread(
        target=refresh_enrichment_cache,
        args=(cache_path, timeout),
        daemon=True,
    )
    thread.start()


def compose_enrichment_block(cache: dict | None, principles: list[str]) -> str:
    """Compose a '## Situational context' markdown block. Capped at MAX_ENRICHMENT_CHARS."""
    sections: list[str] = []

    # Jira critical/high tasks
    jira_tasks = (cache or {}).get("jira", [])
    if jira_tasks:
        task_lines = []
        for t in jira_tasks[:10]:
            status_suffix = f" [{t['status']}]" if t.get("status") else ""
            task_lines.append(f"- {t['key']}: {t['summary']}{status_suffix}")
        sections.append("**Board (critical/high):**\n" + "\n".join(task_lines))

    # Calendar events
    calendar_events = (cache or {}).get("calendar", [])
    if calendar_events:
        event_lines = []
        for e in calendar_events[:5]:
            time_str = e.get("start", "")
            event_lines.append(f"- {e['title']} ({time_str})")
        sections.append("**Today's agenda:**\n" + "\n".join(event_lines))

    # Active principles
    if principles:
        principle_lines = [f"- {p}" for p in principles[:5]]
        sections.append("**Active principles:**\n" + "\n".join(principle_lines))

    if not sections:
        return ""

    block = "## Situational context\n" + "\n\n".join(sections)

    if len(block) > MAX_ENRICHMENT_CHARS:
        cutoff = block[:MAX_ENRICHMENT_CHARS].rfind("\n")
        if cutoff > 0:
            block = block[:cutoff] + "\n..."
        else:
            block = block[:MAX_ENRICHMENT_CHARS - 3] + "..."

    return block
