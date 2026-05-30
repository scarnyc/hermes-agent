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
import subprocess  # P272/MOL-2219: read-only git for git_commits claim
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

# P252/MOL-2040: seam-anchored variant — the manifest plus any newlines
# immediately around it. Used to excise the comment without disturbing
# blank-line runs elsewhere in the report body.
_MANIFEST_SEAM_RE = re.compile(r"\n*" + _MANIFEST_RE.pattern + r"\n*", re.DOTALL)

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


def _strip_manifest(text: str) -> str:
    # P252/MOL-2040: remove the raw WORK_MANIFEST HTML comment from the
    # DELIVERED copy after it has been extracted. The saved cron output file
    # (scheduler.py:2346, written pre-verify) keeps the manifest intact so CLI
    # replay still parses it; only the Telegram/return text is cleaned. In
    # text-mode Telegram the comment renders verbatim, which is the ugliest
    # noise source for every claims_expected cron. Anchored to the manifest
    # seam (the comment plus its surrounding newlines collapse to one blank
    # line) so blank-line runs elsewhere in the report body survive intact.
    return _MANIFEST_SEAM_RE.sub("\n\n", text).strip()


def _parse_manifest(yaml_body: str) -> dict:
    import yaml

    parsed = yaml.safe_load(yaml_body) or {}
    if not isinstance(parsed, dict):
        return {}
    return {
        "file_ops": parsed.get("file_ops") or [],
        "jira_transitions": parsed.get("jira_transitions") or [],
        "jira_comments": parsed.get("jira_comments") or [],
        # P272/MOL-2219: results-reporting claim types. git_commits proves a
        # cherry-pick actually landed THIS run (not merely present); jira_status
        # asserts a ticket's current state (pair with jira_transitions for the
        # who-did-it changelog-window check).
        "git_commits": parsed.get("git_commits") or [],
        "jira_status": parsed.get("jira_status") or [],
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


def _run_git(repo: str, args: list[str], timeout: float = 10.0) -> tuple[int, str]:
    # P272/MOL-2219: read-only git on the patch-preserved runtime tree. ONLY
    # query verbs (rev-parse / merge-base --is-ancestor / log) are ever passed —
    # never fetch/rebase/reset/pull. Returns (returncode, combined_output).
    try:
        proc = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except Exception as e:
        return 127, f"{type(e).__name__}: {e}"


def _verify_git_commit(entry: dict) -> ClaimResult:
    # P272/MOL-2219: prove a cherry-pick actually LANDED THIS RUN, not merely that
    # it is present in history (skeptic #1). With a pre-swarm `head_before`
    # baseline we assert NOT-ancestor-before AND ancestor-now; without it we
    # degrade to a present-only pass and say so honestly.
    repo_raw = str(entry.get("repo", ""))
    sha = str(entry.get("sha", ""))
    if not repo_raw or not sha:
        return ClaimResult("git_commit", entry, False, "missing repo or sha field")

    repo = os.path.expanduser(repo_raw)

    rc, _ = _run_git(repo, ["rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"])
    if rc != 0:
        return ClaimResult("git_commit", entry, False, f"{sha} — unknown commit in {repo_raw}")

    rc, _ = _run_git(repo, ["merge-base", "--is-ancestor", sha, "HEAD"])
    if rc != 0:
        return ClaimResult("git_commit", entry, False, f"{sha} — NOT reachable from HEAD")

    subject = str(entry.get("subject", "")).strip()
    if subject:
        src, actual_subject = _run_git(repo, ["log", "-1", "--format=%s", sha])
        if src != 0 or subject.lower() not in actual_subject.lower():
            return ClaimResult(
                "git_commit", entry, False,
                f"{sha} — subject mismatch: expected substring {subject!r}, "
                f"actual {actual_subject!r}",
            )

    head_before = str(entry.get("head_before", "")).strip()
    if head_before:
        brc, _ = _run_git(repo, ["rev-parse", "--verify", "--quiet", f"{head_before}^{{commit}}"])
        if brc != 0:
            return ClaimResult(
                "git_commit", entry, True,
                f"{sha} — present in HEAD; baseline {head_before} unresolvable "
                f"(not proven to have landed during this run)",
            )
        arc, _ = _run_git(repo, ["merge-base", "--is-ancestor", sha, head_before])
        if arc == 0:
            return ClaimResult(
                "git_commit", entry, False,
                f"{sha} — already present before run (ancestor of baseline "
                f"{head_before}) — no-op",
            )
        return ClaimResult(
            "git_commit", entry, True,
            f"{sha} — landed this run (absent from baseline {head_before}, "
            f"reachable from HEAD)",
        )

    return ClaimResult(
        "git_commit", entry, True,
        f"{sha} — present in HEAD (no baseline; not proven to have landed during this run)",
    )


def _verify_jira_status(entry: dict) -> ClaimResult:
    # P272/MOL-2219: assert a ticket's CURRENT live status. Complements
    # jira_transitions (changelog-within-window) — pair them so the report
    # proves both who-flipped-it and where-it-rests-now (skeptic #5).
    issue = str(entry.get("issue", ""))
    expected = str(entry.get("status", ""))
    if not issue or not expected:
        return ClaimResult("jira_status", entry, False, "missing issue or status field")

    auth = _jira_auth()
    if not auth:
        return ClaimResult("jira_status", entry, False, f"{issue} — no Jira credentials in env")

    try:
        data = _jira_get(f"/rest/api/2/issue/{issue}?fields=status", auth)
    except Exception as e:
        return ClaimResult(
            "jira_status", entry, False,
            f"{issue} — status API error: {type(e).__name__}: {e}",
        )

    actual = ((data.get("fields", {}) or {}).get("status", {}) or {}).get("name", "")
    if actual.strip().lower() == expected.strip().lower():
        return ClaimResult("jira_status", entry, True, f"{issue} — status is {actual!r} (matches)")
    return ClaimResult(
        "jira_status", entry, False,
        f"{issue} — status is {actual!r}, expected {expected!r}",
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


# P276/MOL-2219: prose↔manifest divergence guard. The collector is a transcriber
# (omit-don't-fabricate); this is the SOLE checker, so the affirmative-claim scan
# lives here. Negation-aware + sentence-scoped so an honest no-op ("no commit
# needed", "nothing to cherry-pick") never trips a false ❌ (skeptic Trickster
# HIGH: never a false ❌). It fires only when prose NET-asserts a commit/push/
# transition while the manifest carried no corresponding verifiable claim — the
# exact MOL-631 fabrication shape (synth prose said "committed 6ef1ad7 + pushed +
# → Done" with an empty git_commits manifest).
_AFFIRM_COMMIT_RE = re.compile(
    r"\b(?:committed|cherry-?picked|ported|commit\s+[0-9a-f]{7,40})\b", re.I)
_AFFIRM_PUSH_RE = re.compile(r"\bpushed\b", re.I)
_AFFIRM_DONE_RE = re.compile(
    r"(?:→\s*done\b|\b(?:transitioned|moved|marked|set)\b[^.]*\bdone\b)", re.I)
_NEGATION_RE = re.compile(
    r"\b(?:no|not|never|nothing|none|without|skip|skipped|unable|"
    r"didn't|doesn't|wasn't|weren't|don't|couldn't|cannot|can't)\b|n't|no-op",
    re.I)
_SENTENCE_SPLIT_RE = re.compile(r"[.!?\n]+")


def _prose_asserts_unverified_work(prose: str) -> "str | None":
    """Return a short reason if PROSE net-affirms a commit/push/transition (so an
    empty manifest is suspect), else None. Sentence-scoped + negation-aware so an
    honest no-op never trips it."""
    if not prose:
        return None
    asserted: list[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(prose):
        if _NEGATION_RE.search(sentence):
            continue
        if _AFFIRM_COMMIT_RE.search(sentence) and "commit" not in asserted:
            asserted.append("commit")
        if _AFFIRM_PUSH_RE.search(sentence) and "push" not in asserted:
            asserted.append("push")
        if _AFFIRM_DONE_RE.search(sentence) and "transition to Done" not in asserted:
            asserted.append("transition to Done")
    return ", ".join(asserted) if asserted else None


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

        # P252/MOL-2040: manifest extracted — strip the raw comment from the
        # delivered text so all downstream returns (malformed/empty/passed)
        # ship clean copy. Saved output file retains the manifest for replay.
        final_response = _strip_manifest(final_response)

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

        # P272/MOL-2219: results-reporting claims (no job_start/now args — the
        # git_commit baseline lives in the manifest, jira_status is point-in-time).
        for entry in manifest.get("git_commits", []):
            if isinstance(entry, dict):
                results.append(_verify_git_commit(entry))

        for entry in manifest.get("jira_status", []):
            if isinstance(entry, dict):
                results.append(_verify_jira_status(entry))

        if not results:
            # P276/MOL-2219: an empty manifest is benign ONLY if the prose makes no
            # affirmative work claim. If prose net-asserts a commit/push/transition
            # with no corresponding verifiable claim, treat it as SUSPECT (the
            # MOL-631 fabrication shape) and fail loudly — never disguise it as
            # "none claimed". This is a meta-observation, not a fabricated verdict.
            suspect = _prose_asserts_unverified_work(final_response)
            if suspect:
                return (
                    f"❌ UNVERIFIABLE CLAIM — synthesizer prose asserts {suspect} "
                    "but emitted no verifiable WORK_MANIFEST claim. Treated as "
                    "SUSPECT (possible fabrication); NOT confirmed.\n\n"
                    f"{SEPARATOR}\n\n{final_response}")
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
