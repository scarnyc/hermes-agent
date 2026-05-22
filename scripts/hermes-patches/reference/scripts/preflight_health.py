#!/usr/bin/env python3
"""P152/MOL-503 — Symphony-bridge pre-flight infra-health gate.

Phase 2 of Symphony v2. Before symphony_bridge.main() spawns Phase 1 on a
ticket, probe all 4 harness × provider cells in parallel. If 3+ cells return
a confirmed-degraded signal, abort the tick cleanly with a cron-output
banner — stopping the 60-min death-spiral observed on the 2026-05-10 MOL-481
run before any phase work begins.

Cells (matching symphony_bridge's 4-tier matrix):
  cc+ds        — claude -p → api.deepseek.com/anthropic
  cc+anthropic — claude -p → api.anthropic.com (native)
  hermes+ds    — hermes -z → DeepSeek provider
  hermes+kimi  — hermes -z → Kimi K2.6 provider

Per-cell outcomes:
  probe_succeeded         exit 0, non-empty stdout
  probe_returned_degraded non-zero exit AND stderr/stdout matches a known
                          degradation pattern (rate limit, auth, 5xx), OR
                          exit-0 with empty stdout (silent-CC-spawn class).
  probe_exception         probe-side failure (timeout, OSError, ambiguous
                          non-zero exit) — does NOT count toward abort.
                          Probe-can't-run is NOT the same as provider-says-
                          degraded; the bridge is the source of truth.

Abort decision (degraded count only — exceptions excluded):
  0 degraded  proceed normally
  1 degraded  proceed (single-cell loss survivable; serial fallback handles it)
  2 degraded  proceed with banner warning
  3 degraded  ABORT — emit INFRA:DEGRADED banner
  4 degraded  ABORT — emit INFRA:DEGRADED ALL-DEAD banner

Probe cost (initial ship, no cache): ~15s wall-clock per tick (4 cells in
parallel), ~$0.001 in API spend per tick. Revisit caching only when probe
cost or latency becomes material.
"""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────────

HOME = Path.home()
LOG_DIR = HOME / ".hermes" / "logs"
LOG_FILE = LOG_DIR / "symphony_bridge.log"

CLAUDE_BIN = os.path.expanduser("~/.local/bin/claude")
DEEPSEEK_ENDPOINT = "https://api.deepseek.com/anthropic/v1/messages"

_PROBE_TIMEOUT_SECONDS = 15

# Provider env keys stripped from every probe's env before the cell-specific
# key is set. Prevents cross-cell leakage where (e.g.) ANTHROPIC_API_KEY
# inherited from os.environ could shadow Kimi/DeepSeek routing in the
# hermes -z subprocess.
_FOREIGN_PROVIDER_ENV_KEYS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
    "DEEPSEEK_API_KEY", "KIMI_API_KEY", "OPENROUTER_API_KEY",
    "GOOGLE_API_KEY",
)

# Degradation signatures: substrings (lowercased) that confirm a cell is
# returning a real provider/auth/quota unhealthy signal — not a generic
# probe failure. All entries are anchored phrasings; bare HTTP status digits
# would match unrelated content (PIDs, max_capacity field values, etc.) —
# the same false-positive class the P149 retry classifier hit on bare 503.
_DEGRADED_SUBSTRINGS = [
    # Rate limit / quota
    "rate_limit", "rate limit", "too many requests",
    "quota exceeded", "quota_exceeded", "overloaded",
    "http 429", "status 429", "code 429", " 429 ", " 429:",
    # Auth / config
    "unauthorized", "forbidden",
    "invalid api key", "invalid_api_key",
    "authentication failed", "authentication_failed",
    "permission denied", "api key not", "missing api key",
    "http 401", "status 401", "code 401",
    "http 403", "status 403", "code 403",
    # 5xx connectivity (anchored, not bare digits)
    "http 500", "http 502", "http 503", "http 504",
    "service unavailable", "bad gateway", "gateway timeout",
    "upstream connect error", "connection reset",
    # Hermes-specific
    "envchain", "deepseek_api_key", "kimi_api_key",
]


def _log_event(event: str, **fields) -> bool:
    """Append a JSONL record to ~/.hermes/logs/symphony_bridge.log. Returns
    True on durable write, False on OSError (caller can fall back to stderr
    on catastrophic-log-loss paths).

    Schema parity with symphony_bridge._log_event so a single grep covers the
    full tick surface. Duplicated rather than imported to keep this module
    importable without symphony_bridge (circular-import safe).
    """
    record = {"ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
              "event": event, **fields}
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except OSError:
        return False


