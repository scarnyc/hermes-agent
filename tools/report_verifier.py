"""Post-run verifier for cron job reports (MOL-214 Phase 1).

Parses a WORK_MANIFEST block from the agent's final response and verifies
each declared claim against ground truth (filesystem, Jira API). Writes
per-claim rows to `cron_verifications` in `~/.hermes/memory/hermes.db`.

Entry point: verify_and_annotate(job, final_response, cron_session_id, job_start_ts_utc).
Returns the annotated report (header prepended). Never raises — all
exceptions are caught and surfaced inline so tick() stays alive.

Also runnable as CLI (audits a historical cron output file):
    ~/.hermes/hermes-agent/venv/bin/python3 -m tools.report_verifier \
        ~/.hermes/cron/output/<job_id>/<YYYY-MM-DD_HH-MM-SS>.md
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

HERMES_DB = Path.home() / ".hermes" / "memory" / "hermes.db"
JIRA_BASE = "https://deep-agent-one.atlassian.net"
SEPARATOR = "═" * 40

# MOL-227 v2 (P40): legacy job-id whitelist retained for emergency override;
# normal gate is per-job `claims_expected: true` in jobs.json. Set to None to
# trust the job.claims_expected flag alone (current default). Populate with
# job_ids to force-enable verification regardless of claims_expected.
VERIFIER_WHITELIST: Optional[frozenset] = None

_MANIFEST_RE = re.compile(
    r"<!--\s*WORK_MANIFEST[^\n]*\n(.*?)WORK_MANIFEST\s*-->",
    re.DOTALL,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cron_verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    run_timestamp TEXT NOT NULL,
    session_id TEXT,
    claim_type TEXT NOT NULL,
    claim_payload_json TEXT NOT NULL,
    verified INTEGER NOT NULL,
    verification_output TEXT,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_cron_verif_job
    ON cron_verifications(job_id, run_timestamp);
CREATE INDEX IF NOT EXISTS idx_cron_verif_failed
    ON cron_verifications(verified) WHERE verified = 0;
"""


@dataclass
class ClaimResult:
    claim_type: str
    payload: dict
    verified: bool
    message: str

    def format_line(self) -> str:
        icon = "✅" if self.verified else "❌"
        return f"{icon} {self.claim_type} — {self.message}"


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def _extract_manifest(text: str) -> str | None:
    m = _MANIFEST_RE.search(text)
    return m.group(1).strip() if m else None


def _parse_manifest(yaml_body: str) -> dict:
    import yaml

    parsed = yaml.safe_load(yaml_body) or {}
    if not isinstance(parsed, dict):
        return {}
    return {
        "file_ops": parsed.get("file_ops") or [],
        "jira_transitions": parsed.get("jira_transitions") or [],
        "jira_comments": parsed.get("jira_comments") or [],
    }


def _verify_file_op(entry: dict, job_start_ts_utc: float) -> ClaimResult:
    path_raw = str(entry.get("path", ""))
    action = str(entry.get("action", ""))
    if not path_raw:
        return ClaimResult("file_op", entry, False, "missing path field")

    path = os.path.expanduser(path_raw)

    if not os.path.exists(path):
        return ClaimResult(
            "file_op", entry, False,
            f"{action} {path_raw} — file does not exist",
        )

    if action in ("created", "modified"):
        mtime = os.path.getmtime(path)
        if mtime < job_start_ts_utc:
            mtime_iso = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(timespec="seconds")
            start_iso = datetime.fromtimestamp(job_start_ts_utc, tz=timezone.utc).isoformat(timespec="seconds")
            return ClaimResult(
                "file_op", entry, False,
                f"{action} {path_raw} — mtime {mtime_iso} predates job start {start_iso}",
            )

    return ClaimResult(
        "file_op", entry, True,
        f"{action} {path_raw} — verified",
    )


