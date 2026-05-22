#!/usr/bin/env bash
# cdt-mcp-reaper.sh — chrome-devtools-mcp idle reaper.
#
# Kills chrome-devtools-mcp node MCP server processes that leak when CC
# sessions close without graceful shutdown. Each CC session lazy-spawns one
# on first tools/list RPC; killing the node parent closes its stdio pipes,
# which the Chromium child detects and exits in turn (no SIGHUP cascade —
# node's child watchdog tracks pipe-close, not signals).
#
# Observed at initial deployment (2026-05-12, 15 active CC sessions): about
# 15 stale node procs + 1 orphan Chromium, ~3-4GB combined RSS. Actual
# steady-state depends on cdt-mcp version + CC session lifecycle.
#
# Modes:
#   --idle-only   Skip PIDs whose fd 0 (stdin) is still bound to a peer.
#                 Best-effort lsof heuristic; treats lsof errors and timeouts
#                 as "skip this PID" — prefer false negatives over false-
#                 positive kills.
#   --dry-run     Print PIDs that would be killed without killing.
#
# Audit: one JSONL line per run at ~/.hermes/logs/cdt-mcp-reaper.jsonl with
# fields ts / mode / before / attempted / succeeded / sigkill_used / after.
#
# Scheduled via ~/Library/LaunchAgents/ai.hermes.cdt-mcp-reaper.plist
# (StartCalendarInterval 04:30 local daily). Fires after the 04:00
# consolidation cron, before morning work.
#
# Production deployment note: install coreutils (`brew install coreutils`)
# so `gtimeout` is on PATH — without it, the lsof orphan check in
# --idle-only mode has no hard timeout and could hang on a stuck process.
#
## Patch reference: P178/MOL-564 (kept out of --help output).

set -uo pipefail

AUDIT_LOG="$HOME/.hermes/logs/cdt-mcp-reaper.jsonl"
IDLE_ONLY=0
DRY_RUN=0

# Match node procs whose argv references chrome-devtools-mcp. Tightened from
# bare "chrome-devtools-mcp" so parent shells whose argv contains the literal
# string (log tailers, editor sessions, the launchctl invocation of this
# script itself) don't self-match into the kill list.
PGREP_PATTERN='node.*chrome-devtools-mcp'

# Detect timeout binary for the --idle-only lsof check. coreutils ships
# `gtimeout` on macOS; Linux ships `timeout`. If neither is present, proceed
# without timeout and rely on the docstring note above.
if command -v timeout >/dev/null 2>&1; then
  LSOF_TIMEOUT="timeout 5"
elif command -v gtimeout >/dev/null 2>&1; then
  LSOF_TIMEOUT="gtimeout 5"
else
  LSOF_TIMEOUT=""
fi

while [ $# -gt 0 ]; do
  case "$1" in
    --idle-only) IDLE_ONLY=1; shift ;;
    --dry-run)   DRY_RUN=1; shift ;;
    -h|--help)
      # Strip the ## tag line so internal patch refs don't leak into --help.
      grep -E '^#( |$)' "$0" | grep -v '^## ' | sed 's/^#\s\?//'
      exit 0
      ;;
    *) echo "cdt-mcp-reaper: unknown arg '$1'" >&2; exit 64 ;;
  esac
done

mkdir -p "$(dirname "$AUDIT_LOG")"

# Incremental trap registration: each mktemp gets its rm-cleanup as soon as
# the variable is bound, so a failure of mktemp #2 still cleans up mktemp #1.
PIDS_FILE=$(mktemp) || { echo "cdt-mcp-reaper: mktemp PIDS_FILE failed" >&2; exit 71; }
trap 'rm -f "$PIDS_FILE"' EXIT

KILL_FILE=$(mktemp) || { echo "cdt-mcp-reaper: mktemp KILL_FILE failed" >&2; exit 71; }
trap 'rm -f "$PIDS_FILE" "$KILL_FILE"' EXIT

pgrep -f "$PGREP_PATTERN" > "$PIDS_FILE" 2>/dev/null || true
COUNT_BEFORE=$(wc -l < "$PIDS_FILE" | tr -d ' ')

