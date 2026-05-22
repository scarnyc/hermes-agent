"""Signal extractors for MOL-246 evening enrichment.

Three extractors pull action-items from live data sources and emit
``SignalItem`` candidates. Each extractor is fail-soft: any degraded path
emits a ``DEGRADED_<SOURCE>: <reason>`` breadcrumb to stderr and returns
``[]`` so the orchestrator can still append the remaining signals.

The subprocess wrapper (``_run_signal``) centralises timeout + non-zero
handling so every extractor uses the same 30 s budget and the same
stderr format.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import date, datetime, time
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from .types import SignalItem

# ---------------------------------------------------------------------------
# Shared subprocess wrapper
# ---------------------------------------------------------------------------

_TIMEOUT_SEC = 30
_ET = ZoneInfo("America/New_York")

# Hermes self-forward sender — configurable via env var, defaults to the
# user's known address. Used by the Gmail noise filter.
_SELF_FORWARD_ADDR = os.environ.get(
    "ATLASSIAN_EMAIL", "billyscardino@gmail.com"
).lower()


def _run_signal(
    cmd: list[str], src: str
) -> Optional[subprocess.CompletedProcess]:
    """Run ``cmd`` with a 30 s timeout, logging degraded paths to stderr.

    Returns ``None`` on timeout or non-zero exit; callers should parse
    ``r.stdout`` when a ``CompletedProcess`` comes back.
    """
    try:
        r = subprocess.run(
            cmd,
            timeout=_TIMEOUT_SEC,
            check=False,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"DEGRADED_{src}: timeout\n")
        return None
    if r.returncode != 0:
        sys.stderr.write(f"DEGRADED_{src}: rc={r.returncode}\n")
        return None
    return r


# ---------------------------------------------------------------------------
# Extractor 1 — Calendar → "# NOW"
# ---------------------------------------------------------------------------


def extract_calendar_items(for_date: date) -> list[SignalItem]:
    """Fetch ``for_date``'s primary-calendar events and emit prep bullets.

    Each event becomes one ``SignalItem`` whose body is
    ``"Prep for HH:MM <summary>"`` for timed events or
    ``"Prep for <summary>"`` for all-day events. Section is always
    ``"# NOW"``; source is ``"calendar"``; source_id is the event id.
    """
    start_dt = datetime.combine(for_date, time(0, 0, 0), tzinfo=_ET)
    end_dt = datetime.combine(for_date, time(23, 59, 59), tzinfo=_ET)
    params = {
        "calendarId": "primary",
        "timeMin": start_dt.isoformat(),
        "timeMax": end_dt.isoformat(),
        "singleEvents": True,
        "orderBy": "startTime",
    }
    cmd = ["gws", "calendar", "events", "list", "--params", json.dumps(params)]
    r = _run_signal(cmd, "CALENDAR")
    if r is None:
        return []

    try:
        payload = json.loads(r.stdout)
    except json.JSONDecodeError:
        sys.stderr.write("DEGRADED_CALENDAR: json-decode\n")
        return []

    items_raw = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items_raw, list):
        return []

    out: list[SignalItem] = []
    for ev in items_raw:
        if not isinstance(ev, dict):
            continue
        ev_id = ev.get("id")
        summary = ev.get("summary") or "(no title)"
        if not ev_id:
            continue
        start = ev.get("start") or {}
        dt_str = start.get("dateTime")
        if dt_str:
            try:
                # gws returns RFC3339 / ISO-8601 with tz offset.
                dt = datetime.fromisoformat(dt_str)
                hhmm = dt.astimezone(_ET).strftime("%H:%M")
                body = f"Prep for {hhmm} {summary}"
            except (TypeError, ValueError):
                body = f"Prep for {summary}"
        else:
            # All-day event — start.date is present instead of dateTime.
            body = f"Prep for {summary}"
        out.append(
            SignalItem(
                source="calendar",
                section="# NOW",
                body=body,
                source_id=str(ev_id),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Extractor 2 — Gmail → "# This Week"
# ---------------------------------------------------------------------------

# Noise filter: strip these senders entirely. Matched as substring against
# the lowercased ``from`` column so truncated display (``…``) still hits.
_GMAIL_NOISE_SUBSTRINGS = (
    "jobalerts-noreply@linkedin.com",
    "newsletter@email.particle.news",
    "@substack.com",
    "no-reply@ashbyhq.com",
    "messaging-digest-noreply@linke",  # matches the truncated form too
    _SELF_FORWARD_ADDR,
)

_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def _find_col_positions(header: str) -> Optional[dict[str, int]]:
    """Return ``{col_name: start_index}`` or ``None`` if any column missing."""
    positions = {}
    for col in ("date", "from", "id", "subject"):
        idx = header.find(col)
        if idx < 0:
            return None
        positions[col] = idx
    return positions


def _parse_gmail_triage(text: str) -> list[SignalItem]:
    """Parse tabular ``gws gmail +triage`` output into SignalItems.

    Uses header-derived column slices (date, from, id, subject) rather
    than whitespace splitting to tolerate embedded spaces in the
    ``from`` column (e.g. display names).

    Noise rows are dropped silently. Rows whose column slices don't
    yield a 16-hex id emit ``DEGRADED_GMAIL: unparseable-line`` and are
    skipped.
    """
    lines = text.splitlines()
    # Skip leading blank lines.
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return []

    header = lines[0]
    positions = _find_col_positions(header)
    if positions is None:
        sys.stderr.write("DEGRADED_GMAIL: unparseable-line\n")
        return []

    date_start = positions["date"]
    from_start = positions["from"]
    id_start = positions["id"]
    subject_start = positions["subject"]

    out: list[SignalItem] = []
    for raw in lines[1:]:
        if not raw.strip():
            continue
        # Skip the horizontal-rule row (solid ─ chars with spaces).
        stripped = raw.strip()
        non_space = stripped.replace(" ", "")
        if non_space and all(ch == "─" for ch in non_space):
            continue

        # Defensive: row shorter than header → unparseable.
        if len(raw) < subject_start:
            sys.stderr.write("DEGRADED_GMAIL: unparseable-line\n")
            continue

        from_field = raw[from_start:id_start].rstrip()
        id_field = raw[id_start:subject_start].strip()
        subject = raw[subject_start:].rstrip()

        if not _ID_RE.match(id_field):
            sys.stderr.write("DEGRADED_GMAIL: unparseable-line\n")
            continue

        from_lower = from_field.lower()
        if any(noise in from_lower for noise in _GMAIL_NOISE_SUBSTRINGS):
            continue

        # Extract display name — text before "<addr>", else the whole field.
        lt = from_field.find("<")
        if lt > 0:
            display = from_field[:lt].strip().strip('"')
        else:
            display = from_field.strip()

        # Strip trailing ellipsis if the subject was truncated.
        clean_subject = subject.rstrip().rstrip("…").rstrip()

        body = f"Reply: {clean_subject} ({display})"
        out.append(
            SignalItem(
                source="gmail",
                section="# This Week",
                body=body,
                source_id=id_field,
            )
        )
    return out


def extract_gmail_items() -> list[SignalItem]:
    """Run ``gws gmail +triage`` with a 1-day unread window and parse rows."""
    cmd = [
        "gws",
        "gmail",
        "+triage",
        "--query",
        "is:unread newer_than:1d",
    ]
    r = _run_signal(cmd, "GMAIL")
    if r is None:
        return []
    return _parse_gmail_triage(r.stdout)


# ---------------------------------------------------------------------------
# Extractor 3 — Granola → "# This Week"
# ---------------------------------------------------------------------------

_HERMES_AGENT_PATH = os.path.expanduser("~/.hermes/hermes-agent")

# Bullet regex — tolerant of leading spaces, bullets (•, -, *), optional
# trailing source tag. One group = the action-item body.
_GRANOLA_BULLET_RE = re.compile(
    r"^[\s•\-\*]+(.+?)(?:\s*\[source:.*?\])?\s*$",
    re.MULTILINE,
)


def _load_query_fn() -> Optional[Callable]:
    """Import ``query_granola_meetings`` from the Hermes plugin tree.

    Returns ``None`` after logging ``DEGRADED_GRANOLA: import-error`` if
    the module can't be found.
    """
    if _HERMES_AGENT_PATH not in sys.path:
        sys.path.insert(0, _HERMES_AGENT_PATH)
    try:
        from plugins.memory.tiered.granola_tools import (  # type: ignore
            query_granola_meetings,
        )
    except ImportError:
        sys.stderr.write("DEGRADED_GRANOLA: import-error\n")
        return None
    return query_granola_meetings


def _scan_for_bullets(payload: object) -> list[str]:
    """Recursively walk JSON-ish payload, regex-scanning any string leaves."""
    hits: list[str] = []
    if isinstance(payload, str):
        for m in _GRANOLA_BULLET_RE.finditer(payload):
            body = m.group(1).strip()
            if body:
                hits.append(body)
    elif isinstance(payload, dict):
        # Prefer known keys first, then fall back to all values.
        for key in ("content", "text"):
            if key in payload:
                hits.extend(_scan_for_bullets(payload[key]))
        for k, v in payload.items():
            if k in ("content", "text"):
                continue
            hits.extend(_scan_for_bullets(v))
    elif isinstance(payload, list):
        for item in payload:
            hits.extend(_scan_for_bullets(item))
    return hits


def extract_granola_items(
    for_date: date, *, _query_fn: Optional[Callable] = None
) -> list[SignalItem]:
    """Pull today's Granola action-items via the local MCP client.

    The ``_query_fn`` kwarg is test-injection only; production callers
    omit it and the real import is used.
    """
    query_fn = _query_fn if _query_fn is not None else _load_query_fn()
    if query_fn is None:
        return []

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                query_fn, query="action items from today"
            )
            try:
                result = future.result(timeout=_TIMEOUT_SEC)
            except FuturesTimeoutError:
                sys.stderr.write("DEGRADED_GRANOLA: timeout\n")
                return []
    except Exception:
        # Any crash before we get a result → treat as auth/OAuth failure
        # (most common cause in practice).
        sys.stderr.write("DEGRADED_GRANOLA: auth\n")
        return []

    # Extract the payload string — MCP responses use {"content": <json>}.
    content: object
    if isinstance(result, dict) and "content" in result:
        content = result["content"]
    else:
        content = result

    if isinstance(content, str):
        try:
            payload: object = json.loads(content)
        except json.JSONDecodeError:
            # Treat as raw text — scan directly.
            payload = content
    else:
        payload = content

    bullets = _scan_for_bullets(payload)
    if not bullets:
        sys.stderr.write("DEGRADED_GRANOLA: no-match\n")
        return []

    out: list[SignalItem] = []
    seen_ids: set[str] = set()
    for line in bullets:
        body = line.strip()
        if not body:
            continue
        digest = hashlib.sha1(
            f"{for_date.isoformat()}:{body}".encode("utf-8")
        ).hexdigest()[:12]
        if digest in seen_ids:
            continue
        seen_ids.add(digest)
        out.append(
            SignalItem(
                source="granola",
                section="# This Week",
                body=body,
                source_id=digest,
            )
        )
    return out