def _jira_auth() -> tuple[str, str] | None:
    email = os.environ.get("ATLASSIAN_EMAIL")
    token = os.environ.get("ATLASSIAN_API_TOKEN")
    if not email or not token:
        return None
    return email, token


def _jira_get(path: str, auth: tuple[str, str], timeout: float = 10.0) -> dict:
    url = f"{JIRA_BASE}{path}"
    creds = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Basic {creds}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 — fixed JIRA_BASE URL
        return json.loads(resp.read().decode())


def _parse_jira_ts(ts_str: str) -> float:
    if not ts_str:
        return 0.0
    try:
        ts = ts_str.replace("Z", "+00:00")
        if re.search(r"[+-]\d{4}$", ts):
            ts = ts[:-2] + ":" + ts[-2:]
        return datetime.fromisoformat(ts).timestamp()
    except (ValueError, AttributeError):
        logger.debug("Failed to parse Jira timestamp %r", ts_str)
        return 0.0


def _verify_jira_transition(entry: dict, job_start_ts_utc: float, now_utc: float) -> ClaimResult:
    issue = str(entry.get("issue", ""))
    expected_to = str(entry.get("to", ""))
    if not issue or not expected_to:
        return ClaimResult("jira_transition", entry, False, "missing issue or 'to' field")

    auth = _jira_auth()
    if not auth:
        return ClaimResult(
            "jira_transition", entry, False,
            f"{issue} → {expected_to} — no Jira credentials in env",
        )

    try:
        data = _jira_get(f"/rest/api/3/issue/{issue}/changelog", auth)
    except Exception as e:
        return ClaimResult(
            "jira_transition", entry, False,
            f"{issue} → {expected_to} — changelog API error: {type(e).__name__}: {e}",
        )

    matches_in_window: list[str] = []
    matches_outside: list[tuple[str, float]] = []
    for history in data.get("values", []):
        created_str = history.get("created", "")
        created_ts = _parse_jira_ts(created_str)
        for item in history.get("items", []):
            if item.get("field") != "status":
                continue
            to_str = str(item.get("toString", ""))
            if to_str.lower() != expected_to.lower():
                continue
            if job_start_ts_utc <= created_ts <= now_utc:
                matches_in_window.append(created_str)
            else:
                matches_outside.append((created_str, created_ts))

    if matches_in_window:
        return ClaimResult(
            "jira_transition", entry, True,
            f"{issue} → {expected_to} — changelog entry at "
            f"{matches_in_window[0][:19]} inside job window",
        )

    if matches_outside:
        most_recent = max(matches_outside, key=lambda x: x[1])[0][:19]
        return ClaimResult(
            "jira_transition", entry, False,
            f"{issue} → {expected_to} — transition at {most_recent} is OUTSIDE "
            f"job window (cron did not cause it)",
        )

    return ClaimResult(
        "jira_transition", entry, False,
        f"{issue} → {expected_to} — no matching transition in changelog",
    )


_JIRA_ESCAPE_RE = re.compile(r"\\([^\w\s])")


def _unescape_jira_markdown(text: str) -> str:
    """Strip jira-cli's backslash escaping of markdown chars so substring
    matching survives the escape pass on comment submit. Covers jira-cli's
    full escape set: brackets, parens, underscores, asterisks, hyphens,
    dots, plus, bang, hash, angle brackets, braces, pipe, and any other
    non-word non-space char jira-cli chooses to escape."""
    return _JIRA_ESCAPE_RE.sub(r"\1", text)


def _comment_body_to_text(body) -> str:
    if isinstance(body, str):
        return body
    if isinstance(body, dict):
        parts: list[str] = []
        for content in body.get("content", []) or []:
            for item in content.get("content", []) or []:
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts) or json.dumps(body)[:200]
    return str(body)