if [ "$IDLE_ONLY" -eq 1 ]; then
  while IFS= read -r pid; do
    [ -z "$pid" ] && continue

    # Step 1: confirm proc is alive and accessible. Skip on ESRCH (already
    # gone) or EPERM (wrong UID); we shouldn't be killing either case.
    kill -0 "$pid" 2>/dev/null || continue

    # Step 2: probe fd 0 via lsof. `-a` ANDs the -p and -d filters (without
    # `-a`, macOS lsof ORs them and returns ~700 lines for any PID). `-F t`
    # is terse type-field output. Direct capture (no `tail` / `grep` pipe)
    # sidesteps the MOL-516 pipefail+grep-q SIGPIPE inversion entirely.
    # `|| continue` skips this PID on any lsof failure (timeout 124, internal
    # 125, "no match" 1) — preferring false negatives over false-positive
    # kills.
    if [ -n "$LSOF_TIMEOUT" ]; then
      fd0=$($LSOF_TIMEOUT lsof -a -p "$pid" -d 0 -F t 2>/dev/null) || continue
    else
      fd0=$(lsof -a -p "$pid" -d 0 -F t 2>/dev/null) || continue
    fi

    # lsof exited clean. Empty fd 0 output → no peer holding stdin → orphan
    # candidate. Non-empty → has an open fd 0 → likely live, skip.
    if [ -z "$fd0" ]; then
      printf '%s\n' "$pid" >> "$KILL_FILE"
    fi
  done < "$PIDS_FILE"
else
  cp "$PIDS_FILE" "$KILL_FILE"
fi

COUNT_KILL=$(wc -l < "$KILL_FILE" | tr -d ' ')

if [ "$DRY_RUN" -eq 1 ]; then
  MODE_NAME=$([ "$IDLE_ONLY" -eq 1 ] && echo idle-only || echo all)
  printf 'cdt-mcp-reaper: dry-run (mode=%s)\n' "$MODE_NAME"
  printf '  total node procs matching chrome-devtools-mcp: %d\n' "$COUNT_BEFORE"
  printf '  would kill: %d\n' "$COUNT_KILL"
  awk 'NF { print "    pid=" $0 }' "$KILL_FILE"
  exit 0
fi

# First pass: SIGTERM. Track actual success (kill EPERM/ESRCH absorbed but
# counted separately so the audit log distinguishes intent from outcome).
ATTEMPTED=0
SUCCEEDED=0
while IFS= read -r pid; do
  [ -z "$pid" ] && continue
  ATTEMPTED=$((ATTEMPTED + 1))
  if kill "$pid" 2>/dev/null; then
    SUCCEEDED=$((SUCCEEDED + 1))
  fi
done < "$KILL_FILE"

# Poll for cleanup (node usually exits within ~100ms on SIGTERM under low
# load; up to ~5s on a loaded box).
COUNT_AFTER=$COUNT_BEFORE
if [ "$ATTEMPTED" -gt 0 ]; then
  for _ in 1 2 3 4 5; do
    sleep 1
    COUNT_AFTER=$(pgrep -f "$PGREP_PATTERN" 2>/dev/null | wc -l | tr -d ' ')
    [ "$COUNT_AFTER" -eq 0 ] && break
  done
fi

# Second pass: SIGKILL survivors. Captured in the audit so frequent SIGKILL
# escalation is observable, not just narrated in stderr.
SIGKILL_USED=0
if [ "$COUNT_AFTER" -gt 0 ] && [ "$ATTEMPTED" -gt 0 ]; then
  SURVIVORS_FILE=$(mktemp) || SURVIVORS_FILE=""
  if [ -n "$SURVIVORS_FILE" ]; then
    trap 'rm -f "$PIDS_FILE" "$KILL_FILE" "$SURVIVORS_FILE"' EXIT
    pgrep -f "$PGREP_PATTERN" > "$SURVIVORS_FILE" 2>/dev/null || true
    while IFS= read -r pid; do
      [ -z "$pid" ] && continue
      if kill -9 "$pid" 2>/dev/null; then
        SIGKILL_USED=$((SIGKILL_USED + 1))
      fi
    done < "$SURVIVORS_FILE"
    sleep 1
    COUNT_AFTER=$(pgrep -f "$PGREP_PATTERN" 2>/dev/null | wc -l | tr -d ' ')
  fi
fi

# Audit: surface the write failure rather than dropping the only observability
# artifact this script produces.
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
MODE=$([ "$IDLE_ONLY" -eq 1 ] && echo idle-only || echo all)
AUDIT_LINE=$(printf '{"ts":"%s","mode":"%s","before":%d,"attempted":%d,"succeeded":%d,"sigkill_used":%d,"after":%d}' \
  "$TS" "$MODE" "$COUNT_BEFORE" "$ATTEMPTED" "$SUCCEEDED" "$SIGKILL_USED" "$COUNT_AFTER")

if ! printf '%s\n' "$AUDIT_LINE" >> "$AUDIT_LOG" 2>/dev/null; then
  echo "cdt-mcp-reaper: AUDIT WRITE FAILED ($AUDIT_LOG)" >&2
  echo "cdt-mcp-reaper: $AUDIT_LINE" >&2
fi

# Exit 75 (EX_TEMPFAIL) so launchd surfaces survivors as a job failure instead
# of treating the run as success.
if [ "$COUNT_AFTER" -gt 0 ]; then
  echo "cdt-mcp-reaper: $COUNT_AFTER process(es) survived SIGTERM+SIGKILL — investigate." >&2
  exit 75
fi

exit 0