def _get_api_key(name: str) -> str:
    """envchain-wrapper.sh exports provider keys into the gateway env at
    launch; bare `os.environ.get` is safe here. Returns '' if unset.
    """
    return os.environ.get(name, "")


def _classify_probe_output(exit_code: int | None, stdout: str, stderr: str,
                           timed_out: bool, oserror: bool) -> str:
    """Map a probe result to one of three outcome buckets.

      probe_exception          probe-side failures (timeout, OSError, missing
                               binary) AND non-zero exit without a recognized
                               degradation pattern. These are NOT provider-
                               degraded signals; conservative for ambiguous
                               CLI-level failures (flag drift, version bumps).
      probe_returned_degraded  non-zero exit WITH a recognized degradation
                               substring in stdout/stderr, OR exit-0 with
                               empty stdout (the silent-CC-spawn failure
                               class MOL-481 demonstrated — an LLM probe
                               that exits cleanly with no output is broken,
                               not healthy).
      probe_succeeded          exit 0 with non-empty stdout.
    """
    if timed_out or oserror:
        return "probe_exception"
    combined = ((stdout or "") + " " + (stderr or "")).lower()
    has_degraded_pattern = any(sub in combined for sub in _DEGRADED_SUBSTRINGS)
    if exit_code != 0 and has_degraded_pattern:
        return "probe_returned_degraded"
    if exit_code == 0:
        if (stdout or "").strip():
            return "probe_succeeded"
        # An LLM probe target that exits 0 with empty stdout is the silent-
        # CC-spawn failure class — countable toward abort, not excluded.
        return "probe_returned_degraded"
    return "probe_exception"


