"""Reflection agent for cron-run outputs (MOL-227 Phase 1 / v1).

Semantic companion to tools/report_verifier.py. The structural verifier
catches mechanical failures (missing files, wrong transition timings, snippet
mismatch). This reflection agent catches the failures that were out of scope
for the structural verifier:

  - Fabricated API fields (e.g. MOL-220's hallucinated `total_tool_errors`).
  - Claimed actions with no textual evidence in the run body (e.g. MOL-212
    claimed a comment it never posted).
  - Claims about disk state that contradict what the run body shows
    (MOL-219 disk-vs-memory).
  - Missing Jira transitions on completed work (`transition_missing`, P40).
  - Work-not-completed: ticket closed with only bookkeeping edits instead of
    the functionality the ticket requested (`work_not_completed`, P43 /
    MOL-TBD — catches the MOL-236 "created 4 sub-tickets and called it done"
    pattern).

Uses Kimi K2.5 via OpenRouter as the reviewer (single-turn, JSON output).
Writes concerns to stdout and appends one JSONL line per invocation to
~/.hermes/logs/reflection-agent.log.

v1 is human-triggered CLI only. Scheduler auto-invocation + new
cron_reflections DB table are v2 concerns.

Entry point: analyze_cron_run(job_id, job_start_ts_utc, output_path).
Never raises — returns empty list on any error, logs the exception.

CLI:
    envchain hermes-llm ~/.hermes/hermes-agent/venv/bin/python3 \\
        -m tools.reflection_agent \\
        ~/.hermes/cron/output/<job_id>/<YYYY-MM-DD_HH-MM-SS>.md
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from tools import report_verifier as rv

logger = logging.getLogger(__name__)

_MODEL = "moonshotai/kimi-k2.5"
_BASE_URL = "https://openrouter.ai/api/v1"
_API_KEY_ENV = "OPENROUTER_API_KEY"
_REVIEWER_TEMP = 1.0  # Kimi K2.5 requires temperature=1.0 (provider-imposed).
# Kimi K2.5 thinking mode can burn 2-4k tokens on reasoning before producing
# content. 4096 was too tight — a 20k-char review prompt left zero tokens for
# message.content. 16384 gives comfortable headroom for both reasoning + JSON.
_MAX_TOKENS = 16384

_LOG_PATH = Path.home() / ".hermes" / "logs" / "reflection-agent.log"
_HERMES_DB = Path.home() / ".hermes" / "memory" / "hermes.db"

# Lazy-init module-level client so tests can monkeypatch _call_kimi without
# needing an API key in the test env.
_client = None


Severity = Literal["high", "medium", "low"]
# MOL-227 v2 (P40): added "transition_missing" to catch the variant the 4am
# Tiered Memory Consolidation cron used to clean up (agent completed work but
# left a Jira ticket in a non-terminal state). With 4am scope-creep fixed,
# reflection catches this category instead.
Category = Literal["semantic", "fabrication", "evidence", "transition_missing", "work_not_completed"]


@dataclass
class Concern:
    severity: Severity
    category: Category
    description: str
    evidence: str


def _get_client():
    global _client
    if _client is None:
        from openai import OpenAI  # local import: openai is optional for tests
        api_key = os.environ.get(_API_KEY_ENV, "").strip()
        if not api_key:
            raise RuntimeError(f"{_API_KEY_ENV} not set — cannot call reviewer")
        _client = OpenAI(base_url=_BASE_URL, api_key=api_key)
    return _client


def _call_kimi(prompt: str) -> str:
    """Single-turn Kimi K2.5 call. Returns response content string.

    Kimi gotchas honored (per CLAUDE.md):
      - temperature fixed at 1.0 (provider-imposed).
      - extra_body["thinking"] enables reasoning (NOT extra_body["reasoning"]).
      - Single-turn, so reasoning_content invariant on assistant messages
        doesn't apply — but note this before adding multi-turn in v2.

    Tests stub this function entirely via monkeypatch.
    """
    client = _get_client()
    response = client.chat.completions.create(
        model=_MODEL,
        max_tokens=_MAX_TOKENS,
        temperature=_REVIEWER_TEMP,
        messages=[{"role": "user", "content": prompt}],
        extra_body={"thinking": {"type": "enabled"}},
    )
    msg = response.choices[0].message
    content = msg.content or ""
    # If Kimi spent all tokens on reasoning and produced no content, fall back
    # to the reasoning trace so we at least capture what it was thinking. Log
    # a warning so it's visible in triage.
    if not content.strip():
        reasoning = getattr(msg, "reasoning", None) or ""
        if reasoning.strip():
            logger.warning(
                "Kimi returned empty content (finish_reason=%s); using reasoning trace as fallback",
                response.choices[0].finish_reason,
            )
            return reasoning
    return content


_PROMPT_TEMPLATE = """You are a careful code-review auditor reading the output of an autonomous cron job that claims to have done some work (Jira ticket transitions, comments, file edits). The run has already been checked by a STRUCTURAL verifier that confirms file mtimes, Jira changelog timings, and literal/fuzzy snippet matching against Jira comment bodies.