def _verify_jira_comment(entry: dict, job_start_ts_utc: float, now_utc: float) -> ClaimResult:
    issue = str(entry.get("issue", ""))
    snippet = str(entry.get("snippet", ""))
    if not issue or not snippet:
        return ClaimResult("jira_comment", entry, False, "missing issue or snippet field")

    auth = _jira_auth()
    if not auth:
        return ClaimResult(
            "jira_comment", entry, False,
            f"{issue} — no Jira credentials in env",
        )

    try:
        data = _jira_get(f"/rest/api/2/issue/{issue}?fields=comment", auth)
    except Exception as e:
        return ClaimResult(
            "jira_comment", entry, False,
            f"{issue} — comment API error: {type(e).__name__}: {e}",
        )

    comments = (data.get("fields", {}) or {}).get("comment", {}) or {}
    comments_list = comments.get("comments", []) or []
    in_window = [
        c for c in comments_list
        if job_start_ts_utc <= _parse_jira_ts(c.get("created", "")) <= now_utc
    ]

    if not in_window:
        return ClaimResult(
            "jira_comment", entry, False,
            f"{issue} — no comments created in job window "
            f"(checked {len(comments_list)} total)",
        )

    snippet_norm = _unescape_jira_markdown(snippet)
    for c in in_window:
        body = _unescape_jira_markdown(_comment_body_to_text(c.get("body", "")))
        if snippet_norm in body:
            when = c.get("created", "")[:19]
            return ClaimResult(
                "jira_comment", entry, True,
                f"{issue} — snippet matched (comment at {when})",
            )

    # MOL-214 P36: token-set fallback for paraphrase-drop where the agent
    # wrote simplified words ("diff and dry run") that don't substring-match
    # the literal body ("--diff and --dry-run"). Jaccard |A∩B|/|A| ≥ 0.80
    # with min 5 tokens. Length guard runs before division.
    snippet_tokens = set(re.findall(r"[a-z0-9]+", snippet_norm.lower()))
    if len(snippet_tokens) >= 5:
        for c in in_window:
            body = _unescape_jira_markdown(_comment_body_to_text(c.get("body", "")))
            body_tokens = set(re.findall(r"[a-z0-9]+", body.lower()))
            overlap = len(snippet_tokens & body_tokens) / len(snippet_tokens)
            if overlap >= 0.80:
                when = c.get("created", "")[:19]
                return ClaimResult(
                    "jira_comment", entry, True,
                    f"{issue} — matched via token-set ({overlap:.0%} overlap, comment at {when})",
                )

    first_body = _unescape_jira_markdown(_comment_body_to_text(in_window[0].get("body", "")))
    preview = first_body[:80].replace("\n", " ") + ("…" if len(first_body) > 80 else "")
    return ClaimResult(
        "jira_comment", entry, False,
        f"{issue} — snippet {snippet!r} not in {len(in_window)} window-comment(s); "
        f"actual: {preview!r}",
    )


def _persist_results(job: dict, cron_session_id: str, results: list[ClaimResult]) -> None:
    if not HERMES_DB.parent.exists():
        return
    run_ts = datetime.now(timezone.utc).isoformat()
    try:
        with sqlite3.connect(str(HERMES_DB)) as conn:
            _ensure_schema(conn)
            conn.executemany(
                "INSERT INTO cron_verifications "
                "(job_id, run_timestamp, session_id, claim_type, "
                "claim_payload_json, verified, verification_output) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        str(job.get("id", "")),
                        run_ts,
                        cron_session_id,
                        r.claim_type,
                        json.dumps(r.payload, default=str),
                        1 if r.verified else 0,
                        r.message,
                    )
                    for r in results
                ],
            )
            conn.commit()
    except Exception as e:
        logger.warning("Failed to persist verification results: %s: %s", type(e).__name__, e)


def _format_header(results: list[ClaimResult]) -> str:
    total = len(results)
    failed = sum(1 for r in results if not r.verified)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    status = (
        f"✅ VERIFICATION: {total}/{total} claims passed ({stamp})"
        if failed == 0
        else f"⚠️ VERIFICATION: {failed}/{total} claims failed ({stamp})"
    )
    body = [status, *[r.format_line() for r in results], "", SEPARATOR,
            "(original report from Hermes follows)", SEPARATOR]
    return "\n".join(body)