def _run_probe(name: str, argv: list[str], env: dict[str, str]) -> dict:
    """Probe child may orphan descendants on SIGKILL; accepted because probes
    are short (15s) and the bridge's main() SIGKILL-recovery sweep catches
    survivors on the next tick. Revisit if observed zombie-orphan rate
    exceeds 1/day.
    """
    start = time.monotonic()
    stdout, stderr = "", ""
    exit_code: int | None = None
    timed_out = False
    oserror_exc: str | None = None
    try:
        r = subprocess.run(
            argv,
            capture_output=True, text=True,
            timeout=_PROBE_TIMEOUT_SECONDS, env=env,
        )
        stdout = r.stdout or ""
        stderr = r.stderr or ""
        exit_code = r.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = (exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
    except (OSError, FileNotFoundError) as exc:
        oserror_exc = f"{type(exc).__name__}: {exc}"

    elapsed_ms = int((time.monotonic() - start) * 1000)
    outcome = _classify_probe_output(exit_code, stdout, stderr,
                                     timed_out, oserror_exc is not None)
    return {
        "cell": name,
        "outcome": outcome,
        "elapsed_ms": elapsed_ms,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "oserror": oserror_exc,
        "stdout_head": stdout[:200],
        "stderr_tail": stderr[-1000:],
    }


def _build_probe_env(scoped_keys: dict[str, str]) -> dict[str, str]:
    """Build a clean per-probe env: strip ALL foreign provider keys, then set
    only the keys this probe should see.

    Without stripping, `os.environ.copy()` carries every provider key the
    parent process holds (DS, Anthropic, Kimi, etc.) into the child — which
    can shadow provider routing or mis-attribute the probe. Each cell must
    test ITS OWN provider, not whatever happens to be in the parent env.
    """
    env = os.environ.copy()
    for k in _FOREIGN_PROVIDER_ENV_KEYS:
        env.pop(k, None)
    env.update(scoped_keys)
    return env


def _probe_cc(name: str, scoped_keys: dict[str, str]) -> dict:
    """Probe a `claude -p` subprocess with a minimal 'ok' prompt.

    Argv matches symphony_bridge's real CC invocations minus repo-specific
    flags so the probe exercises the same code paths.
    """
    argv = [
        CLAUDE_BIN, "-p",
        "--permission-mode", "auto",
        "--max-turns", "1",
        "--output-format", "json",
        "--no-session-persistence",
        "--add-dir", "/tmp",
        "ok",
    ]
    return _run_probe(name, argv, _build_probe_env(scoped_keys))


def _probe_hermes(name: str, provider: str, model: str,
                  key_name: str, key: str) -> dict:
    """Probe a `hermes -z` subprocess for a given provider/model.

    Matches symphony_bridge's hermes swarm argv shape.
    """
    argv = [
        sys.executable, "-m", "hermes_cli.main",
        "-z", "ok",
        "--provider", provider,
        "--model", model,
    ]
    return _run_probe(name, argv, _build_probe_env({key_name: key}))


def _missing_key_result(cell: str, key_name: str) -> dict:
    """Treat envchain-missing-key as a deterministic-degraded signal.

    A missing API key is not a probe-side failure (probe is fine; it just
    refused to spawn). It IS a confirmed provider/auth degradation — the cell
    cannot serve. Count toward abort.
    """
    return {
        "cell": cell,
        "outcome": "probe_returned_degraded",
        "elapsed_ms": 0,
        "exit_code": None,
        "timed_out": False,
        "oserror": None,
        "stdout_head": "",
        "stderr_tail": f"{key_name} unavailable in envchain",
        "missing_key": key_name,
    }


def _build_cell_jobs() -> dict[str, callable]:
    """Each callable reads its env at execution time so missing-key gates
    apply per-cell.
    """
    ds_key = _get_api_key("DEEPSEEK_API_KEY")
    anthropic_key = _get_api_key("ANTHROPIC_API_KEY")
    kimi_key = _get_api_key("KIMI_API_KEY")

    def _cc_ds() -> dict:
        if not ds_key:
            return _missing_key_result("cc+ds", "DEEPSEEK_API_KEY")
        return _probe_cc("cc+ds", {
            "ANTHROPIC_BASE_URL": DEEPSEEK_ENDPOINT,
            "ANTHROPIC_AUTH_TOKEN": ds_key,
        })

    def _cc_anthropic() -> dict:
        if not anthropic_key:
            return _missing_key_result("cc+anthropic", "ANTHROPIC_API_KEY")
        # _build_probe_env already pops ANTHROPIC_BASE_URL/AUTH_TOKEN, so a
        # residual DS-direct override in the parent can't leak through.
        return _probe_cc("cc+anthropic", {"ANTHROPIC_API_KEY": anthropic_key})

    def _hermes_ds() -> dict:
        if not ds_key:
            return _missing_key_result("hermes+ds", "DEEPSEEK_API_KEY")
        return _probe_hermes("hermes+ds", "deepseek",
                             "deepseek-v4-pro[1m]",
                             "DEEPSEEK_API_KEY", ds_key)

    def _hermes_kimi() -> dict:
        if not kimi_key:
            return _missing_key_result("hermes+kimi", "KIMI_API_KEY")
        return _probe_hermes("hermes+kimi", "kimi-coding",
                             "kimi-k2.6",
                             "KIMI_API_KEY", kimi_key)

    return {
        "cc+ds": _cc_ds,
        "cc+anthropic": _cc_anthropic,
        "hermes+ds": _hermes_ds,
        "hermes+kimi": _hermes_kimi,
    }


def _emit_banner(event_name: str, banner: str, results: list[dict]) -> None:
    """Emit a structured JSONL event PLUS a cron-output banner.

    Cron-output banner is the existing reflection-agent surface for INFRA-class
    concerns (see CLAUDE.md cron-failure-signals reference). Writing to both
    stdout AND stderr because some cron pipelines capture only one.
    """
    _log_event(event_name, cells=[
        {"cell": r["cell"], "outcome": r["outcome"],
         "stderr_tail": (r.get("stderr_tail") or "")[:300],
         "missing_key": r.get("missing_key")}
        for r in results
    ])
    print(banner, file=sys.stderr)
    print(banner)


def check() -> tuple[bool, dict]:
    """Run the 4-cell parallel probe. Returns (should_proceed, summary).

    should_proceed=False means caller MUST abort the tick before any work.
    Caller is responsible for early-return; this function never sys.exits.

    summary["decision"] is one of: proceed_all_healthy /
    proceed_one_cell_degraded / proceed_matrix_degraded /
    abort_three_degraded / abort_all_degraded. summary["abort_reason"] is
    empty when proceeding. Per-cell breakdown lives in summary["cells"].
    """
    start = time.monotonic()
    jobs = _build_cell_jobs()
    results: list[dict] = []
    _log_event("preflight_health_start", cell_count=len(jobs),
               probe_timeout_secs=_PROBE_TIMEOUT_SECONDS)

    with ThreadPoolExecutor(max_workers=4) as pool:
        future_to_cell = {pool.submit(fn): cell for cell, fn in jobs.items()}
        for fut in as_completed(future_to_cell):
            cell = future_to_cell[fut]
            try:
                results.append(fut.result())
            except Exception as exc:  # noqa: BLE001 — fail-open guard
                # A probe lambda shouldn't raise (_run_probe catches its own
                # subprocess errors); this branch exists so a programmer bug
                # in the lambda layer doesn't kill the whole tick.
                results.append({
                    "cell": cell, "outcome": "probe_exception",
                    "elapsed_ms": 0, "exit_code": None,
                    "timed_out": False,
                    "oserror": f"unexpected:{type(exc).__name__}:{exc}",
                    "stdout_head": "", "stderr_tail": "",
                })
                wrote = _log_event("preflight_probe_unexpected_exc",
                                   cell=cell, exc_type=type(exc).__name__,
                                   exc=str(exc))
                if not wrote:
                    # Compound fail-open guard: if LOG_DIR is unwritable AND a
                    # probe lambda raised, cron captures stderr regardless —
                    # use it as a last-ditch durable surface.
                    print(f"[preflight_health] unexpected probe exception "
                          f"cell={cell} type={type(exc).__name__} exc={exc}",
                          file=sys.stderr)

    succeeded = sum(1 for r in results if r["outcome"] == "probe_succeeded")
    degraded = sum(1 for r in results if r["outcome"] == "probe_returned_degraded")
    exception = sum(1 for r in results if r["outcome"] == "probe_exception")
    total_elapsed_ms = int((time.monotonic() - start) * 1000)

    # P152/MOL-503: abort thresholds operate on degraded COUNT ONLY.
    # Exceptions are logged but never count toward abort — probe-can't-run
    # is not the same as provider-says-degraded.
    should_proceed = True
    abort_reason = ""
    if degraded == 0:
        decision = "proceed_all_healthy"
    elif degraded == 1:
        decision = "proceed_one_cell_degraded"
    elif degraded == 2:
        decision = "proceed_matrix_degraded"
        _emit_banner(
            "preflight_warn_two_degraded",
            "⚠️ symphony-bridge preflight: 2 cells degraded — tick may be slower",
            results,
        )
    elif degraded == 3:
        decision = "abort_three_degraded"
        should_proceed = False
        abort_reason = "matrix_critically_degraded"
        _emit_banner(
            "preflight_abort_three_degraded",
            "⚠️ INFRA:DEGRADED symphony-bridge preflight aborted: "
            "3 cells degraded — skipping ticket",
            results,
        )
    else:
        # 4 or unexpectedly more
        decision = "abort_all_degraded"
        should_proceed = False
        abort_reason = "all_providers_dead"
        _emit_banner(
            "preflight_abort_all_degraded",
            "⚠️ INFRA:DEGRADED symphony-bridge preflight aborted: "
            "ALL providers/harnesses dead — skipping ticket",
            results,
        )

    summary = {
        "cells": results,
        "succeeded_count": succeeded,
        "degraded_count": degraded,
        "exception_count": exception,
        "decision": decision,
        "abort_reason": abort_reason,
        "total_elapsed_ms": total_elapsed_ms,
    }

    _log_event("preflight_health_decision",
               decision=decision,
               succeeded_count=succeeded,
               degraded_count=degraded,
               exception_count=exception,
               total_elapsed_ms=total_elapsed_ms,
               should_proceed=should_proceed,
               cells=[{"cell": r["cell"], "outcome": r["outcome"],
                       "elapsed_ms": r.get("elapsed_ms")} for r in results])

    # Probe-can't-run all-4-cells case: high-severity log signal but tick
    # still proceeds — bridge runs its normal fallback chain.
    if exception == 4:
        _log_event("preflight_all_probes_exception",
                   note="all 4 probes errored; tick proceeds with normal fallback chain")

    return should_proceed, summary


if __name__ == "__main__":
    proceed, result = check()
    print(json.dumps(result, default=str, indent=2))
    sys.exit(0 if proceed else 1)