Your job is to catch the four things the structural verifier CANNOT catch:

  (a) SEMANTIC: claims that are internally inconsistent with the run body (the agent says it did X in summary but the transcript/tool calls show it did Y).
  (b) FABRICATION: the run body cites an API field, function name, file path, or flag that doesn't exist in the surrounding code or isn't documented anywhere.
  (c) EVIDENCE: the run summary asserts an action happened but the transcript has zero textual evidence of that action being taken (e.g. claims a comment was posted but there's no jira-cli call shown). P58/MOL-268: DO NOT flag EVIDENCE if the output makes NO positive claim of action — a bare informational message, reminder, briefing, or FYI that just shares information is not subject to EVIDENCE concerns; there's no claimed action to verify.
  (d) TRANSITION_MISSING: the manifest declares work done on a Jira ticket (jira_comments populated or file_ops touched a ticket's referenced files) BUT the ticket's status is still non-terminal (To Do / In Progress / similar) AND jira_transitions is absent or incomplete. The agent finished the work but forgot to move the ticket. This is the single most common silent failure mode on the knockout cron and the structural verifier can't catch it because there's no manifest entry to check.

  (e) WORK_NOT_COMPLETED: read the ticket's original ask (in the run body's "I tackled MOL-XXX" section or the task-plan header). Check whether the run body contains evidence that the underlying *functionality* was built — concrete code edits, config edits, doc edits, or direct-action tool calls that address what the ticket asked for. Jira ticket creates, Jira comments, Jira transitions, TASKS.md edits, and memory-note edits are BOOKKEEPING, not functionality. If the only actions are bookkeeping AND the ticket was transitioned to a terminal state (Done / Closed / Resolved), the agent reported completion without actually completing. Flag as HIGH severity. Exception: if the ticket is itself explicitly documentation-only or task-organization-only per its description, bookkeeping-only is valid — but this must be clear from the ticket text, not inferred.

Return ONLY a JSON object — no prose, no code fences. Shape:

    {{"concerns": [
        {{"severity": "high", "category": "fabrication", "description": "Short one-sentence description.", "evidence": "Literal snippet from the run body or a specific token/field name that supports this concern."}}
    ]}}

If you find no concerns, return {{"concerns": []}}. Do not invent concerns. Do not flag things the structural verifier already passed. Severity "high" means the claim is materially wrong and misleading; "medium" means suspect but not confirmed; "low" means stylistic nit.

---

STRUCTURAL VERIFIER FINDINGS (already computed, do not re-check):
{claims_summary}

---

CRON RUN OUTPUT:
{output_body}

---

Return JSON now."""


def _build_prompt(output_body: str, manifest: Optional[dict], claims: list[dict]) -> str:
    if claims:
        lines = []
        for c in claims:
            status = "PASS" if c.get("verified") else "FAIL"
            lines.append(
                f"  - [{status}] {c.get('claim_type', '?')}: {c.get('verification_output', '')}"
            )
        claims_summary = "\n".join(lines)
    else:
        claims_summary = "  (no structural findings recorded — new run or verifier skipped)"

    # Cap output body to keep prompt under Kimi's practical context; historical
    # outputs are 12–20k chars, well inside Kimi's 262k window, but cap anyway.
    if len(output_body) > 80_000:
        output_body = output_body[:80_000] + "\n\n[...truncated for prompt-size cap...]"

    return _PROMPT_TEMPLATE.format(
        claims_summary=claims_summary,
        output_body=output_body,
    )


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_BARE_JSON_RE = re.compile(r"(\{.*\})", re.DOTALL)


def _parse_concerns(response_text: str) -> list[Concern]:
    """Extract Concern list from Kimi's response. Tolerate markdown fences,
    surrounding prose, and malformed JSON. Return empty list on parse failure."""
    if not response_text or not response_text.strip():
        return []

    # Try fenced block first, then bare JSON object.
    for pat in (_JSON_FENCE_RE, _BARE_JSON_RE):
        m = pat.search(response_text)
        if not m:
            continue
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        concerns_raw = data.get("concerns", []) if isinstance(data, dict) else []
        out: list[Concern] = []
        for c in concerns_raw:
            if not isinstance(c, dict):
                continue
            sev = str(c.get("severity", "medium")).lower()
            if sev not in ("high", "medium", "low"):
                sev = "medium"
            cat = str(c.get("category", "semantic")).lower()
            if cat not in ("semantic", "fabrication", "evidence", "transition_missing", "work_not_completed"):
                cat = "semantic"
            out.append(
                Concern(
                    severity=sev,  # type: ignore[arg-type]
                    category=cat,  # type: ignore[arg-type]
                    description=str(c.get("description", ""))[:500],
                    evidence=str(c.get("evidence", ""))[:500],
                )
            )
        return out

    return []


def _query_claims(job_id: str, job_start_ts_utc: float, now_utc: float) -> list[dict]:
    """Fetch structural-verifier rows from cron_verifications for this run.

    Returns a list of dicts with keys matching the table columns. Empty list
    on any DB error — the reflection agent runs even if the structural
    verifier hasn't populated claims (new runs, whitelist misses).
    """
    if not _HERMES_DB.exists():
        return []
    start_iso = datetime.fromtimestamp(job_start_ts_utc, tz=timezone.utc).isoformat()
    # Allow 1h headroom after the run to catch late-persisted rows.
    end_iso = datetime.fromtimestamp(now_utc + 3600, tz=timezone.utc).isoformat()
    try:
        with sqlite3.connect(str(_HERMES_DB)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT claim_type, verified, verification_output, claim_payload_json "
                "FROM cron_verifications "
                "WHERE job_id = ? AND run_timestamp BETWEEN ? AND ? "
                "ORDER BY id ASC",
                (job_id, start_iso, end_iso),
            )
            return [dict(row) for row in cur.fetchall()]
    except sqlite3.Error as e:
        logger.warning("cron_verifications query failed: %s", e)
        return []


def _log_invocation(
    job_id: str,
    concerns: list[Concern],
    raw_response: str,
    error: Optional[str] = None,
) -> None:
    """Append one JSONL line per invocation. Creates parent dir if missing."""
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "job_id": job_id,
            "concern_count": len(concerns),
            "concerns": [asdict(c) for c in concerns],
            "raw_response_preview": raw_response[:2000] if raw_response else "",
            "error": error,
        }
        with _LOG_PATH.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        logger.warning("reflection-agent log write failed: %s", e)


def analyze_output(
    output_body: str,
    manifest: Optional[dict] = None,
    claims: Optional[list[dict]] = None,
    job_id: str = "?",
) -> tuple[list[Concern], ReviewStatus]:
    """P64/MOL-259: pure semantic-review seam.

    Given an output body (and optional manifest + structural-verifier claims),
    call the reviewer LLM and return (concerns, status). Used by three
    callers: ``analyze_cron_run`` (file-based CLI path), ``reflect_and_annotate``
    (scheduler in-memory path), and the registered ``reflect_on_output`` tool
    surface. Centralizing the prompt + LLM-call + log in one place keeps the
    three entry points in sync.

    Status: "ok" = call succeeded (concerns may be empty); "unavailable" =
    LLM call failed (concerns always empty). Never raises.
    """
    if not output_body or not output_body.strip():
        return ([], "ok")
    prompt = _build_prompt(output_body, manifest, claims or [])
    try:
        raw = _call_kimi(prompt)
    except Exception as e:  # noqa: BLE001 — any LLM-call error is containable
        err = f"{type(e).__name__}: {e}"
        logger.warning("analyze_output: reviewer call failed: %s", err)
        _log_invocation(job_id, [], "", error=err)
        return ([], "unavailable")
    concerns = _parse_concerns(raw)
    _log_invocation(job_id, concerns, raw)
    return (concerns, "ok")


def analyze_cron_run(
    job_id: str,
    job_start_ts_utc: float,
    output_path: Path,
) -> list[Concern]:
    """Reflect on a cron run's output. Never raises.

    P64/MOL-259: delegates to ``analyze_output`` seam after reading the file
    and computing manifest + structural-verifier claims.
    """
    try:
        output_body = output_path.read_text(encoding="utf-8")
    except OSError as e:
        _log_invocation(job_id, [], "", error=f"read failed: {e}")
        return []

    manifest_text = rv._extract_manifest(output_body)
    manifest: Optional[dict] = None
    if manifest_text:
        try:
            manifest = rv._parse_manifest(manifest_text)
        except Exception as e:  # noqa: BLE001 — rv._parse_manifest wraps yaml errors
            logger.info("manifest parse failed (non-fatal): %s", e)

    now_utc = datetime.now(timezone.utc).timestamp()
    claims = _query_claims(job_id, job_start_ts_utc, now_utc)

    concerns, _status = analyze_output(output_body, manifest, claims, job_id)
    return concerns


# ---------------------------------------------------------------------------
# MOL-227 v2 (P40): scheduler-invoked entry point — returns annotated text,
# raw concerns, and a status flag distinguishing "clean" vs "unavailable"
# vs "concerns present". Mirrors tools.report_verifier.verify_and_annotate.
# ---------------------------------------------------------------------------

SEPARATOR = "═" * 40

ReviewStatus = Literal["ok", "unavailable", "disabled"]


def _format_reviewer_header(concerns: list[Concern], status: ReviewStatus, attempt: int = 1) -> str:
    """Build the REVIEWER header prepended to delivery. Mirrors report_verifier
    style so operators see the same banner shape from both verifier layers.

    `attempt` is the flywheel retry attempt number (1 = first attempt, 2+ = retry).
    """
    if status == "unavailable":
        # MOL-227 v2 review finding #2: include attempt number so operators
        # can tell WHICH attempt hit the LLM error. Asymmetric absence was
        # a real debug-time gap.
        return (
            f"⚠️ REVIEWER UNAVAILABLE (attempt {attempt}) — semantic review skipped for this run\n"
            "   (reflection agent LLM call failed; delivery proceeds uninspected)\n"
            f"{SEPARATOR}\n"
            "(original report follows)\n"
            f"{SEPARATOR}"
        )

    if not concerns:
        # P54/MOL-240: suppress clean-pass header on attempt 1.
        # Attempt >= 2 means the flywheel retried and recovered — still
        # worth surfacing ("REVIEWER: 0 concerns (attempt 2)" is a positive
        # recovery signal). Only attempt 1 is pure noise on reminder-style
        # crons and was the user-reported regression after P40 shipped.
        if attempt == 1 and status == "ok":
            return ""
        return (
            f"🔍 REVIEWER: 0 concerns (attempt {attempt})\n"
            f"{SEPARATOR}\n"
            "(original report follows)\n"
            f"{SEPARATOR}"
        )

    high = [c for c in concerns if c.severity == "high"]
    medium = [c for c in concerns if c.severity == "medium"]
    low = [c for c in concerns if c.severity == "low"]

    # MOL-227 v2 review finding #1: singular noun when exactly one concern.
    noun = "concern" if len(concerns) == 1 else "concerns"
    lines = [f"🔍 REVIEWER: {len(concerns)} {noun} ({len(high)} HIGH, {len(medium)} MEDIUM, {len(low)} LOW) (attempt {attempt})"]
    for c in high + medium + low:
        marker = "⚠️" if c.severity == "high" else ("•" if c.severity == "medium" else "·")
        desc = c.description.replace("\n", " ").strip()
        if len(desc) > 140:
            desc = desc[:140] + "…"
        lines.append(f"{marker} [{c.severity.upper()}] {c.category}: {desc}")
    lines.append(SEPARATOR)
    lines.append("(original report follows)")
    lines.append(SEPARATOR)
    return "\n".join(lines)


def reflect_and_annotate(
    job: dict,
    final_response: str,
    cron_session_id: str,
    job_start_ts_utc: float,
    attempt: int = 1,
) -> tuple[list[Concern], ReviewStatus, str]:
    """P65/MOL-271: scheduler-facing wrapper. Returns a 3-tuple of
    ``(concerns, status, banner)`` — the caller (scheduler) is now responsible
    for prepending the banner to the delivery payload AND for keeping the
    pristine final_response out of the cross-turn ``messages`` history.

    Previously this returned ``(annotated_text, concerns, status)`` and
    embedded the REVIEWER banner directly into the body. That caused
    reviewer-feedback contagion — the banner-containing body landed in
    ``state.db.messages``, ``session_search`` retrieved it, and the next
    cron's classifier saw "REVIEWER: X concerns" as user prompt and went
    off-rails (the 2026-04-23 $60 incident). The 3-tuple split lets the
    scheduler send the pristine body to ``messages`` and the banner-prepended
    delivery payload to the user — they no longer share a buffer.

    Status contract (unchanged from prior version):
      - "ok"          → review completed (concerns may be empty = clean pass)
      - "unavailable" → review failed (LLM call errored); concerns is always []
      - "disabled"    → review skipped because job._reflection_cfg.enabled=False;
                         concerns is always []; banner is empty.

    Never raises — fail-open semantics: any uncaught error returns
    ``([], "unavailable", <unavailable_banner>)`` so the scheduler can still
    surface the visible signal alongside the pristine body.
    """
    try:
        if not final_response or not final_response.strip():
            return ([], "ok", "")

        job_id = str(job.get("id", "?"))

        # Flag disable (config.yaml cron.reflection.enabled=false) — no-op.
        # Checking here (not in scheduler) keeps the config surface narrow.
        _cfg = job.get("_reflection_cfg") or {}
        if _cfg.get("enabled") is False:
            return ([], "disabled", "")

        manifest_text = rv._extract_manifest(final_response)
        manifest: Optional[dict] = None
        if manifest_text:
            try:
                manifest = rv._parse_manifest(manifest_text)
            except Exception as e:  # noqa: BLE001
                logger.info("manifest parse failed (non-fatal): %s", e)

        now_utc = datetime.now(timezone.utc).timestamp()
        claims = _query_claims(job_id, job_start_ts_utc, now_utc)

        # P64/MOL-259: delegate semantic review through analyze_output seam.
        concerns, status = analyze_output(final_response, manifest, claims, job_id)
        if status == "unavailable":
            banner = _format_reviewer_header([], "unavailable", attempt)
            return ([], "unavailable", banner)

        # P65/MOL-271: banner is returned separately — never folded into the body.
        banner = _format_reviewer_header(concerns, "ok", attempt)
        return (concerns, "ok", banner)

    except Exception as e:  # noqa: BLE001 — last-ditch containment
        # MOL-227 v2 review finding #5: even on uncaught crash, attach the
        # visible REVIEWER UNAVAILABLE banner so operators see the signal.
        logger.exception("reflect_and_annotate crashed (delivery proceeds)")
        _log_invocation(str(job.get("id", "?")), [], "", error=f"{type(e).__name__}: {e}")
        try:
            banner = _format_reviewer_header([], "unavailable", attempt)
            return ([], "unavailable", banner)
        except Exception:  # noqa: BLE001 — if header build itself errors, return empty banner
            return ([], "unavailable", "")


# ---------------------------------------------------------------------------
# P64/MOL-259: registered tool surface — ``reflect_on_output``
#
# Exposes the analyze_output seam as a regular Hermes tool so the agent can
# self-review intermediate output (not just scheduler-triggered cron runs).
# Fail-CLOSED: any LLM-call error returns the literal sentinel string
# ``"reviewer unavailable"`` so the caller can't mistake silence for a clean
# review. (Distinct from the scheduler's fail-OPEN behavior, which still
# delivers but tags the banner.)
# ---------------------------------------------------------------------------

REFLECT_ON_OUTPUT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "reflect_on_output",
        "description": (
            "Run the semantic reviewer (Kimi K2.5) on a block of agent output "
            "and return a JSON-serialized list of concerns (severity, "
            "category, description, evidence). Use when you want a second-pass "
            "check on a long answer before delivering it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "output_body": {
                    "type": "string",
                    "description": "The text to review.",
                },
                "job_id": {
                    "type": "string",
                    "description": "Optional identifier for log correlation.",
                },
            },
            "required": ["output_body"],
        },
    },
}


def _reflect_on_output_handler(**kwargs) -> str:
    """P64/MOL-259 tool handler. Fail-CLOSED — returns literal
    ``"reviewer unavailable"`` on any LLM error so the agent doesn't mistake
    silence for a clean review."""
    output_body = kwargs.get("output_body", "") or ""
    job_id = str(kwargs.get("job_id", "?"))
    if not output_body.strip():
        return json.dumps({"concerns": [], "status": "ok"})
    concerns, status = analyze_output(output_body, manifest=None, claims=None, job_id=job_id)
    if status == "unavailable":
        return "reviewer unavailable"
    return json.dumps(
        {
            "concerns": [asdict(c) for c in concerns],
            "status": status,
        }
    )


try:
    from tools.registry import registry as _registry

    _registry.register(
        name="reflect_on_output",
        toolset="reflection",
        schema=REFLECT_ON_OUTPUT_SCHEMA,
        handler=_reflect_on_output_handler,
        description=(
            "Run the semantic reviewer on a block of output and return "
            "concerns as JSON."
        ),
        emoji="🔍",
    )
except Exception as _e:  # noqa: BLE001 — tool registration is best-effort
    logger.warning("reflect_on_output tool registration skipped: %s", _e)


def _cli(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            "Usage: python3 -m tools.reflection_agent <cron_output_file.md>",
            file=sys.stderr,
        )
        return 1
    path_str = argv[1]
    if not os.path.exists(path_str):
        print(f"File not found: {path_str}", file=sys.stderr)
        return 1

    path = Path(path_str)
    basename = path.name
    m_ts = re.match(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})", basename)
    if m_ts:
        date_part, hh, mm, ss = m_ts.groups()
        local_dt = datetime.strptime(f"{date_part} {hh}:{mm}:{ss}", "%Y-%m-%d %H:%M:%S")
        # Filename ts = completion time. Back off 10 min to cover job start.
        # Mirrors report_verifier._cli.
        job_start_ts_utc = local_dt.timestamp() - 600
    else:
        job_start_ts_utc = os.path.getmtime(path) - 600

    job_id = path.parent.name or "cli"

    concerns = analyze_cron_run(job_id, job_start_ts_utc, path)

    print(json.dumps(
        {"job_id": job_id, "concerns": [asdict(c) for c in concerns]},
        indent=2,
    ))
    return 0  # Reflection is advisory — never gate on severity in v1.


if __name__ == "__main__":
    try:
        sys.exit(_cli(sys.argv))
    except Exception:  # noqa: BLE001 — containment: CLI must not traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(2)