def _no_manifest_header() -> str:
    return "\n".join([
        "⚠️ NO WORK_MANIFEST EMITTED — verification skipped",
        "   (agent may have forgotten the manifest or is hiding work)",
        "",
        SEPARATOR,
        "(original report from Hermes follows)",
        SEPARATOR,
    ])


def verify_and_annotate(
    job: dict,
    final_response: str,
    cron_session_id: str,
    job_start_ts_utc: float,
) -> str:
    """Verify claims in final_response; return annotated report. Never raises."""
    try:
        if not final_response:
            return final_response

        # MOL-227 v2 (P40) gate logic: either legacy whitelist OR per-job
        # claims_expected flag. Default (whitelist=None + claims_expected absent)
        # means no verification — jobs must opt in explicitly via
        # claims_expected: true in jobs.json. Prevents "NO WORK_MANIFEST"
        # warnings from spamming review-only cron deliveries.
        claims_expected = bool(job.get("claims_expected", False))
        whitelisted = (
            VERIFIER_WHITELIST is not None
            and str(job.get("id", "")) in VERIFIER_WHITELIST
        )
        if not (claims_expected or whitelisted):
            return final_response

        yaml_body = _extract_manifest(final_response)
        if yaml_body is None:
            return _no_manifest_header() + "\n\n" + final_response

        try:
            manifest = _parse_manifest(yaml_body)
        except Exception as e:
            logger.warning("Malformed manifest YAML: %s", e)
            return _no_manifest_header() + "\n\n" + final_response

        now_utc = datetime.now(timezone.utc).timestamp()
        results: list[ClaimResult] = []

        for entry in manifest.get("file_ops", []):
            if isinstance(entry, dict):
                results.append(_verify_file_op(entry, job_start_ts_utc))

        for entry in manifest.get("jira_transitions", []):
            if isinstance(entry, dict):
                results.append(_verify_jira_transition(entry, job_start_ts_utc, now_utc))

        for entry in manifest.get("jira_comments", []):
            if isinstance(entry, dict):
                results.append(_verify_jira_comment(entry, job_start_ts_utc, now_utc))

        if not results:
            return ("ℹ️ Empty WORK_MANIFEST (no claims to verify)\n\n"
                    f"{SEPARATOR}\n\n{final_response}")

        _persist_results(job, cron_session_id, results)

        return _format_header(results) + "\n\n" + final_response

    except Exception as e:
        logger.exception("verify_and_annotate crashed")
        return f"⚠️ VERIFIER ERROR: {type(e).__name__}: {e}\n\n{final_response}"


def _cli(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: python3 -m tools.report_verifier <cron_output_file.md>", file=sys.stderr)
        return 1
    path = argv[1]
    if not os.path.exists(path):
        print(f"File not found: {path}", file=sys.stderr)
        return 1

    with open(path, "r") as f:
        text = f.read()

    m = re.search(r"##\s*Response\s*\n\n(.*)", text, re.DOTALL)
    response = m.group(1) if m else text

    basename = os.path.basename(path)
    m_ts = re.match(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})", basename)
    if m_ts:
        date_part, hh, mm, ss = m_ts.groups()
        local_dt = datetime.strptime(f"{date_part} {hh}:{mm}:{ss}", "%Y-%m-%d %H:%M:%S")
        # Filename ts is COMPLETION time (scheduler writes file at end of run).
        # Back off 10 min so we capture the actual start + all claims made during run.
        job_start_ts_utc = local_dt.timestamp() - 600
    else:
        job_start_ts_utc = os.path.getmtime(path) - 600

    parent = os.path.basename(os.path.dirname(path))
    job = {"id": parent or "cli", "name": f"CLI run on {basename}"}
    session_id = f"cli_{parent}_{basename}"

    annotated = verify_and_annotate(job, response, session_id, job_start_ts_utc)
    print(annotated)
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
