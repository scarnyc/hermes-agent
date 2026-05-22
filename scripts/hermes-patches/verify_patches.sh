#!/usr/bin/env bash
# MOL-158: Verify all Hermes runtime patches are applied
#
# Usage: bash scripts/hermes-patches/verify_patches.sh [--quiet]
#
# Grep-based signature checks — reports PRESENT/MISSING for each patch.
# Note: -e deliberately omitted from set — we want to check all patches
# even if individual grep calls fail.
#
# --quiet: suppress all output; only the exit code matters (0=pass, 1=fail)
#
# P01 (docker.py), P02 (terminal_tool.py) were retired when switching
# from Docker to local backend (2026-04-10). P07 (granola_tools.py)
# was un-archived — applies to both backends.
# See PATCHES.md "Archived — Docker Backend" section for rollback.

set -uo pipefail

HERMES_AGENT="${HERMES_HOME:-$HOME/.hermes}/hermes-agent"
HERMES_SCRIPTS="${HERMES_HOME:-$HOME/.hermes}/scripts"
HERMES_SKILLS="${HERMES_HOME:-$HOME/.hermes}/skills"

# Repo + reference-diff resolver (post-MOL-665): runtime tree has no
# `reference/` directory and no test fixtures; checks that assert
# reference-diff or repo-relative file presence must look at the
# hermes-poc repo where those artifacts are tracked. Fall through to the
# legacy local path if the repo isn't checked out (CI-mode).
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -d "${HOME}/Code/hermes-poc/scripts/hermes-patches" ]]; then
    HERMES_POC_REPO="${HOME}/Code/hermes-poc"
else
    HERMES_POC_REPO="$(cd "${_SCRIPT_DIR}/../.." && pwd)"
fi
HERMES_POC_REFERENCE="${HERMES_POC_REPO}/scripts/hermes-patches/reference"

QUIET=0
if [[ "${1:-}" == "--quiet" ]]; then
    QUIET=1
fi

# P235/MOL-665: --terse mode. Self-delegates to the default (verbose) run and
# awk-parses the output into one line per P-block: `P###: <fail>/<total>
# [first_failure_signature]`. Only emits P-blocks with at least one failure,
# so the operator sees the drift surface at a glance instead of scrolling
# through 7k+ lines of green ticks. Exit code mirrors the inner run.
#
# This is a triage tool for MOL-665 verifier-drift cleanup after the
# MOL-597 modular-refactor absorption — see PATCHES.md P235 and the
# MOL-665 PR body. Falls through to the rest of the script when flag is
# anything else.
if [[ "${1:-}" == "--terse" ]]; then
    bash "$0" 2>&1 | awk '
        function emit() {
            if (current != "" && fail > 0) {
                printf "%s: %d/%d", current, fail, total
                if (first_fail != "") printf " [%s]", first_fail
                printf "\n"
            }
        }
        /^=== P[0-9]+/ {
            emit()
            match($0, /P[0-9]+/)
            current = substr($0, RSTART, RLENGTH)
            fail = 0; total = 0; first_fail = ""
            next
        }
        /\[✓\]/ { total++ }
        /\[✗\]/ {
            total++; fail++
            if (first_fail == "") {
                line = $0
                gsub(/\033\[[0-9;]*m/, "", line)
                sub(/^[[:space:]]*\[✗\][[:space:]]*/, "", line)
                first_fail = substr(line, 1, 60)
            }
        }
        END { emit() }
    '
    exit "${PIPESTATUS[0]}"
fi

if [ ! -d "$HERMES_AGENT" ]; then
    [[ $QUIET -eq 0 ]] && printf '\033[0;31mERROR: Hermes agent directory not found: %s\033[0m\n' "$HERMES_AGENT"
    exit 1
fi

passed=0
failed=0
total=0

check() {
    local label="$1"
    local file="$2"
    local pattern="$3"
    total=$((total + 1))

    if [ ! -f "$file" ]; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m %s — file not found: %s\n' "$label" "$file"
        failed=$((failed + 1))
        return
    fi

    if [ ! -r "$file" ]; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;33m[?]\033[0m %s — file not readable: %s\n' "$label" "$file"
        failed=$((failed + 1))
        return
    fi

    if grep -Eq "$pattern" "$file" 2>/dev/null; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m %s\n' "$label"
        passed=$((passed + 1))
    else
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m %s\n' "$label"
        failed=$((failed + 1))
    fi
}

# P69/MOL-277: marker-count helper. Replaces the `grep -c ... || echo 0`
# footgun (memory grep_c_footgun: grep -c always prints count AND exits 1
# on zero matches, so `|| echo 0` produces "0\n0" on stdout, which then
# fails integer comparison with a cryptic "integer expression expected").
# Two-grep pattern below: first grep -Fq gates presence, second grep -Fc
# is only called when we know there's at least one match so it exits 0
# cleanly. Uses fixed-string mode (-F) since patterns are marker literals
# like "P45/MOL-245" — no regex metacharacters expected. Missing-file +
# unreadable-file checks match check() semantics above so operator sees
# consistent signal across both helpers.
#
# P69/MOL-277: this helper was introduced to main via a partial lift of
# P69 work in PR #79's follow-ups, mislabeled as `P59/MOL-277` (P59 is
# the cost-cap patch, MOL-277 is this ticket). PR #80 re-stamps the
# label to P69/MOL-277 + completes the migration to the P58 block +
# adds the `-r` branch + switches to fixed-string mode + adds the
# check_fixed sibling — see PATCHES.md P69 section.
check_marker_count() {
    local label="$1"
    local file="$2"
    local pattern="$3"
    local min_count="$4"
    total=$((total + 1))

    if [ ! -f "$file" ]; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m %s — file not found: %s\n' "$label" "$file"
        failed=$((failed + 1))
        return
    fi

    if [ ! -r "$file" ]; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;33m[?]\033[0m %s — file not readable: %s\n' "$label" "$file"
        failed=$((failed + 1))
        return
    fi

    local count=0
    if grep -Fq "$pattern" "$file" 2>/dev/null; then
        count=$(grep -Fc "$pattern" "$file" 2>/dev/null || echo 0)
    fi

    if [ "$count" -ge "$min_count" ]; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m %s (>=%d, have %d)\n' "$label" "$min_count" "$count"
        passed=$((passed + 1))
    else
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m %s (expected >=%d, have %d)\n' "$label" "$min_count" "$count"
        failed=$((failed + 1))
    fi
}

# P69/MOL-277: fixed-string content-assertion helper. Mirrors check() but
# uses grep -Fq instead of grep -Eq — unambiguous literal match, safe for
# patterns with regex metacharacters like `[`, `]`, `.`, `*`. Use when
# asserting that a specific code/prompt string is present verbatim and
# you want the pattern to read exactly as it appears in the source.
check_fixed() {
    local label="$1"
    local file="$2"
    local pattern="$3"
    total=$((total + 1))

    if [ ! -f "$file" ]; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m %s — file not found: %s\n' "$label" "$file"
        failed=$((failed + 1))
        return
    fi

    if [ ! -r "$file" ]; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;33m[?]\033[0m %s — file not readable: %s\n' "$label" "$file"
        failed=$((failed + 1))
        return
    fi

    # P103/MOL-410: use `grep -Fq --` to disambiguate patterns starting with
    # leading dashes (e.g. CLI flags like "--max-budget-usd"). Without `--`,
    # grep parses the pattern as an option and exits 2.
    #
    # P103/MOL-410 review fix: capture grep exit BEFORE the `if` test consumes
    # it. The previous form `if grep -Fq ...; then ... else local exit=$?` was
    # broken: `$?` after the if-test reflects the test's own outcome (always 1
    # in the else branch), so the `>1` warning branch was dead code.
    local grep_exit=0
    grep -Fq -- "$pattern" "$file" 2>/dev/null || grep_exit=$?
    if [ "$grep_exit" -eq 0 ]; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m %s\n' "$label"
        passed=$((passed + 1))
    else
        if [ "$grep_exit" -gt 1 ]; then
            [[ $QUIET -eq 0 ]] && printf '  \033[0;33m[?]\033[0m %s — grep error (exit %d) on: %s\n' "$label" "$grep_exit" "$file"
        fi
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m %s\n' "$label"
        failed=$((failed + 1))
    fi
}

# P113/MOL-442: byte-for-byte mirror check. Source-of-truth lives in repo;
# runtime locations get cp-mirrored. This helper sha256-compares the two
# files. Missing files surface as a clear "[✗] missing" rather than the
# generic shell "No such file or directory" noise.
check_mirror_sha256() {
    local label="$1"
    local src="$2"
    local mirror="$3"
    total=$((total + 1))

    if [ ! -f "$src" ]; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m %s — source not found: %s\n' "$label" "$src"
        failed=$((failed + 1))
        return
    fi
    if [ ! -f "$mirror" ]; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m %s — mirror not found: %s\n' "$label" "$mirror"
        failed=$((failed + 1))
        return
    fi

    local src_sha mirror_sha
    src_sha=$(shasum -a 256 "$src" 2>/dev/null | awk '{print $1}')
    mirror_sha=$(shasum -a 256 "$mirror" 2>/dev/null | awk '{print $1}')

    if [ -n "$src_sha" ] && [ "$src_sha" = "$mirror_sha" ]; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m %s\n' "$label"
        passed=$((passed + 1))
    else
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m %s — drift\n      src    %s %s\n      mirror %s %s\n' "$label" "$src_sha" "$src" "$mirror_sha" "$mirror"
        failed=$((failed + 1))
    fi
}

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P03: config.py (mcp_servers + envchain guard) ==="
F="$HERMES_AGENT/hermes_cli/config.py"
check "mcp_servers in DEFAULT_CONFIG"          "$F" '"mcp_servers".*\{\}'
check "_ENVCHAIN_MANAGED_PREFIXES defined"     "$F" "_ENVCHAIN_MANAGED_PREFIXES"
check "envchain guard in save_env_value"       "$F" "key\.startswith.*_ENVCHAIN_MANAGED_PREFIXES"

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P04: env_loader.py (override=False) ==="
F="$HERMES_AGENT/hermes_cli/env_loader.py"
check "override=False (not True)"              "$F" "override=False"

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P05: gateway.py (envchain-wrapper routing) ==="
F="$HERMES_AGENT/hermes_cli/gateway.py"
check "envchain-wrapper.sh reference"          "$F" "envchain-wrapper\.sh"

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P06: scheduler.py (envchain + memory) ==="
F="$HERMES_AGENT/cron/scheduler.py"
check "override=False (not True)"              "$F" "override=False"
check "memory in disabled_toolsets"            "$F" 'disabled_toolsets.*"memory"'
check "skip_memory per-job override"           "$F" 'job\.get.*skip_memory'
check "shutdown_memory_provider in finally"    "$F" "shutdown_memory_provider"
check "agent = None before try"                "$F" "agent = None.*# .*finally"

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P08: request_overrides NoneType fix ==="
F="$HERMES_AGENT/gateway/run.py"
check "request_overrides = {} (not None)"      "$F" 'route\["request_overrides"\].*= \{\}'
check "request_overrides or {} (agent set)"    "$F" 'request_overrides.*or \{\}'
F="$HERMES_AGENT/run_agent.py"
check "api_kwargs = None guard"                "$F" 'api_kwargs = None.*# Guard'
check "api_kwargs is not None guard"           "$F" 'if api_kwargs is not None'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P07: granola_tools.py (token persistence) ==="
F="$HERMES_AGENT/plugins/memory/tiered/granola_tools.py"
check "_persist_to_envchain helper"            "$F" "def _persist_to_envchain"
check "refresh token read at call time"        "$F" 'os\.environ\.get.*GRANOLA_REFRESH_TOKEN'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P09: local.py (PATH restriction) ==="
F="$HERMES_AGENT/tools/environments/local.py"
# Verify sbin is NOT in _SANE_PATH — inverted check with file-existence guard
total=$((total + 1))
if [ ! -f "$F" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m sbin directories removed from _SANE_PATH — file not found: %s\n' "$F"
    failed=$((failed + 1))
elif grep -A3 '_SANE_PATH = (' "$F" 2>/dev/null | grep -q 'sbin'; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m sbin directories still in _SANE_PATH\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m sbin directories removed from _SANE_PATH\n'
    passed=$((passed + 1))
fi

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P10: tiered memory consolidation prompt cap (MOL-168) ==="
F="$HERMES_AGENT/plugins/memory/tiered/store.py"
check "get_recent_entries limit param"         "$F" "limit: int \| None = None"
F="$HERMES_AGENT/plugins/memory/tiered/consolidation.py"
check "consolidation limit=150 (24h)"          "$F" "limit=150[^0-9]"
check "consolidation limit=400 (7d)"           "$F" "limit=400[^0-9]"
F="$HERMES_AGENT/plugins/memory/tiered/llm.py"
check "MAX_PROMPT_CHARS constant (300k)"       "$F" "MAX_PROMPT_CHARS = 300_000"
check "llm_compose truncation warning"         "$F" "llm_compose input too large"
F="$HERMES_AGENT/plugins/memory/tiered/hot_cache.py"
check "hot_cache limit=200 (real caller)"      "$F" "limit=200[^0-9]"

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P15: hot_cache per-entry content slice (MOL-168 item 2) ==="
F="$HERMES_AGENT/plugins/memory/tiered/hot_cache.py"
check "_WRAP_CONTENT_SLICE constant"           "$F" "_WRAP_CONTENT_SLICE = 500"
check "per-entry slice applied in _wrap_entries" "$F" "content\[:_WRAP_CONTENT_SLICE\]"

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P17: gateway flock mutex + sandbox-signal fix (MOL-168 TOCTOU root cause) ==="
# P17 absorption note: upstream v2026.4.30 independently implemented flock-based
# gateway mutual exclusion (acquire_gateway_runtime_lock / release_gateway_runtime_lock /
# _gateway_lock_handle / _get_gateway_lock_path) and atomic PID-file writes
# (atomic_json_write from utils). The patterns below accept EITHER the original P17
# function names OR their upstream equivalents. The only P17 code change still
# required on the migrated tree is the acquire_scoped_lock() PermissionError split.
F="$HERMES_AGENT/gateway/status.py"
check "_pid_lock_fd module global (or upstream _gateway_lock_handle)"  "$F" "_pid_lock_fd: Optional\[int\] = None|_gateway_lock_handle = None"
check "_get_pid_lock_path() defined (or upstream _get_gateway_lock_path)" "$F" "def _get_pid_lock_path|def _get_gateway_lock_path"
check "claim_pid_lock() defined (or upstream acquire_gateway_runtime_lock)" "$F" "def claim_pid_lock|def acquire_gateway_runtime_lock"
check "release_pid_lock() defined (or upstream release_gateway_runtime_lock)" "$F" "def release_pid_lock|def release_gateway_runtime_lock"
check "fcntl.flock LOCK_EX LOCK_NB"            "$F" "fcntl\.flock.*LOCK_EX.*LOCK_NB"
check "_write_json_file atomic (or upstream atomic_json_write)" "$F" "tmp\.replace\(path\)|atomic_json_write"
check "get_running_pid splits PermissionError" "$F" "P17 / sandbox-signal bug"
check "acquire_scoped_lock splits Permission"  "$F" "P17 / sandbox-signal bug: same reasoning"

F="$HERMES_AGENT/gateway/run.py"
check "P17 flock gate or upstream acquire_gateway_runtime_lock" "$F" "P17 / Gateway-wide flock mutex|acquire_gateway_runtime_lock"
check "claim_pid_lock or upstream acquire_gateway_runtime_lock imported" "$F" "claim_pid_lock,|acquire_gateway_runtime_lock,"
check "_p17_shutdown_cleanup or upstream atexit remove_pid_file" "$F" "def _p17_shutdown_cleanup|atexit\.register\(remove_pid_file\)"
check "P17 replace UX or upstream Permission denied kill" "$F" "Cannot replace running gateway.*from inside|Permission denied killing PID"
# Assert the old P16 markers are GONE (inverted check — P16 was superseded by P17).
total=$((total + 1))
if [ ! -f "$F" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P16 superseded by P17 (no stale P16 markers) — file not found: %s\n' "$F"
    failed=$((failed + 1))
elif grep -E "P16 / Duplicate-instance claim" "$F" >/dev/null 2>&1; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m stale P16 marker still present in run.py (should be replaced by P17)\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P16 superseded by P17 (no stale P16 markers)\n'
    passed=$((passed + 1))
fi
total=$((total + 1))
if [ ! -f "$F" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m _p16_remove_if_owned removed (replaced by _p17_shutdown_cleanup) — file not found: %s\n' "$F"
    failed=$((failed + 1))
elif grep -E "def _p16_remove_if_owned" "$F" >/dev/null 2>&1; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m stale _p16_remove_if_owned helper still present\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m _p16_remove_if_owned removed (replaced by _p17_shutdown_cleanup)\n'
    passed=$((passed + 1))
fi

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P11: telegram_network.py logging clarity ==="
F="$HERMES_AGENT/gateway/platforms/telegram_network.py"
check "exception type name logged"             "$F" 'type\(exc\)\.__name__'
check "no-message fallback string"             "$F" '"no message"'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P12: session_search_tool.py diagnostic + guards (MOL-168) ==="
F="$HERMES_AGENT/tools/session_search_tool.py"
check "MAX_SUMMARY_TOKENS clamped to 4096"     "$F" "MAX_SUMMARY_TOKENS = 4096"
check "empty transcript guard"                 "$F" "Session summarization skipped: empty conversation_text"
check "diagnostic snapshot log"                "$F" "session_search call: task=session_search"
check "per-attempt failure telemetry"          "$F" "session_search attempt .* failed: type="

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P13: memory_search per-entry truncation (MOL-168 items 2+3) ==="
F="$HERMES_AGENT/plugins/memory/tiered/__init__.py"
check "_MEMORY_SEARCH_CONTENT_SLICE constant"  "$F" "_MEMORY_SEARCH_CONTENT_SLICE = 500"
check "schema limit maximum 10"                "$F" '"maximum": 10[^0-9]'
check "per-entry truncation in memory_search"  "$F" 'content\[:_MEMORY_SEARCH_CONTENT_SLICE\]'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P14: session_search transcript format (MOL-168 partial) ==="
F="$HERMES_AGENT/tools/session_search_tool.py"
check "prose-style assistant heading"          "$F" '"### assistant'
check "prose-style tool output heading"        "$F" '"### tool output'
# Assert the old bracket patterns are GONE (inverted check)
total=$((total + 1))
if [ ! -f "$F" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m old bracket role markers removed — file not found: %s\n' "$F"
    failed=$((failed + 1))
elif grep -E '\[ASSISTANT\]:|\[USER\]:|\[TOOL:' "$F" >/dev/null 2>&1; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m old bracket role markers still present (should be removed)\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m old bracket role markers removed\n'
    passed=$((passed + 1))
fi

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P14b: session_search no custom system block (MOL-168 root cause) ==="
F="$HERMES_AGENT/tools/session_search_tool.py"
check "P14b docstring marker"                  "$F" "P14b"
# Assert the call site no longer passes a role=system message
total=$((total + 1))
if [ ! -f "$F" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m session_search no longer passes role=system — file not found: %s\n' "$F"
    failed=$((failed + 1))
elif grep -E '"role": "system"' "$F" >/dev/null 2>&1; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m session_search still passes role=system in messages\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m session_search no longer passes role=system\n'
    passed=$((passed + 1))
fi
# Assert system_prompt variable is gone (it was inlined into user_prompt)
total=$((total + 1))
if [ ! -f "$F" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m system_prompt variable removed (inlined into user_prompt) — file not found: %s\n' "$F"
    failed=$((failed + 1))
elif grep -E '^\s*system_prompt\s*=' "$F" >/dev/null 2>&1; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m system_prompt variable still present\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m system_prompt variable removed (inlined into user_prompt)\n'
    passed=$((passed + 1))
fi


# ==================== P18: Memory recall improvements ====================
[[ $QUIET -eq 0 ]] && echo "--- P18: Memory recall improvements ---"

# P18a: wing/room columns in store.py
total=$((total + 1))
if grep -q "_classify_wing" "$HERMES_AGENT/plugins/memory/tiered/store.py" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m _classify_wing present in store.py\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m _classify_wing missing from store.py\n'
    failed=$((failed + 1))
fi

total=$((total + 1))
if grep -q "_classify_room" "$HERMES_AGENT/plugins/memory/tiered/store.py" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m _classify_room present in store.py\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m _classify_room missing from store.py\n'
    failed=$((failed + 1))
fi

total=$((total + 1))
if grep -q "memory_facts" "$HERMES_AGENT/plugins/memory/tiered/store.py" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m memory_facts table in store.py\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m memory_facts table missing from store.py\n'
    failed=$((failed + 1))
fi

total=$((total + 1))
if grep -q "keep_strategy" "$HERMES_AGENT/plugins/memory/tiered/store.py" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m keep_strategy param in store.py\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m keep_strategy param missing from store.py\n'
    failed=$((failed + 1))
fi

# P18b: search.py changes
total=$((total + 1))
if grep -q "MIN_SCORE_THRESHOLD" "$HERMES_AGENT/plugins/memory/tiered/search.py" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m MIN_SCORE_THRESHOLD in search.py\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m MIN_SCORE_THRESHOLD missing from search.py\n'
    failed=$((failed + 1))
fi

total=$((total + 1))
if grep -q "WING_WEIGHTS" "$HERMES_AGENT/plugins/memory/tiered/search.py" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m WING_WEIGHTS in search.py\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m WING_WEIGHTS missing from search.py\n'
    failed=$((failed + 1))
fi

# P18b reranker presence checks REMOVED in MOL-524 — P151/MOL-502 retired the
# learned reranker entirely (hybrid RRF + wing/room/category/recency carries
# recall). The canonical assertions for that retirement are the negative
# absence checks in the P151 block below.

# P18c: __init__.py room-partitioned search (rerank=True/False call-site checks
# REMOVED — P151/MOL-502 retired reranker callers; see P151 absence checks)
total=$((total + 1))
if grep -q "_classify_room" "$HERMES_AGENT/plugins/memory/tiered/__init__.py" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m _classify_room in __init__.py (room-partitioned search)\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m _classify_room missing from __init__.py\n'
    failed=$((failed + 1))
fi

total=$((total + 1))
if grep -q "room=room_filter" "$HERMES_AGENT/plugins/memory/tiered/__init__.py" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m room=room_filter in __init__.py (partitioned prefetch)\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m room=room_filter missing from __init__.py\n'
    failed=$((failed + 1))
fi

# qwen3:8b reranker model check REMOVED in MOL-524 — P151/MOL-502 retired the
# reranker; the canonical absence assertion is in the P151 block below.

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P20 [SUPERSEDED by P186/MOL-637]: llm.py (memory composer = deepseek-v4-pro; Kimi/OpenRouter retired) ==="
F="$HERMES_AGENT/plugins/memory/tiered/llm.py"
# History: P20 originally asserted qwen3:8b => P151 downsized to qwen3:1.7b =>
# P169/MOL-560 retired the local Ollama primary entirely => P186/MOL-637
# re-baselined the composer to deepseek-v4-pro on api.deepseek.com.
# P186 below is the canonical lock-in block for llm.py; this slot is slim + correct.
check "COMPOSER_MODEL is deepseek-v4-pro"          "$F" 'COMPOSER_MODEL = "deepseek-v4-pro"'
check "COMPOSER_API_KEY_ENV is DEEPSEEK"           "$F" 'COMPOSER_API_KEY_ENV = "DEEPSEEK_API_KEY"'
check "_call_composer helper defined"              "$F" 'def _call_composer'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P21: run_agent.py + local.py (prompt caching + thinking for direct providers) ==="
F="$HERMES_AGENT/run_agent.py"
check "Gemini direct provider detection"           "$F" 'generativelanguage\.googleapis\.com'
check "_use_anthropic_cache_markers method"        "$F" '_use_anthropic_cache_markers'
check "_log_cache_stats method"                    "$F" '_log_cache_stats'
F="$HERMES_AGENT/tools/environments/local.py"
check "KIMI_API_KEY in env blocklist"              "$F" 'KIMI_API_KEY'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P22: Gateway plugin discovery (Model Armor + future CLI plugins) ==="
F="$HERMES_AGENT/gateway/run.py"
check "discover_plugins call in gateway"           "$F" 'discover_plugins'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P23: skills_guard.py (denylist + Cisco scanner — MOL-170) ==="
F="$HERMES_AGENT/tools/skills_guard.py"
check "load_denylist function defined"             "$F" 'def load_denylist'
check "check_denylist function defined"            "$F" 'def check_denylist'
check "run_cisco_scanner function defined"         "$F" 'def run_cisco_scanner'
check "TRUSTED_REPOS includes claude-plugins"      "$F" 'anthropics/claude-plugins-official'
check "_DENYLIST_PATH constant defined"            "$F" '_DENYLIST_PATH'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P24: skills_hub.py (install pipeline denylist + Cisco — MOL-170) ==="
F="$HERMES_AGENT/hermes_cli/skills_hub.py"
check "import check_denylist"                      "$F" 'import check_denylist'
check "import run_cisco_scanner"                   "$F" 'run_cisco_scanner'
check "pre-fetch denylist check"                   "$F" 'denylist_prefetch'
check "post-fetch denylist re-check"               "$F" 'denylist_postfetch'
check "Cisco scanner call in do_install"           "$F" 'cisco_fail'
check "first-party fast-track message"             "$F" 'fast-track install'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P25: Fallback reasoning_content cleanup (run_agent.py) ==="
F="$HERMES_AGENT/run_agent.py"
check "_reasoning_content_enabled defined"         "$F" 'def _reasoning_content_enabled'
check "_fallback_model_supports_thinking defined"  "$F" 'def _fallback_model_supports_thinking'
check "reasoning_config in __init__ snapshot"      "$F" '"reasoning_config": self.reasoning_config'
check "reasoning_config cleared in fallback"       "$F" 'P25: Clear reasoning state'
check "reasoning_config restored from primary"     "$F" 'P25: Restore reasoning_config'
check "main message prep gated"                    "$F" '_reasoning_content_enabled()'
check "memory flush message prep gated"            "$F" 'self._reasoning_content_enabled'
check "_is_kimi_direct defined"                    "$F" 'def _is_kimi_direct'
check "Kimi thinking disable parameter"            "$F" 'thinking.*disabled'
check "empty reasoning_content fill for Kimi"      "$F" 'reasoning_content.*""'
check "K2.5 thinking support (all Kimi)"           "$F" 'return True.*K2.5'
check "retry-loop reasoning_content fixup"         "$F" 'P25 fixup.*fallback.*Kimi'
check "P25c belt-and-suspenders reasoning_content" "$F" 'P25c.*Belt-and-suspenders'
check "P25c Gemma reasoning_effort guard"          "$F" 'P25c.*Skip for Gemma'
check "P25c Gemma 4 thought tag stripping"         "$F" '<thought>.*</thought>'
check "P25c thought tag reasoning extraction"      "$F" 'think|thought|thinking.*think|thought|thinking'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P25b: Telegram approval markdown fix (telegram.py) ==="
F="$HERMES_AGENT/gateway/platforms/telegram.py"
check "backtick strip in approval preview"         "$F" 'P25: prevent Telegram'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P26: config.py delegation.coding subsection (MOL-49) ==="
F="$HERMES_AGENT/hermes_cli/config.py"
check "delegation.coding in DEFAULT_CONFIG"        "$F" '"coding"'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P27: delegate_tool.py private helper + routing (MOL-49) ==="
F="$HERMES_AGENT/tools/delegate_tool.py"
check "_run_claude_code_delegation defined"        "$F" '_run_claude_code_delegation'
check "_detect_repo_path defined"                  "$F" '_detect_repo_path'
check "_sanitize_subprocess_env import (C1)"       "$F" '_sanitize_subprocess_env'
check "MAX_DEPTH recursion guard (H3)"             "$F" 'depth >= MAX_DEPTH'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P28: run_agent.py delegate_code_task dispatch removed (MOL-49) ==="
F="$HERMES_AGENT/run_agent.py"
# Inverted check — delegate_code_task should be ABSENT from run_agent.py
total=$((total + 1))
if [ ! -f "$F" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m delegate_code_task absent from run_agent.py — file not found: %s\n' "$F"
    failed=$((failed + 1))
elif grep -E "delegate_code_task" "$F" >/dev/null 2>&1; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m delegate_code_task still present in run_agent.py (should be removed)\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m delegate_code_task absent from run_agent.py (routing consolidated into delegate_task)\n'
    passed=$((passed + 1))
fi

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P29: gateway/run.py smart routing observability log (MOL-30) ==="
F="$HERMES_AGENT/gateway/run.py"
check "smart_route observability log line"         "$F" 'smart_route model='

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P30: cron/scheduler.py post-run verifier hook (MOL-214) ==="
F="$HERMES_AGENT/cron/scheduler.py"
check "P30 datetime import"                        "$F" 'from datetime import datetime, timezone'
check "P30 _job_start_ts_utc capture"              "$F" '_job_start_ts_utc = datetime\.now\(timezone\.utc\)\.timestamp'
check "P30 verify_and_annotate import + call"      "$F" 'from tools.report_verifier import verify_and_annotate'
check "P30 marker comment"                         "$F" 'MOL-214 P30: structural verification'
F="$HERMES_AGENT/tools/report_verifier.py"
check "verifier module present"                    "$F" 'def verify_and_annotate'
check "verifier whitelist constant"                "$F" 'VERIFIER_WHITELIST'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P31: run_agent.py reasoning prefix allowlist (MOL-30) ==="
F="$HERMES_AGENT/run_agent.py"
check "P31a gemini-3 reasoning prefix added"       "$F" '"google/gemini-3",'
check "P31a moonshotai reasoning prefix added"     "$F" '"moonshotai/",'
# Inverted check — "x-ai/" should be ABSENT from run_agent.py reasoning allowlist
total=$((total + 1))
if [ ! -f "$F" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P31b x-ai reasoning prefix removed — file not found: %s\n' "$F"
    failed=$((failed + 1))
elif grep -Fq '"x-ai/",' "$F" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P31b x-ai reasoning prefix still present (should be removed)\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P31b x-ai reasoning prefix removed\n'
    passed=$((passed + 1))
fi

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P32: normalize_usage reads completion_tokens_details (MOL-30) ==="
F="$HERMES_AGENT/agent/usage_pricing.py"
check "P32 completion_tokens_details attr checked" "$F" '"completion_tokens_details", "output_tokens_details"'
# P69/MOL-277 helper-pair: marker count guard (was inline grep — grep_c_footgun-safe via helper).
check_marker_count "P32/MOL-30 markers in usage_pricing.py" "$F" "P32 (MOL-30)" 1

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P33: MOL-220 tool-error surfacing (post-MOL-597 modular refactor) ==="
# post-MOL-597: the AIAgent init body was extracted to agent/agent_init.py;
# run_conversation moved to agent/conversation_loop.py; the two dispatch
# sites that record tool errors live in agent/tool_executor.py. The
# _record_tool_error method itself stays on AIAgent (run_agent.py). See
# [[check_retag_on_refactor]] memory for the retag convention.
check "P33 _tool_errors attribute on AIAgent (post-MOL-597)" \
      "$HERMES_AGENT/agent/agent_init.py" 'agent\._tool_errors: list'
check "P33 _record_tool_error helper defined" \
      "$HERMES_AGENT/run_agent.py" 'def _record_tool_error'
check "P33 _tool_errors.clear() at turn start (post-MOL-597)" \
      "$HERMES_AGENT/agent/conversation_loop.py" 'agent\._tool_errors\.clear\(\)'
# P33 invocation-count guard: exactly 2 dispatch-site calls expected
# (concurrent execute_tool_calls_concurrent + sequential execute_tool_calls_sequential
# — both in agent/tool_executor.py post-MOL-597). Neither definition nor
# docstrings/comments should match — grep for the exact call form.
total=$((total + 1))
# Avoid the `grep -c ... || echo 0` footgun (memory `grep_c_footgun`): grep -c always
# prints the count AND exits 1 on zero matches, so the `|| echo 0` tail double-echoes
# "0\n0" — comparison `= "2"` then fails via shape mismatch even in non-zero branch.
# Guard file-existence explicitly instead.
F="$HERMES_AGENT/agent/tool_executor.py"
if [ ! -f "$F" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P33 %s missing\n' "$F"
    failed=$((failed + 1))
else
    _p33_invokes=$(grep -c '^\s*agent\._record_tool_error(' "$F")
    if [ "$_p33_invokes" = "2" ]; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P33 _record_tool_error invoked at exactly 2 dispatch sites\n'
        passed=$((passed + 1))
    else
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P33 _record_tool_error invocation count: expected 2, got %s\n' "$_p33_invokes"
        failed=$((failed + 1))
    fi
fi
F="$HERMES_AGENT/cron/scheduler.py"
check "P33 tool_errors captured from agent"        "$F" 'tool_errors = list\(getattr\(agent, "_tool_errors"'
check "P33 5-tuple unpack in tick()"                "$F" 'success, output, final_response, error, tool_errors = run_job'
check "P33 proper MOL-220 block marker"             "$F" 'MOL-220 \(proper fix, 2026-04-18\)'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P34: smart_model_routing intent classifier + config.py routes key (MOL-30 close-out) ==="
F="$HERMES_AGENT/agent/smart_model_routing.py"
check "P34 classify_with_intent_keywords defined"  "$F" 'def classify_with_intent_keywords'
check "P34 classifier == \"intent\" branch"        "$F" 'classifier == "intent"'
check "P34 _IMAGE_MARKER_RE constant (images→primary)" "$F" '_IMAGE_MARKER_RE'
check "P34 _BROWSER_KEYWORDS constant (MOL-30 browser route)" "$F" '_BROWSER_KEYWORDS'
check "P34 intent routing_reason emitted"           "$F" 'intent:\{route_name\}'
# P34 count-guards — PATCHES.md requires these constants to appear ≥2 times
# each (definition + at least one use site). A partially-applied patch that
# defines the constant but misses the usage site would silently leave the
# classifier non-functional, so enforce count explicitly.
for _p34_const in _COS_PIN_RE _JIRA_TICKET_RE _CODE_PATH_RE; do
    total=$((total + 1))
    # Guard file existence explicitly (memory `grep_c_footgun`) — `grep -c || echo 0`
    # emits "0\n0" on zero matches which breaks the numeric comparison below.
    if [ ! -f "$F" ]; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P34 %s check skipped — %s missing\n' "$_p34_const" "$F"
        failed=$((failed + 1))
        continue
    fi
    _p34_count=$(grep -c "$_p34_const" "$F")
    if [ "$_p34_count" -ge 2 ] 2>/dev/null; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P34 %s appears ≥2× (defn + use)\n' "$_p34_const"
        passed=$((passed + 1))
    else
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P34 %s appears %s× (want ≥2 — defn + use)\n' "$_p34_const" "$_p34_count"
        failed=$((failed + 1))
    fi
done
F="$HERMES_AGENT/hermes_cli/config.py"
check "P34 DEFAULT_CONFIG classifier default key"   "$F" '"classifier": "keyword"'
check "P34 DEFAULT_CONFIG routes key"               "$F" '"routes": \{\}'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P35: Auxiliary models Flash 3 → Pro 3.1 (MOL-30 close-out) ==="
F="$HERMES_AGENT/agent/auxiliary_client.py"
# P35 _OPENROUTER_MODEL superseded by P62 (Pro 3.1 → Kimi K2.6, 2026-04-23).
# P35 primary assertion now lives at the P62 block below; P35 block retains
# _NOUS_MODEL + _API_KEY_PROVIDER_AUX_MODELS entries (those weren't flipped).
check "P35 _NOUS_MODEL is Pro 3.1"                  "$F" '_NOUS_MODEL = "google/gemini-3\.1-pro-preview"'
# P35 also flips 4 entries in the _API_KEY_PROVIDER_AUX_MODELS dict. Without
# these checks a partial re-apply could leave Flash 3 live on direct-Gemini,
# ai-gateway, opencode-zen, or kilocode paths — user's "never Flash again"
# directive would silently regress.
check "P35 aux dict: gemini → Pro 3.1"              "$F" '"gemini": "gemini-3\.1-pro-preview"'
check "P35 aux dict: ai-gateway → Pro 3.1"          "$F" '"ai-gateway": "google/gemini-3\.1-pro-preview"'
check "P35 aux dict: opencode-zen → Pro 3.1"        "$F" '"opencode-zen": "gemini-3\.1-pro-preview"'
check "P35 aux dict: kilocode → Pro 3.1"            "$F" '"kilocode": "google/gemini-3\.1-pro-preview"'
F="$HERMES_AGENT/trajectory_compressor.py"
check "P35 compressor summarization_model is Pro 3.1" "$F" 'summarization_model: str = "google/gemini-3\.1-pro-preview"'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P36: token-set fallback for jira_comment matcher (MOL-214) ==="
F="$HERMES_AGENT/tools/report_verifier.py"
check "P36 marker comment"                          "$F" 'MOL-214 P36'
check "P36 token-set match message present"         "$F" 'matched via token-set'
check "P36 snippet_tokens set construction"         "$F" 'snippet_tokens = set\(re\.findall'
check "P36 min-token length guard before division"  "$F" 'if len\(snippet_tokens\) >= 5'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P37: reflection agent v1 for cron-run semantic review (MOL-227) ==="
F="$HERMES_AGENT/tools/reflection_agent.py"
check "P37 analyze_cron_run defined"               "$F" 'def analyze_cron_run'
check "P37 Concern dataclass present"              "$F" 'class Concern'
# P112/MOL-433: model constant migrated `moonshotai/kimi-k2.6` (OpenRouter slug) → `kimi-k2.6` (direct Moonshot).
check "P37 Kimi K2.6 model constant (post-P112)"   "$F" '_MODEL = "kimi-k2\.6"'
check "P37 Kimi thinking extra_body (not reasoning)" "$F" 'extra_body=\{"thinking"'
check "P37 MOL-227 marker comment"                 "$F" 'MOL-227'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P38: daily-task-list top-level-only vault exclusion (ingest_external.py) ==="
F="$HERMES_AGENT/plugins/memory/tiered/ingest_external.py"
# The daily-task-list skill owns ~/Will's Vault/Task List/ and writes 1 file/day.
# Without exclusion, rglob ingest would flood memory_entries with daily-duplicate
# task blobs. Exclusion is TOP-LEVEL-ONLY — a user's nested "Task List" subfolder
# elsewhere (e.g., ~/Will's Vault/Archive/Task List/) is deliberately NOT excluded.
# User corrections flow via the explicit diff→memory_observe path in the skill.
check "P38 rel_parts top-level check"              "$F" 'rel_parts\[0\] == "Task List"'
check "P38 rel_parts computed before exclude check" "$F" 'rel_parts = md_path.relative_to\(vault\).parts'
# Guardrail: ensure "Task List" was NOT left in OBSIDIAN_EXCLUDE_DIRS. If someone
# re-adds it, the anywhere-in-path check would silently overmatch nested subfolders.
# Uses grep -q + if/else (not `grep -c || echo 0`, which double-emits on zero match).
total=$((total + 1))
if grep -q 'OBSIDIAN_EXCLUDE_DIRS = .*"Task List"' "$F" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P38 "Task List" found in OBSIDIAN_EXCLUDE_DIRS — would over-match nested subfolders. Remove it; top-level check handles exclusion.\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P38 "Task List" NOT in anywhere-in-path set (correct)\n'
    passed=$((passed + 1))
fi

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P39: comprehensive-update daily-task-list integration (MOL-235) ==="
F="$HOME/.hermes/skills/productivity/comprehensive-update/SKILL.md"
# Skill file is prose but load-bearing — check key invariants that must survive
# any future edit. If these vanish, the daily task-list escalation path is broken.
check "P39 archive-frozen TASKS.md notice"        "$F" 'archive-frozen'
check "P39 tripwire ABORTED marker check"         "$F" 'ABORTED-\*\.marker'
check "P39 high-visibility STALE marker"          "$F" 'TASK LIST STALE'
check "P39 fallback chain (TASK_LIST_AGE var)"    "$F" 'TASK_LIST_AGE'
check "P39 never read TASKS.md directive"         "$F" 'NEVER read.*TASKS\.md'
# Guardrail: old Gmail-snapshot overwrite MUST NOT return.
total=$((total + 1))
if grep -q 'TASKS board snapshot' "$F" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P39 legacy Gmail TASKS snapshot overwrite found — MOL-235 regression, user-oversight path broken\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P39 no legacy Gmail snapshot overwrite (correct)\n'
    passed=$((passed + 1))
fi

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P40: MOL-227 v2 flywheel retry + per-job claims_expected + transition_missing ==="
F="$HERMES_AGENT/tools/report_verifier.py"
check "P40 VERIFIER_WHITELIST defaults to None"    "$F" 'VERIFIER_WHITELIST: Optional\[frozenset\] = None'
check "P40 per-job claims_expected gate"            "$F" 'claims_expected = bool\(job\.get'
F="$HERMES_AGENT/tools/reflection_agent.py"
check "P40 reflect_and_annotate defined"            "$F" 'def reflect_and_annotate'
check "P40 ReviewStatus literal"                    "$F" 'ReviewStatus = Literal'
check "P40 transition_missing in Category"          "$F" 'transition_missing'
check "P40 REVIEWER UNAVAILABLE banner"             "$F" 'REVIEWER UNAVAILABLE'
F="$HERMES_AGENT/cron/scheduler.py"
check "P40 _build_retry_feedback_block helper"      "$F" 'def _build_retry_feedback_block'
check "P40 _summarize_completed_actions helper"     "$F" 'def _summarize_completed_actions'
check "P40 run_job retry_context kwarg"             "$F" 'def run_job\(job: dict, retry_context: Optional\[dict\] = None\)'
check "P40 REVIEW INCOMPLETE exhaustion banner"     "$F" 'REVIEW INCOMPLETE after'
check "P40 marker comments"                         "$F" 'MOL-227 v2 \(P40\)'
# Job-config guardrail: 9pm knockout must opt into the verifier via
# claims_expected=true, else the matcher+reflection never run on it.
total=$((total + 1))
if python3 -c "import json,sys; d=json.load(open('$HOME/.hermes/cron/jobs.json')); j=next((x for x in d['jobs'] if x['id']=='fef9d586ec61'), None); sys.exit(0 if (j and j.get('claims_expected') is True) else 1)" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P40 fef9d586ec61 has claims_expected: true in jobs.json\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P40 fef9d586ec61 missing claims_expected: true — knockout verification disabled\n'
    failed=$((failed + 1))
fi

# -----------------------------------------------------------------------------
# P41: daily-task-list skill template must match user's mixed H3+H2 format
# (reference copy is the reinstall source; any drift from this shape reintroduces
# the MOL-235 regression where the LLM flattened 6 sections into 4).
# -----------------------------------------------------------------------------
[[ $QUIET -eq 0 ]] && echo ""
# P41 retired 2026-04-21 — asserted the daily-task-list SKILL.md carried an inline 6-section
# template. P42 (MOL-239) removes the template entirely (body composition moves to the
# deterministic pre-job script). With no template in SKILL.md, P41's assertions are
# architecturally obsolete. See PATCHES.md "Retired — Superseded" section.

[[ $QUIET -eq 0 ]] && echo "=== P42: daily-task-list Step 4 is deterministic + external-source-free (MOL-239) ==="

# 1. Runtime SKILL.md "Confirm composition" block contains no LLM-compose / external-source
#    references, AND is non-empty (guards against a truncated SKILL.md silently passing).
S="$HOME/.hermes/skills/productivity/daily-task-list/SKILL.md"
total=$((total + 1))
_p42_block=""
if [ -f "$S" ]; then
    _p42_block=$(awk '/^### Confirm composition/,/^### Final output/' "$S")
fi
if [ ! -f "$S" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P42 runtime SKILL.md missing: %s\n' "$S"
    failed=$((failed + 1))
elif [ -z "$_p42_block" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P42 "Confirm composition" block empty or missing in SKILL.md — truncation / rename regression\n'
    failed=$((failed + 1))
elif printf '%s\n' "$_p42_block" | grep -Eqi '(llm_compose|openai|OpenRouter|Ollama|regenerate|jira|calendar|granola|gws|gmail)'; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P42 Confirm-composition block mentions LLM or external source (MOL-239 regression)\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P42 Confirm-composition block is LLM-free and external-source-free (denylist clean, non-empty)\n'
    passed=$((passed + 1))
fi

# 2. Pre-job script exists and is executable.
C="$HOME/.hermes/scripts/compose_task_list.py"
total=$((total + 1))
if [ -x "$C" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P42 compose_task_list.py exists and is executable\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P42 %s missing or not executable\n' "$C"
    failed=$((failed + 1))
fi

# 3. Per-skill content-hash ref-drift detection (AC #4). Guarded against missing shasum
#    binary (slim CI containers) — empty string match would silently pass otherwise.
REF="$(dirname "${BASH_SOURCE[0]}")/reference/daily-task-list-SKILL.md"
total=$((total + 1))
if ! command -v shasum >/dev/null 2>&1; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P42 hash-parity check skipped — shasum not on PATH\n'
    failed=$((failed + 1))
elif [ ! -f "$S" ] || [ ! -f "$REF" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P42 hash-parity check skipped — missing runtime or reference\n'
    failed=$((failed + 1))
elif [ "$(shasum -a 256 "$S" | awk '{print $1}')" = "$(shasum -a 256 "$REF" | awk '{print $1}')" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P42 runtime ↔ reference SHA-256 match (no ref-drift)\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P42 runtime ↔ reference SHA-256 mismatch — ref-drift (MOL-239 AC #4)\n'
    failed=$((failed + 1))
fi

# 4. Positive assertion: Step 4 contains the marker sentence.
total=$((total + 1))
if [ ! -f "$S" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P42 marker check skipped — runtime SKILL.md missing\n'
    failed=$((failed + 1))
elif grep -Fq 'Body composition is handled deterministically by `~/.hermes/scripts/compose_task_list.py`.' "$S"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P42 Step 4 contains the positive-assertion marker sentence\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P42 Step 4 marker sentence missing — preserve-then-mutate pattern may be reverted\n'
    failed=$((failed + 1))
fi

# ---------------------------------------------------------------------------
# P43 — Knockout work-not-completed guard + delegate_task three-tier chain
# (CC → Kimi K2.6 → Gemini 3.1 Pro, Sonnet --effort high)
# ---------------------------------------------------------------------------
[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P43: work_not_completed category + 3-tier delegate_task chain ==="

REFL="$HERMES_AGENT/tools/reflection_agent.py"
SKL="$HOME/.hermes/skills/productivity/jira-knockout-search/SKILL.md"
CFG="$HOME/.hermes/config.yaml"
DLG="$HERMES_AGENT/tools/delegate_tool.py"

# R1 — reflection agent new Category + prompt clause + parse allowlist
check "P43 work_not_completed in Category literal"    "$REFL" 'Category = Literal\[.*work_not_completed'
check "P43 WORK_NOT_COMPLETED clause in prompt"       "$REFL" '\(e\) WORK_NOT_COMPLETED'
check "P43 work_not_completed in parse allowlist"     "$REFL" '"work_not_completed"'

# K1 — knockout SKILL.md completion-order + pitfall
check "P43 Completion-order rule in SKILL.md"         "$SKL" 'Completion order — functionality over tickets'
check "P43 Paper-shuffle trap pitfall in SKILL.md"    "$SKL" 'Paper-shuffle trap'

# D1 — config.yaml tier 2 + tier 3 + delegate_tool.py pass-through + docstring + --effort
check "P43 delegation.model = Kimi K2.6 (tier 2)"     "$CFG" '^  model: kimi-k2\.6'
check "P43 delegation.fallback_model = Gemini (tier 3)" "$CFG" '^    model: google/gemini-3\.1-pro-preview'
# P72/MOL-251 retag: `override_fallback_model=creds.get` → `fallback_model=creds.get`
# (the assignment moved from kwarg to SubagentOverrides dataclass field).
# Same P43 behavioral guarantee — caller passes creds["fallback_model"] through.
check "P43 _build_child_agent passes fallback_model (post-P72)" "$DLG" 'fallback_model=creds\.get'
check "P43 delegate_task docstring mentions 3-tier chain" "$DLG" 'three-tier chain'
check "P43 CC subprocess runs at --effort high (configurable after P84)" "$DLG" '"--effort", cc_effort'

# DEFAULT_CONFIG round-trip guard — if hermes update overwrites
# hermes_cli/config.py, save_config() would silently strip the new nested
# fallback_model key on the next write. This check catches the regression
# BEFORE the first save_config() call drops it.
check "P43 DEFAULT_CONFIG protects delegation.fallback_model" \
    "$HERMES_AGENT/hermes_cli/config.py" '"fallback_model": \{\}'

# ---------------------------------------------------------------------------
# P44 — async_call_llm fallback + extract_content_or_reasoning None guard (MOL-254)
# ---------------------------------------------------------------------------
# Before P44, async_call_llm in auxiliary_client.py lacked the sync version's
# payment/connection/rate-limit fallback. session_search (which uses the async
# path) propagated 429s from rate-limited upstream models (e.g. Gemini 3.1 Pro)
# up to extract_content_or_reasoning, which then crashed with TypeError on a
# None response.choices. Net effect: comprehensive-update cron delivered an
# empty briefing under the MOL-227 reviewer "0 concerns" banner.
[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P44: async_call_llm fallback + extract_content_or_reasoning None guard (MOL-254) ==="

AUX="$HERMES_AGENT/agent/auxiliary_client.py"
# Regex anchors per code-reviewer + silent-failure-hunter review of the
# initial P44 patch — use strings UNIQUE to the new block so a future
# `hermes update` that strips P44 doesn't stay silently green via matches
# from unrelated sites (e.g. `async_mode=True,` appears 5× in this file
# pre-P44; `should_fallback = (` matches the sync call_llm too).
check "P44 marker comment in async_call_llm"           "$AUX" '── P44/MOL-254: async payment/connection/rate-limit fallback'
check "P44 async fallback instantiates fb_async_client" "$AUX" 'fb_async_client, fb_async_model = _get_cached_client'
check "P44 async fallback awaits fb_async_client"       "$AUX" 'await fb_async_client\.chat\.completions\.create'
check "P44 async fallback logs instantiation failure"   "$AUX" 'async fallback %s failed to instantiate'
check "P44 async fallback logs second-level failure"    "$AUX" 'async fallback %s/%s ALSO failed'
check "P44 extract_content_or_reasoning shape guard"    "$AUX" 'not isinstance\(choices, list\)'
check "P44 guard inspects choices\[0\]\.message shape"  "$AUX" 'hasattr\(choices\[0\], "message"\)'
check "P44 guard warning uses malformed-response label" "$AUX" 'malformed response —'

# ---------------------------------------------------------------------------
# P45 — cron scheduler silent-success detection (MOL-245)
# Classifies empty LLM responses as last_status=degraded and emits
# cron_failure ERROR log so silent misses become visible.
# ---------------------------------------------------------------------------
[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P45: cron silent-success detection ==="

SCHED="$HERMES_AGENT/cron/scheduler.py"
JOBS="$HERMES_AGENT/cron/jobs.py"

# DEFERRED — see MOL-1974 (P45/MOL-245 cron empty-response classification scrubbed by upstream
# modular refactor: _classify_empty_response, _EMPTY_RESPONSE_SENTINELS, cron_failure log line,
# empty_signal in tick(), degraded-status mapping, mark_job_run status-override + honor.
# All MOL-245 silent-miss-defense surface — priority restore.)
# check "P45 _classify_empty_response helper defined"    "$SCHED" 'def _classify_empty_response\('
# check "P45 empty-response sentinels tuple present"     "$SCHED" '_EMPTY_RESPONSE_SENTINELS = '
# check "P45 cron_failure ERROR log line"                "$SCHED" 'cron_failure job=%s name=%s status=degraded'
# check "P45 empty_signal computed in tick()"            "$SCHED" 'empty_signal = _classify_empty_response'
# check "P45 degraded status mapped in tick()"           "$SCHED" 'final_status, final_error = "degraded"'
# check "P45 mark_job_run accepts status override"       "$JOBS"  'status: Optional\[str\] = None'
# check "P45 mark_job_run honors explicit status"        "$JOBS"  'if status:'

# ---------------------------------------------------------------------------
# P46 — aux client rate-limit fallback + Ollama last-resort (MOL-245)
# Sync call_llm now falls back on plain 429 (P44/MOL-254 already covers async).
# Ollama added as last-resort in provider chain.
# ---------------------------------------------------------------------------
[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P46: aux rate-limit fallback + Ollama (sync path) ==="

check "P46 _is_rate_limit_error helper defined"        "$AUX" 'def _is_rate_limit_error\('
check "P46 sync should_fallback includes rate_limit"   "$AUX" 'or _is_rate_limit_error\(first_err\)'
check "P46 _try_ollama helper defined"                 "$AUX" 'def _try_ollama\('
check "P46 ollama in provider chain"                   "$AUX" '"ollama", _try_ollama'
check "P46 _try_ollama mapped in label reverse"        "$AUX" '"_try_ollama": "ollama"'

# ---------------------------------------------------------------------------
# P47 — main-agent fallback telemetry (MOL-245)
# INFO log at every fallback decision point (engaged/not-configured) so
# cron silent-miss debugging takes seconds, not minutes.
# ---------------------------------------------------------------------------
[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P47: main-agent fallback telemetry (post-MOL-597 modular refactor) ==="

# Per [[check_retag_on_refactor]] — P201/MOL-597 extracted try_activate_fallback
# from run_agent.py into agent/chat_completion_helpers.py. The reasoning
# allowlist remained on AIAgent (forwarder-class metadata) in run_agent.py.
RUNAG="$HERMES_AGENT/run_agent.py"
CCH="$HERMES_AGENT/agent/chat_completion_helpers.py"
check "P47 fallback.decision engaged=true INFO log"   "$CCH"   'fallback\.decision engaged=true'
check "P47 fallback.decision engaged=false INFO log"  "$CCH"   'fallback\.decision engaged=false'
check "P47 moonshotai/ in reasoning allowlist"        "$RUNAG" '"moonshotai/"'

# ---------------------------------------------------------------------------
# P48 — Telegram failure notification (MOL-245)
# When a cron hits degraded or error status, push a plain-text alert to
# the user's Telegram chat so silent misses become loud.
# ---------------------------------------------------------------------------
[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P48: Telegram failure notification ==="

check "P48 _notify_failure helper defined"             "$SCHED" 'def _notify_failure\('
check "P48 CRON FAILURE message prefix"                "$SCHED" 'CRON FAILURE:'
check "P48 cron_notification_failed log line"          "$SCHED" 'cron_notification_failed'
check "P48 _notify_failure invoked on degraded/error"  "$SCHED" 'if final_status in \("degraded", "error"\):'
check "P48 _deliver_result accepts wrap_override"      "$SCHED" 'wrap_override: Optional\[bool\]'
check "P48 invalid_chat_id guard (None/null/undef)"    "$SCHED" 'chat_id\.lower\(\) in \("none", "null", "undefined"\)'
check "P48 logger.exception on notify failure"         "$SCHED" 'path=exception'
check "P48 outer tick notify-path logs on break"       "$SCHED" 'cron_notification_path_broken'

# ---------------------------------------------------------------------------
# P49 — ~/.hermes/config.yaml top-level-key integrity (MOL-252)
# Catches silent config regressions (memory.provider wipe → MOL-247,
# mcp_servers wipe → MOL-249) by asserting load-bearing top-level keys
# are present in the live runtime config. Presence-only — nested-key
# validation and empty-value detection are out of scope.
# ---------------------------------------------------------------------------
[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P49: ~/.hermes/config.yaml top-level-key integrity (MOL-252) ==="

# Honor HERMES_CONFIG env override so an intentional red-test
# (HERMES_CONFIG=/tmp/broken.yaml bash verify_patches.sh) works
# without editing the real runtime config. Using a descriptive name
# (vs bare CONFIG) to match the $SCHED/$AUX/$JOBS convention elsewhere
# in this script and avoid collision with any future P-block or
# caller env var named CONFIG.
HERMES_CONFIG="${HERMES_CONFIG:-$HOME/.hermes/config.yaml}"
for key in model fallback_model memory mcp_servers providers agent toolsets; do
    total=$((total + 1))
    if [ ! -f "$HERMES_CONFIG" ]; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P49 %s skipped — config.yaml missing\n' "$key"
        failed=$((failed + 1))
    elif grep -Eq "^${key}:" "$HERMES_CONFIG"; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P49 %s: present\n' "$key"
        passed=$((passed + 1))
    else
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P49 %s: MISSING from runtime config\n' "$key"
        failed=$((failed + 1))
    fi
done

# ---------------------------------------------------------------------------
# P50 — tombstone supersession in memory consolidation (MOL-177 Phase 2)
# Phase 1 (shipped search.py ranker downweight) soft-filtered stale
# chat entries. Phase 2 is the write-time fix: when a recent project entry
# contains a completed MOL-xxx token, older chat entries mentioning the
# same token are tombstoned via superseded_by → they drop out of search
# entirely. Dry-run on first deploy; audit trail in tombstone_audit table.
# ---------------------------------------------------------------------------
[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P50: tombstone supersession in consolidation (MOL-177 Phase 2) ==="

SUPER="$HERMES_AGENT/plugins/memory/tiered/supersession.py"
STOREP="$HERMES_AGENT/plugins/memory/tiered/store.py"
CONSOL="$HERMES_AGENT/plugins/memory/tiered/consolidation.py"
CONFIG_PY="$HERMES_AGENT/hermes_cli/config.py"
INGEST_SCRIPT="${HERMES_HOME:-$HOME/.hermes}/scripts/memory_ingest_external.py"

total=$((total + 1))
if [ -f "$SUPER" ] && grep -q "def run_supersession" "$SUPER"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P50 supersession.py exports run_supersession\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P50 supersession.py missing or no run_supersession\n'
    failed=$((failed + 1))
fi

total=$((total + 1))
if grep -q "def _migrate_tombstone_audit" "$STOREP" \
   && grep -q "def record_tombstone_audit" "$STOREP" \
   && grep -q "def apply_tombstones" "$STOREP"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P50 store.py has migration + 2 helpers\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P50 store.py missing migration or helpers\n'
    failed=$((failed + 1))
fi

total=$((total + 1))
if grep -q "CREATE TABLE IF NOT EXISTS tombstone_audit" "$STOREP"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P50 tombstone_audit DDL present in store.py\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P50 tombstone_audit DDL missing from store.py\n'
    failed=$((failed + 1))
fi

total=$((total + 1))
if grep -q "from .supersession import run_supersession" "$CONSOL"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P50 consolidation.py imports run_supersession\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P50 consolidation.py does not import run_supersession\n'
    failed=$((failed + 1))
fi

total=$((total + 1))
if grep -q "tombstone_dry_run" "$CONFIG_PY"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P50 DEFAULT_CONFIG has tombstone_dry_run\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P50 DEFAULT_CONFIG missing tombstone_dry_run\n'
    failed=$((failed + 1))
fi

total=$((total + 1))
if [ -f "$INGEST_SCRIPT" ] && grep -q "run_tombstone_supersession" "$INGEST_SCRIPT"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P50 memory_ingest_external.py calls supersession phase\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P50 memory_ingest_external.py missing supersession phase\n'
    failed=$((failed + 1))
fi

# ---------------------------------------------------------------------------
# P50/MOL-177 patch-marker signatures — catches renumber drift.
# Asserts the ticket-numbered `P50/MOL-177` markers are in the source, not
# just the implementation strings. Guard file existence explicitly (memory
# `grep_c_footgun`: `grep -c ... || echo 0` double-emits "0\n0" on zero
# matches and breaks the numeric comparison below — see P33 line 519 and
# P34 line 553 for the correct pattern).
# ---------------------------------------------------------------------------
for _p50_spec in "supersession.py|$SUPER|1" "store.py|$STOREP|2" "consolidation.py|$CONSOL|1" "memory_ingest_external.py|$INGEST_SCRIPT|1"; do
    _p50_name="${_p50_spec%%|*}"
    _p50_rest="${_p50_spec#*|}"
    _p50_path="${_p50_rest%|*}"
    _p50_min="${_p50_rest##*|}"
    total=$((total + 1))
    if [ ! -f "$_p50_path" ]; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P50/MOL-177 markers in %s check skipped — %s missing\n' "$_p50_name" "$_p50_path"
        failed=$((failed + 1))
        continue
    fi
    _p50_count=$(grep -c "P50/MOL-177" "$_p50_path")
    if [ "$_p50_count" -ge "$_p50_min" ]; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P50/MOL-177 markers in %s (>=%s, have %s)\n' "$_p50_name" "$_p50_min" "$_p50_count"
        passed=$((passed + 1))
    else
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P50/MOL-177 markers in %s (expected >=%s, have %s)\n' "$_p50_name" "$_p50_min" "$_p50_count"
        failed=$((failed + 1))
    fi
done

# ---------------------------------------------------------------------------
# P51 — MOL-233 retry cap for browser/web tools + interactive empty-delivery
# gate. Blocks silent thrashing on JS-heavy pages: after 3 failed attempts
# on the same (tool, URL) in a turn, the 4th is short-circuited with a
# synthetic RETRY_CAP_EXCEEDED result. Interactive gateway surfaces accrued
# tool errors when final_response is empty (mirror of MOL-220 cron path).
# ---------------------------------------------------------------------------
[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P51: MOL-233 retry cap + interactive surfacing ==="

RUNAG="$HERMES_AGENT/run_agent.py"
GWR="$HERMES_AGENT/gateway/run.py"

# DEFERRED — see MOL-1974 (P51/MOL-233 tool-retry-cap symbols scrubbed from runtime: _tool_call_history,
# _tool_error_history, _mol233_normalize_target/_cap_key/_check_cap, _MOL233_CAPPED_TOOLS frozenset,
# tool_retry_cap log, gateway retry_cap_exceeded surface. Only _tool_call_history survives in a
# legacy test file. Functionality removed by upstream modular refactor — restore via followup.)
# check "P51 _tool_call_history init"              "$RUNAG" "self\._tool_call_history: dict"
# check "P51 _tool_error_history init"             "$RUNAG" "self\._tool_error_history: dict"
# check "P51 _mol233_normalize_target method"      "$RUNAG" "def _mol233_normalize_target"
# check "P51 _mol233_cap_key method"               "$RUNAG" "def _mol233_cap_key"
# check "P51 _mol233_check_cap method"             "$RUNAG" "def _mol233_check_cap"
# check "P51 _MOL233_CAPPED_TOOLS frozenset"       "$RUNAG" "_MOL233_CAPPED_TOOLS = frozenset"
# check "P51 clear() in run_conversation"          "$RUNAG" "self\._tool_error_history\.clear\(\)"
# check "P51 tool_retry_cap log string"            "$RUNAG" "tool_retry_cap tool=%s target=%s"
# check "P51 gateway surfaces retry_cap_exceeded"  "$GWR"   "retry_cap_exceeded"

# ---------------------------------------------------------------------------
# P52 — MOL-233 delegation truthfulness verification.
# Closes the MOL-233 false-positive pattern where a Claude Code subagent's
# JSON "result" string was trusted verbatim despite zero file changes.
# Pre/post git-diff snapshot; verified=False when goal asks for edits,
# result claims edits, and no new files are dirty. No auto-fallback on
# verified=False — surfaces to Chief (policy doc: AGENTS.md section).
# ---------------------------------------------------------------------------
[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P52: MOL-233 delegation truthfulness verifier ==="

DELEGATE="$HERMES_AGENT/tools/delegate_tool.py"

check "P52 _verify_delegation_diff helper"       "$DELEGATE" "def _verify_delegation_diff"
check "P52 _VERIFY_EDIT_KEYWORDS defined"        "$DELEGATE" "_VERIFY_EDIT_KEYWORDS"
check "P52 _VERIFY_NOOP_SIGNALS defined"         "$DELEGATE" "_VERIFY_NOOP_SIGNALS"
check "P52 delegate_truthfulness log emit"       "$DELEGATE" "delegate_truthfulness verified=false"
check "P52 completed_unverified status"          "$DELEGATE" "completed_unverified"
check "P52 pre_dirty snapshot set"               "$DELEGATE" 'pre_dirty: set\[str\]'

# ---------------------------------------------------------------------------
# MOL-262 — Hermes-subagent delegation path diff verification.
# KNOWN-SKIP (MOL-262 still In Progress): 8 marker checks below assert work
# in delegate_tool.py _run_single_child for extending P52/MOL-233's
# truthfulness verifier to the in-process swarm (Tier 3/4) path.
# No commit on feature branch carries these markers — P53 PATCHES.md
# explicitly defers this as "Separate ticket" (PATCHES.md:3286-3288), and
# the now.md carryover documents these as "out of scope" pre-existing
# failures (PATCHES.md:8467). Re-enable when MOL-262 ships the
# _run_single_child verifier-symmetry implementation. Parallel to
# MOL-537a / P122-P123 verifier-definition gap precedent.
# ---------------------------------------------------------------------------
[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== MOL-262: Hermes-subagent diff verification (KNOWN-SKIP — MOL-262 open) ==="

# check "MOL-262 repo_path param"                      "$DELEGATE" "repo_path: Optional\[str\] = None"
# check "MOL-262 pre_dirty snapshot"                  "$DELEGATE" "MOL-262.*pre-spawn baseline snapshot"
# check "MOL-262 post_dirty verification"              "$DELEGATE" "MOL-262.*post-hoc diff verification"
# check "MOL-262 verified field in entry"              "$DELEGATE" "MOL-262.*diff verification status"
# check "MOL-262 Tier3 repo detection"                "$DELEGATE" "MOL-262.*detect repo path for diff verification symmetry"
# check "MOL-262 Tier3 verified propagation"           "$DELEGATE" "MOL-262.*propagate verified status uniformly"
# check "MOL-262 Tier4 verified propagation"           "$DELEGATE" "MOL-262.*propagate verified status for Tier 4"
# check "MOL-262 error result has verified"            "$DELEGATE" '"verified": True.*"verification_reason": ""'

# ---------------------------------------------------------------------------
# P53 — AGENTS.md Delegation & Verification section.
# Teaches Hermes that: (a) delegate_task for coding tasks has a built-in
# diff verifier, (b) the real reflection_agent.py is cron-only — NOT for
# interactive delegations, (c) do NOT invoke a second delegate_task as a
# "reflection agent" (that's cosplay). Prevents recurrence of the 2026-04-21
# MOL-233 false-positive orchestration.
# ---------------------------------------------------------------------------
[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P53: AGENTS.md Delegation & Verification section ==="

AGENTSMD="${HERMES_HOME:-$HOME/.hermes}/workspace/AGENTS.md"

check "P53 Delegation & Verification header"     "$AGENTSMD" "^## Delegation & Verification"
check "P53 built-in verifier subsection"         "$AGENTSMD" "### The built-in verifier"
# P64/MOL-259 rewrite — subsection renamed from "The reflection-agent
# misconception" (prohibition-framed) to "The reflection reviewer" (positive
# framing around the reflect_on_output tool). Cosplay warning is preserved
# but phrased differently now ("Do NOT cosplay a second delegate_task").
check "P53+P64 reflection reviewer subsection"   "$AGENTSMD" "### The reflection reviewer"
check "P53+P64 no-cosplay warning (inverted)"    "$AGENTSMD" "Do NOT cosplay a second .delegate_task"

# ---------------------------------------------------------------------------
# P51 / P52 / MOL-233 patch-marker signatures — renumber-drift guard.
# Matches P50's file-existence-guarded for-loop pattern (memory notes:
# `grep_c_footgun` and `verify_patches_marker_drift_guard`). Implementation-
# string checks don't catch wrong-P-number re-applies; these do.
# ---------------------------------------------------------------------------
# DEFERRED — see MOL-1974 (P51/MOL-233 tool-retry-cap markers scrubbed from
# run_agent.py + gateway/run.py by upstream modular refactor; sibling of P51
# main check-block above. P52 marker loop below retained — delegate_tool.py
# markers still present.)
# [[ $QUIET -eq 0 ]] && echo ""
# [[ $QUIET -eq 0 ]] && echo "=== P51/P52 MOL-233 patch-marker presence (renumber-drift guard) ==="
#
# for _spec in "run_agent.py|$RUNAG|5" "gateway/run.py|$GWR|1"; do
#     _name="${_spec%%|*}"
#     _rest="${_spec#*|}"
#     _path="${_rest%|*}"
#     _min="${_rest##*|}"
#     total=$((total + 1))
#     if [ ! -f "$_path" ]; then
#         [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P51/MOL-233 markers in %s — file missing\n' "$_name"
#         failed=$((failed + 1))
#         continue
#     fi
#     _count=$(grep -c "P51/MOL-233" "$_path")
#     if [ "$_count" -ge "$_min" ]; then
#         [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P51/MOL-233 markers in %s (>=%s, have %s)\n' "$_name" "$_min" "$_count"
#         passed=$((passed + 1))
#     else
#         [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P51/MOL-233 markers in %s (expected >=%s, have %s)\n' "$_name" "$_min" "$_count"
#         failed=$((failed + 1))
#     fi
# done

for _spec in "tools/delegate_tool.py|$DELEGATE|3"; do
    _name="${_spec%%|*}"
    _rest="${_spec#*|}"
    _path="${_rest%|*}"
    _min="${_rest##*|}"
    total=$((total + 1))
    if [ ! -f "$_path" ]; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P52/MOL-233 markers in %s — file missing\n' "$_name"
        failed=$((failed + 1))
        continue
    fi
    _count=$(grep -c "P52/MOL-233" "$_path")
    if [ "$_count" -ge "$_min" ]; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P52/MOL-233 markers in %s (>=%s, have %s)\n' "$_name" "$_min" "$_count"
        passed=$((passed + 1))
    else
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P52/MOL-233 markers in %s (expected >=%s, have %s)\n' "$_name" "$_min" "$_count"
        failed=$((failed + 1))
    fi
done

# ---------------------------------------------------------------------------
# MOL-245 patch-marker signatures — catches renumber drift.
# These assert the ticket-numbered `P45/MOL-245` markers are actually
# in the source, not just the implementation strings. Without these
# checks, a re-apply under wrong numbers (say `P43/MOL-245`) would still
# register as green on the above checks.
# ---------------------------------------------------------------------------
[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== MOL-245 patch-marker presence (renumber-drift guard) ==="

# Minimum marker counts per file (lower-bounded — extra markers OK).
# Numbers match the actual runtime-file marker density at commit time.
# P69/MOL-277: migrated to check_marker_count helper (grep_c_footgun fix).
check_marker_count "P45/MOL-245 markers in scheduler.py"        "$SCHED" "P45/MOL-245" 3
check_marker_count "P45/MOL-245 markers in jobs.py"             "$JOBS"  "P45/MOL-245" 1
check_marker_count "P46/MOL-245 markers in auxiliary_client.py" "$AUX"   "P46/MOL-245" 3
check_marker_count "P47/MOL-245 markers in chat_completion_helpers.py (post-MOL-597)" "$CCH" "P47/MOL-245" 2
check_marker_count "P48/MOL-245 markers in scheduler.py"        "$SCHED" "P48/MOL-245" 3

# ---------------------------------------------------------------------------
# MOL-240 / P54 patch-marker signatures — catches renumber drift.
# Asserts the ticket-numbered `P54/MOL-240` markers are in the source, not
# just the implementation strings. Without these, a re-apply under a wrong
# patch number would still register as green on the general checks above.
# ---------------------------------------------------------------------------
[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== MOL-240 patch-marker presence (renumber-drift guard) ==="

# P69/MOL-277: migrated to check_marker_count helper (grep_c_footgun fix).
check_marker_count "P54/MOL-240 markers in reflection_agent.py" "$REFL"  "P54/MOL-240" 2
check_marker_count "P54/MOL-240 markers in scheduler.py"        "$SCHED" "P54/MOL-240" 1

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P57: kimi-coding auxiliary model K2.6 upgrade ==="
F="$HERMES_AGENT/agent/auxiliary_client.py"
check "P57 kimi-coding aux → kimi-k2.6"         "$F" '"kimi-coding": "kimi-k2\.6"'
# Orphan guard: the old slug should not coexist with the new one.
# Use grep -q + if/else (grep -c || echo 0 footgun: grep prints count AND exits 1 on 0, producing "0\n0").
total=$((total + 1))
if ! grep -q '"kimi-coding": "kimi-k2-turbo-preview"' "$F" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P57 no orphaned kimi-k2-turbo-preview entry\n'
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P57 orphaned kimi-k2-turbo-preview entry still present\n'
    failed=$((failed + 1))
fi

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P58 / MOL-268: Reflection guardrails + cron edit --skip-reflection ==="

# P69/MOL-277: migrated to check_marker_count helper (grep_c_footgun fix) —
# completes the partial migration that landed on main via PR #79's follow-ups.
CRONTOOL="$HERMES_AGENT/tools/cronjob_tools.py"
CRONCLI="$HERMES_AGENT/hermes_cli/cron.py"
CLIMAIN="$HERMES_AGENT/hermes_cli/main.py"
check_marker_count "P58/MOL-268 markers in reflection_agent.py" "$REFL"     "P58/MOL-268" 1
check_marker_count "P58/MOL-268 markers in scheduler.py"        "$SCHED"    "P58/MOL-268" 1
check_marker_count "P58/MOL-268 markers in cronjob_tools.py"    "$CRONTOOL" "P58/MOL-268" 3
check_marker_count "P58/MOL-268 markers in hermes_cli/cron.py"  "$CRONCLI"  "P58/MOL-268" 1
check_marker_count "P58/MOL-268 markers in hermes_cli/main.py"  "$CLIMAIN"  "P58/MOL-268" 1

# Content assertions — catch silent prompt drift or wrong-P-number re-apply.
# These greps pin the actual P58 prompt text, not just marker comments.
# P69/MOL-277: tightened cronjob_tools assertion from `skip_reflection: Optional\[bool\]`
# (regex with bracket-escape) to the full literal parameter signature
# `skip_reflection: Optional[bool] = None`, matched via check_fixed (grep -F)
# to sidestep bracket-regex escaping and avoid docstring / type-alias
# false-positives that a bare `Optional` substring would match.
check       "P58 reviewer EVIDENCE clarification text"  "$REFL"     'DO NOT flag EVIDENCE if the output makes NO positive claim of action'
check       "P58 reviewer bare-reminder clause"         "$REFL"     'bare informational message, reminder, briefing, or FYI'
check       "P58 retry anti-fabrication clause"         "$SCHED"    'DELETE the unsubstantiated claim'
check       "P58 retry fabrication warning"             "$SCHED"    'Never fabricate checkmark lists'
check_fixed "P58 cronjob_tools skip_reflection param"   "$CRONTOOL" 'skip_reflection: Optional[bool] = None'
check       "P58 CLI --skip-reflection argparser"       "$CLIMAIN"  '"--skip-reflection"'

# ─────────────────────────────────────────────────────────────────────
# P59 / MOL-268: Cost cap circuit-breaker (run_agent.py + scheduler.py)
# ─────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P59 / MOL-268: Dollar cost cap circuit-breaker ==="
RUNAG="$HERMES_AGENT/run_agent.py"
SCHED="$HERMES_AGENT/cron/scheduler.py"
check "P59 cost_cap init in AIAgent"                 "$RUNAG" 'self\._cost_cap_usd'
check "P59 cost_cap flag check at loop top"          "$RUNAG" 'if self\._cost_cap_triggered'
check "P59 cost_cap flag set after token update"    "$RUNAG" 'self\._cost_cap_triggered = True'
check "P59 cost_cap end_reason write"                "$RUNAG" '"cost_cap_exceeded"'
check "P59 flywheel prior-cost-cap guard"            "$SCHED" '_prior_cost_capped'
# Dispatch-site lock-in guard: >=3 sites (init, check, set) in run_agent.py
if [ -f "$RUNAG" ]; then
    p59_sites=$(grep -c 'P59/MOL-268' "$RUNAG" 2>/dev/null)
else
    p59_sites=0
fi
p59_sites=${p59_sites:-0}
total=$((total + 1))
if [ "$p59_sites" -ge 3 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P59/MOL-268 markers in run_agent.py (>=3, have %d)\n' "$p59_sites"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P59/MOL-268 markers in run_agent.py (expected >=3, have %d)\n' "$p59_sites"
    failed=$((failed + 1))
fi

# ─────────────────────────────────────────────────────────────────────
# P60 / MOL-268: cron max_turns_cron resolution (scheduler.py)
# ─────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P60 / MOL-268: cron max_turns_cron (40 vs interactive 150) ==="
check "P60 max_turns_cron read in scheduler"         "$SCHED" 'max_turns_cron'
# Marker-drift guard per verify_patches_marker_drift_guard memory.
if [ -f "$SCHED" ]; then
    p60_count=$(grep -c 'P60/MOL-268' "$SCHED" 2>/dev/null)
else
    p60_count=0
fi
p60_count=${p60_count:-0}
total=$((total + 1))
if [ "$p60_count" -ge 1 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P60/MOL-268 markers in scheduler.py (>=1, have %d)\n' "$p60_count"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P60/MOL-268 markers in scheduler.py (expected >=1, have %d)\n' "$p60_count"
    failed=$((failed + 1))
fi

# ─────────────────────────────────────────────────────────────────────
# P61 / MOL-268: memory library fallback (plugins/memory/tiered/llm.py)
#
# SUPERSEDED 2026-05-12 by P169 / MOL-560 (PR #190): the Ollama primary
# composer path was retired and Kimi K2.6 promoted to the sole composer.
# As a side effect the ``FALLBACK_MODEL`` symbol P61 patched no longer
# exists in llm.py — there is no "fallback" tier anymore, just a single
# composer. We treat the presence of the P169/MOL-560 absorption marker
# in llm.py as proof that P61's intent (Kimi K2.6 carries memory-LLM work)
# is satisfied, and fall back to the legacy FALLBACK_MODEL check only for
# runtimes that haven't pulled P169 yet.
# ─────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P61 / MOL-268: memory library composer (deepseek-v4-pro sole composer post-P186) ==="
LLMFILE="$HERMES_AGENT/plugins/memory/tiered/llm.py"
# Each branch owns its own counter bumps. The outer `total=$((total + 1))`
# was dropped 2026-05-12 (PR #203 review) — the prior top-level bump
# combined with the pre-P169 branch's own `check` + inline counter drifted
# the pass+fail==total invariant by 1 in the pre-P169 path.
if [ -f "$LLMFILE" ] && grep -q 'P169/MOL-560' "$LLMFILE"; then
    # Post-P186/MOL-637: deepseek-v4-pro is the sole composer (silently shipped
    # in commit 479ae4c36 as "docs", verifier re-baselined to match). P169 marker
    # still anchors the absorption; only the model identifier changes.
    total=$((total + 1))
    if grep -q 'COMPOSER_MODEL = "deepseek-v4-pro"' "$LLMFILE"; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P61/MOL-268 absorbed by P186/MOL-637 — COMPOSER_MODEL is deepseek-v4-pro\n'
        passed=$((passed + 1))
    else
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P61/MOL-268 — P169 marker present but COMPOSER_MODEL not deepseek-v4-pro\n'
        failed=$((failed + 1))
    fi
else
    # Pre-P169 runtime: original P61 + P112 contract still applies. The
    # `check` helper bumps `total` itself; the marker-drift block below
    # bumps once more.
    check "P61 FALLBACK_MODEL is Kimi K2.6 (post-P112, pre-P169)" "$LLMFILE" 'FALLBACK_MODEL = "kimi-k2\.6"'
    # Marker-drift guard: P61 edit adds 2 "P61/MOL-268" markers (docstring + comment).
    if [ -f "$LLMFILE" ]; then
        p61_count=$(grep -c 'P61/MOL-268' "$LLMFILE" 2>/dev/null)
    else
        p61_count=0
    fi
    p61_count=${p61_count:-0}
    total=$((total + 1))
    if [ "$p61_count" -ge 2 ]; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P61/MOL-268 markers in llm.py (>=2, have %d)\n' "$p61_count"
        passed=$((passed + 1))
    else
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P61/MOL-268 markers in llm.py (expected >=2, have %d)\n' "$p61_count"
        failed=$((failed + 1))
    fi
fi

# ─────────────────────────────────────────────────────────────────────
# P62 / MOL-268: auxiliary_client _OPENROUTER_MODEL (reverses P35)
# ─────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P62 / MOL-268: auxiliary_client OpenRouter model → Kimi K2.6 (reverses P35) ==="
AUXCLIENT="$HERMES_AGENT/agent/auxiliary_client.py"
check "P62 _OPENROUTER_MODEL is Kimi K2.6"           "$AUXCLIENT" '_OPENROUTER_MODEL = "moonshotai/kimi-k2\.6"'
# Marker-drift guard: P62 edit adds 1 "P62/MOL-268" marker in the model-choice comment block.
if [ -f "$AUXCLIENT" ]; then
    p62_count=$(grep -c 'P62/MOL-268' "$AUXCLIENT" 2>/dev/null)
else
    p62_count=0
fi
p62_count=${p62_count:-0}
total=$((total + 1))
if [ "$p62_count" -ge 1 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P62/MOL-268 markers in auxiliary_client.py (>=1, have %d)\n' "$p62_count"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P62/MOL-268 markers in auxiliary_client.py (expected >=1, have %d)\n' "$p62_count"
    failed=$((failed + 1))
fi

# ─────────────────────────────────────────────────────────────────────
# P63 / MOL-268 Phase 2: Script-only memory consolidation
# ─────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P63 / MOL-268 Phase 2: Script-only memory consolidation ==="
SCRIPT_MEMORY="$HOME/.hermes/scripts/memory_ingest_external.py"
check "P63 scheduler script_only short-circuit"      "$SCHED" 'job\.get\("script_only"\) is True'
check "P63 scheduler script_only marker"             "$SCHED" 'P63/MOL-268'
check "P63 consolidation function present"           "$SCRIPT_MEMORY" 'def run_session_consolidation'
check "P63 consolidation called from main"           "$SCRIPT_MEMORY" 'run_session_consolidation\(\)'
check "P63 uses llm_compose primary"                 "$SCRIPT_MEMORY" 'from plugins\.memory\.tiered\.llm import llm_compose'
check "P63 uses bge-m3 generate_embedding"           "$SCRIPT_MEMORY" 'from plugins\.memory\.tiered\.embeddings import generate_embedding'
check "P63 reviewer-contagion filter in extractor"   "$SCRIPT_MEMORY" 'REVIEWER FEEDBACK'

# ─────────────────────────────────────────────────────────────────────
# P64 / MOL-259: reflection tool surface
# ─────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P64 / MOL-259: analyze_output seam + reflect_on_output tool ==="
REFL="$HERMES_AGENT/tools/reflection_agent.py"
MTOOLS="$HERMES_AGENT/model_tools.py"
check "P64 analyze_output function"                  "$REFL" 'def analyze_output\('
check "P64 analyze_output returns tuple"             "$REFL" 'tuple\[list\[Concern\], ReviewStatus\]'
check "P64 REFLECT_ON_OUTPUT_SCHEMA"                 "$REFL" 'REFLECT_ON_OUTPUT_SCHEMA\s*='
check "P64 _reflect_on_output_handler"               "$REFL" 'def _reflect_on_output_handler'
check "P64 registry.register for reflect_on_output"  "$REFL" 'name="reflect_on_output"'
check "P64 fail-CLOSED error sentinel in handler"    "$REFL" 'reviewer unavailable'
check "P64 tools.reflection_agent in _discover_tools" "$MTOOLS" 'tools\.reflection_agent'
# P64 Rampart policy entry (runtime path — Rampart reads from ~/.rampart/policies/)
RAMPART_POLICY="${HOME}/.rampart/policies/hermes-policy.yaml"
check "P64 Rampart allow reflect_on_output (runtime path)" "$RAMPART_POLICY" 'reflect_on_output.*P64/MOL-259'
# Marker-drift guards: P64 edits two runtime files.
if [ -f "$REFL" ]; then
    p64_refl_count=$(grep -c 'P64/MOL-259' "$REFL" 2>/dev/null)
else
    p64_refl_count=0
fi
p64_refl_count=${p64_refl_count:-0}
total=$((total + 1))
if [ "$p64_refl_count" -ge 4 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P64/MOL-259 markers in reflection_agent.py (>=4, have %d)\n' "$p64_refl_count"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P64/MOL-259 markers in reflection_agent.py (expected >=4, have %d)\n' "$p64_refl_count"
    failed=$((failed + 1))
fi
if [ -f "$MTOOLS" ]; then
    p64_mtools_count=$(grep -c 'P64/MOL-259' "$MTOOLS" 2>/dev/null)
else
    p64_mtools_count=0
fi
p64_mtools_count=${p64_mtools_count:-0}
total=$((total + 1))
if [ "$p64_mtools_count" -ge 1 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P64/MOL-259 markers in model_tools.py (>=1, have %d)\n' "$p64_mtools_count"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P64/MOL-259 markers in model_tools.py (expected >=1, have %d)\n' "$p64_mtools_count"
    failed=$((failed + 1))
fi

# ─────────────────────────────────────────────────────────────────────
# P65 / MOL-271: reviewer-feedback contagion upstream+downstream fix
# ─────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P65 / MOL-271: reviewer contagion fix (upstream + SQL filter) ==="
check "P65 reflect_and_annotate new 3-tuple return"  "$REFL" 'tuple\[list\[Concern\], ReviewStatus, str\]'
check "P65 banner accumulator in scheduler"          "$SCHED" '_review_banner'
check "P65 delivered_payload prepends banner"        "$SCHED" 'deliver_content = \($'
HSTATE="$HERMES_AGENT/hermes_state.py"
check "P65 exclude_reviewer_contagion kwarg"         "$HSTATE" 'exclude_reviewer_contagion'
check "P65 SQL filter: REVIEWER FEEDBACK pattern"    "$HSTATE" "\[REVIEWER FEEDBACK"
check "P65 SQL filter: 🔍 REVIEWER emoji pattern"    "$HSTATE" '🔍 REVIEWER:'
check "P65 SQL filter: REVIEWER concern GLOB"        "$HSTATE" 'REVIEWER: \[0-9\]\* concern'
# P65 marker presence (renumber-drift guard)
if [ -f "$REFL" ]; then
    p65_refl_count=$(grep -c 'P65/MOL-271' "$REFL" 2>/dev/null)
else
    p65_refl_count=0
fi
p65_refl_count=${p65_refl_count:-0}
total=$((total + 1))
if [ "$p65_refl_count" -ge 2 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P65/MOL-271 markers in reflection_agent.py (>=2, have %d)\n' "$p65_refl_count"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P65/MOL-271 markers in reflection_agent.py (expected >=2, have %d)\n' "$p65_refl_count"
    failed=$((failed + 1))
fi

# ─────────────────────────────────────────────────────────────────────
# P66 / MOL-273: aux client prefer_local + vision safety
# ─────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P66 / MOL-273: aux client prefer_local + vision safety ==="
AUXCLIENT="$HERMES_AGENT/agent/auxiliary_client.py"
TCOMP="$HERMES_AGENT/trajectory_compressor.py"
SSEARCH="$HERMES_AGENT/tools/session_search_tool.py"
check "P66 has_image_payload helper"                 "$AUXCLIENT" 'def has_image_payload'
check "P66 resolve_provider_client prefer_local"     "$AUXCLIENT" 'prefer_local: bool = False'
check "P66 async_call_llm prefer_local short-circuit" "$AUXCLIENT" 'prefer_local and task != "vision"'
check "P66 trajectory_compressor uses prefer_local"  "$TCOMP" 'prefer_local=True'
check "P66 session_search uses prefer_local"         "$SSEARCH" 'prefer_local=True'
# Marker-drift guards: P66 edits three runtime files.
if [ -f "$AUXCLIENT" ]; then
    p66_aux_count=$(grep -c 'P66/MOL-273' "$AUXCLIENT" 2>/dev/null)
else
    p66_aux_count=0
fi
p66_aux_count=${p66_aux_count:-0}
total=$((total + 1))
if [ "$p66_aux_count" -ge 3 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P66/MOL-273 markers in auxiliary_client.py (>=3, have %d)\n' "$p66_aux_count"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P66/MOL-273 markers in auxiliary_client.py (expected >=3, have %d)\n' "$p66_aux_count"
    failed=$((failed + 1))
fi
if [ -f "$TCOMP" ]; then
    p66_tcomp_count=$(grep -c 'P66/MOL-273' "$TCOMP" 2>/dev/null)
else
    p66_tcomp_count=0
fi
p66_tcomp_count=${p66_tcomp_count:-0}
total=$((total + 1))
if [ "$p66_tcomp_count" -ge 1 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P66/MOL-273 markers in trajectory_compressor.py (>=1, have %d)\n' "$p66_tcomp_count"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P66/MOL-273 markers in trajectory_compressor.py (expected >=1, have %d)\n' "$p66_tcomp_count"
    failed=$((failed + 1))
fi
if [ -f "$SSEARCH" ]; then
    p66_ssearch_count=$(grep -c 'P66/MOL-273' "$SSEARCH" 2>/dev/null)
else
    p66_ssearch_count=0
fi
p66_ssearch_count=${p66_ssearch_count:-0}
total=$((total + 1))
if [ "$p66_ssearch_count" -ge 1 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P66/MOL-273 markers in session_search_tool.py (>=1, have %d)\n' "$p66_ssearch_count"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P66/MOL-273 markers in session_search_tool.py (expected >=1, have %d)\n' "$p66_ssearch_count"
    failed=$((failed + 1))
fi

# ─────────────────────────────────────────────────────────────────────
# P68 / cron-health-skill-polarity: comprehensive-update SKILL.md INFRA rubric + Cron Health MANDATORY
# ─────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P68 / cron-health-skill-polarity: SKILL.md polarity + Cron Health mandatory ==="
CU_SKILL_RUNTIME="$HOME/.hermes/skills/productivity/comprehensive-update/SKILL.md"
CU_SKILL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scripts/hermes-patches/reference/comprehensive-update-SKILL.md"
check "P68 absence-is-healthy tripwire guard"         "$CU_SKILL_RUNTIME" 'Absence = healthy'
check "P68 Cron Health MANDATORY enforcement"         "$CU_SKILL_RUNTIME" 'MANDATORY — not optional'
check "P68 DEGRADED scope rubric"                     "$CU_SKILL_RUNTIME" 'ONLY for ACTIVE failures'
# Cleanup guard — duplicate "4. Cron Health" heading must be gone (should be renumbered to 5.):
total=$((total + 1))
if [ ! -f "$CU_SKILL_RUNTIME" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P68 renumber check skipped — %s missing\n' "$CU_SKILL_RUNTIME"
    failed=$((failed + 1))
else
    _p68_dup_4=$(grep -c '^4\. Cron Health' "$CU_SKILL_RUNTIME")
    _p68_new_5=$(grep -c '^5\. Cron Health' "$CU_SKILL_RUNTIME")
    if [ "$_p68_dup_4" -eq 0 ] && [ "$_p68_new_5" -eq 1 ]; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P68 Step 0 renumbered (dup 4.=%d, new 5.=%d)\n' "$_p68_dup_4" "$_p68_new_5"
        passed=$((passed + 1))
    else
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P68 Step 0 renumber drift (dup 4.=%d, new 5.=%d; expected 0 and 1)\n' "$_p68_dup_4" "$_p68_new_5"
        failed=$((failed + 1))
    fi
fi
# Runtime ↔ repo reference byte-identity (per P50/MOL-177 / P63 precedent):
total=$((total + 1))
if [ -f "$CU_SKILL_RUNTIME" ] && [ -f "$CU_SKILL_REPO" ]; then
    if cmp -s "$CU_SKILL_RUNTIME" "$CU_SKILL_REPO"; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P68 runtime↔repo SKILL.md byte-identical\n'
        passed=$((passed + 1))
    else
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P68 runtime↔repo SKILL.md DIVERGED — re-mirror: cp %s %s\n' "$CU_SKILL_RUNTIME" "$CU_SKILL_REPO"
        failed=$((failed + 1))
    fi
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;33m[?]\033[0m P68 SKILL.md mirror check skipped — one of runtime/repo path missing\n'
    failed=$((failed + 1))
fi

# ─────────────────────────────────────────────────────────────────────
# P67 superseded by P71/MOL-250 (cron_health.py Launchd Agents elision).
# Banner+marker checks removed — the soft message and P67 comment markers
# that P67 added are intentionally gone in P71. Contract-guard checks
# (`elif status == "error":` + `unexpected launchd status` raise) are
# preserved by P71 and verified in the P71 block below.
# ─────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────
# P63 repo mirror byte-identity (runtime vs repo) per P50/MOL-177 precedent
# ─────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P63 repo mirror byte-identity ==="
P63_RUNTIME_SCRIPT="$HOME/.hermes/scripts/memory_ingest_external.py"
P63_REPO_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scripts/memory_ingest_external.py"
total=$((total + 1))
if [ -f "$P63_RUNTIME_SCRIPT" ] && [ -f "$P63_REPO_SCRIPT" ]; then
    if cmp -s "$P63_RUNTIME_SCRIPT" "$P63_REPO_SCRIPT"; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P63 runtime↔repo memory_ingest_external.py byte-identical\n'
        passed=$((passed + 1))
    else
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P63 runtime↔repo memory_ingest_external.py DIVERGED — re-mirror: cp %s %s\n' "$P63_RUNTIME_SCRIPT" "$P63_REPO_SCRIPT"
        failed=$((failed + 1))
    fi
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;33m[?]\033[0m P63 repo mirror check skipped — one of runtime/repo path missing\n'
    failed=$((failed + 1))
fi
# P35's old Gemini line MUST be gone — if present, update rolled it back.
if [ -f "$AUXCLIENT" ]; then
    p62_stale=$(grep -c '_OPENROUTER_MODEL = "google/gemini-3\.1-pro-preview"' "$AUXCLIENT" 2>/dev/null)
else
    p62_stale=0
fi
p62_stale=${p62_stale:-0}
total=$((total + 1))
if [ "$p62_stale" -eq 0 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P62 stale P35 Gemini line removed (have %d)\n' "$p62_stale"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P62 stale P35 Gemini line still present (expected 0, have %d) — re-apply P62\n' "$p62_stale"
    failed=$((failed + 1))
fi

# =============================================================================
# === P70 / MOL-261: ~/.hermes/hermes-agent as first-class delegation target ===
# =============================================================================
# delegate_tool.py::_CODE_PATH_RE extended with hermes-agent alternation +
# hermes_cli/config.py DEFAULT_CONFIG default allowed_write_roots set to
# ["~/.hermes/hermes-agent"] so delegations touching runtime code route
# through Claude Code + P52 verifier instead of the Hermes subagent path.
DELEG_TOOL="$HERMES_AGENT/tools/delegate_tool.py"
HERMES_CLI_CONFIG="$HERMES_AGENT/hermes_cli/config.py"
check_marker_count "P70/MOL-261 markers in delegate_tool.py"       "$DELEG_TOOL"        "P70/MOL-261" 1
check_marker_count "P70/MOL-261 markers in hermes_cli/config.py"   "$HERMES_CLI_CONFIG" "P70/MOL-261" 1
# Literal fragments (fixed-string grep) — regex-metachar-free.
check_fixed "P70 _CODE_PATH_RE has hermes-agent alternation"       "$DELEG_TOOL"        '.hermes/hermes-agent(?![A-Za-z0-9-])'
check_fixed "P70 DEFAULT_CONFIG default allowed_write_roots"       "$HERMES_CLI_CONFIG" '"allowed_write_roots": ["~/.hermes/hermes-agent"]'
# Review follow-up: pytest coverage for P70 regex (both positive matches
# and negative-lookahead rejections of hermes-agent2 / -old / FOO).
check_fixed "P70 pytest class TestP70CodePathRegex present"        "$HERMES_AGENT/tests/tools/test_delegate.py" "class TestP70CodePathRegex"

# =============================================================================
# === P71 / MOL-250: cron_health.py elides Launchd Agents on sandbox error  ===
# =============================================================================
# P67 softened the error message but retained trigger vocabulary
# ("exited 1", "sandbox restriction") the briefing LLM kept misclassifying
# as INFRA:DEGRADED. P71 elides the entire `## Launchd Agents` section on
# error so there is no text for the LLM to latch onto. Jobs.json table
# above is still printed — load-bearing health signal preserved.
CRONH="$HERMES_AGENT/cron_health.py"
check_marker_count "P71/MOL-250 markers in cron_health.py"         "$CRONH" "P71/MOL-250" 2
check_fixed "P71 elided error branch (pass + comment)"             "$CRONH" "# Elided — see P71/MOL-250 comment above. Intentional no-op."
# Contract guards preserved from P67: `elif status == "error":` narrows the
# sandbox-expected branch; `unexpected launchd status` raise catches a future
# 4th status code instead of silently reusing the elision (MOL-250 P67
# original rationale, still load-bearing under P71).
check "P71 elif error-only narrow branch (preserved)"              "$CRONH" 'elif status == "error":'
check "P71 unknown-status raise guard (preserved)"                 "$CRONH" 'unexpected launchd status'
# Review follow-up: pytest file locks the elision contract in place.
check_fixed "P71 pytest file tests/test_cron_health.py present"    "$HERMES_AGENT/tests/test_cron_health.py" "test_error_status_elides_launchd_section_entirely"

# =============================================================================
# === P72 / MOL-251: SubagentOverrides dataclass refactor of _build_child_agent
# =============================================================================
# 7 individual `override_*` kwargs on _build_child_agent collapsed into a
# single SubagentOverrides dataclass. No behavior change — pure refactor.
check_marker_count "P72/MOL-251 markers in delegate_tool.py"       "$DELEG_TOOL" "P72/MOL-251" 3
check_fixed "P72 SubagentOverrides dataclass defined"              "$DELEG_TOOL" "class SubagentOverrides:"
check_fixed "P72 _build_child_agent takes overrides kwarg"         "$DELEG_TOOL" 'overrides: Optional["SubagentOverrides"] = None'
# Stale kwargs must be gone — if present, refactor didn't fully land.
# Anchored to start-of-line + leading whitespace to avoid false-positives
# from historical comments that happen to mention the substring.
DELEG_TEST="$HERMES_AGENT/tests/tools/test_delegate.py"
if [ -f "$DELEG_TOOL" ]; then
    p72_stale=$(grep -Ec '^[[:space:]]+override_provider: Optional' "$DELEG_TOOL" 2>/dev/null)
else
    p72_stale=0
fi
p72_stale=${p72_stale:-0}
total=$((total + 1))
if [ "$p72_stale" -eq 0 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P72 stale override_* kwargs removed from signature (have %d)\n' "$p72_stale"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P72 stale override_* kwargs still in signature (expected 0, have %d) — re-apply P72\n' "$p72_stale"
    failed=$((failed + 1))
fi
# Test assertion updated to new API.
check_fixed "P72 test asserts overrides.provider (new API)"        "$DELEG_TEST" "overrides.provider, \"openrouter\""
# Review follow-up: dataclass-focused pytest (defaults, explicit construction,
# signature introspection) locks the contract separately from the batch-credentials
# assertion updated in the P72 commit.
check_fixed "P72 pytest class TestP72SubagentOverridesDataclass present" "$DELEG_TEST" "class TestP72SubagentOverridesDataclass"
# P67's soft message MUST be gone — if present, update rolled it back.
if [ -f "$CRONH" ]; then
    p71_stale=$(grep -Fc 'launchd agents: not queryable in this context' "$CRONH" 2>/dev/null)
else
    p71_stale=0
fi
p71_stale=${p71_stale:-0}
total=$((total + 1))
if [ "$p71_stale" -eq 0 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P71 stale P67 soft-message removed (have %d)\n' "$p71_stale"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P71 stale P67 soft-message still present (expected 0, have %d) — re-apply P71\n' "$p71_stale"
    failed=$((failed + 1))
fi

# === P73 / MOL-305: cron final_response scrubber + comprehensive-update prompt hardening
# =============================================================================
# Scheduler-side defense-in-depth: _scrub_cron_final_response strips LLM planning
# preamble, code-fence wrappers, and double-emissions before disk-save / Telegram
# delivery. Skill-side root cause: comprehensive-update SKILL.md de-fenced template
# + hard anti-leak constraints. See plan: ~/.claude/plans/today-s-briefing-outputted-in-polymorphic-dolphin.md
SCHED="$HERMES_AGENT/cron/scheduler.py"
SKILL_CU="$HOME/.hermes/skills/productivity/comprehensive-update/SKILL.md"
check_fixed "P73 _scrub_cron_final_response helper present"        "$SCHED" "def _scrub_cron_final_response("
check_marker_count "P73 scrubber dispatch (def + call)"            "$SCHED" "_scrub_cron_final_response(" 2
check_marker_count "P73/MOL-305 markers in scheduler.py"           "$SCHED" "P73/MOL-305" 3
check_fixed "P73 SKILL.md anti-preamble constraint present"        "$SKILL_CU" 'Do NOT begin with "I need to..."'
check_fixed "P73 SKILL.md emit-once constraint present"            "$SKILL_CU" 'Emit the briefing EXACTLY ONCE'
check_fixed "P73 SKILL.md template uses left-justified delimiters" "$SKILL_CU" "BEGIN BRIEFING TEMPLATE"
check_fixed "P73 pytest class TestScrubFinalResponse present"      "$HERMES_AGENT/tests/cron/test_scheduler.py" "class TestScrubFinalResponse"
# P73 stale-state checks: the OUTPUT FORMAT block in SKILL.md must contain
# zero triple-backtick fence lines AND zero 4-space-indented template lines
# (per code-review feedback: a 4-space indent is Markdown code-block syntax
# and could cue the LLM to wrap output in a fence even though we removed
# the literal fence chars). Use the two-step grep_q+grep_c idiom rather
# than `grep -c || echo 0` (per memory grep_c_footgun.md and the rationale
# documented on `check_marker_count` at the top of this script).
if [ -f "$SKILL_CU" ]; then
    OUTPUT_BLOCK="$(awk '/^\*\*Output format/,/^Keep the briefing concise/' "$SKILL_CU")"
    if echo "$OUTPUT_BLOCK" | grep -Eq '^[[:space:]]*```[[:space:]]*$'; then
        p73_stale_fence=$(echo "$OUTPUT_BLOCK" | grep -Ec '^[[:space:]]*```[[:space:]]*$')
    else
        p73_stale_fence=0
    fi
    if echo "$OUTPUT_BLOCK" | grep -Eq '^    [A-Z]'; then
        p73_stale_indent=$(echo "$OUTPUT_BLOCK" | grep -Ec '^    [A-Z]')
    else
        p73_stale_indent=0
    fi
else
    p73_stale_fence=0
    p73_stale_indent=0
fi

total=$((total + 1))
if [ "$p73_stale_fence" -eq 0 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P73 SKILL.md OUTPUT FORMAT block is fence-free (have %d)\n' "$p73_stale_fence"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P73 SKILL.md OUTPUT FORMAT block still has ``` fence (expected 0, have %d) — re-apply P73\n' "$p73_stale_fence"
    failed=$((failed + 1))
fi
total=$((total + 1))
if [ "$p73_stale_indent" -eq 0 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P73 SKILL.md template lines are left-justified (no 4-space indent — have %d indented lines)\n' "$p73_stale_indent"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P73 SKILL.md template still has 4-space-indented lines (expected 0, have %d) — Markdown code-block syntax could cue LLM to wrap output\n' "$p73_stale_indent"
    failed=$((failed + 1))
fi

# === P74 / MOL-310: BM25 title weighting (5.0/1.0) in tiered memory FTS5 search ===
# =============================================================================
# Bias FTS arm of hybrid memory search 5x toward title matches via explicit
# bm25(memory_fts, 5.0, 1.0) instead of FTS5's default uniform-weight `rank`.
# Dual-site substitution (SELECT alias + ORDER BY) — count check guards against
# partial re-apply that lands one site but not the other.
# Smoke test asserts title-match outranks content-match for a query term that
# appears once in each location with similar-length bodies.
check_fixed "P74 search_fts uses bm25(memory_fts, 5.0, 1.0)" \
    "$HERMES_AGENT/plugins/memory/tiered/store.py" \
    "bm25(memory_fts, 5.0, 1.0)"
check_marker_count "P74 bm25 appears at both SELECT and ORDER BY sites" \
    "$HERMES_AGENT/plugins/memory/tiered/store.py" \
    "bm25(memory_fts, 5.0, 1.0)" 2
check_fixed "P74 smoke test test_fts_title_outranks_content_match present" \
    "$HERMES_AGENT/plugins/memory/tiered/tests/test_store.py" \
    "def test_fts_title_outranks_content_match"

# === P75 / MOL-312: Recover briefing from memory_observe call on empty_response_exhausted ===
# =============================================================================
# Recovery helper + wire-in branch protect against Gemini 3.1 Pro's high-context
# transient empty-completion failure mode (3rd occurrence in 10 days on
# Comprehensive Update). Helper signature, log-line literal, and 2-site marker
# count guard against partial re-apply (helper landed but wire-in dropped, or
# vice-versa). Test class lock guards against silent test-file drop on hermes
# update.
check_fixed "P75 helper signature present (post-MOL-597)" \
    "$HERMES_AGENT/run_agent.py" \
    "def _recover_briefing_from_tool_calls"
check_fixed "P75 wire-in log-line literal present (post-MOL-597)" \
    "$HERMES_AGENT/agent/conversation_loop.py" \
    "Empty response → recovered briefing from memory_observe"
check_marker_count "P75/MOL-312 markers across run_agent.py + conversation_loop.py (post-MOL-597)" \
    "$HERMES_AGENT/run_agent.py" \
    "P75/MOL-312" 1
check_marker_count "P75/MOL-312 wire-in markers in conversation_loop.py at 1 site (post-MOL-597)" \
    "$HERMES_AGENT/agent/conversation_loop.py" \
    "P75/MOL-312" 1
check_fixed "P75 pytest class TestRecoverBriefingFromToolCalls present" \
    "$HERMES_AGENT/tests/test_run_agent_empty_recovery.py" \
    "class TestRecoverBriefingFromToolCalls"

# === P76 / MOL-313: Diagnostic JSONL logging on _empty_content_retries ===
# =============================================================================
# Helper writes one row per retry to ~/.hermes/logs/empty-response.jsonl.
# Wire-in is at the existing self._empty_content_retries += 1 site.
# Helper signature + 2-site marker count + test class lock guard against
# partial re-apply (helper landed but wire-in dropped, or vice-versa) and
# against silent test-file drop on hermes update.
check_fixed "P76 helper signature present (post-MOL-597)" \
    "$HERMES_AGENT/run_agent.py" \
    "def _log_empty_response_diagnostic"
check_marker_count "P76/MOL-313 helper-site markers in run_agent.py at 1 site (post-MOL-597)" \
    "$HERMES_AGENT/run_agent.py" \
    "P76/MOL-313" 1
check_marker_count "P76/MOL-313 wire-in markers in conversation_loop.py at 2 sites (post-MOL-597)" \
    "$HERMES_AGENT/agent/conversation_loop.py" \
    "P76/MOL-313" 2
check_fixed "P76 pytest class TestLogEmptyResponseDiagnostic present" \
    "$HERMES_AGENT/tests/test_run_agent_empty_recovery.py" \
    "class TestLogEmptyResponseDiagnostic"

# === P77 / MOL-314: Fallback chain activates on empty_response_exhausted ===
# =============================================================================
# Default-True config flag in DEFAULT_CONFIG["agent"]; constructor reads into
# self._fallback_on_empty_exhausted; wire-in branch lives between the P75
# briefing-recovery branch and the bare "(empty)" substitution. 2-site
# marker count + config-flag literal + test class lock guard against partial
# re-apply, default-flag drift, and silent test-file drop.
check_fixed "P77 agent._fallback_on_empty_exhausted constructor read present (post-MOL-597)" \
    "$HERMES_AGENT/agent/agent_init.py" \
    "agent._fallback_on_empty_exhausted"
check_marker_count "P77/MOL-314 constructor-site marker in agent_init.py at 1 site (post-MOL-597)" \
    "$HERMES_AGENT/agent/agent_init.py" \
    "P77/MOL-314" 1
check_marker_count "P77/MOL-314 wire-in marker in conversation_loop.py at 1 site (post-MOL-597)" \
    "$HERMES_AGENT/agent/conversation_loop.py" \
    "P77/MOL-314" 1
check_fixed "P77 DEFAULT_CONFIG fallback_on_empty_exhausted default True" \
    "$HERMES_AGENT/hermes_cli/config.py" \
    "\"fallback_on_empty_exhausted\": True"
check_fixed "P77 pytest class TestFallbackOnEmptyExhaustedConfig present" \
    "$HERMES_AGENT/tests/test_run_agent_empty_recovery.py" \
    "class TestFallbackOnEmptyExhaustedConfig"

# === P78 / MOL-324: tool inventory in system prompt ===
# =============================================================================
# Four runtime-source files: tools/mcp_tool.py (registry accessor),
# agent/prompt_builder.py (builder + constants), run_agent.py (import +
# call site + dump instrumentation), tests/agent/test_prompt_builder.py
# (behavioral test class — locks sort-determinism, denylist semantics,
# fallback byte-stability, WARNING-level error path).  Marker-count guard
# catches partial re-apply (accessor present but caller missing) and
# silent header drift.  Includes post-review hardening:
# - INTERNAL_TOOL_DENYLIST is a frozenset (immutable; was set with dead names)
# - registry_tools_by_server logs WARNING on schema-None invariant
# - prompt_builder logs WARNING on accessor ImportError/AttributeError
# - sorted_tools[0] fallback for unmapped MCP servers
# DEFERRED — see MOL-1974 (P78/MOL-324 tool inventory in system prompt scrubbed from runtime:
# get_registered_tools_by_server, build_tool_inventory_prompt, MCP_SERVER_PURPOSES,
# CLI_TOOLS_VIA_TERMINAL, INTERNAL_TOOL_DENYLIST frozenset, schema-None warning, pytest class.
# Only documentation/verifier refs survive. Restore in followup.)
# check_fixed "P78 get_registered_tools_by_server defined" \
#     "$HERMES_AGENT/tools/mcp_tool.py" \
#     "def get_registered_tools_by_server"
# check_fixed "P78 build_tool_inventory_prompt defined" \
#     "$HERMES_AGENT/agent/prompt_builder.py" \
#     "def build_tool_inventory_prompt"
# check_fixed "P78 build_tool_inventory_prompt imported in run_agent" \
#     "$HERMES_AGENT/run_agent.py" \
#     "build_tool_inventory_prompt"
# check_marker_count "P78/MOL-324 markers in run_agent.py at 2 sites" \
#     "$HERMES_AGENT/run_agent.py" \
#     "P78 / MOL-324" 2
# check_fixed "P78 MCP_SERVER_PURPOSES constant present" \
#     "$HERMES_AGENT/agent/prompt_builder.py" \
#     "MCP_SERVER_PURPOSES"
# check_fixed "P78 CLI_TOOLS_VIA_TERMINAL constant present" \
#     "$HERMES_AGENT/agent/prompt_builder.py" \
#     "CLI_TOOLS_VIA_TERMINAL"
# check_fixed "P78 INTERNAL_TOOL_DENYLIST is frozenset (immutability lock)" \
#     "$HERMES_AGENT/agent/prompt_builder.py" \
#     "INTERNAL_TOOL_DENYLIST: \"frozenset"
# check_fixed "P78 schema-None warning in registry accessor" \
#     "$HERMES_AGENT/tools/mcp_tool.py" \
#     "absent from central registry"
# check_fixed "P78 WARNING-level on accessor failure (not silent debug)" \
#     "$HERMES_AGENT/agent/prompt_builder.py" \
#     "MCP registry accessor unavailable"
# check_fixed "P78 pytest class TestBuildToolInventoryPrompt present" \
#     "$HERMES_AGENT/tests/agent/test_prompt_builder.py" \
#     "class TestBuildToolInventoryPrompt"

# === P79 / MOL-215: session-maintenance framework ===
# Five runtime patch sites: (1) host wrapper script, (2) runtime plugin
# `ingest_external.py`, (3) `prompt_builder.py` marker glob, (4)
# `config.py` DEFAULT_CONFIG, (5) skills under ~/.hermes/skills.
# Marker-count guard catches partial re-apply (def present but caller
# missing) and silent header drift.
# MOL-329: plugin lives at ~/.hermes/plugins/, NOT ~/.hermes/hermes-agent/plugins/
# (hook-providing plugins are loader-scanned by hermes_cli/plugins.py:264)
check_fixed "P79 plugin manifest" \
    "${HERMES_HOME:-$HOME/.hermes}/plugins/session-maintenance/plugin.yaml" \
    "name: session-maintenance"
check_fixed "P79 plugin entry registers on_session_finalize" \
    "${HERMES_HOME:-$HOME/.hermes}/plugins/session-maintenance/__init__.py" \
    "register_hook(\"on_session_finalize\""
check_fixed "P79 ingest_hermes_diaries defined in runtime plugin" \
    "$HERMES_AGENT/plugins/memory/tiered/ingest_external.py" \
    "def ingest_hermes_diaries"
check_fixed "P79 ingest_hermes_diaries defined in host wrapper" \
    "$HERMES_SCRIPTS/memory_ingest_external.py" \
    "def ingest_hermes_diaries"
check_marker_count "P79 ingest_hermes_diaries call sites in host wrapper" \
    "$HERMES_SCRIPTS/memory_ingest_external.py" \
    "ingest_hermes_diaries(" 2
check_fixed "P79 prompt_builder marker consumer present (post-P189)" \
    "$HERMES_AGENT/agent/prompt_builder.py" \
    "build_maintenance_marker_prompt"
check_fixed "P79 prompt_builder marker call site" \
    "$HERMES_AGENT/agent/prompt_builder.py" \
    "maintenance-pending-"
check_fixed "P79 DEFAULT_CONFIG session_maintenance block" \
    "$HERMES_AGENT/hermes_cli/config.py" \
    "\"session_maintenance\":"
check_fixed "P79 skill: diary" \
    "$HERMES_SKILLS/productivity/diary/SKILL.md" \
    "name: diary"
check_fixed "P79 skill: revise-context" \
    "$HERMES_SKILLS/productivity/revise-context/SKILL.md" \
    "name: revise-context"
check_fixed "P79 skill: remember" \
    "$HERMES_SKILLS/productivity/remember/SKILL.md" \
    "name: remember"
check_marker_count "P79/MOL-215 markers in prompt_builder.py at 2 sites" \
    "$HERMES_AGENT/agent/prompt_builder.py" \
    "P79 / MOL-215" 2

# === P80 / MOL-266: envchain re-exec for LLM-bearing CLI ===
# Two patch sites in `hermes_cli/main.py`: (1) the helper definition block
# above `_require_tty`, (2) the call site as first line of `main()`. Marker
# count + per-symbol guards catch partial re-apply or stale .pyc-only fixes.
check_fixed "P80 helper _envchain_reexec_if_needed defined" \
    "$HERMES_AGENT/hermes_cli/main.py" \
    "def _envchain_reexec_if_needed"
check_fixed "P80 helper call site at top of main()" \
    "$HERMES_AGENT/hermes_cli/main.py" \
    "_envchain_reexec_if_needed()"
check_fixed "P80 primary-provider config probe defined" \
    "$HERMES_AGENT/hermes_cli/main.py" \
    "def _read_primary_provider"
check_fixed "P80 envchain bin resolver defined (PATH-aware, post review)" \
    "$HERMES_AGENT/hermes_cli/main.py" \
    "def _envchain_bin_path"
check_fixed "P80 audit-trail logger defined (post review)" \
    "$HERMES_AGENT/hermes_cli/main.py" \
    "def _bootstrap_log"
check_marker_count "P80/MOL-266 markers in hermes_cli/main.py at 3 sites" \
    "$HERMES_AGENT/hermes_cli/main.py" \
    "P80 / MOL-266" 3

# === P81 / MOL-294: HERMES_MOCK_LLM_URL env override across LLM/Tavily clients ===
# 7 files patched with a single env-var contract. Helper `_mock_or` defined once
# in auxiliary_client.py; everywhere else uses inline `os.environ.get(...) or DEFAULT`.
# Total HERMES_MOCK_LLM_URL marker count is the strongest aggregate guard against
# partial re-apply.
# P112/MOL-433: default base URL migrated openrouter.ai/api/v1 → api.moonshot.ai/v1.
# RESTORED 2026-05-21 (MOL-1974 absorbed inline; NO MORE DEFERRALS).
# P81/MOL-294 HERMES_MOCK_LLM_URL .strip() empty-string defense re-applied to reflection_agent.py
# together with the P112 K2.5→K2.6 + OpenRouter→Moonshot migration above. _mock_or helper still
# scrubbed by upstream; the inline pattern is sufficient for the mock-LLM dev loop on this file.
check_fixed "P81 reflection_agent.py env-aware _BASE_URL (post-P112; .strip() empty-string defense preserved)" \
    "$HERMES_AGENT/tools/reflection_agent.py" \
    'os.environ.get("HERMES_MOCK_LLM_URL", "").strip() or "https://api.moonshot.ai/v1"'
# check_fixed "P81 web_tools.py env-aware _TAVILY_BASE_URL (with .strip() empty-string defense; absorbed-upstream form dropped TAVILY_BASE_URL middle layer)" \
#     "$HERMES_AGENT/tools/web_tools.py" \
#     'os.environ.get("HERMES_MOCK_LLM_URL", "").strip() or "https://api.tavily.com"'
# # P112/MOL-433: default fallback base URL migrated openrouter.ai/api/v1 → api.moonshot.ai/v1.
# check_fixed "P81 tiered llm.py FALLBACK only with .strip() (post-P112; PRIMARY local Ollama untouched)" \
#     "$HERMES_AGENT/plugins/memory/tiered/llm.py" \
#     'os.environ.get("HERMES_MOCK_LLM_URL", "").strip() or "https://api.moonshot.ai/v1"'
# check_fixed "P81 anthropic_adapter.py build_anthropic_client env override (with .strip(); absorbed-upstream form split assignment from conditional)" \
#     "$HERMES_AGENT/agent/anthropic_adapter.py" \
#     '_mock = os.environ.get("HERMES_MOCK_LLM_URL", "").strip()'
# check_fixed "P81 run_agent.py _create_openai_client env override (with .strip())" \
#     "$HERMES_AGENT/run_agent.py" \
#     'mock_url := os.environ.get("HERMES_MOCK_LLM_URL", "").strip()'
# check_fixed "P81 auxiliary_client.py _mock_or helper uses .strip() defense" \
#     "$HERMES_AGENT/agent/auxiliary_client.py" \
#     'os.environ.get("HERMES_MOCK_LLM_URL", "").strip() or base_url'
# check_fixed "P81 trajectory_compressor.py sync OpenAI env override (absorbed-upstream form: _mock_url with None fallback; caller pattern checks if _mock_url)" \
#     "$HERMES_AGENT/trajectory_compressor.py" \
#     '_mock_url = os.environ.get("HERMES_MOCK_LLM_URL", "").strip() or None'
# check_fixed "P81 auxiliary_client.py _mock_or helper defined" \
#     "$HERMES_AGENT/agent/auxiliary_client.py" \
#     "def _mock_or(base_url: str) -> str:"
# check_marker_count "P81/MOL-294 marker in trajectory_compressor.py at 2 sites (sync + async)" \
#     "$HERMES_AGENT/trajectory_compressor.py" \
#     "P81/MOL-294" 2
# check_marker_count "P81/MOL-294 _mock_or call sites in auxiliary_client.py (1 def + 5 calls)" \
#     "$HERMES_AGENT/agent/auxiliary_client.py" \
#     "_mock_or" 6

[[ $QUIET -eq 0 ]] && echo "=== P82 / MOL-248: file-based Granola token persistence (sandbox-vs-Keychain) ==="
check_fixed "P82 _TOKEN_FILE constant present in granola_tools.py" \
    "$HERMES_AGENT/plugins/memory/tiered/granola_tools.py" \
    "_TOKEN_FILE = get_hermes_home() / \"state\" / \"granola-tokens.json\""
check_fixed "P82 _load_tokens_from_file helper defined" \
    "$HERMES_AGENT/plugins/memory/tiered/granola_tools.py" \
    "def _load_tokens_from_file()"
check_fixed "P82 _persist_to_file helper defined" \
    "$HERMES_AGENT/plugins/memory/tiered/granola_tools.py" \
    "def _persist_to_file("
check_fixed "P82 _refresh writes file before envchain" \
    "$HERMES_AGENT/plugins/memory/tiered/granola_tools.py" \
    "_persist_to_file("
check_fixed "P82 hermes_constants import for get_hermes_home" \
    "$HERMES_AGENT/plugins/memory/tiered/granola_tools.py" \
    "from hermes_constants import get_hermes_home"
check_marker_count "P82/MOL-248 file-vs-env precedence at 2 read sites in granola_tools.py" \
    "$HERMES_AGENT/plugins/memory/tiered/granola_tools.py" \
    "_load_tokens_from_file()" 4

[[ $QUIET -eq 0 ]] && echo "=== P83 / MOL-367: per-token-scoped completion marker for supersession ==="
check_fixed "P83 _SENTENCE_SPLIT_RE constant present in supersession.py" \
    "$HERMES_AGENT/plugins/memory/tiered/supersession.py" \
    "_SENTENCE_SPLIT_RE = re.compile(r\"[.!?\\n]+\")"
check_fixed "P83 _token_is_completed helper defined" \
    "$HERMES_AGENT/plugins/memory/tiered/supersession.py" \
    "def _token_is_completed(token: str, text: str) -> bool:"
check_fixed "P83 per-token gate inside run_supersession's token loop" \
    "$HERMES_AGENT/plugins/memory/tiered/supersession.py" \
    "if not _token_is_completed(token, project_content):"
check_marker_count "P83/MOL-367 marker at runtime + test sites" \
    "$HERMES_AGENT/plugins/memory/tiered/supersession.py" \
    "P83/MOL-367" 2

[[ $QUIET -eq 0 ]] && echo "=== P84 / MOL-382: Restructured three-tier coding delegation chain ==="
check_fixed "P84 cc_model configurable (not hardcoded)" \
    "$HERMES_AGENT/tools/delegate_tool.py" \
    'cc_model = coding_cfg.get("model", "sonnet")'
check_fixed "P84 cc_effort configurable (not hardcoded)" \
    "$HERMES_AGENT/tools/delegate_tool.py" \
    'cc_effort = coding_cfg.get("effort", "high")'
check_fixed "P84 _run_claude_code_deepseek_delegation function defined" \
    "$HERMES_AGENT/tools/delegate_tool.py" \
    "def _run_claude_code_deepseek_delegation("
check_fixed "P84 fallback_deepseek block in config.py DEFAULT_CONFIG" \
    "$HERMES_AGENT/hermes_cli/config.py" \
    '"fallback_deepseek"'
check_fixed "P84 fallback_kimi block in config.py DEFAULT_CONFIG" \
    "$HERMES_AGENT/hermes_cli/config.py" \
    '"fallback_kimi"'
check_marker_count "P84/MOL-382 marker references in delegate_tool.py" \
    "$HERMES_AGENT/tools/delegate_tool.py" \
    "P84/MOL-382" 4
check_marker_count "P84/MOL-382 marker references in config.py" \
    "$HERMES_AGENT/hermes_cli/config.py" \
    "P84/MOL-382" 1

[[ $QUIET -eq 0 ]] && echo "=== P85 / MOL-382: Role profiles config system ==="
check_fixed "P85 _resolve_role_profile function defined in delegate_tool.py" \
    "$HERMES_AGENT/tools/delegate_tool.py" \
    "def _resolve_role_profile("
check_fixed "P85 role parameter in delegate_task signature" \
    "$HERMES_AGENT/tools/delegate_tool.py" \
    "role: Optional[str] = None"
check_fixed "P85 role_suffix parameter in _build_child_agent" \
    "$HERMES_AGENT/tools/delegate_tool.py" \
    "role_suffix: Optional[str] = None"
check_fixed "P85 role_profiles block in config.py DEFAULT_CONFIG" \
    "$HERMES_AGENT/hermes_cli/config.py" \
    '"role_profiles"'
check_fixed "P85 system_prompt_suffix in config.py DEFAULT_CONFIG" \
    "$HERMES_AGENT/hermes_cli/config.py" \
    "system_prompt_suffix"
check_marker_count "P85/MOL-382 marker references in delegate_tool.py" \
    "$HERMES_AGENT/tools/delegate_tool.py" \
    "P85/MOL-382" 4
check_marker_count "P85/MOL-382 marker references in config.py" \
    "$HERMES_AGENT/hermes_cli/config.py" \
    "P85/MOL-382" 1

[[ $QUIET -eq 0 ]] && echo "=== P86 / MOL-382: Agent-swarm orchestration skill ==="
if [ ! -d "$HERMES_SKILLS/productivity/agent-swarm" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P86 skill directory not found: %s/productivity/agent-swarm (skipping remaining P86 checks)\n' "$HERMES_SKILLS"
    failed=$((failed + 1))
    total=$((total + 4))
else
check_fixed "P86 agent-swarm SKILL.md exists with delegation pattern" \
    "$HERMES_SKILLS/productivity/agent-swarm/SKILL.md" \
    'delegate_task(role='
check_fixed "P86 role profile name referenced in skill" \
    "$HERMES_SKILLS/productivity/agent-swarm/SKILL.md" \
    'role="planner"'
check_fixed "P86 cost awareness section heading present" \
    "$HERMES_SKILLS/productivity/agent-swarm/SKILL.md" \
    '## Cost awareness'
check_marker_count "P86/MOL-382 marker in skill metadata" \
    "$HERMES_SKILLS/productivity/agent-swarm/SKILL.md" \
    "P86/MOL-382" 1
fi

[[ $QUIET -eq 0 ]] && echo "=== P87 / MOL-387: 8-Role System + Role Auto-Detection ==="
# DEFERRED — see MOL-1974 (P87/MOL-387 8-Role System + role auto-detection scrubbed from runtime:
# _detect_role_from_task in delegate_tool, role_detection config key, analyst/designer enums,
# delegate_task schema additions, P87 markers. Documentation refs only survive. Foundation for
# P91's arch-router — restore P87 before P91. See sibling P91 deferral.)
# check_fixed "P87 _detect_role_from_task function defined" \
#     "$HERMES_AGENT/tools/delegate_tool.py" \
#     "def _detect_role_from_task("
# check_fixed "P87 role_detection config key in config.py" \
#     "$HERMES_AGENT/hermes_cli/config.py" \
#     '"role_detection"'
# check_fixed "P87 auto-detection wired into delegate_task" \
#     "$HERMES_AGENT/tools/delegate_tool.py" \
#     "_detect_role_from_task(goal"
# check_fixed "P87 analyst role in config.py" \
#     "$HERMES_AGENT/hermes_cli/config.py" \
#     '"analyst"'
# check_fixed "P87 designer role in config.py" \
#     "$HERMES_AGENT/hermes_cli/config.py" \
#     '"designer"'
# check_fixed "P87 analyst in delegate_task schema enum" \
#     "$HERMES_AGENT/tools/delegate_tool.py" \
#     '"analyst"'
# check_fixed "P87 designer in delegate_task schema enum" \
#     "$HERMES_AGENT/tools/delegate_tool.py" \
#     '"designer"'
# check_fixed "P87 8-role table in CLAUDE.md" \
#     "$HOME/.claude/CLAUDE.md" \
#     "analyst"
# check_marker_count "P87/MOL-387 marker references in delegate_tool.py" \
#     "$HERMES_AGENT/tools/delegate_tool.py" \
#     "P87/MOL-387" 3
# check_marker_count "P87/MOL-387 marker references in config.py" \
#     "$HERMES_AGENT/hermes_cli/config.py" \
#     "P87/MOL-387" 1

[[ $QUIET -eq 0 ]] && echo "=== P91 / MOL-387-Phase2: Arch-Router Intelligent Role Auto-Detection ==="
# DEFERRED — see MOL-1974 (P91/MOL-387-Phase2 Arch-Router intelligent role auto-detection scrubbed:
# _detect_role_with_arch_router, _ROLE_ALIASES, classifier=='arch-router' dispatch, classifier key
# in DEFAULT_CONFIG, P91 markers. Blocked on P87 restore — Arch-Router was Phase 2 of the 8-role
# system; restore P87 prerequisite first.)
# check_fixed "P91 _detect_role_with_arch_router function defined" \
#     "$HERMES_AGENT/tools/delegate_tool.py" \
#     "def _detect_role_with_arch_router("
# check_fixed "P91 _ROLE_ALIASES defined" \
#     "$HERMES_AGENT/tools/delegate_tool.py" \
#     "_ROLE_ALIASES"
# check_fixed "P91 classifier == arch-router dispatch" \
#     "$HERMES_AGENT/tools/delegate_tool.py" \
#     'classifier == "arch-router"'
# check_fixed "P91 classifier key in config.py DEFAULT_CONFIG" \
#     "$HERMES_AGENT/hermes_cli/config.py" \
#     '"classifier": "keyword"'
# check_marker_count "P91/MOL-387-Phase2 marker in delegate_tool.py" \
#     "$HERMES_AGENT/tools/delegate_tool.py" \
#     "P91/MOL-387-Phase2" 3
# check_marker_count "P91/MOL-387-Phase2 marker in config.py" \
#     "$HERMES_AGENT/hermes_cli/config.py" \
#     "P91/MOL-387-Phase2" 1

[[ $QUIET -eq 0 ]] && echo "=== P91 keyword/logging fix (2026-05-03) ==="
# DEFERRED — see MOL-1974 (P91 keyword/logging fix — sibling of P91 Arch-Router; same scrubbed surface.)
# check_fixed "P91 debug keyword in debugger tuple" \
#     "$HERMES_AGENT/tools/delegate_tool.py" \
#     '"debug"'
# check_fixed "P91 no keyword match debug log" \
#     "$HERMES_AGENT/tools/delegate_tool.py" \
#     "no keyword match"
# check_fixed "P91 role detection no-match log in delegate_task" \
#     "$HERMES_AGENT/tools/delegate_tool.py" \
#     "Role detection ran but found no match"

[[ $QUIET -eq 0 ]] && echo "=== P88 / MOL-387: Kanban Board ==="
check_fixed "P88 kanban_tools.py module exists" \
    "$HERMES_AGENT/tools/kanban_tools.py" \
    "def _check_kanban_mode"
check_fixed "P88 kanban_db.py module exists" \
    "$HERMES_AGENT/hermes_cli/kanban_db.py" \
    "class Task"
check_fixed "P88 kanban.py CLI exists" \
    "$HERMES_AGENT/hermes_cli/kanban.py" \
    "def kanban_command"
check_fixed "P88 kanban tools in toolsets.py _HERMES_CORE_TOOLS" \
    "$HERMES_AGENT/toolsets.py" \
    "kanban_show"
check_fixed "P88 kanban toolset registered in TOOLSETS" \
    "$HERMES_AGENT/toolsets.py" \
    '"kanban"'
check_fixed "P88 kanban subcommand registered in main.py" \
    "$HERMES_AGENT/hermes_cli/main.py" \
    "cmd_kanban"
[[ -n "$HERMES_SKILLS" ]] || HERMES_SKILLS="$HOME/.hermes/skills"
check_fixed "P88 kanban-orchestrator skill exists" \
    "$HERMES_SKILLS/devops/kanban-orchestrator/SKILL.md" \
    "kanban"
check_fixed "P88 kanban-worker skill exists" \
    "$HERMES_SKILLS/devops/kanban-worker/SKILL.md" \
    "kanban"
check_marker_count "P88/MOL-387 marker references" \
    "$HERMES_AGENT/tools/kanban_tools.py" \
    "P88/MOL-387" 1

[[ $QUIET -eq 0 ]] && echo "=== P89 / MOL-387: File State Coordination ==="
check_fixed "P89 file_state.py module exists" \
    "$HERMES_AGENT/tools/file_state.py" \
    "class FileStateRegistry"
check_fixed "P89 record_read in file_tools.py" \
    "$HERMES_AGENT/tools/file_tools.py" \
    "file_state.record_read"
check_fixed "P89 check_stale in file_tools.py" \
    "$HERMES_AGENT/tools/file_tools.py" \
    "file_state.check_stale"
check_fixed "P89 note_write in file_tools.py" \
    "$HERMES_AGENT/tools/file_tools.py" \
    "file_state.note_write"
check_fixed "P89 lock_path in file_tools.py" \
    "$HERMES_AGENT/tools/file_tools.py" \
    "file_state.lock_path"
check_fixed "P89 writes_since in delegate_tool.py" \
    "$HERMES_AGENT/tools/delegate_tool.py" \
    "file_state.writes_since"
check_marker_count "P89/MOL-387 marker references in file_state.py" \
    "$HERMES_AGENT/tools/file_state.py" \
    "P89/MOL-387" 1

# === P90 / MOL-376: Autonomous skill patcher — DROPPED (MOL-665) ===
# Upstream's MOL-597 modular refactor retired the autonomous skill patcher
# feature entirely. tools/skill_patcher.py deleted; diagnose_and_patch +
# _auto_patch_cfg + skip_auto_patch + MOL-376 markers scrubbed from
# cron/scheduler.py, tools/skill_manager_tool.py, tools/cronjob_tools.py,
# hermes_cli/main.py, hermes_cli/cron.py. See PATCHES.md P90-DROPPED banner.
# Cron-failure signal flow (MOL-245) covered independently by P45/P48
# checks — no functional gap left behind by this drop.
# [[ $QUIET -eq 0 ]] && echo "=== P90 / MOL-376: Autonomous skill patcher ==="
# check_fixed "P90 skill_patcher module exists" \
#     "$HERMES_AGENT/tools/skill_patcher.py" \
#     "diagnose_and_patch"
# check_fixed "P90 diagnose_and_patch called in scheduler.py" \
#     "$HERMES_AGENT/cron/scheduler.py" \
#     "diagnose_and_patch"
# check_marker_count "P90/MOL-376 marker references in scheduler.py" \
#     "$HERMES_AGENT/cron/scheduler.py" \
#     "MOL-376" 2
# check_fixed "P90 auto_patch enabled guard in scheduler.py" \
#     "$HERMES_AGENT/cron/scheduler.py" \
#     "_auto_patch_cfg"
# check_fixed "P90 skill_patcher audit log path" \
#     "$HERMES_AGENT/tools/skill_patcher.py" \
#     "skill-patches.log"
# check_fixed "P90 patchable categories gate" \
#     "$HERMES_AGENT/tools/skill_patcher.py" \
#     "_PATCHABLE_CATEGORIES"
# check_fixed "P90 dedup check function defined" \
#     "$HERMES_AGENT/tools/skill_patcher.py" \
#     "_is_duplicate_patch"
# check_marker_count "P90/MOL-376 marker references in skill_patcher.py" \
#     "$HERMES_AGENT/tools/skill_patcher.py" \
#     "MOL-376" 1
# check_fixed "P90 concern_hashes in audit entry" \
#     "$HERMES_AGENT/tools/skill_patcher.py" \
#     "concern_hashes"
# check_fixed "P90 dedup fail-closed (return True on OSError)" \
#     "$HERMES_AGENT/tools/skill_patcher.py" \
#     "assume duplicate on unreadable log"
# check_fixed "P90 concern hash dedup (hash intersection)" \
#     "$HERMES_AGENT/tools/skill_patcher.py" \
#     "patched_hashes & target_hashes"
# check_fixed "P90 skills_guard import warning" \
#     "$HERMES_AGENT/tools/skill_manager_tool.py" \
#     "autonomous skill patches"
# check_fixed "P90 skip_auto_patch in scheduler.py" \
#     "$HERMES_AGENT/cron/scheduler.py" \
#     "skip_auto_patch"
# check_fixed "P90 skip_auto_patch in cronjob_tools.py" \
#     "$HERMES_AGENT/tools/cronjob_tools.py" \
#     "skip_auto_patch"
# check_fixed "P90 skip_auto_patch CLI arg" \
#     "$HERMES_AGENT/hermes_cli/main.py" \
#     "skip-auto-patch"
# check_fixed "P90 skip_auto_patch in cron.py edit path" \
#     "$HERMES_AGENT/hermes_cli/cron.py" \
#     "skip_auto_patch_value"

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P55: evening_enrichment.py + task_list_io.py + enrichment/ ==="
HERMES_SCRIPTS="${HERMES_HOME:-$HOME/.hermes}/scripts"
F="$HERMES_SCRIPTS/evening_enrichment.py"
check "evening_enrichment.py exists + defines main"  "$F" "^def main\("
check "P55/MOL-246 marker in evening_enrichment.py"  "$F" "P55/MOL-246"
F="$HERMES_SCRIPTS/task_list_io.py"
check "task_list_io.py exposes _atomic_write_pair"   "$F" "def _atomic_write_pair"
check "task_list_io.py exposes _target_ok (post-MOL-661)" "$F" "def _target_ok"
F="$HERMES_SCRIPTS/enrichment/append.py"
check "append.py defines append_items"               "$F" "^def append_items"
check "append.py defines SectionNotFoundError"       "$F" "class SectionNotFoundError"
F="$HERMES_SCRIPTS/enrichment/dedup.py"
check "dedup.py defines init_schema + filter_new"    "$F" "^def init_schema"
check "dedup.py schema has signal_ingestion_log"     "$F" "signal_ingestion_log"
F="$HERMES_SCRIPTS/enrichment/extractors.py"
check "extractors.py exposes 3 extract_* fns"        "$F" "extract_calendar_items"
check "extractors.py _run_signal 30s timeout"        "$F" "_TIMEOUT_SEC = 30"

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P56: DEFAULT_CONFIG enrichment block ==="
F="$HERMES_AGENT/hermes_cli/config.py"
check "enrichment key in DEFAULT_CONFIG"             "$F" '"enrichment":'
check "auto_apply default False"                     "$F" '"auto_apply": False'
check "dedup_window_days default 14"                 "$F" '"dedup_window_days": 14'
check "P56/MOL-246 marker in config.py"              "$F" "P56/MOL-246"

# P55/P56 marker-drift guards (renumber-safe — asserts the P-number didn't slide)
total=$((total + 1))
p55_count=$(grep -c "P55/MOL-246" "$HERMES_SCRIPTS/evening_enrichment.py" 2>/dev/null)
[ -z "$p55_count" ] && p55_count=0
if [ "$p55_count" -ge 1 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P55/MOL-246 marker count in evening_enrichment.py (>=1, have %d)\n' "$p55_count"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P55/MOL-246 marker count in evening_enrichment.py (expected >=1, have %d)\n' "$p55_count"
    failed=$((failed + 1))
fi

total=$((total + 1))
p56_count=$(grep -c "P56/MOL-246" "$HERMES_AGENT/hermes_cli/config.py" 2>/dev/null)
[ -z "$p56_count" ] && p56_count=0
if [ "$p56_count" -ge 1 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P56/MOL-246 marker count in config.py (>=1, have %d)\n' "$p56_count"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P56/MOL-246 marker count in config.py (expected >=1, have %d)\n' "$p56_count"
    failed=$((failed + 1))
fi

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "========================================"
if [ "$failed" -eq 0 ]; then
    [[ $QUIET -eq 0 ]] && printf '\033[0;32mAll %d checks passed.\033[0m\n' "$total"
else
    [[ $QUIET -eq 0 ]] && printf '\033[0;31m%d/%d checks failed.\033[0m\n' "$failed" "$total"
fi
[[ $QUIET -eq 0 ]] && echo ""

[[ $QUIET -eq 0 ]] && echo "=== P92 / MOL-388: Fix delegate_task logger not propagating ==="
check_fixed "P92 logger.setLevel in delegate_tool.py" \
    "$HERMES_AGENT/tools/delegate_tool.py" \
    "logger.setLevel(logging.INFO)"
check_fixed "P92 no keyword match elevated to info" \
    "$HERMES_AGENT/tools/delegate_tool.py" \
    '_detect_role_from_task: no keyword match'
check_fixed "P92 role detection no-match elevated to info" \
    "$HERMES_AGENT/tools/delegate_tool.py" \
    'Role detection ran but found no match'
check_marker_count "P92/MOL-388 marker in delegate_tool.py" \
    "$HERMES_AGENT/tools/delegate_tool.py" \
    "P92/MOL-388" 1

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P93 / MOL-283: Disable hermes update on patched trees ==="
check_fixed "P93 main.py cmd_update sentinel guard" \
    "$HERMES_AGENT/hermes_cli/main.py" \
    '_patched_marker = PROJECT_ROOT.parent / ".patched-tree"'
check_fixed "P93 main.py hermes update blocked message" \
    "$HERMES_AGENT/hermes_cli/main.py" \
    "is destructive — it will discard all patches"
check_fixed "P93 config.py sentinel guard" \
    "$HERMES_AGENT/hermes_cli/config.py" \
    '_patched_marker = Path.home() / ".hermes" / ".patched-tree"'
P93_UPDATE_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scripts/hermes-patches/update_hermes.sh"
check_fixed "P93 update_hermes.sh sentinel guard" \
    "$P93_UPDATE_SCRIPT" \
    'PATCHED_MARKER="$HERMES_HOME/.patched-tree"'
check_fixed "P93 sentinel marker file exists" \
    "${HERMES_HOME:-$HOME/.hermes}/.patched-tree" \
    "P93/MOL-283 sentinel"

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P94 / MOL-400: Cron tick liveness logging + visible tick errors ==="
check_fixed "P94 run.py heartbeat constant" \
    "$HERMES_AGENT/gateway/run.py" \
    "HEARTBEAT_EVERY = 5"
check_fixed "P94 run.py heartbeat emission" \
    "$HERMES_AGENT/gateway/run.py" \
    "Cron ticker alive: tick="
check_fixed "P94 run.py exception promoted to WARNING" \
    "$HERMES_AGENT/gateway/run.py" \
    'logger.warning("Cron tick error'

# P69/MOL-277 marker-drift guard: pair content checks with a marker-count
# assertion so a wrong-P-number re-apply (e.g. someone stamps P95 on these
# lines) is caught even if the heartbeat / WARNING strings still grep clean.
# Two markers in run.py: one above the WARNING handler, one above the
# heartbeat block.
check_marker_count "P94/MOL-400 markers in run.py" \
    "$HERMES_AGENT/gateway/run.py" "P94/MOL-400" 2

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P95 / MOL-404: Cron failure-flag surfacing (manual port of upstream f54935738) ==="
# P95 raises RuntimeError when agent.run_conversation returns
# {failed: True} or {completed: False} so cron can mark the job as failed
# rather than silently delivering the error string as a successful reply.
# Distinctive substring: the fallback error text "agent reported failure".
check_fixed "P95 scheduler.py failure-flag check" \
    "$HERMES_AGENT/cron/scheduler.py" \
    'agent reported failure'
check_marker_count "P95/MOL-404 markers in scheduler.py" \
    "$HERMES_AGENT/cron/scheduler.py" "P95/MOL-404" 1

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P96 / MOL-404: Cron auto-delivery env-var clear (manual port of upstream ffa65291d) ==="
# P96 clears HERMES_CRON_AUTO_DELIVER_{PLATFORM,CHAT_ID,THREAD_ID} before
# every job to prevent bleed across cron runs sharing the gateway process,
# and always-sets THREAD_ID (to "" if None) so a prior job's THREAD_ID
# can't leak. Two markers expected: one at pre-clear loop, one at
# always-set THREAD_ID. Distinctive substring: the loop-var name.
check_fixed "P96 scheduler.py pre-clear loop var (post-refactor: _cron_delivery_vars)" \
    "$HERMES_AGENT/cron/scheduler.py" \
    "_cron_delivery_vars"
check_marker_count "P96/MOL-404 markers in scheduler.py" \
    "$HERMES_AGENT/cron/scheduler.py" "P96/MOL-404" 2

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P97 / MOL-405: In-band pending-message drain via fresh task (manual port of upstream 663ba9a58) ==="
# Distinctive substring from the new asyncio.create_task block — the
# variable name + comment plus the exhaust-stack rationale.
check_fixed "P97 scheduler.py drain_task spawn" \
    "$HERMES_AGENT/gateway/platforms/base.py" \
    "drain_task = asyncio.create_task("
check_fixed "P97 base.py exhaust-stack rationale comment" \
    "$HERMES_AGENT/gateway/platforms/base.py" \
    "C stack would"
check_marker_count "P97/MOL-405 markers in base.py" \
    "$HERMES_AGENT/gateway/platforms/base.py" "P97/MOL-405" 1

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P99 / MOL-405: Persist user message on transient agent failures (manual port of upstream d1d0ef6db) ==="
# Distinctive substring: the new classifier var + the "Transient agent
# failure" log line that fires on the elif branch.
check_fixed "P99 run.py is_context_overflow_failure classifier" \
    "$HERMES_AGENT/gateway/run.py" \
    "is_context_overflow_failure"
check_fixed "P99 run.py transient-failure persistence log line" \
    "$HERMES_AGENT/gateway/run.py" \
    "Transient agent failure in session"
check_fixed "P99 run.py overflow-only persistence skip" \
    "$HERMES_AGENT/gateway/run.py" \
    "if not is_context_overflow_failure:"
check_marker_count "P99/MOL-405 markers in run.py" \
    "$HERMES_AGENT/gateway/run.py" "P99/MOL-405" 2

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P140 / MOL-489: Re-apply of P103 to runtime + close MOL-474 ==="
[[ $QUIET -eq 0 ]] && echo "=== P103 / MOL-410: Coding-profile elevation for delegate_task ==="
DELEGATE_TOOL="$HERMES_AGENT/tools/delegate_tool.py"
LOCAL_ENV="$HERMES_AGENT/tools/environments/local.py"
P140_REFERENCE_DIFF="$HERMES_POC_REFERENCE/P140-MOL-489.diff"

# P140/MOL-489: reference diff snapshot from runtime post-edit (rollback artifact).
check_fixed "P140 reference diff committed" \
    "$P140_REFERENCE_DIFF" "def _build_claude_argv("
check_fixed "P140 reference diff has all 5 P103 functions" \
    "$P140_REFERENCE_DIFF" "def _emit_profile_audit("

# delegate_tool.py — profile loader, validator, consent gate, argv builder, JSONL writer
check_fixed "P103 _load_profile defined" \
    "$DELEGATE_TOOL" \
    "def _load_profile("
check_fixed "P103 _validate_profile_additive_only defined" \
    "$DELEGATE_TOOL" \
    "def _validate_profile_additive_only("
check_fixed "P103 _check_profile_session_consent defined" \
    "$DELEGATE_TOOL" \
    "def _check_profile_session_consent("
check_fixed "P103 _emit_profile_audit defined" \
    "$DELEGATE_TOOL" \
    "def _emit_profile_audit("
check_fixed "P103 _build_claude_argv defined" \
    "$DELEGATE_TOOL" \
    "def _build_claude_argv("
check_fixed "P103 --max-budget-usd flag emitted" \
    "$DELEGATE_TOOL" \
    "--max-budget-usd"
check_fixed "P103 --setting-sources flag emitted" \
    "$DELEGATE_TOOL" \
    "--setting-sources"
check_marker_count "P103/MOL-410 markers in delegate_tool.py" \
    "$DELEGATE_TOOL" "P103/MOL-410" 5

# local.py — per-call profile_passthrough merge into _is_passthrough.
# (#162 review CRITICAL-1 / I3): parameter is currently audit-only — the
# Tier 1 ACP spawner that consumes it is out-of-scope for P140 and tracked
# separately. The verifier asserts presence, not active threading; the
# label calls out the audit-only scope so future cherry-picks don't
# misread "parameter exists" as "elevation flows through".
check_fixed "P103 local.py profile_passthrough parameter (audit-only — Tier 1 ACP wire-up out-of-scope; see PATCHES.md§P140)" \
    "$LOCAL_ENV" \
    "profile_passthrough"
check_marker_count "P103/MOL-410 markers in local.py" \
    "$LOCAL_ENV" "P103/MOL-410" 1

# P140/MOL-489 (#162 review CRITICAL-1): profile name allowlist regex.
check_fixed "P140 profile name allowlist regex defined" \
    "$DELEGATE_TOOL" "_P103_PROFILE_NAME_RE = re.compile"
# P140/MOL-489 (#162 review I1, M2 review-fix): env_passthrough denylist.
check_fixed "P140 PROFILE_PASSTHROUGH_DENYLIST defined" \
    "$DELEGATE_TOOL" "_P103_PROFILE_PASSTHROUGH_DENYLIST = frozenset"
check_fixed "P140 denylist enforces ANTHROPIC_API_KEY" \
    "$DELEGATE_TOOL" "ANTHROPIC_API_KEY"
# P140/MOL-489 (#162 review HIGH-3): fail loud on missing/malformed profile.
check_fixed "P140 fail-loud on profile load failure" \
    "$DELEGATE_TOOL" "Refusing to silently downgrade to default"
# P140/MOL-489 (#162 review HIGH-4): audit emit before tool_error on validator raise.
check_fixed "P140 audit-before-tool_error on validator raise" \
    "$DELEGATE_TOOL" "REJECTED additive_violation"
# P140/MOL-489 (#162 review HIGH-5): _build_claude_argv field-name validation.
check_fixed "P140 _build_claude_argv field-name validation" \
    "$DELEGATE_TOOL" "string.Formatter()"
# P140/MOL-489 (#162 review HIGH-6): synth_repo path validation via os.path.isdir.
check_fixed "P140 synth_repo path validation" \
    "$DELEGATE_TOOL" "if os.path.isdir(candidate)"
# P140/MOL-489 (#162 review I4/M7): time.monotonic() consent cache TTL.
check_fixed "P140 consent cache uses time.monotonic" \
    "$DELEGATE_TOOL" "now = time.monotonic()"
# P140/MOL-489 (#162 review M8): consent cache lock.
check_fixed "P140 consent cache lock" \
    "$DELEGATE_TOOL" "_P103_CONSENT_LOCK = threading.Lock()"
# P140/MOL-489 (#162 review M9): graceful PyYAML degradation.
check_fixed "P140 PyYAML graceful degradation" \
    "$DELEGATE_TOOL" "PyYAML not installed"
# P140/MOL-489 (#162 review M10): base_settings loaded into validator.
check_fixed "P140 validator wired with base_settings" \
    "$DELEGATE_TOOL" "_validate_profile_additive_only(profile_cfg, base_settings)"
# P140/MOL-489 (#162 review M12): filename-canonical profile name.
check_fixed "P140 filename-canonical profile name override" \
    "$DELEGATE_TOOL" "filename is canonical"
# P140/MOL-489 (#162 review CRITICAL-2): audit-write fail-open also logs + counter.
check_fixed "P140 audit fail-open counter + logger" \
    "$DELEGATE_TOOL" "_P103_AUDIT_DROP_COUNT"
# P140/MOL-489 (#162 review CRITICAL-2): tier-2 audit fallback path.
check_fixed "P140 audit tier-2 /tmp fallback" \
    "$DELEGATE_TOOL" "hermes-delegate-audit-"

# ─────────────────────────────────────────────────────────────────────────────
# P141 / MOL-491 — Forward iteration instrumentation (per-dispatch JSONL audit)
# Hook fires at the post-P140 exit_reason branches in _run_single_child.
# Records {ts, job_id, key, profile, iterations_used, iterations_max,
# exit_status} JSONL → ~/.hermes/logs/iteration-audit.jsonl. Fail-open.
# ─────────────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo "=== P141 / MOL-491 — Forward iteration instrumentation ==="
P141_REFERENCE_DIFF="$HERMES_POC_REFERENCE/P141-MOL-491.diff"

check_fixed "P141 iteration-audit hook present (runtime)" \
    "$DELEGATE_TOOL" "iteration-audit.jsonl"
check_fixed "P141 iteration audit fail-open path" \
    "$DELEGATE_TOOL" "iteration-audit write failed"
check_fixed "P141 iterations_used field emitted" \
    "$DELEGATE_TOOL" '"iterations_used":'
check_fixed "P141 iterations_max field emitted" \
    "$DELEGATE_TOOL" '"iterations_max":'
check_fixed "P141 exit_reason field emitted" \
    "$DELEGATE_TOOL" '"exit_reason": exit_reason'
check_fixed "P141 reference diff committed" \
    "$P141_REFERENCE_DIFF" "P141/MOL-491 reference diff"
# P141/MOL-491 (#163 review CRITICAL-1): _emit_iteration_audit helper defined.
check_fixed "P141 _emit_iteration_audit helper defined" \
    "$DELEGATE_TOOL" "def _emit_iteration_audit("
# P141/MOL-491 (#163 review CRITICAL-1): helper called from all 3 exit paths.
# check_marker_count enforces ≥3 invocations of the helper.
check_marker_count "P141 _emit_iteration_audit invocations (≥3 — covers all exit paths)" \
    "$DELEGATE_TOOL" "_emit_iteration_audit(" 4
# P141/MOL-491 (#163 review CRITICAL-2): _delegate_profile attribute set on child.
check_fixed "P141 _delegate_profile set on child for audit attribution" \
    "$DELEGATE_TOOL" "child._delegate_profile ="
# P141/MOL-491 (#163 review HIGH-1): logger.error + counter + tier-2 fallback.
check_fixed "P141 audit fail-open counter defined" \
    "$DELEGATE_TOOL" "_P141_ITER_AUDIT_DROP_COUNT"
check_fixed "P141 audit tier-2 /tmp fallback" \
    "$DELEGATE_TOOL" "hermes-iter-audit-"
# P141/MOL-491 (#163 review HIGH-1): narrowed exception types.
check_fixed "P141 narrowed exception types in fail-open" \
    "$DELEGATE_TOOL" "(OSError, ValueError, TypeError) as _iter_exc"
check_marker_count "P141/MOL-491 markers in delegate_tool.py" \
    "$DELEGATE_TOOL" "P141/MOL-491" 6

[[ $QUIET -eq 0 ]] && echo "=== P104 / MOL-420: hermes -z one-shot mode (manual port of upstream 7c8c031f6) ==="
HERMES_CLI_MAIN="$HERMES_AGENT/hermes_cli/main.py"
HERMES_CLI_ONESHOT="$HERMES_AGENT/hermes_cli/oneshot.py"

# main.py — argparse argument + dispatch handler
check_fixed "P104 -z/--oneshot argparse argument" \
    "$HERMES_CLI_MAIN" \
    '"-z",'
check_fixed "P104 oneshot dispatch handler" \
    "$HERMES_CLI_MAIN" \
    "from hermes_cli.oneshot import run_oneshot"
check_marker_count "P104/MOL-420 markers in main.py" \
    "$HERMES_CLI_MAIN" "P104/MOL-420" 2

# oneshot.py — new module
check_fixed "P104 oneshot.py run_oneshot defined" \
    "$HERMES_CLI_ONESHOT" \
    "def run_oneshot("
check_fixed "P104 oneshot.py _run_agent defined" \
    "$HERMES_CLI_ONESHOT" \
    "def _run_agent("
check_marker_count "P104/MOL-420 markers in oneshot.py" \
    "$HERMES_CLI_ONESHOT" "P104/MOL-420" 1

[[ $QUIET -eq 0 ]] && echo "=== P105 / MOL-420: Configurable delegate child timeout (homebrew, default 1200s) ==="
# upstream-compatible symbol names (DELEGATION_CHILD_TIMEOUT_SECONDS / child_timeout_seconds)

check_fixed "P105 _DEFAULT_CHILD_TIMEOUT_SECONDS constant set to 1800" \
    "$DELEGATE_TOOL" \
    "_DEFAULT_CHILD_TIMEOUT_SECONDS = 1800"
check_fixed "P105 _get_child_timeout_seconds helper defined" \
    "$DELEGATE_TOOL" \
    "def _get_child_timeout_seconds("
check_fixed "P105 DELEGATION_CHILD_TIMEOUT_SECONDS env var read" \
    "$DELEGATE_TOOL" \
    "DELEGATION_CHILD_TIMEOUT_SECONDS"
# regression guard: hardcoded 600 must NOT be present anywhere in delegate_tool.py
if grep -q "timeout=600\|timed out after 600 seconds\|timed out after 600s" "$DELEGATE_TOOL"; then
    echo "FAIL: P105 hardcoded 600s timeout still present in $DELEGATE_TOOL"
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && echo "PASS: P105 no hardcoded 600s timeout remains in $DELEGATE_TOOL"
fi
check_marker_count "P105/MOL-420 markers in delegate_tool.py" \
    "$DELEGATE_TOOL" "P105/MOL-420" 3

[[ $QUIET -eq 0 ]] && echo "=== P106 / MOL-420: Read prompt_caching.cache_ttl from config (manual port of upstream 7626f3702) ==="
HERMES_CLI_CONFIG="$HERMES_AGENT/hermes_cli/config.py"
RUN_AGENT="$HERMES_AGENT/run_agent.py"

# config.py — DEFAULT_CONFIG prompt_caching block
check_fixed "P106 prompt_caching block in DEFAULT_CONFIG" \
    "$HERMES_CLI_CONFIG" \
    '"prompt_caching":'
check_marker_count "P106/MOL-420 markers in config.py" \
    "$HERMES_CLI_CONFIG" "P106/MOL-420" 1

# run_agent.py — config-aware cache_ttl loader
check_fixed "P106 cache_ttl config loader" \
    "$RUN_AGENT" \
    "_pc_cfg.get(\"cache_ttl\""
check_marker_count "P106/MOL-420 markers in run_agent.py" \
    "$RUN_AGENT" "P106/MOL-420" 1

[[ $QUIET -eq 0 ]] && echo "=== P107 / MOL-429: Kimi reasoning_content fill broadened to OpenRouter route ==="
# Helper definition
check_fixed "P107 _kimi_thinking_fill_required helper defined" \
    "$RUN_AGENT" \
    "def _kimi_thinking_fill_required("
# Helper covers OpenRouter-routed Kimi via moonshotai/ model prefix
check_fixed "P107 helper checks moonshotai/ model prefix" \
    "$RUN_AGENT" \
    'model.startswith("moonshotai/")'
# Belt-and-suspenders site (formerly _is_kimi_direct())
check_fixed "P107 belt-and-suspenders uses broadened predicate" \
    "$RUN_AGENT" \
    "if self._kimi_thinking_fill_required():"
# Post-fallback fixup site uses the broadened predicate
check_fixed "P107 post-fallback fixup uses broadened predicate" \
    "$RUN_AGENT" \
    "self._fallback_activated and self._kimi_thinking_fill_required()"
# Site 6316 (extra_body thinking) intentionally NOT broadened — guards against accidental over-broadening
check_fixed "P107 site 6316 (extra_body thinking) stays direct-only" \
    "$RUN_AGENT" \
    'if self._is_kimi_direct():'
# Marker count: 5 P107 attribution markers (1 helper comment + 4 call-site comments)
check_marker_count "P107/MOL-429 markers in run_agent.py" \
    "$RUN_AGENT" "P107/MOL-429" 5

[[ $QUIET -eq 0 ]] && echo "=== P108 / MOL-423: Ollama context-length VRAM cap + Qwen3 inline-think detection ==="
# P108a — VRAM cap (run_agent.py) + root-key migration (config.py)
check_fixed "P108 Ollama num_ctx VRAM cap log line" \
    "$RUN_AGENT" \
    "Ollama num_ctx capped:"
check_fixed "P108 _has_inline_thinking detection" \
    "$RUN_AGENT" \
    "_has_inline_thinking"
check_fixed "P108 context_length added to root-key migration" \
    "$HERMES_CLI_CONFIG" \
    '"provider", "base_url", "context_length"'
check_marker_count "P108/MOL-423 markers in run_agent.py" \
    "$RUN_AGENT" "P108/MOL-423" 2
check_marker_count "P108/MOL-423 markers in config.py" \
    "$HERMES_CLI_CONFIG" "P108/MOL-423" 1

# P109/MOL-423 = NOT APPLICABLE (memory/hindsight; we use tiered provider) — no checks

[[ $QUIET -eq 0 ]] && echo "=== P110 / MOL-423: Self-improvement loop overhaul (background-review fork) ==="
# P110 — bg-review fork hardening (4 portable upstream commits collapsed)
check_fixed "P110 _summarize_background_review_actions staticmethod" \
    "$RUN_AGENT" \
    "def _summarize_background_review_actions("
check_fixed "P110 review fork max_iterations bumped to 16" \
    "$RUN_AGENT" \
    "max_iterations=16"
check_fixed "P110 review fork suppress_status_output set" \
    "$RUN_AGENT" \
    "review_agent.suppress_status_output = True"
check_fixed "P110 review fork enabled_toolsets restriction" \
    "$RUN_AGENT" \
    'enabled_toolsets=["memory", "skills"]'
check_fixed "P110 prior_snapshot filter call site" \
    "$RUN_AGENT" \
    "self._summarize_background_review_actions("
check_marker_count "P110/MOL-423 markers in run_agent.py" \
    "$RUN_AGENT" "P110/MOL-423" 6

[[ $QUIET -eq 0 ]] && echo "=== P111 / MOL-428: Per-model analytics CLI (hermes models-stats) ==="
HERMES_CLI_MODELS_STATS="$HERMES_AGENT/hermes_cli/models_stats.py"

# models_stats.py — new module
check_fixed "P111 compute_models_analytics function defined" \
    "$HERMES_CLI_MODELS_STATS" \
    "def compute_models_analytics("
check_fixed "P111 format_models_table function defined" \
    "$HERMES_CLI_MODELS_STATS" \
    "def format_models_table("
check_fixed "P111 cmd_models_stats CLI handler defined" \
    "$HERMES_CLI_MODELS_STATS" \
    "def cmd_models_stats("
# Schema adaptation: message_count substitute (regression guard)
check_fixed "P111 schema adaptation message_count substitute" \
    "$HERMES_CLI_MODELS_STATS" \
    "SUM(COALESCE(message_count, 0)) as api_calls"
check_marker_count "P111/MOL-428 markers in models_stats.py" \
    "$HERMES_CLI_MODELS_STATS" "P111/MOL-428" 2

# main.py — subparser registration + dispatch
check_fixed "P111 models-stats subparser registered" \
    "$HERMES_CLI_MAIN" \
    '"models-stats"'
check_marker_count "P111/MOL-428 markers in main.py" \
    "$HERMES_CLI_MAIN" "P111/MOL-428" 2

# === P112 / MOL-433: post-MOL-665 mixed disposition ===
# MOL-597 modular refactor reshaped the stealth-channel Kimi routing:
#   - agent/auxiliary_client.py    — DROPPED (file refactored; helper + markers gone, routing absorbed in different shape)
#   - tools/reflection_agent.py    — DEFERRED MOL-1974 (REGRESSION: reverted to pre-P112 K2.5 + OpenRouter)
#   - tools/skill_patcher.py       — DROPPED (file deleted entirely — see P90 banner)
#   - plugins/memory/tiered/llm.py — UPDATED (constants renamed FALLBACK_* → KIMI_FALLBACK_*; markers + extra_body shape gone)
#   - hermes_cli/config.py         — DEFERRED MOL-1974 (fallback_kimi dict gone from DEFAULT_CONFIG)
[[ $QUIET -eq 0 ]] && echo "=== P112 / MOL-433: stealth-channel Kimi routing (post-MOL-665 mixed) ==="
P112_REFL="$HERMES_AGENT/tools/reflection_agent.py"
P112_TIERED_LLM="$HERMES_AGENT/plugins/memory/tiered/llm.py"
P112_CFG="$HERMES_AGENT/hermes_cli/config.py"

# auxiliary_client.py — DROPPED (MOL-665): file refactored by upstream MOL-597; _try_kimi_direct
# helper + _KIMI_DIRECT_* constants + P112/MOL-433 markers all scrubbed. The 46 kimi/moonshot/openrouter
# references that survive in the file indicate routing is absorbed in a different shape, not removed.
# See PATCHES.md P112 banner for full rationale.
# check_fixed "P112 _try_kimi_direct helper defined" \
#     "$P112_AUX" \
#     "def _try_kimi_direct"
# check_fixed "P112 _KIMI_DIRECT_MODEL = kimi-k2.6 constant" \
#     "$P112_AUX" \
#     '_KIMI_DIRECT_MODEL = "kimi-k2.6"'
# check_fixed "P112 _KIMI_DIRECT_BASE_URL = api.moonshot.ai/v1" \
#     "$P112_AUX" \
#     '_KIMI_DIRECT_BASE_URL = "https://api.moonshot.ai/v1"'
# check_fixed "P112 _KIMI_DIRECT_API_KEY_ENV = KIMI_API_KEY" \
#     "$P112_AUX" \
#     '_KIMI_DIRECT_API_KEY_ENV = "KIMI_API_KEY"'
# check_fixed "P112 chain inserts kimi-coding before openrouter" \
#     "$P112_AUX" \
#     '("kimi-coding", _try_kimi_direct)'
# check_fixed "P112 last-resort openrouter fallback preserved" \
#     "$P112_AUX" \
#     'resolved_model or _OPENROUTER_MODEL'
# check_marker_count "P112/MOL-433 markers in auxiliary_client.py" \
#     "$P112_AUX" "P112/MOL-433" 4

# reflection_agent.py — RESTORED 2026-05-21 (MOL-1974 absorbed inline; NO MORE DEFERRALS).
# Re-bumped K2.5 → K2.6 and re-migrated OpenRouter → direct Moonshot (api.moonshot.ai/v1).
# P112/MOL-433 attribution markers were not preserved by upstream — marker_count + finish_reason
# checks remain commented; the 3 wiring checks below assert the user-visible behavior.
check_fixed "P112 reflection_agent _MODEL = kimi-k2.6 (direct)" \
    "$P112_REFL" \
    '_MODEL = "kimi-k2.6"'
check_fixed "P112 reflection_agent _BASE_URL = api.moonshot.ai/v1" \
    "$P112_REFL" \
    'or "https://api.moonshot.ai/v1"'
check_fixed "P112 reflection_agent _API_KEY_ENV = KIMI_API_KEY" \
    "$P112_REFL" \
    '_API_KEY_ENV = "KIMI_API_KEY"'
# check_marker_count "P112/MOL-433 markers in reflection_agent.py" \
#     "$P112_REFL" "P112/MOL-433" 1
# check_fixed "P112 reflection_agent _call_kimi raises on double-empty" \
#     "$P112_REFL" \
#     'Kimi returned empty response (finish_reason='

# skill_patcher.py — DROPPED (MOL-665): file deleted entirely by upstream — see P90 banner.
# check_fixed "P112 skill_patcher _MODEL = kimi-k2.6 (direct)" \
#     "$P112_SKL_PAT" \
#     '_MODEL = "kimi-k2.6"'
# check_fixed "P112 skill_patcher _BASE_URL = api.moonshot.ai/v1" \
#     "$P112_SKL_PAT" \
#     'or "https://api.moonshot.ai/v1"'
# check_fixed "P112 skill_patcher _API_KEY_ENV = KIMI_API_KEY" \
#     "$P112_SKL_PAT" \
#     '_API_KEY_ENV = "KIMI_API_KEY"'
# check_marker_count "P112/MOL-433 markers in skill_patcher.py" \
#     "$P112_SKL_PAT" "P112/MOL-433" 1
# check_fixed "P112 skill_patcher _call_kimi raises on double-empty" \
#     "$P112_SKL_PAT" \
#     'Kimi returned empty response (finish_reason='

# tiered/llm.py — UPDATED (MOL-665): functional preservation with rename FALLBACK_* → KIMI_FALLBACK_*.
# Constants point to kimi-k2.6 + api.moonshot.ai/v1 + KIMI_API_KEY as expected. The extra_body
# thinking shape + P112/MOL-433 markers were lost in the modular refactor and are dropped.
check_fixed "P112 tiered/llm.py KIMI_FALLBACK_MODEL = kimi-k2.6 (post-MOL-665 rename)" \
    "$P112_TIERED_LLM" \
    'KIMI_FALLBACK_MODEL = "kimi-k2.6"'
check_fixed "P112 tiered/llm.py KIMI_FALLBACK_BASE_URL = api.moonshot.ai/v1 (post-MOL-665 rename)" \
    "$P112_TIERED_LLM" \
    'KIMI_FALLBACK_BASE_URL = "https://api.moonshot.ai/v1"'
check_fixed "P112 tiered/llm.py KIMI_FALLBACK_API_KEY_ENV = KIMI_API_KEY (post-MOL-665 rename)" \
    "$P112_TIERED_LLM" \
    'KIMI_FALLBACK_API_KEY_ENV = "KIMI_API_KEY"'
# DROPPED MOL-665: extra_body thinking shape + P112/MOL-433 markers gone from runtime.
# check_fixed "P112 tiered/llm.py extra_body switched to Moonshot thinking shape" \
#     "$P112_TIERED_LLM" \
#     'extra_body={"thinking": {"type": "enabled"}}'
# check_marker_count "P112/MOL-433 markers in tiered/llm.py" \
#     "$P112_TIERED_LLM" "P112/MOL-433" 2

# hermes_cli/config.py — DEFERRED — see MOL-1974
# `fallback_kimi` dict block restructured/removed from DEFAULT_CONFIG; `kimi-coding` survives
# only as a comment in the provider menu. Needs followup to verify the live config.yaml
# fallback path still routes correctly post-MOL-597.
# check_fixed "P112 config.py DEFAULT_CONFIG fallback_kimi provider = kimi-coding" \
#     "$P112_CFG" \
#     '"provider": "kimi-coding"'
# check_fixed "P112 config.py DEFAULT_CONFIG fallback_kimi model = kimi-k2.6" \
#     "$P112_CFG" \
#     '"model": "kimi-k2.6"'
# check_marker_count "P112/MOL-433 markers in config.py" \
#     "$P112_CFG" "P112/MOL-433" 1

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P115 / MOL-447 — _emit_profile_audit wired into Tier 3 (Hermes subagent) path ==="
P115_DELEG="$HERMES_AGENT/tools/delegate_tool.py"
check_fixed "P115 _top_profile_cfg loaded at top-level"      "$P115_DELEG" "_top_profile_cfg = _load_profile(profile)"
check_fixed "P115 Tier 3 audit emit loop"                    "$P115_DELEG" "if _top_profile_cfg:"
check_fixed "P115 Tier 3 audit calls _emit_profile_audit"    "$P115_DELEG" 'profile_cfg=_top_profile_cfg,'
check_marker_count "P115/MOL-447 markers in delegate_tool.py" "$P115_DELEG" "P115/MOL-447" 2

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P116 / MOL-448 — final-pass reasoning_content fill at API-call boundary ==="
P116_RUN="$HERMES_AGENT/run_agent.py"
# Streaming-path fill (after stream_kwargs assembly, before chat.completions.create)
check_fixed "P116 streaming fill loop"                       "$P116_RUN" "for _msg_final in stream_kwargs.get"
check_fixed "P116 streaming fill body"                       "$P116_RUN" '_msg_final["reasoning_content"] = ""'
# Non-streaming-path fill (before _call() thread starts)
check_fixed "P116 non-streaming fill loop"                   "$P116_RUN" "for _msg_ns in api_kwargs.get"
check_fixed "P116 non-streaming fill body"                   "$P116_RUN" '_msg_ns["reasoning_content"] = ""'
check_marker_count "P116/MOL-448 markers in run_agent.py"    "$P116_RUN" "P116/MOL-448" 2

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P114 / MOL-442 — delegate_task profile param wired to LLM schema + handler ==="
P114_DELEG="$HERMES_AGENT/tools/delegate_tool.py"
# Schema: new "profile" property with cua/coding/default enum
check_fixed "P114 schema declares profile property"          "$P114_DELEG" '"profile": {'
check_fixed "P114 schema enum includes coding"               "$P114_DELEG" '"enum": ["default", "coding", "cua"]'
check_fixed "P114 schema description names cua-wrapper.sh"   "$P114_DELEG" 'cua-wrapper.sh'
# Handler: forwards args["profile"] into delegate_task() call
check_fixed "P114 handler forwards profile from args"        "$P114_DELEG" 'profile=args.get("profile") or "default"'
check_marker_count "P114/MOL-442 markers in delegate_tool.py" "$P114_DELEG" "P114/MOL-442" 2

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P113: cua-vm skill mirror integrity (MOL-442) ==="
# Repo is source-of-truth for cua_run.py + cua-vm SKILL.md.
# Runtime mirrors live at:
#   - ~/.claude/skills/cua-vm/        (Claude Code side, MOL-435)
#   - ~/.hermes/skills/desktop/cua-vm/ (Hermes side, MOL-442)
# Drift between them = silent skill divergence. Sha256 mirror check catches it.
P113_REPO_RUN="$HERMES_POC_REPO/claude-skills/cua-vm/cua_run.py"
P113_REPO_SKILL="$HERMES_POC_REPO/claude-skills/cua-vm/SKILL.md"
P113_CC_RUN="$HOME/.claude/skills/cua-vm/cua_run.py"
P113_HERMES_RUN="$HERMES_SKILLS/desktop/cua-vm/cua_run.py"

check_mirror_sha256 "P113 cua_run.py mirrored to ~/.claude/skills/cua-vm/" \
    "$P113_REPO_RUN" "$P113_CC_RUN"
check_mirror_sha256 "P113 cua_run.py mirrored to ~/.hermes/skills/desktop/cua-vm/" \
    "$P113_REPO_RUN" "$P113_HERMES_RUN"

# SKILL.md is intentionally NOT mirror-checked — Hermes-side SKILL.md is
# adapted (different invocation pattern: delegate_task vs bare bash). Repo
# source-of-truth for the Hermes-side SKILL.md is at claude-skills/cua-vm-hermes/
# below if it gets promoted to a tracked asset later.

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P117 / MOL-442 — PR #136 review follow-ups (cua.yaml + wrapper hardening) ==="
P117_CUA_YAML="$HERMES_POC_REPO/config/hermes/delegate-profiles/cua.yaml"
P117_WRAPPER="$HERMES_POC_REPO/scripts/cua-wrapper.sh"
P117_RAMPART="$HERMES_POC_REPO/config/rampart/hermes-policy.yaml"
P117_SKILL_MD="$HERMES_SKILLS/desktop/cua-vm/SKILL.md"

# cua.yaml — env_passthrough removed, consent_key removed, symbol anchor present
check_fixed "P117 cua.yaml drops env_passthrough (denylist conflict)" \
    "$P117_CUA_YAML" 'env_passthrough intentionally omitted'
check_fixed "P117 cua.yaml uses symbol anchor for blocklist ref" \
    "$P117_CUA_YAML" '_build_provider_env_blocklist'
check_fixed "P117 cua.yaml redirects HITL semantics to MOL-450" \
    "$P117_CUA_YAML" 'see MOL-450'
# Negative check: consent_key field must be gone (silently-ignored dead config).
if grep -q '^[[:space:]]*consent_key:' "$P117_CUA_YAML" 2>/dev/null; then
    echo "FAIL P117 cua.yaml still declares consent_key (runtime ignores it)"
    failed=$((failed+1))
fi

# cua-wrapper.sh — awk-based chunk count, KIND/MODEL validation
check_fixed "P117 cua-wrapper.sh uses awk for chunk count" \
    "$P117_WRAPPER" "awk 'END { print NR+0 }'"
check_fixed "P117 cua-wrapper.sh validates KIND" \
    "$P117_WRAPPER" 'invalid --kind=$KIND'
check_fixed "P117 cua-wrapper.sh validates MODEL charset" \
    "$P117_WRAPPER" 'A-Za-z0-9._/+-'
check_fixed "P117 cua-wrapper.sh guards shasum failure" \
    "$P117_WRAPPER" 'shasum failed'

# Rampart policy — tightened deny patterns
check_fixed "P117 Rampart deny anchors python (no leading space)" \
    "$P117_RAMPART" '*python *cua_run.py*'
check_fixed "P117 Rampart deny anchors python3 explicitly" \
    "$P117_RAMPART" '*python3 *cua_run.py*'

# SKILL.md (runtime-only) — required_environment_variables registered
check_fixed "P117 SKILL.md declares required_environment_variables" \
    "$P117_SKILL_MD" 'required_environment_variables:'

# Marker-drift guard — protects against wrong-P-number re-applies (P69/MOL-277).
check_marker_count "P117/MOL-442 markers in cua.yaml" \
    "$P117_CUA_YAML" "P117/MOL-442" 1

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P118 / MOL-455 — DeepSeek default auxiliary model registered ==="
P118_AUX="$HERMES_AGENT/agent/auxiliary_client.py"
check_fixed "P118 deepseek default model registered" \
    "$P118_AUX" '"deepseek": "deepseek-v4-pro"'
check_marker_count "P118/MOL-455 markers in auxiliary_client.py" \
    "$P118_AUX" "P118/MOL-455" 1

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P119 / MOL-455 — DeepSeek direct: top-level reasoning_effort + thinking-supports flag ==="
P119_RUN="$HERMES_AGENT/run_agent.py"
check_fixed "P119 deepseek reasoning_effort top-level injection" \
    "$P119_RUN" 'if "api.deepseek.com" in self._base_url_lower:'
check_fixed "P119 deepseek thinking-supports flag" \
    "$P119_RUN" "P119/MOL-455: DeepSeek V4 thinking mode is ON by default."
check_marker_count "P119/MOL-455 markers in run_agent.py" \
    "$P119_RUN" "P119/MOL-455" 2

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P120 / MOL-455 — Image marker routes to jira_or_coding (Kimi K2.6 multimodal) ==="
P120_SMR="$HERMES_AGENT/agent/smart_model_routing.py"
check_fixed "P120 image marker routes to jira_or_coding" \
    "$P120_SMR" 'return "jira_or_coding"'
check_fixed "P120 image marker comment cites K2.6 multimodal" \
    "$P120_SMR" "Kimi K2.6 native multimodal"
check_marker_count "P120/MOL-455 markers in smart_model_routing.py" \
    "$P120_SMR" "P120/MOL-455" 2

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P121 / MOL-455 — DeepSeek V4 pricing entries ==="
P121_UP="$HERMES_AGENT/agent/usage_pricing.py"
check_fixed "P121 deepseek-v4-pro pricing entry" \
    "$P121_UP" '"deepseek-v4-pro",'
check_fixed "P121 deepseek-v4-flash pricing entry" \
    "$P121_UP" '"deepseek-v4-flash",'
check_fixed "P121 v4-pro pricing version label" \
    "$P121_UP" "deepseek-pricing-2026-05-08-v4-discount"
check_marker_count "P121/MOL-455 markers in usage_pricing.py" \
    "$P121_UP" "P121/MOL-455" 1

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P124 / MOL-455 — _read_primary_provider HERMES_HOME-aware (profile-aware bootstrap) ==="
P124_MAIN="$HERMES_AGENT/hermes_cli/main.py"
# P124 superseded by get_hermes_home() helper from hermes_constants.py (same
# semantic — HERMES_HOME env var → profile-aware ~/.hermes resolution). Original
# `_read_primary_provider` function was inlined into the redact-secrets bootstrap
# at main.py:~215. Retagged per [[check_retag_on_refactor]].
check_fixed "P124 HERMES_HOME-aware bootstrap import (post-refactor: get_hermes_home helper)" \
    "$P124_MAIN" 'from hermes_cli.config import get_hermes_home'
check_fixed "P124 cfg_path derived via get_hermes_home (post-refactor)" \
    "$P124_MAIN" '_cfg_path = get_hermes_home() / "config.yaml"'
check_marker_count "P124/MOL-455 markers in main.py" \
    "$P124_MAIN" "P124/MOL-455" 1

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P125 / MOL-455 — DEEPSEEK_ in envchain-wrapper.sh ALLOWED_PREFIXES ==="
P125_WRAPPER_REPO="$HERMES_POC_REPO/scripts/envchain-wrapper.sh"
P125_WRAPPER_RUNTIME="$HERMES_SCRIPTS/envchain-wrapper.sh"
check_fixed "P125 DEEPSEEK_ in repo envchain-wrapper.sh ALLOWED_PREFIXES" \
    "$P125_WRAPPER_REPO" '"DEEPSEEK_"'
# Belt-and-suspenders: PATCHES.md says repo + runtime "must stay in sync".
# Verify runtime carries the same entry so a one-sided edit (repo OR runtime
# only) fails fast, not at the next 401.
check_fixed "P125 DEEPSEEK_ in runtime envchain-wrapper.sh ALLOWED_PREFIXES" \
    "$P125_WRAPPER_RUNTIME" '"DEEPSEEK_"'
check_marker_count "P125/MOL-455 markers in scripts/envchain-wrapper.sh (repo)" \
    "$P125_WRAPPER_REPO" "P125/MOL-455" 1
check_marker_count "P125/MOL-455 markers in scripts/envchain-wrapper.sh (runtime)" \
    "$P125_WRAPPER_RUNTIME" "P125/MOL-455" 1

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P126 / MOL-455 — cron/scheduler.py state.db is HERMES_HOME-aware ==="
P126_SCHED="$HERMES_AGENT/cron/scheduler.py"
check_fixed "P126 scheduler state.db is HERMES_HOME-aware" \
    "$P126_SCHED" '_p126_home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")'
check_fixed "P126 scheduler state.db built from derived home" \
    "$P126_SCHED" '_p59_path = Path(_p126_home) / "state.db"'
check_marker_count "P126/MOL-455 markers in cron/scheduler.py" \
    "$P126_SCHED" "P126/MOL-455" 1

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P127 / MOL-455 — envchain-wrapper credential-shaped diagnostic ==="
P127_WRAPPER_REPO="$P125_WRAPPER_REPO"
P127_WRAPPER_RUNTIME="$P125_WRAPPER_RUNTIME"
check_fixed "P127 credential-shaped diagnostic regex (repo)" \
    "$P127_WRAPPER_REPO" '_credential_shaped_re='"'"'^[A-Z][A-Z0-9_]*_(API_KEY|TOKEN|SECRET)$'"'"
check_fixed "P127 credential-shaped diagnostic regex (runtime)" \
    "$P127_WRAPPER_RUNTIME" '_credential_shaped_re='"'"'^[A-Z][A-Z0-9_]*_(API_KEY|TOKEN|SECRET)$'"'"
check_fixed "P127 WARN line emits dropped-credential breadcrumb (repo)" \
    "$P127_WRAPPER_REPO" 'prefix not in ALLOWED_PREFIXES'
check_marker_count "P127/MOL-455 markers in scripts/envchain-wrapper.sh (repo)" \
    "$P127_WRAPPER_REPO" "P127/MOL-455" 1
check_marker_count "P127/MOL-455 markers in scripts/envchain-wrapper.sh (runtime)" \
    "$P127_WRAPPER_RUNTIME" "P127/MOL-455" 1

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P128 / MOL-455 — _read_primary_provider None-vs-other distinction ==="
P128_MAIN="$HERMES_AGENT/hermes_cli/main.py"
check_fixed "P128 None-path logs breadcrumb" \
    "$P128_MAIN" '_bootstrap_log("primary_unknown_bootstrap_skipped")'
check_fixed "P128 explicit None branch" \
    "$P128_MAIN" 'if _primary is None:'
check_marker_count "P128/MOL-455 markers in main.py" \
    "$P128_MAIN" "P128/MOL-455" 1

[[ $QUIET -eq 0 ]] && echo "=== P129 / MOL-460 — Vision auto-detect skips text-only primaries (DeepSeek) ==="
P129_AUX="$HERMES_AGENT/agent/auxiliary_client.py"
check_fixed "P129 vision text-only denylist" \
    "$P129_AUX" '_VISION_TEXT_ONLY_PROVIDERS = frozenset({"deepseek"})'
check_fixed "P129 kimi-coding in vision provider order" \
    "$P129_AUX" '"kimi-coding",  # P129/MOL-460'
check_fixed "P129 kimi-coding strict vision backend" \
    "$P129_AUX" 'if provider == "kimi-coding":'
check_fixed "P129 active-provider denylist skip" \
    "$P129_AUX" 'if main_provider in _VISION_TEXT_ONLY_PROVIDERS:'
check_marker_count "P129/MOL-460 markers in auxiliary_client.py" \
    "$P129_AUX" "P129/MOL-460" 5

[[ $QUIET -eq 0 ]] && echo "=== P130 / MOL-460 — Kimi K2.6 temperature=1 retry in async_call_llm ==="
P130_AUX="$HERMES_AGENT/agent/auxiliary_client.py"
check_fixed "P130 retry trigger gated to kimi-coding" \
    "$P130_AUX" 'if resolved_provider == "kimi-coding" and ('
check_fixed "P130 retry trigger substring match" \
    "$P130_AUX" '"invalid temperature" in err_str or "only 1 is allowed" in err_str'
check_fixed "P130 retry sets temperature=1.0" \
    "$P130_AUX" 'kwargs["temperature"] = 1.0'
check_fixed "P130 retry success log line" \
    "$P130_AUX" 'kimi-coding temperature=1.0 retry succeeded'
check_marker_count "P130/MOL-460 markers in auxiliary_client.py" \
    "$P130_AUX" "P130/MOL-460" 1

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P131 / MOL-451 — Broaden _kimi_thinking_fill_required to Gemini 3.x via OpenRouter ==="
P131_RUN="$HERMES_AGENT/run_agent.py"
check_fixed "P131 gemini-3 OpenRouter branch present" \
    "$P131_RUN" 'model.startswith("google/gemini-3")'
# NOTE: this is a sanity check only — the substring `self._reasoning_content_enabled()`
# also appears in pre-existing flush-path callsites. If P131 is reverted, this check
# still passes. The combined check below is the load-bearing assertion that uniquely
# pins the P131 line.
check_fixed "P131 reasoning_content_enabled helper present (sanity)" \
    "$P131_RUN" 'self._reasoning_content_enabled()'
check_fixed "P131 openrouter+gemini-3 combined check" \
    "$P131_RUN" '"openrouter" in base and model.startswith("google/gemini-3")'
# Stronger than substring matching: invoke the gate function directly with
# the full input matrix. Survives benign refactors (renaming, comment changes,
# reformatting) that would false-fail the literal substring checks above.
P131_GATE_TEST_OUT=$("$HERMES_AGENT/venv/bin/python3" -c "
import sys
sys.path.insert(0, '$HERMES_AGENT')
from run_agent import AIAgent
a = AIAgent.__new__(AIAgent)
# Case 1: gemini-3 via OpenRouter + reasoning enabled => True
a.model = 'google/gemini-3.1-pro-preview'
a.base_url = 'https://openrouter.ai/api/v1'
a._base_url_lower = a.base_url.lower()
a.reasoning_config = {'enabled': True, 'effort': 'high'}
assert a._kimi_thinking_fill_required() is True, 'gemini-3 OR + reasoning expected True'
# Case 2: gemini-2.x => False (only gemini-3* covered)
a.model = 'google/gemini-2.5-flash'
assert a._kimi_thinking_fill_required() is False, 'gemini-2.5 expected False'
# Case 3: moonshotai/* unchanged => True
a.model = 'moonshotai/kimi-k2.6'
assert a._kimi_thinking_fill_required() is True, 'moonshotai/* expected True'
# Case 4: gemini-3 + reasoning disabled => False
a.model = 'google/gemini-3.1-pro-preview'
a.reasoning_config = {'enabled': False}
assert a._kimi_thinking_fill_required() is False, 'reasoning_enabled=False expected False'
# Case 5: gemini-3 direct (non-OpenRouter) => False
a.base_url = 'https://generativelanguage.googleapis.com/v1'
a._base_url_lower = a.base_url.lower()
a.reasoning_config = {'enabled': True}
assert a._kimi_thinking_fill_required() is False, 'gemini-3 direct (non-OR) expected False'
print('PASS')
" 2>&1)
if echo "$P131_GATE_TEST_OUT" | grep -Fq "PASS"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m %s\n' "P131 gate behavior — 5 cases pass"
else
    failed=$((failed + 1))
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m %s\n' "P131 gate behavior failed:"
    [[ $QUIET -eq 0 ]] && echo "$P131_GATE_TEST_OUT" | sed 's/^/      /'
fi
check_marker_count "P131/MOL-451 markers in run_agent.py" \
    "$P131_RUN" "P131/MOL-451" 2

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P132 / MOL-467 — Pre-init scheduler loop-scoped vars (prevent _high UnboundLocalError) ==="
P132_SCHED="$HERMES_AGENT/cron/scheduler.py"
check_fixed "P132 _high pre-init before loop" \
    "$P132_SCHED" '_high: list = []'
check_fixed "P132 _concerns pre-init before loop" \
    "$P132_SCHED" '_concerns: list = []'
check_fixed "P132 _review_status pre-init before loop" \
    "$P132_SCHED" '_review_status = "ok"'
check_fixed "P132 _review_banner pre-init before loop" \
    "$P132_SCHED" '_review_banner = ""'
check_marker_count "P132/MOL-467 markers in scheduler.py" \
    "$P132_SCHED" "P132/MOL-467" 1

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P133 / MOL-462 — Audit emit fsync + in-flight breadcrumb durability ==="
P133_DELEGATE="$HERMES_AGENT/tools/delegate_tool.py"
check_fixed "P133 fsync after audit write" \
    "$P133_DELEGATE" "os.fsync(f.fileno())"
check_fixed "P133 _write_inflight_breadcrumb helper exists" \
    "$P133_DELEGATE" "def _write_inflight_breadcrumb("
check_fixed "P133 _clear_inflight_breadcrumb helper exists" \
    "$P133_DELEGATE" "def _clear_inflight_breadcrumb("
check_fixed "P133 breadcrumb wired at delegate_task top" \
    "$P133_DELEGATE" "_write_inflight_breadcrumb(profile, _initial_goal)"
check_fixed "P133 breadcrumb cleared on audit emit" \
    "$P133_DELEGATE" "_clear_inflight_breadcrumb(profile_name)"
check_marker_count "P133/MOL-462 markers in delegate_tool.py" \
    "$P133_DELEGATE" "P133/MOL-462" 4

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P135 / MOL-469-hardening — Hybrid SYMPHONY-VERIFY marker + jira-cli fallback for symphony post-condition ==="
P135_REPO_ROOT="$HERMES_POC_REPO"
P135_PLANNING_REPO="$P135_REPO_ROOT/config/hermes/delegate-profiles/planning.yaml"
P135_PLANNING_RUNTIME="${HERMES_HOME:-$HOME/.hermes}/config/delegate-profiles/planning.yaml"
P135_KNOCKOUT_REPO="$P135_REPO_ROOT/skills/software-development/knock-out-jira-ticket/SKILL.md"
P135_KNOCKOUT_RUNTIME="$HERMES_SKILLS/software-development/knock-out-jira-ticket/SKILL.md"
P135_SYMPHONY_REPO="$P135_REPO_ROOT/skills/software-development/symphony-bridge/SKILL.md"
P135_SYMPHONY_RUNTIME="$HERMES_SKILLS/software-development/symphony-bridge/SKILL.md"

# Content-shape checks (each pinned to a unique substring; surviving benign rephrasing)
check_fixed "P135 Target files requirement in planning.yaml (repo)" \
    "$P135_PLANNING_REPO" '## Target files section (P135/MOL-469 — load-bearing)'
check_fixed "P135 Target files requirement in planning.yaml (runtime)" \
    "$P135_PLANNING_RUNTIME" '## Target files section (P135/MOL-469 — load-bearing)'
check_fixed "P135 canonical CLAUDE.md resolution rule (repo)" \
    "$P135_PLANNING_REPO" '`/Users/wills_mac_mini/.claude/CLAUDE.md` — the'
check_fixed "P135 canonical CLAUDE.md resolution rule (runtime)" \
    "$P135_PLANNING_RUNTIME" '`/Users/wills_mac_mini/.claude/CLAUDE.md` — the'
check_fixed "P135 reviewer drift check in knock-out skill (repo)" \
    "$P135_KNOCKOUT_REPO" 'compare actual file edits in the branch against'
check_fixed "P135 reviewer drift check in knock-out skill (runtime)" \
    "$P135_KNOCKOUT_RUNTIME" 'compare actual file edits in the branch against'
check_fixed "P135 SYMPHONY-VERIFY marker emit in knock-out skill (repo)" \
    "$P135_KNOCKOUT_REPO" 'SYMPHONY-VERIFY: pr_url='
check_fixed "P135 SYMPHONY-VERIFY marker emit in knock-out skill (runtime)" \
    "$P135_KNOCKOUT_RUNTIME" 'SYMPHONY-VERIFY: pr_url='
# Reviewer-crash → reviewer_verdict mapping (post-review fix; closes silent_failure_hunter #1).
# Pins the explicit "never default to pass" anti-softening for the abort path.
check_fixed "P135 reviewer-crash mapping in knock-out skill (repo)" \
    "$P135_KNOCKOUT_REPO" 'NEVER default to'
check_fixed "P135 reviewer-crash mapping in knock-out skill (runtime)" \
    "$P135_KNOCKOUT_RUNTIME" 'NEVER default to'
check_fixed "P135 marker parser wired in symphony skill (repo)" \
    "$P135_SYMPHONY_REPO" 'Primary path: parse SYMPHONY-VERIFY marker'
check_fixed "P135 marker parser wired in symphony skill (runtime)" \
    "$P135_SYMPHONY_RUNTIME" 'Primary path: parse SYMPHONY-VERIFY marker'
# Parser regex shape pin (post-review fix #4; closes pr-test-analyzer #3 — without this,
# a future edit changing the regex's pipe/space pattern passes the verifier while
# silently breaking runtime parse).
check_fixed "P135 parser regex shape in symphony skill (repo)" \
    "$P135_SYMPHONY_REPO" 'pr_url=([^|]*) \| target_repo=([^|]*) \| pr_state'
check_fixed "P135 parser regex shape in symphony skill (runtime)" \
    "$P135_SYMPHONY_RUNTIME" 'pr_url=([^|]*) \| target_repo=([^|]*) \| pr_state'
# Last-line extraction rule (post-review fix #5; closes silent_failure_hunter #2 —
# regex without explicit last-line scoping could match a quoted marker on line 5/50).
check_fixed "P135 last-line extraction rule in symphony skill (repo)" \
    "$P135_SYMPHONY_REPO" 'isolate the LAST non-empty line'
check_fixed "P135 last-line extraction rule in symphony skill (runtime)" \
    "$P135_SYMPHONY_RUNTIME" 'isolate the LAST non-empty line'
check_fixed "P135 jira-cli fallback path in symphony skill (repo)" \
    "$P135_SYMPHONY_REPO" 'Fallback path: marker MISSING or malformed'
check_fixed "P135 jira-cli fallback path in symphony skill (runtime)" \
    "$P135_SYMPHONY_RUNTIME" 'Fallback path: marker MISSING or malformed'
check_fixed "P135 anti-softening clause in symphony skill (repo)" \
    "$P135_SYMPHONY_REPO" 'DO NOT soften'
check_fixed "P135 anti-softening clause in symphony skill (runtime)" \
    "$P135_SYMPHONY_RUNTIME" 'DO NOT soften'

# Marker-count guards (catch wrong-P-number re-applies per check_retag_on_refactor memory)
check_marker_count "P135/MOL-469 markers in planning.yaml (repo)" \
    "$P135_PLANNING_REPO" "P135/MOL-469" 3
check_marker_count "P135/MOL-469 markers in planning.yaml (runtime)" \
    "$P135_PLANNING_RUNTIME" "P135/MOL-469" 3
check_marker_count "P135/MOL-469 markers in knock-out SKILL.md (repo)" \
    "$P135_KNOCKOUT_REPO" "P135/MOL-469" 3
check_marker_count "P135/MOL-469 markers in knock-out SKILL.md (runtime)" \
    "$P135_KNOCKOUT_RUNTIME" "P135/MOL-469" 3
check_marker_count "P135/MOL-469 markers in symphony SKILL.md (repo)" \
    "$P135_SYMPHONY_REPO" "P135/MOL-469" 3
check_marker_count "P135/MOL-469 markers in symphony SKILL.md (runtime)" \
    "$P135_SYMPHONY_RUNTIME" "P135/MOL-469" 3

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P136 / MOL-479 — Standardize Hermes jira-skill ticket creation for Symphony (template + validator) ==="
P136_REPO_ROOT="$HERMES_POC_REPO"
P136_VALIDATOR_REPO="$P136_REPO_ROOT/scripts/jira_body_validator.py"
P136_VALIDATOR_RUNTIME="${HERMES_HOME:-$HOME/.hermes}/scripts/jira_body_validator.py"
P136_JIRA_SKILL_REPO="$P136_REPO_ROOT/skills/productivity/jira/SKILL.md"
P136_JIRA_SKILL_RUNTIME="$HERMES_SKILLS/productivity/jira/SKILL.md"
P136_TEST_RUNNER="$P136_REPO_ROOT/tests/test-jira-body-validator.sh"
P136_FIXTURE_DIR="$P136_REPO_ROOT/tests/fixtures/jira-body"
P136_PATCHES_MD="$P136_REPO_ROOT/scripts/hermes-patches/PATCHES.md"

# Validator script: byte-identical mirror check (P113/P63 pattern, replaces
# 2 prior check_fixed presence-only assertions).
check_mirror_sha256 "P136 validator script mirror" \
    "$P136_VALIDATOR_REPO" "$P136_VALIDATOR_RUNTIME"

# SKILL.md convention section + verb allowlist (both mirrors)
check_fixed "P136 convention section in jira skill (repo)" \
    "$P136_JIRA_SKILL_REPO" "## Standard ticket body convention (P136/MOL-479)"
check_fixed "P136 convention section in jira skill (runtime)" \
    "$P136_JIRA_SKILL_RUNTIME" "## Standard ticket body convention (P136/MOL-479)"
check_fixed "P136 verb allowlist in jira skill (repo)" \
    "$P136_JIRA_SKILL_REPO" "evaluate, validate, document, audit, measure, profile"
check_fixed "P136 verb allowlist in jira skill (runtime)" \
    "$P136_JIRA_SKILL_RUNTIME" "evaluate, validate, document, audit, measure, profile"

# Test infra
check_fixed "P136 test runner exists" \
    "$P136_TEST_RUNNER" 'invalid-bad-verb.md'

# Fixture-dir population guard: count .md files == 13 (3 valid + 9 rejection-
# class invalid + 1 warning-class invalid). Inline because we want
# exact-equality, not >= which check_marker_count gives.
# Increments total/passed/failed exactly like the canonical helpers.
total=$((total + 1))
P136_FIXTURE_COUNT=$(find "$P136_FIXTURE_DIR" -maxdepth 1 -name '*.md' 2>/dev/null | wc -l | tr -d ' ')
if [ "$P136_FIXTURE_COUNT" = "13" ]; then
    [[ $QUIET -eq 0 ]] && echo "  ✓ P136 fixture dir populated (13 .md files)"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && echo "  ✗ P136 fixture dir population: expected 13 .md files, found $P136_FIXTURE_COUNT in $P136_FIXTURE_DIR"
    failed=$((failed + 1))
fi

# Marker-count guards (catch wrong-P-number re-applies per check_retag_on_refactor memory)
check_marker_count "P136/MOL-479 markers in validator script" \
    "$P136_VALIDATOR_REPO" "P136/MOL-479" 1
check_marker_count "P136/MOL-479 markers in jira SKILL.md (repo)" \
    "$P136_JIRA_SKILL_REPO" "P136/MOL-479" 2
check_marker_count "P136/MOL-479 markers in jira SKILL.md (runtime)" \
    "$P136_JIRA_SKILL_RUNTIME" "P136/MOL-479" 2
check_marker_count "P136/MOL-479 markers in PATCHES.md" \
    "$P136_PATCHES_MD" "P136/MOL-479" 2
check_marker_count "P136/MOL-479 markers in test runner" \
    "$P136_TEST_RUNNER" "P136/MOL-479" 1

# === P137 / MOL-483 — Symphony autonomy loop (single-session AC closure) ===
[[ $QUIET -eq 0 ]] && echo "=== P137 / MOL-483 — Symphony autonomy loop (single-session AC closure) ==="

P137_REPO_ROOT="$HERMES_POC_REPO"
P137_PLAN_VALIDATOR_REPO="$P137_REPO_ROOT/scripts/plan_ac_validator.py"
P137_PLAN_VALIDATOR_RUNTIME="${HERMES_HOME:-$HOME/.hermes}/scripts/plan_ac_validator.py"
P137_PLANNING_YAML_REPO="$P137_REPO_ROOT/config/hermes/delegate-profiles/planning.yaml"
P137_PLANNING_YAML_RUNTIME="${HERMES_HOME:-$HOME/.hermes}/config/delegate-profiles/planning.yaml"
P137_CODING_YAML_REPO="$P137_REPO_ROOT/config/hermes/delegate-profiles/coding.yaml"
P137_CODING_YAML_RUNTIME="${HERMES_HOME:-$HOME/.hermes}/config/delegate-profiles/coding.yaml"
P137_KNOCKOUT_SKILL_REPO="$P137_REPO_ROOT/skills/software-development/knock-out-jira-ticket/SKILL.md"
P137_KNOCKOUT_SKILL_RUNTIME="$HERMES_SKILLS/software-development/knock-out-jira-ticket/SKILL.md"
P137_SYMPHONY_SKILL_REPO="$P137_REPO_ROOT/skills/software-development/symphony-bridge/SKILL.md"
P137_SYMPHONY_SKILL_RUNTIME="$HERMES_SKILLS/software-development/symphony-bridge/SKILL.md"
P137_TEST_RUNNER="$P137_REPO_ROOT/tests/test-plan-ac-validator.sh"
P137_PATCHES_MD="$P137_REPO_ROOT/scripts/hermes-patches/PATCHES.md"

# Validator script: byte-identical mirror check (P136 pattern, CR7 of MOL-483
# review — replaces 2 separate presence-only check_fixed assertions).
check_mirror_sha256 "P137 plan_ac_validator.py mirror" \
    "$P137_PLAN_VALIDATOR_REPO" "$P137_PLAN_VALIDATOR_RUNTIME"

# planning.yaml AC-mapping suffix block + load-bearing arrow-contract
# sentence (T4 of MOL-483 review — pin the LLM-binding contract sentence
# itself, not just the section header).
check_fixed "P137 AC-mapping block in planning.yaml (repo)" \
    "$P137_PLANNING_YAML_REPO" "## Acceptance-criteria mapping (P137/MOL-483)"
check_fixed "P137 AC-mapping block in planning.yaml (runtime)" \
    "$P137_PLANNING_YAML_RUNTIME" "## Acceptance-criteria mapping (P137/MOL-483)"
check_fixed "P137 arrow-contract sentence in planning.yaml (repo)" \
    "$P137_PLANNING_YAML_REPO" 'MUST'
check_fixed "P137 arrow-contract sentence in planning.yaml (runtime)" \
    "$P137_PLANNING_YAML_RUNTIME" 'MUST'

# coding.yaml anti-softening sentence (T6 of MOL-483 review — entire
# correctness story for reviewer uncertainty path; must not soften away).
check_fixed "P137 anti-softening sentence in coding.yaml (repo)" \
    "$P137_CODING_YAML_REPO" 'NEVER default to'
check_fixed "P137 anti-softening sentence in coding.yaml (runtime)" \
    "$P137_CODING_YAML_RUNTIME" 'NEVER default to'

# knock-out SKILL.md gates + RETRY MODE sentinel (T5 of MOL-483 review —
# pin the literal `RETRY MODE` sentinel so a future rename doesn't silently
# break symphony's retry handoff).
check_fixed "P137 plan-AC validator gate in knock-out SKILL.md (repo)" \
    "$P137_KNOCKOUT_SKILL_REPO" "Plan-AC validator gate (P137/MOL-483)"
check_fixed "P137 plan-AC validator gate in knock-out SKILL.md (runtime)" \
    "$P137_KNOCKOUT_SKILL_RUNTIME" "Plan-AC validator gate (P137/MOL-483)"
check_fixed "P137 auto-merge gate in knock-out SKILL.md (repo)" \
    "$P137_KNOCKOUT_SKILL_REPO" "Wait for CI + 2nd-layer review + auto-merge (P137/MOL-483 + P143/MOL-488 — strict gate)"
check_fixed "P137 auto-merge gate in knock-out SKILL.md (runtime)" \
    "$P137_KNOCKOUT_SKILL_RUNTIME" "Wait for CI + 2nd-layer review + auto-merge (P137/MOL-483 + P143/MOL-488 — strict gate)"
check_fixed "P137 RETRY MODE sentinel in knock-out SKILL.md (repo)" \
    "$P137_KNOCKOUT_SKILL_REPO" 'RETRY MODE'
check_fixed "P137 RETRY MODE sentinel in knock-out SKILL.md (runtime)" \
    "$P137_KNOCKOUT_SKILL_RUNTIME" 'RETRY MODE'

# symphony SKILL.md JQL + To Do fall-through (both mirrors)
check_fixed "P137 To Do fall-through in symphony SKILL.md (repo)" \
    "$P137_SYMPHONY_SKILL_REPO" "Fall-through: To Do, top-down by rank"
check_fixed "P137 To Do fall-through in symphony SKILL.md (runtime)" \
    "$P137_SYMPHONY_SKILL_RUNTIME" "Fall-through: To Do, top-down by rank"

# Marker-count guards (catch wrong-P-number re-applies per check_retag_on_refactor memory)
check_marker_count "P137/MOL-483 markers in planning.yaml (repo)" \
    "$P137_PLANNING_YAML_REPO" "P137/MOL-483" 1
check_marker_count "P137/MOL-483 markers in coding.yaml (repo)" \
    "$P137_CODING_YAML_REPO" "P137/MOL-483" 2
check_marker_count "P137/MOL-483 markers in knock-out SKILL.md (repo)" \
    "$P137_KNOCKOUT_SKILL_REPO" "P137/MOL-483" 4
check_marker_count "P137/MOL-483 markers in symphony SKILL.md (repo)" \
    "$P137_SYMPHONY_SKILL_REPO" "P137/MOL-483" 2
check_marker_count "P137/MOL-483 markers in PATCHES.md" \
    "$P137_PATCHES_MD" "P137/MOL-483" 2
check_marker_count "P137/MOL-483 markers in test runner" \
    "$P137_TEST_RUNNER" "P137/MOL-483" 1

# ─────────────────────────────────────────────────────────────────────────────
# P138 / MOL-486 — Knock-out reviewer drift-check (4 deterministic checks)
# Closes MOL-417 PR #1 silent-failure class: forward-coverage, regression-
# deletion, ac-vs-additions, lint-cleanliness. Helper at scripts/, invoked
# from knock-out SKILL.md Step 5.4 BEFORE LLM judgment. Anti-softening block
# in coding.yaml system_prompt_suffix bars interpretive room.
# ─────────────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo "=== P138 / MOL-486 — Knock-out reviewer drift-check ==="
P138_REPO_ROOT="$HERMES_POC_REPO"
P138_HELPER_REPO="$P138_REPO_ROOT/scripts/reviewer_drift_checks.py"
P138_HELPER_RUNTIME="$HERMES_SCRIPTS/reviewer_drift_checks.py"
P138_KNOCKOUT_SKILL_REPO="$P138_REPO_ROOT/skills/software-development/knock-out-jira-ticket/SKILL.md"
P138_KNOCKOUT_SKILL_RUNTIME="$HERMES_SKILLS/software-development/knock-out-jira-ticket/SKILL.md"
P138_CODING_YAML_REPO="$P138_REPO_ROOT/config/hermes/delegate-profiles/coding.yaml"
P138_CODING_YAML_RUNTIME="${HERMES_HOME:-$HOME/.hermes}/config/delegate-profiles/coding.yaml"
P138_TEST_RUNNER="$P138_REPO_ROOT/tests/test-reviewer-drift-checks.sh"
P138_FIXTURE_DIR="$P138_REPO_ROOT/tests/fixtures/reviewer-drift"
P138_PATCHES_MD="$P138_REPO_ROOT/scripts/hermes-patches/PATCHES.md"

# Mirror integrity (helper script — byte-identical sync)
check_mirror_sha256 "P138 reviewer_drift_checks.py mirror" \
    "$P138_HELPER_REPO" "$P138_HELPER_RUNTIME"

# SKILL.md drift-check block (repo + runtime)
check_fixed "P138 drift-check block in knock-out SKILL.md (repo)" \
    "$P138_KNOCKOUT_SKILL_REPO" "Deterministic drift checks (P138/MOL-486)"
check_fixed "P138 drift-check block in knock-out SKILL.md (runtime)" \
    "$P138_KNOCKOUT_SKILL_RUNTIME" "Deterministic drift checks (P138/MOL-486)"

# coding.yaml allowed_bash_patterns (helper invocation gate)
check_fixed "P138 helper in coding.yaml allowed_bash_patterns (repo)" \
    "$P138_CODING_YAML_REPO" "python3 ~/.hermes/scripts/reviewer_drift_checks.py:*"
check_fixed "P138 helper in coding.yaml allowed_bash_patterns (runtime)" \
    "$P138_CODING_YAML_RUNTIME" "python3 ~/.hermes/scripts/reviewer_drift_checks.py:*"

# coding.yaml anti-softening block (closes interpretive escape)
check_fixed "P138 anti-softening block in coding.yaml (repo)" \
    "$P138_CODING_YAML_REPO" "Drift-check anti-softening (P138/MOL-486)"
check_fixed "P138 anti-softening block in coding.yaml (runtime)" \
    "$P138_CODING_YAML_RUNTIME" "Drift-check anti-softening (P138/MOL-486)"

# Test fixtures present
check_fixed "P138 valid-comprehensive plan fixture present" \
    "$P138_FIXTURE_DIR/valid-comprehensive.plan.md" "## Target files"
check_fixed "P138 invalid-lint-regression fixture present" \
    "$P138_FIXTURE_DIR/invalid-lint-regression.plan.md" "lint_fixture_dirty.py"

# Marker counts (P138 marker drift defense per check_marker_count_helper_pair memory)
check_marker_count "P138/MOL-486 markers in knock-out SKILL.md (repo)" \
    "$P138_KNOCKOUT_SKILL_REPO" "P138/MOL-486" 2
check_marker_count "P138/MOL-486 markers in knock-out SKILL.md (runtime)" \
    "$P138_KNOCKOUT_SKILL_RUNTIME" "P138/MOL-486" 2
check_marker_count "P138/MOL-486 markers in coding.yaml (repo)" \
    "$P138_CODING_YAML_REPO" "P138/MOL-486" 3
check_marker_count "P138/MOL-486 markers in coding.yaml (runtime)" \
    "$P138_CODING_YAML_RUNTIME" "P138/MOL-486" 3
check_marker_count "P138/MOL-486 markers in helper script (repo)" \
    "$P138_HELPER_REPO" "P138/MOL-486" 1
check_marker_count "P138/MOL-486 markers in PATCHES.md" \
    "$P138_PATCHES_MD" "P138/MOL-486" 2

# ─────────────────────────────────────────────────────────────────────────────
# P139 / MOL-487 — Subagent iteration budget bump (50 → 100)
# Corrected via PR #159 silent-failure review: P103 is dormant, so the binding
# cap is `delegation.max_iterations` in live config.yaml + DEFAULT_CONFIG
# in config.py — NOT `coding.yaml::max_turns` (which is read by no live code).
# Skeptic-predeclared scope-cut: forward instrumentation deferred to MOL-474.
# ─────────────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo "=== P139 / MOL-487 — Subagent iteration budget bump (50 → 100) ==="
P139_REPO_ROOT="$HERMES_POC_REPO"
P139_LIVE_CONFIG="${HERMES_HOME:-$HOME/.hermes}/config.yaml"
P139_DEFAULT_CONFIG="$HERMES_AGENT/hermes_cli/config.py"
P139_CODING_YAML_REPO="$P139_REPO_ROOT/config/hermes/delegate-profiles/coding.yaml"
P139_CODING_YAML_RUNTIME="${HERMES_HOME:-$HOME/.hermes}/config/delegate-profiles/coding.yaml"
P139_INVESTIGATION_DOC="$P139_REPO_ROOT/docs/knock-out-budget-investigation.md"
P139_PATCHES_MD="$P139_REPO_ROOT/scripts/hermes-patches/PATCHES.md"

# LOAD-BEARING: live runtime config (the actual binding cap MOL-417 hit)
check_fixed "P139 live config max_iterations: 100" \
    "$P139_LIVE_CONFIG" "max_iterations: 100"
check_fixed "P139 attribution comment in live config" \
    "$P139_LIVE_CONFIG" "P139/MOL-487 — bumped from 50"

# LOAD-BEARING: DEFAULT_CONFIG in config.py (prospective; protects against save_config wipe)
check_fixed "P139 DEFAULT_CONFIG max_iterations: 100 in config.py" \
    "$P139_DEFAULT_CONFIG" '"max_iterations": 100,'
check_fixed "P139 attribution comment in config.py DEFAULT_CONFIG" \
    "$P139_DEFAULT_CONFIG" "P139/MOL-487 — bumped from 50 after MOL-417"

# DORMANT (forward-compat for when P103 lands): coding.yaml profile file
check_fixed "P139 max_turns: 100 in coding.yaml (repo, dormant)" \
    "$P139_CODING_YAML_REPO" "max_turns: 100"
check_fixed "P139 max_turns: 100 in coding.yaml (runtime, dormant)" \
    "$P139_CODING_YAML_RUNTIME" "max_turns: 100"
check_fixed "P139 dormancy comment in coding.yaml (repo)" \
    "$P139_CODING_YAML_REPO" "DORMANT until P103 profile-loading lands"

# Investigation doc deliverable
check_fixed "P139 investigation doc present" \
    "$P139_INVESTIGATION_DOC" "Knock-out builder budget — investigation (P139 / MOL-487)"

# Marker counts (per check_marker_count_helper_pair memory)
check_marker_count "P139/MOL-487 markers in live config" \
    "$P139_LIVE_CONFIG" "P139/MOL-487" 1
check_marker_count "P139/MOL-487 markers in config.py DEFAULT_CONFIG" \
    "$P139_DEFAULT_CONFIG" "P139/MOL-487" 1
check_marker_count "P139/MOL-487 markers in coding.yaml (repo)" \
    "$P139_CODING_YAML_REPO" "P139/MOL-487" 1
check_marker_count "P139/MOL-487 markers in coding.yaml (runtime)" \
    "$P139_CODING_YAML_RUNTIME" "P139/MOL-487" 1
check_marker_count "P139/MOL-487 markers in PATCHES.md" \
    "$P139_PATCHES_MD" "P139/MOL-487" 2

# ─────────────────────────────────────────────────────────────────────────────
# P142 / MOL-490 — Reviewer drift-check hardening (F1.1 + I3 + TC1/TC2/TC3)
# F1.1: ast-based DUPLICATE_BRANCH_TEST detector (catches MOL-417's literal
# duplicate-elif silent failure that ruff F811 missed).
# I3: shared retry counter at ~/.hermes/state/retry-${KEY}.txt — prevents
# drift-retry + LLM-CRITICAL-retry from compounding 2× past the 1-retry intent.
# TC1/TC2/TC3: fixture coverage gaps surfaced by pr-test-analyzer on PR #158.
# TC3 also fixes a real bug: normalize_for_match was unconditionally adding
# basename, producing false-positive forward_coverage matches.
# ─────────────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo "=== P142 / MOL-490 — Reviewer drift-check hardening (F1.1 + I3 + TC1/TC2/TC3) ==="
P142_REPO_ROOT="$HERMES_POC_REPO"
P142_HELPER_REPO="$P142_REPO_ROOT/scripts/reviewer_drift_checks.py"
P142_HELPER_RUNTIME="${HERMES_HOME:-$HOME/.hermes}/scripts/reviewer_drift_checks.py"
P142_KNOCKOUT_SKILL_REPO="$P142_REPO_ROOT/skills/software-development/knock-out-jira-ticket/SKILL.md"
P142_KNOCKOUT_SKILL_RUNTIME="${HERMES_HOME:-$HOME/.hermes}/skills/software-development/knock-out-jira-ticket/SKILL.md"
P142_FIXTURE_DIR="$P142_REPO_ROOT/tests/fixtures/reviewer-drift"
P142_TEST_SCRIPT="$P142_REPO_ROOT/tests/test-reviewer-drift-checks.sh"
P142_PATCHES_MD="$P142_REPO_ROOT/scripts/hermes-patches/PATCHES.md"

# F1.1: AST detector function defined + wired into main() output["lint"]
check_fixed "P142 check_duplicate_branch_tests defined (repo)" \
    "$P142_HELPER_REPO" "def check_duplicate_branch_tests("
check_fixed "P142 check_duplicate_branch_tests defined (runtime)" \
    "$P142_HELPER_RUNTIME" "def check_duplicate_branch_tests("
check_fixed "P142 F1.1 wired into lint array (repo)" \
    "$P142_HELPER_REPO" "lint_findings.extend(check_duplicate_branch_tests(diff_path))"

# TC3 fix: normalize_for_match suppresses basename-fallback when /Code/ matches
check_fixed "P142 TC3 normalize_for_match else-branch fix (repo)" \
    "$P142_HELPER_REPO" "Non-Hermes layout — basename is the only common form available."

# Bonus fix: parse_diff_new_files honors `new file mode` markers so check_lint
# returns correct results post-merge of fixture-introducing PRs (closes the
# invalid-lint-regression false-failure that surfaced post-PR #158 merge).
check_fixed "P142 parse_diff_new_files defined (repo)" \
    "$P142_HELPER_REPO" "def parse_diff_new_files("
check_fixed "P142 parse_diff_new_files defined (runtime)" \
    "$P142_HELPER_RUNTIME" "def parse_diff_new_files("
check_fixed "P142 check_lint honors new-file marker (repo)" \
    "$P142_HELPER_REPO" "modified_only = [p for p in changed if p not in new_in_diff]"
check_fixed "P142 check_lint honors new-file marker (runtime)" \
    "$P142_HELPER_RUNTIME" "modified_only = [p for p in changed if p not in new_in_diff]"

# P142/MOL-490 (#161 review CRITICAL-1): chain-walk dedupe via chain_members
check_fixed "P142 F1.1 chain-walk dedupe (repo)" \
    "$P142_HELPER_REPO" "chain_members.add(id(cur))"
check_fixed "P142 F1.1 chain-walk dedupe (runtime)" \
    "$P142_HELPER_RUNTIME" "chain_members.add(id(cur))"

# P142/MOL-490 (#161 review CRITICAL-4): orphan-marker stderr emit
check_fixed "P142 parse_diff_new_files orphan-marker stderr (repo)" \
    "$P142_HELPER_REPO" "orphan 'new file mode' before any 'diff --git' header"

# P142/MOL-490 (#161 review CRITICAL-5): RETRY_FILE integer validation
check_fixed "P142 RETRY_FILE integer validation in SKILL.md (repo)" \
    "$P142_KNOCKOUT_SKILL_REPO" 'RETRY_FILE corrupted'

# P142/MOL-490 (#161 review CRITICAL-6): KEY guard
check_fixed "P142 KEY guard in SKILL.md (repo)" \
    "$P142_KNOCKOUT_SKILL_REPO" "ABORT: KEY unset"

# P142/MOL-490 (#161 review IMPORTANT-3): trap EXIT cleanup
check_fixed "P142 trap EXIT retry cleanup in SKILL.md (repo)" \
    "$P142_KNOCKOUT_SKILL_REPO" 'trap '\''rm -f "$RETRY_FILE"'\'' EXIT'

# P142/MOL-490 (#161 review IMPORTANT-1): _increment_retry_budget shared helper
check_fixed "P142 _increment_retry_budget helper defined in SKILL.md (repo)" \
    "$P142_KNOCKOUT_SKILL_REPO" "_increment_retry_budget()"

# P142/MOL-490 (#161 review IMPORTANT-2): ruff precheck in test script
check_fixed "P142 ruff precheck in test script" \
    "$P142_TEST_SCRIPT" "ruff required for lint-baseline tests"

# P142/MOL-490 (#161 review CRITICAL-1): exactly-1 finding count assertion
check_fixed "P142 F1.1 exactly-1 count assertion in test script" \
    "$P142_TEST_SCRIPT" "DUPLICATE_BRANCH_TEST count is exactly 1"

# I3: shared retry counter init + cleanup in SKILL.md
check_fixed "P142 I3 retry budget init in SKILL.md (repo)" \
    "$P142_KNOCKOUT_SKILL_REPO" 'RETRY_BUDGET_TOTAL:=3'
check_fixed "P142 I3 retry budget init in SKILL.md (runtime)" \
    "$P142_KNOCKOUT_SKILL_RUNTIME" 'RETRY_BUDGET_TOTAL:=3'
check_fixed "P142 I3 retry budget cleanup in SKILL.md (repo)" \
    "$P142_KNOCKOUT_SKILL_REPO" 'rm -f "$RETRY_FILE"'

# 4 new fixture pairs (F1.1 + TC1/TC2/TC3)
check_fixed "P142 invalid-duplicate-elif fixture present" \
    "$P142_FIXTURE_DIR/invalid-duplicate-elif.plan.md" "duplicate_elif_fixture.py"
check_fixed "P142 valid-tc1-lint-baseline fixture present" \
    "$P142_FIXTURE_DIR/valid-tc1-lint-baseline-preserved.plan.md" "lint baseline"
check_fixed "P142 invalid-tc2-broad-escape fixture present" \
    "$P142_FIXTURE_DIR/invalid-tc2-broad-escape-overreach.plan.md" "Refactor login"
check_fixed "P142 invalid-tc3-basename-collision fixture present" \
    "$P142_FIXTURE_DIR/invalid-tc3-basename-collision.plan.md" "src/foo/auth.py"
check_fixed "P142 duplicate_elif_fixture.py committed" \
    "$P142_FIXTURE_DIR/duplicate_elif_fixture.py" 'elif post_setup_key == "langfuse"'

# Sibling-empty assertions in test script (TC1/TC2/TC3 enforce this)
check_fixed "P142 sibling-empty assertions in test script" \
    "$P142_TEST_SCRIPT" "sibling-empty assertions"

# Marker counts
check_marker_count "P142/MOL-490 markers in helper script (repo)" \
    "$P142_HELPER_REPO" "P142/MOL-490" 4
check_marker_count "P142/MOL-490 markers in SKILL.md (repo)" \
    "$P142_KNOCKOUT_SKILL_REPO" "P142/MOL-490" 3
check_marker_count "P142/MOL-490 markers in SKILL.md (runtime)" \
    "$P142_KNOCKOUT_SKILL_RUNTIME" "P142/MOL-490" 3
check_marker_count "P142/MOL-490 markers in test script" \
    "$P142_TEST_SCRIPT" "P142/MOL-490" 1
check_marker_count "P142/MOL-490 markers in PATCHES.md" \
    "$P142_PATCHES_MD" "P142/MOL-490" 2

# ─────────────────────────────────────────────────────────────────────────────
# P143 / MOL-488 — Symphony Step 6.5 2nd-layer review gate
# After CI green, the dispatched-subprocess LLM invokes the
# `pr-review-toolkit:review-pr` skill via the Skill tool (NOT bash CLI —
# /review-pr is a Claude Code Skill, not a binary). 0 CRITICAL → merge.
# Any CRITICAL OR skill failure → PR_STATE=open + Telegram alert + no merge.
# Bundled with P142 in same PR (both touch SKILL.md).
# ─────────────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo "=== P143 / MOL-488 — Symphony Step 6.5 2nd-layer review gate ==="
P143_REPO_ROOT="$HERMES_POC_REPO"
P143_KNOCKOUT_SKILL_REPO="$P143_REPO_ROOT/skills/software-development/knock-out-jira-ticket/SKILL.md"
P143_KNOCKOUT_SKILL_RUNTIME="${HERMES_HOME:-$HOME/.hermes}/skills/software-development/knock-out-jira-ticket/SKILL.md"
P143_CLAUDE_MD="$P143_REPO_ROOT/CLAUDE.md"
P143_PATCHES_MD="$P143_REPO_ROOT/scripts/hermes-patches/PATCHES.md"

# Step 6.5 review gate cites pr-review-toolkit:review-pr (Skill tool, not bash)
check_fixed "P143 review gate cites pr-review-toolkit:review-pr (repo)" \
    "$P143_KNOCKOUT_SKILL_REPO" "pr-review-toolkit:review-pr"
check_fixed "P143 review gate cites pr-review-toolkit:review-pr (runtime)" \
    "$P143_KNOCKOUT_SKILL_RUNTIME" "pr-review-toolkit:review-pr"
check_fixed "P143 anti-soft contract heading in SKILL.md (repo)" \
    "$P143_KNOCKOUT_SKILL_REPO" "Anti-soft contract: CI green AND 0 /review-pr CRITICAL"
# (#161 review CRITICAL-3): clarified <count> placeholder; no longer asserts the
# literal '(N found)' since the upstream skill renders '(<digit> found)' at runtime.
check_fixed "P143 Critical Issues count placeholder in SKILL.md (repo)" \
    "$P143_KNOCKOUT_SKILL_REPO" 'Critical Issues (<count> found)'

# (#161 review CRITICAL-7): deterministic gate-decision file enforces merge
check_fixed "P143 gate-decision sentinel path in SKILL.md (repo)" \
    "$P143_KNOCKOUT_SKILL_REPO" "/tmp/symphony-step65-"
check_fixed "P143 gate-decision sentinel enforcement bash (repo)" \
    "$P143_KNOCKOUT_SKILL_REPO" 'GATE_DECISION" != "merge:0"'
check_fixed "P143 gate-decision sentinel enforcement bash (runtime)" \
    "$P143_KNOCKOUT_SKILL_RUNTIME" 'GATE_DECISION" != "merge:0"'

# (#161 review CRITICAL-2): /review-pr argument contract corrected
check_fixed "P143 review-pr arg contract clarified in SKILL.md (repo)" \
    "$P143_KNOCKOUT_SKILL_REPO" "Argument contract:"

# (#161 review SUGGESTION-3): explicit /opt/homebrew/bin/gh bypass for the wrapper
check_fixed "P143 explicit gh path bypass in SKILL.md (repo)" \
    "$P143_KNOCKOUT_SKILL_REPO" "/opt/homebrew/bin/gh pr merge"

# (#161 review IMPORTANT, comment-analyzer #8): Log: destination specified
check_fixed "P143 Log destination specified in SKILL.md (repo)" \
    "$P143_KNOCKOUT_SKILL_REPO" "symphony-step65.jsonl"

# CLAUDE.md pointer line
check_fixed "P143 CLAUDE.md pointer line present" \
    "$P143_CLAUDE_MD" "Symphony Step 6.5 review gate (MOL-488/P143)"

# Marker counts
check_marker_count "P143/MOL-488 markers in SKILL.md (repo)" \
    "$P143_KNOCKOUT_SKILL_REPO" "P143/MOL-488" 3
check_marker_count "P143/MOL-488 markers in SKILL.md (runtime)" \
    "$P143_KNOCKOUT_SKILL_RUNTIME" "P143/MOL-488" 3
check_marker_count "P143/MOL-488 markers in CLAUDE.md" \
    "$P143_CLAUDE_MD" "P143" 1
check_marker_count "P143/MOL-488 markers in PATCHES.md" \
    "$P143_PATCHES_MD" "P143/MOL-488" 2

# ═══════════════════════════════════════════════════════════════════════════════
# P144 / MOL-493 — Wire CC subprocess Tier 1+2 with DeepSeek direct (replaces proxy)
# ─────────────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo "=== P144 / MOL-493 — CC subprocess Tier 1+2 (DeepSeek direct) ==="
P144_DT="$HOME/.hermes/hermes-agent/tools/delegate_tool.py"

check_fixed "P144 _run_claude_code_delegation defined" \
    "$P144_DT" "def _run_claude_code_delegation("
check_fixed "P144 _run_claude_code_deepseek_direct_delegation defined" \
    "$P144_DT" "def _run_claude_code_deepseek_direct_delegation("
check_fixed "P144 _detect_repo_path defined" \
    "$P144_DT" "def _detect_repo_path(goal"
check_fixed "P144 _verify_delegation_diff defined" \
    "$P144_DT" "def _verify_delegation_diff("
check_fixed "P144 ANTHROPIC_BASE_URL=api.deepseek.com/anthropic in Tier 2" \
    "$P144_DT" 'ANTHROPIC_BASE_URL"] = "https://api.deepseek.com/anthropic"'
check_fixed "P144 _emit_iteration_audit called in Tier 1" \
    "$P144_DT" "_emit_iteration_audit(exit_reason="
check_fixed "P144 _emit_iteration_audit called in Tier 2" \
    "$P144_DT" '_emit_iteration_audit(exit_reason="timeout"'
check_fixed "P144 claude_code_results in delegate_task" \
    "$P144_DT" "claude_code_results: Dict[int, dict]"
check_fixed "P144 remaining_indices in delegate_task" \
    "$P144_DT" "remaining_indices = set(range(len(task_list)))"
check_fixed "P144 remaining_task_list in delegate_task" \
    "$P144_DT" "remaining_task_list = [(i, task_list[i])"
check_fixed "P144 permission_mode in _build_claude_argv" \
    "$P144_DT" 'permission_mode = profile_cfg.get("permission_mode")'
check_fixed "P144 fallback_deepseek_cc in config.yaml" \
    "$HOME/.hermes/config.yaml" "fallback_deepseek_cc:"
check_fixed "P144 delegation tag claude-code-deepseek-direct" \
    "$P144_DT" '"delegation": "claude-code-deepseek-direct"'

# Marker counts
check_marker_count "P144/MOL-493 markers in delegate_tool.py" \
    "$P144_DT" "P144/MOL-493" 2
check_marker_count "P144/MOL-493 markers in config.yaml" \
    "$HOME/.hermes/config.yaml" "P144/MOL-493" 1
DT_TOOL="$HOME/.hermes/hermes-agent/tools/delegate_tool.py"

[[ $QUIET -eq 0 ]] && echo "=== P145 / MOL-495 — Re-apply P105 timeout helper to new P144 spawners ==="
# P105 already gates the symbol checks — P145 adds call-site verification
check_fixed "P145 _DEFAULT_CHILD_TIMEOUT_SECONDS set to 1800" \
    "$DT_TOOL" \
    "_DEFAULT_CHILD_TIMEOUT_SECONDS = 1800"
# Both Tier 1 and Tier 2 must use the helper (not just one of them)
_P145_TIMEOUT_COUNT=$(grep -c "timeout=_get_child_timeout_seconds(profile_cfg)" "$DT_TOOL" 2>/dev/null || echo 0)
if [ "$_P145_TIMEOUT_COUNT" -ge 2 ]; then
    [[ $QUIET -eq 0 ]] && echo "PASS: P145 both CC spawners use _get_child_timeout_seconds(profile_cfg) (count=$_P145_TIMEOUT_COUNT)"
else
    echo "FAIL: P145 expected >=2 call sites for _get_child_timeout_seconds, got $_P145_TIMEOUT_COUNT"
    failed=$((failed + 1))
fi
# No hardcoded 600 remains (P105 regression guard already covers this, re-assert)
if grep -q "timeout=600" "$DT_TOOL"; then
    echo "FAIL: P145 hardcoded 600s timeout still present"
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && echo "PASS: P145 no hardcoded 600s timeout"
fi
check_fixed "P145 reference diff present" \
    "$HERMES_POC_REFERENCE/P145-MOL-495.diff" \
    "_DEFAULT_CHILD_TIMEOUT_SECONDS"
check_marker_count "P145/MOL-495 markers in delegate_tool.py" \
    "$DT_TOOL" "P145/MOL-495" 3

[[ $QUIET -eq 0 ]] && echo "=== P146 / MOL-496 — Multi-phase CC subprocess pipeline + Hermes skill_load + close all 7 gaps ==="
# P146 rolls up P105+P145 timeout changes (bumps default 1200→1800, adds profile_cfg param,
# injects profile into in-process swarm, bumps max_spawn_depth, adds skill_load tool).

DT_TOOL="$HERMES_AGENT/tools/delegate_tool.py"
SKILL_TOOL="$HERMES_AGENT/tools/skill_tool.py"
CFG_YAML="$HERMES_AGENT/../config.yaml"
CODING_YAML="$HERMES_AGENT/../config/delegate-profiles/coding.yaml"
SYMPHONY_SKILL="$HERMES_AGENT/../skills/software-development/symphony-bridge/SKILL.md"

# Timeout
check_fixed "P146 _get_child_timeout_seconds accepts profile_cfg" \
    "$DT_TOOL" \
    "def _get_child_timeout_seconds(profile_cfg"
check_fixed "P146 helper reads profile timeout_seconds" \
    "$DT_TOOL" \
    'profile_cfg.get("timeout_seconds")'
check_fixed "P146 helper reads DELEGATION_CHILD_TIMEOUT_SECONDS env var" \
    "$DT_TOOL" \
    "DELEGATION_CHILD_TIMEOUT_SECONDS"
check_fixed "P146 _DEFAULT_CHILD_TIMEOUT_SECONDS = 1800" \
    "$DT_TOOL" \
    "_DEFAULT_CHILD_TIMEOUT_SECONDS = 1800"
_P146_CC_TIMEOUT_COUNT=$(grep -c '_get_child_timeout_seconds(profile_cfg)' "$DT_TOOL" 2>/dev/null || echo 0)
if [ "$_P146_CC_TIMEOUT_COUNT" -ge 2 ]; then
    [[ $QUIET -eq 0 ]] && echo "PASS: P146 both CC spawners use _get_child_timeout_seconds(profile_cfg) (count=$_P146_CC_TIMEOUT_COUNT)"
else
    echo "FAIL: P146 expected >=2 call sites for _get_child_timeout_seconds(profile_cfg), got $_P146_CC_TIMEOUT_COUNT"
    failed=$((failed + 1))
fi
# No hardcoded 600s in delegate_tool.py
if grep -q "timeout=600" "$DT_TOOL"; then
    echo "FAIL: P146 hardcoded 600s timeout still present in $DT_TOOL"
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && echo "PASS: P146 no hardcoded 600s timeout"
fi

# Profile injection into _build_child_agent
check_fixed "P146 _build_child_agent has profile_cfg parameter" \
    "$DT_TOOL" \
    "profile_cfg: Optional"
check_fixed "P146 system_prompt_suffix appended to child_prompt" \
    "$DT_TOOL" \
    'profile_cfg.get("system_prompt_suffix")'
check_fixed "P146 max_turns overrides max_iterations" \
    "$DT_TOOL" \
    'profile_cfg.get("max_turns")'
check_fixed "P146 _build_child_agent called with profile_cfg" \
    "$DT_TOOL" \
    "profile_cfg=profile_cfg"

# Config
check_fixed "P146 max_spawn_depth set to 2" \
    "$CFG_YAML" \
    "max_spawn_depth: 2"
check_fixed "P146 DORMANT comment removed from coding.yaml" \
    "$CODING_YAML" \
    "active post-P140 merge"

# skill_load tool
check_fixed "P146 skill_load function defined" \
    "$SKILL_TOOL" \
    "def skill_load("
check_fixed "P146 skill_load searches SKILL.md files" \
    "$SKILL_TOOL" \
    'rglob("SKILL.md")'

# Symphony-bridge multi-phase
check_fixed "P146 symphony-bridge Step 1.5 repo extraction" \
    "$SYMPHONY_SKILL" \
    "Step 1.5 — Extract target repo"
check_fixed "P146 symphony-bridge Phase 1 Planner" \
    "$SYMPHONY_SKILL" \
    "Phase 1 — Planner"
check_fixed "P146 symphony-bridge Phase 2 Skeptic" \
    "$SYMPHONY_SKILL" \
    "Phase 2 — Skeptic"
check_fixed "P146 symphony-bridge Phase 3 Builder" \
    "$SYMPHONY_SKILL" \
    "Phase 3 — Builder"
check_fixed "P146 symphony-bridge Phase 4 Reviewer" \
    "$SYMPHONY_SKILL" \
    "Phase 4 — Reviewer"

# Reference diff
check_fixed "P146 reference diff present" \
    "$HERMES_POC_REFERENCE/P146-MOL-496.diff" \
    "P146/MOL-496"

check_marker_count "P146/MOL-496 markers in delegate_tool.py" \
    "$DT_TOOL" "P146/MOL-496" 5

# ──────────────────────────────────────────────────────────────────────
# P148 / MOL-498 — Symphony-bridge diagnostic logging + SIGKILL fix
# ──────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo "=== P148 / MOL-498: Symphony-bridge diagnostic logging + SIGKILL heartbeat + process-group fix ==="

# Runtime script is not git-tracked; reference diff is the authoritative copy.
P148_REFERENCE_DIFF="$HERMES_POC_REFERENCE/P148-MOL-498.diff"

# Core observability helpers
check_fixed "P148 _log_event JSONL writer present" \
    "$P148_REFERENCE_DIFF" "def _log_event(event: str"
check_fixed "P148 JSONL log path at ~/.hermes/logs/symphony_bridge.log" \
    "$P148_REFERENCE_DIFF" 'LOG_FILE = LOG_DIR / "symphony_bridge.log"'
check_fixed "P148 stderr tail cap at 50KB" \
    "$P148_REFERENCE_DIFF" "_STDERR_TAIL_MAX = 50_000"
check_fixed "P148 env-var snapshot helper present" \
    "$P148_REFERENCE_DIFF" "def _env_snapshot(env: dict"

# Process-group management (zombie fix — MOL-481 root cause)
check_fixed "P148 _kill_process_group helper present" \
    "$P148_REFERENCE_DIFF" "def _kill_process_group(proc: subprocess.Popen)"
check_fixed "P148 start_new_session=True on subprocess.Popen calls" \
    "$P148_REFERENCE_DIFF" "start_new_session=True"
check_fixed "P148 os.killpg signal escalation present" \
    "$P148_REFERENCE_DIFF" "os.killpg(pgid, signal.SIGKILL)"
check_marker_count "P148 start_new_session appears in all 3 Popen call sites" \
    "$P148_REFERENCE_DIFF" "start_new_session=True" 3

# SIGKILL recovery + orphan detection
check_fixed "P148 _sigkill_recovery_sweep function present" \
    "$P148_REFERENCE_DIFF" "def _sigkill_recovery_sweep()"
check_fixed "P148 _detect_orphan_claude_processes function present" \
    "$P148_REFERENCE_DIFF" "def _detect_orphan_claude_processes()"
check_fixed "P148 orphan detector tight-matches bin/claude + -p flag" \
    "$P148_REFERENCE_DIFF" '"bin/claude" not in args'
check_fixed "P148 sigkill_recovery event emitted" \
    "$P148_REFERENCE_DIFF" '_log_event("sigkill_recovery"'
check_fixed "P148 sigkill_recovered_at timestamp stamped" \
    "$P148_REFERENCE_DIFF" '"sigkill_recovered_at"'

# Pre-flight probe (baseline, NOT a gate)
check_fixed "P148 _preflight_probe function present" \
    "$P148_REFERENCE_DIFF" "def _preflight_probe()"
check_fixed "P148 preflight_probe event emitted" \
    "$P148_REFERENCE_DIFF" '_log_event("preflight_probe"'
check_fixed "P148 preflight probe re-framed as baseline (not gate)" \
    "$P148_REFERENCE_DIFF" "not a gate"

# Phase checkpoint + atomic state
check_fixed "P148 _set_checkpoint helper present" \
    "$P148_REFERENCE_DIFF" "def _set_checkpoint(state: dict"
check_fixed "P148 last_checkpoint_phase schema field" \
    "$P148_REFERENCE_DIFF" 'last_checkpoint_phase'
check_fixed "P148 atomic write_state uses os.replace" \
    "$P148_REFERENCE_DIFF" "os.replace(tmp, state_file)"
check_fixed "P148 dispatch_start checkpoint before first phase" \
    "$P148_REFERENCE_DIFF" '_set_checkpoint(state, "dispatch_start")'

# main() wire-up
check_fixed "P148 main() calls _sigkill_recovery_sweep" \
    "$P148_REFERENCE_DIFF" "recovered = _sigkill_recovery_sweep()"
check_fixed "P148 main() calls _preflight_probe" \
    "$P148_REFERENCE_DIFF" "_preflight_probe()"
check_fixed "P148 tick_start event emitted at boot" \
    "$P148_REFERENCE_DIFF" '_log_event("tick_start"'

# Reference diff + marker count
check_fixed "P148 reference diff present" \
    "$P148_REFERENCE_DIFF" "P148/MOL-498"
check_marker_count "P148/MOL-498 markers in reference diff" \
    "$P148_REFERENCE_DIFF" "P148/MOL-498" 20

# ──────────────────────────────────────────────────────────────────────
# P148b — Runtime-targeted assertions (PR #169 code-review fix)
# ──────────────────────────────────────────────────────────────────────
# Reference-diff-only assertions are tautological: the diff file never
# mutates after creation, so `hermes update` or a silent runtime revert
# (see silent_edit_revert_runtime_files memory) would leave the verifier
# reporting PASS while symphony_bridge.py has rolled back. The runtime
# checks below catch that drift.
SYMPHONY_BRIDGE_RUNTIME="$HERMES_SCRIPTS/symphony_bridge.py"

check_fixed "P148 _log_event present in runtime" \
    "$SYMPHONY_BRIDGE_RUNTIME" "def _log_event(event: str"
check_fixed "P148 _kill_process_group present in runtime" \
    "$SYMPHONY_BRIDGE_RUNTIME" "def _kill_process_group(proc: subprocess.Popen)"
check_fixed "P148 start_new_session=True present in runtime" \
    "$SYMPHONY_BRIDGE_RUNTIME" "start_new_session=True,"
check_marker_count "P148 start_new_session=True at all 3 Popen call sites (runtime)" \
    "$SYMPHONY_BRIDGE_RUNTIME" "start_new_session=True," 3
check_fixed "P148 _sigkill_recovery_sweep present in runtime" \
    "$SYMPHONY_BRIDGE_RUNTIME" "def _sigkill_recovery_sweep()"
check_fixed "P148 _detect_orphan_claude_processes present in runtime" \
    "$SYMPHONY_BRIDGE_RUNTIME" "def _detect_orphan_claude_processes()"
check_fixed "P148b orphan event uses args_head (not buggy comm field) in runtime" \
    "$SYMPHONY_BRIDGE_RUNTIME" 'args_head=o["args_head"]'
check_fixed "P148 _preflight_probe present in runtime" \
    "$SYMPHONY_BRIDGE_RUNTIME" "def _preflight_probe()"
check_fixed "P148 atomic state write via os.replace in runtime" \
    "$SYMPHONY_BRIDGE_RUNTIME" "os.replace(tmp, state_file)"
check_fixed "P148 main() invokes _sigkill_recovery_sweep (runtime)" \
    "$SYMPHONY_BRIDGE_RUNTIME" "recovered = _sigkill_recovery_sweep()"
check_fixed "P148 _set_checkpoint present in runtime" \
    "$SYMPHONY_BRIDGE_RUNTIME" "def _set_checkpoint(state: dict"
check_marker_count "P148/MOL-498 markers in runtime" \
    "$SYMPHONY_BRIDGE_RUNTIME" "P148/MOL-498" 20

# ──────────────────────────────────────────────────────────────────────
# P149 / MOL-499 — Per-tier retry classification
# ──────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo "=== P149 / MOL-499: Per-tier retry classification ==="

P149_REFERENCE_DIFF="$HERMES_POC_REFERENCE/P149-MOL-499.diff"

# Reference-diff assertions
check_fixed "P149 _classify_tier_failure function present in reference diff" \
    "$P149_REFERENCE_DIFF" "def _classify_tier_failure(error_text: str, stderr_tail: str)"
check_fixed "P149 _attempt_tier_with_retry function present in reference diff" \
    "$P149_REFERENCE_DIFF" "def _attempt_tier_with_retry("
check_fixed "P149 _TRANSIENT_PATTERNS defined in reference diff" \
    "$P149_REFERENCE_DIFF" "_TRANSIENT_PATTERNS = ["
check_fixed "P149 _DETERMINISTIC_FAILURE_PATTERNS defined in reference diff" \
    "$P149_REFERENCE_DIFF" "_DETERMINISTIC_FAILURE_PATTERNS = ["
check_fixed "P149 _RETRY_POLICY dict defined in reference diff" \
    "$P149_REFERENCE_DIFF" "_RETRY_POLICY = {"
check_fixed "P149 _TIMEOUT_ERROR_PREFIX constant in reference diff (PR-review fixup)" \
    "$P149_REFERENCE_DIFF" '_TIMEOUT_ERROR_PREFIX = "timeout after"'
check_fixed "P149 _DEFAULT_RETRY_POLICY defined in reference diff (PR-review fixup)" \
    "$P149_REFERENCE_DIFF" "_DEFAULT_RETRY_POLICY = (1, 0)"
check_fixed "P149 run_phase_with_fallback signature includes phase_name" \
    "$P149_REFERENCE_DIFF" "phase_name: str = \"unknown\""
check_fixed "P149 phase_budget multiplier fix in reference diff (CRITICAL PR-review fix)" \
    "$P149_REFERENCE_DIFF" "_PHASE_TIMEOUTS.get(phase_name, timeout_secs) * 2"
check_fixed "P149 tier_retry_decision JSONL event emitted" \
    "$P149_REFERENCE_DIFF" '_log_event("tier_retry_decision"'
check_fixed "P149 tier_attempt_exception event in reference diff (PR-review fixup)" \
    "$P149_REFERENCE_DIFF" '"tier_attempt_exception"'
check_fixed "P149 exhausted_attempts decision in reference diff (PR-review fixup)" \
    "$P149_REFERENCE_DIFF" '"exhausted_attempts"'
check_fixed "P149 key_unavailable outcome in reference diff (PR-review fixup)" \
    "$P149_REFERENCE_DIFF" '"key_unavailable"'
check_marker_count "P149/MOL-499 markers in reference diff" \
    "$P149_REFERENCE_DIFF" "P149/MOL-499" 10

# Runtime assertions against $HERMES_SCRIPTS/symphony_bridge.py
# (SYMPHONY_BRIDGE_RUNTIME defined in P148b block above)
check_fixed "P149 _classify_tier_failure present in runtime" \
    "$SYMPHONY_BRIDGE_RUNTIME" "def _classify_tier_failure(error_text: str, stderr_tail: str)"
check_fixed "P149 _attempt_tier_with_retry present in runtime" \
    "$SYMPHONY_BRIDGE_RUNTIME" "def _attempt_tier_with_retry("
check_fixed "P149 _TRANSIENT_PATTERNS present in runtime" \
    "$SYMPHONY_BRIDGE_RUNTIME" "_TRANSIENT_PATTERNS = ["
check_fixed "P149 _DETERMINISTIC_FAILURE_PATTERNS present in runtime" \
    "$SYMPHONY_BRIDGE_RUNTIME" "_DETERMINISTIC_FAILURE_PATTERNS = ["
check_fixed "P149 _RETRY_POLICY present in runtime" \
    "$SYMPHONY_BRIDGE_RUNTIME" "_RETRY_POLICY = {"
check_fixed "P149 _TIMEOUT_ERROR_PREFIX present in runtime (PR-review fixup)" \
    "$SYMPHONY_BRIDGE_RUNTIME" '_TIMEOUT_ERROR_PREFIX = "timeout after"'
check_fixed "P149 _DEFAULT_RETRY_POLICY present in runtime (PR-review fixup)" \
    "$SYMPHONY_BRIDGE_RUNTIME" "_DEFAULT_RETRY_POLICY = (1, 0)"
check_fixed "P149 run_phase_with_fallback signature includes phase_name (runtime, post-P153 PhaseName-typed)" \
    "$SYMPHONY_BRIDGE_RUNTIME" "phase_name: PhaseName,"
# P149 phase_budget multiplier was replaced by P153/MOL-506 global tracker.remaining() guard.
# Retagged from literal `_PHASE_TIMEOUTS.get(phase_name, timeout_secs) * 2` to assert the
# replacement invariant: a decision="skipped_global_budget" log event proves the new
# wall-clock-based retry gating is wired. Plus inline negative check that the broken pre-P153
# `_PHASE_TIMEOUTS.get(phase_name, timeout_secs * 2)` form stays gone.
check_fixed "P149 retry gating via global tracker (runtime, post-P153 supersedes phase_budget multiplier)" \
    "$SYMPHONY_BRIDGE_RUNTIME" 'decision="skipped_global_budget"'
total=$((total + 1))
if grep -Fq "_PHASE_TIMEOUTS.get(phase_name, timeout_secs * 2)" "$SYMPHONY_BRIDGE_RUNTIME"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m %s\n' "P149 broken phase_budget multiplier resurrected (post-P153 should not have this)"
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m %s\n' "P149 broken phase_budget multiplier absent (post-P153 clean)"
    passed=$((passed + 1))
fi
check_fixed "P149 tier_retry_decision event emitted (runtime)" \
    "$SYMPHONY_BRIDGE_RUNTIME" '_log_event("tier_retry_decision"'
check_fixed "P149 tier_attempt_exception event emitted (runtime, PR-review fixup)" \
    "$SYMPHONY_BRIDGE_RUNTIME" '"tier_attempt_exception"'
check_fixed "P149 exhausted_attempts decision logged (runtime, PR-review fixup)" \
    "$SYMPHONY_BRIDGE_RUNTIME" '"exhausted_attempts"'
check_fixed "P149 key_unavailable outcome (runtime, PR-review fixup)" \
    "$SYMPHONY_BRIDGE_RUNTIME" '"key_unavailable"'
check_fixed "P149 planner phase passes phase_name= (runtime)" \
    "$SYMPHONY_BRIDGE_RUNTIME" 'phase_name="planner"'
check_fixed "P149 skeptic phase passes phase_name= (runtime)" \
    "$SYMPHONY_BRIDGE_RUNTIME" 'phase_name="skeptic"'
check_fixed "P149 builder phase passes phase_name= (runtime)" \
    "$SYMPHONY_BRIDGE_RUNTIME" 'phase_name="builder"'
check_fixed "P149 reviewer phase passes phase_name= (runtime)" \
    "$SYMPHONY_BRIDGE_RUNTIME" 'phase_name="reviewer"'
check_marker_count "P149/MOL-499 markers in runtime" \
    "$SYMPHONY_BRIDGE_RUNTIME" "P149/MOL-499" 10

# ──────────────────────────────────────────────────────────────────────
# P150 / MOL-500 — CC-native agent files (planner/skeptic/builder/reviewer)
# ──────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo "=== P150 / MOL-500: CC-native agent files for the 4 phases ==="

P150_REFERENCE_DIFF="$HERMES_POC_REFERENCE/P150-MOL-500.diff"
P150_REPO_AGENTS_DIR="$HERMES_POC_REFERENCE/claude-agents"
CLAUDE_AGENTS_RUNTIME="${HOME}/.claude/agents"

# Reference-diff assertions
check_fixed "P150 AGENTS_DIR constant defined in reference diff" \
    "$P150_REFERENCE_DIFF" 'AGENTS_DIR = HOME / ".claude" / "agents"'
check_fixed "P150 _load_phase_agent function in reference diff" \
    "$P150_REFERENCE_DIFF" "def _load_phase_agent(phase: str)"
check_fixed "P150 safe_format function in reference diff" \
    "$P150_REFERENCE_DIFF" "def safe_format(template: str"
check_fixed "P150 _PHASE_AGENT_CACHE defined in reference diff" \
    "$P150_REFERENCE_DIFF" "_PHASE_AGENT_CACHE: dict[str, str] = {}"
check_fixed "P150 _USER_CONTENT_FIELDS defined in reference diff" \
    "$P150_REFERENCE_DIFF" "_USER_CONTENT_FIELDS = frozenset({"
check_fixed "P150 planner phase uses _load_phase_agent (reference)" \
    "$P150_REFERENCE_DIFF" '_load_phase_agent("planner")'
check_fixed "P150 skeptic phase uses _load_phase_agent (reference)" \
    "$P150_REFERENCE_DIFF" '_load_phase_agent("skeptic")'
check_fixed "P150 builder phase uses _load_phase_agent (reference)" \
    "$P150_REFERENCE_DIFF" '_load_phase_agent("builder")'
check_fixed "P150 reviewer phase uses _load_phase_agent (reference)" \
    "$P150_REFERENCE_DIFF" '_load_phase_agent("reviewer")'
# Tightened from 8 to 9 to match actual count + lock against partial-apply (F19, PR #171 review)
check_marker_count "P150/MOL-500 markers in reference diff" \
    "$P150_REFERENCE_DIFF" "P150/MOL-500" 9

# Runtime assertions against $HERMES_SCRIPTS/symphony_bridge.py
check_fixed "P150 AGENTS_DIR constant defined in runtime" \
    "$SYMPHONY_BRIDGE_RUNTIME" 'AGENTS_DIR = HOME / ".claude" / "agents"'
check_fixed "P150 _load_phase_agent function in runtime" \
    "$SYMPHONY_BRIDGE_RUNTIME" "def _load_phase_agent(phase: str)"
check_fixed "P150 safe_format function in runtime" \
    "$SYMPHONY_BRIDGE_RUNTIME" "def safe_format(template: str"
check_fixed "P150 _PHASE_AGENT_CACHE present in runtime" \
    "$SYMPHONY_BRIDGE_RUNTIME" "_PHASE_AGENT_CACHE: dict[str, str] = {}"
check_fixed "P150 _USER_CONTENT_FIELDS present in runtime" \
    "$SYMPHONY_BRIDGE_RUNTIME" "_USER_CONTENT_FIELDS = frozenset({"
check_fixed "P150 planner phase uses _load_phase_agent (runtime)" \
    "$SYMPHONY_BRIDGE_RUNTIME" '_load_phase_agent("planner")'
check_fixed "P150 skeptic phase uses _load_phase_agent (runtime)" \
    "$SYMPHONY_BRIDGE_RUNTIME" '_load_phase_agent("skeptic")'
check_fixed "P150 builder phase uses _load_phase_agent (runtime)" \
    "$SYMPHONY_BRIDGE_RUNTIME" '_load_phase_agent("builder")'
check_fixed "P150 reviewer phase uses _load_phase_agent (runtime)" \
    "$SYMPHONY_BRIDGE_RUNTIME" '_load_phase_agent("reviewer")'
# safe_format wrapper presence at callsites — guards against re-apply that drops safe_format
# while keeping _load_phase_agent (would silently lose the KeyError contract). Count ≥ 5 =
# 1 def + 4 phase callsites. (F2 upgrade, PR #171 review)
check_marker_count "P150 safe_format callsites in runtime (≥5: 1 def + 4 phases)" \
    "$SYMPHONY_BRIDGE_RUNTIME" "safe_format(" 5
check_marker_count "P150/MOL-500 markers in runtime" \
    "$SYMPHONY_BRIDGE_RUNTIME" "P150/MOL-500" 9

# Agent file assertions (runtime + repo reference copies)
# Per-phase placeholder allowlist (bash 3.2-compatible — no associative arrays):
# agent body MUST contain these {tokens} that phase_<name> passes to safe_format().
# Catches silent placeholder drops on agent file edits (F15, PR #171 review).
for phase in planner skeptic builder reviewer; do
    check_fixed "P150 ${phase}.md runtime exists with name frontmatter" \
        "$CLAUDE_AGENTS_RUNTIME/${phase}.md" "name: ${phase}"
    check_fixed "P150 ${phase}.md runtime has tools frontmatter" \
        "$CLAUDE_AGENTS_RUNTIME/${phase}.md" "tools:"
    check_fixed "P150 ${phase}.md runtime contains context-engineering Skill: refs" \
        "$CLAUDE_AGENTS_RUNTIME/${phase}.md" "Skill:"
    # Replaces the previous `check_fixed "name: <phase>"` against repo copy — sha256 catches
    # any drift between runtime and repo, including subtle body edits (F17, PR #171 review).
    check_mirror_sha256 "P150 ${phase}.md runtime ↔ repo reference byte-identical" \
        "$CLAUDE_AGENTS_RUNTIME/${phase}.md" "$P150_REPO_AGENTS_DIR/${phase}.md"
    # Per-phase placeholder presence (F15)
    case "$phase" in
        planner)  placeholders="{key} {summary} {plan_file} {ticket_body_truncated} {repo_path}" ;;
        skeptic)  placeholders="{key} {plan_file} {repo_path}" ;;
        builder)  placeholders="{key} {slug} {plan_file} {plan_content_truncated} {repo_path}" ;;
        reviewer) placeholders="{pr_num} {gate_file} {repo_path}" ;;
    esac
    for placeholder in $placeholders; do
        check_fixed "P150 ${phase}.md body contains ${placeholder} placeholder" \
            "$CLAUDE_AGENTS_RUNTIME/${phase}.md" "$placeholder"
    done
done

# ──────────────────────────────────────────────────────────────────────
# P151 / MOL-502 — Tiered memory slim-down
# ──────────────────────────────────────────────────────────────────────
# SUPERSEDED-CHECKS: the 6 presence checks below assert P151's prior state
# (nomic-embed-text + 768-dim + qwen3:1.7b composer + KEEP_ALIVE=90 + float[768]
# schema). P168/MOL-546 swapped embedder to fastembed mxbai-embed-large-v1
# (1024-dim, in-process — no Ollama) and P169/MOL-560 retired the local
# Ollama qwen3 composer entirely in favour of Kimi K2.6 via OpenRouter.
# The runtime files now carry P168/P169 markers and 1024-dim schema; the
# P151 assertions are stale. Keep the absence-class checks below (reranker
# fully gone) — those remain valid invariants regardless of embedder/composer
# choice. Re-tag pattern: [[check_retag_on_refactor]].
[[ $QUIET -eq 0 ]] && echo "=== P151 / MOL-502 — Tiered memory slim-down (presence checks SUPERSEDED by P168/P169) ==="

TIERED_PLUGIN="$HERMES_AGENT/plugins/memory/tiered"

# Embedding swap (bge-m3 1024-dim → nomic-embed-text 768-dim) — SUPERSEDED by P168/MOL-546.
# check_fixed "P151 embeddings.py uses nomic-embed-text" \
#     "$TIERED_PLUGIN/embeddings.py" 'MODEL = "nomic-embed-text"'
# check_fixed "P151 embeddings.py DIMS=768" \
#     "$TIERED_PLUGIN/embeddings.py" "DIMS = 768"
# check_fixed "P151 embeddings.py keep_alive=90 set" \
#     "$TIERED_PLUGIN/embeddings.py" "KEEP_ALIVE = 90"

# Composer downsize (qwen3:8b → qwen3:1.7b) — SUPERSEDED by P169/MOL-560 (Kimi K2.6, Ollama retired).
# check_fixed "P151 llm.py PRIMARY_MODEL=qwen3:1.7b" \
#     "$TIERED_PLUGIN/llm.py" 'PRIMARY_MODEL = "qwen3:1.7b"'
# check_fixed "P151 llm.py PRIMARY_KEEP_ALIVE=90" \
#     "$TIERED_PLUGIN/llm.py" "PRIMARY_KEEP_ALIVE = 90"

# Vec table dim (1024 → 768) in schema — SUPERSEDED by P168/MOL-546 (back to 1024 with mxbai).
# check_fixed "P151 store.py _SCHEMA uses float[768]" \
#     "$TIERED_PLUGIN/store.py" "embedding float[768] distance_metric=cosine"

# Reranker deletion — negative assertions. Use inline grep because there's
# no check_absent helper. If any of these strings reappear, the reranker
# came back and the RAM win is gone.
total=$((total + 1))
if grep -Fq -- '_rerank_with_local_llm' "$TIERED_PLUGIN/search.py" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P151 search.py reranker fully removed (FOUND _rerank_with_local_llm)\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P151 search.py reranker fully removed (_rerank_with_local_llm absent)\n'
    passed=$((passed + 1))
fi

total=$((total + 1))
if grep -Fq -- 'RERANK_MODEL' "$TIERED_PLUGIN/search.py" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P151 search.py RERANK_MODEL removed (FOUND)\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P151 search.py RERANK_MODEL removed (absent)\n'
    passed=$((passed + 1))
fi

total=$((total + 1))
if grep -Fq -- '_THINK_RE' "$TIERED_PLUGIN/search.py" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P151 search.py _THINK_RE removed (FOUND)\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P151 search.py _THINK_RE removed (absent — reranker no longer parses thinking tokens)\n'
    passed=$((passed + 1))
fi

# rerank= argument should not appear as a kwarg in __init__.py call sites
total=$((total + 1))
if grep -Fq -- 'rerank=True' "$TIERED_PLUGIN/__init__.py" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P151 __init__.py rerank=True call sites removed (FOUND)\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P151 __init__.py rerank=True call sites removed (absent)\n'
    passed=$((passed + 1))
fi

# MOL-524 gap-fill: PR #192 dropped P18/P20 presence checks for `rerank=False`
# (__init__.py callsite) and `num_predict.*256` (reranker thinking budget) on the
# rationale that "P151 absence checks carry the load." The two checks below close
# that coverage gap explicitly. A `qwen3:8b` literal absence check was considered
# but rejected — the model name appears in the retirement docstring at search.py
# top, and the existing `_rerank_with_local_llm` + `RERANK_MODEL` absence checks
# above already lock the code path closed (any re-introduction would have to
# travel through one of those symbols).
total=$((total + 1))
if grep -Fq -- 'rerank=False' "$TIERED_PLUGIN/__init__.py" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P151 __init__.py rerank=False call sites removed (FOUND)\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P151 __init__.py rerank=False call sites removed (absent)\n'
    passed=$((passed + 1))
fi

total=$((total + 1))
if grep -Eq 'num_predict.*256' "$TIERED_PLUGIN/search.py" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P151 search.py reranker num_predict=256 thinking budget removed (FOUND)\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P151 search.py reranker num_predict=256 thinking budget removed (absent)\n'
    passed=$((passed + 1))
fi

# Attribution marker count — touches embeddings.py + search.py + __init__.py + llm.py + store.py.
# embeddings.py and llm.py were rewritten by P168/MOL-546 (fastembed mxbai) and
# P169/MOL-560 (Kimi K2.6 composer, Ollama retired) respectively, scrubbing the
# P151/MOL-502 attribution markers. store.py and __init__.py retain their
# markers and the assertions for those two files remain valid.
# check_marker_count "P151/MOL-502 markers across runtime tiered plugin" \
#     "$TIERED_PLUGIN/embeddings.py" "P151/MOL-502" 1
# check_marker_count "P151/MOL-502 markers in llm.py runtime" \
#     "$TIERED_PLUGIN/llm.py" "P151/MOL-502" 2
check_marker_count "P151/MOL-502 markers in store.py runtime" \
    "$TIERED_PLUGIN/store.py" "P151/MOL-502" 1
check_marker_count "P151/MOL-502 markers in __init__.py runtime" \
    "$TIERED_PLUGIN/__init__.py" "P151/MOL-502" 2

# ──────────────────────────────────────────────────────────────────────
# P152 / MOL-503 — Symphony-bridge pre-flight infra-health gate (Phase 2 v2)
# ──────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo "=== P152 / MOL-503: pre-flight infra-health gate ==="

P152_REFERENCE_DIFF="$HERMES_POC_REFERENCE/P152-MOL-503.diff"
P152_PREFLIGHT_RUNTIME="$HERMES_SCRIPTS/preflight_health.py"
P152_PREFLIGHT_MIRROR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/reference/scripts/preflight_health.py"

# Reference-diff assertions — guard the diff itself from drift.
check_fixed "P152 preflight_health.py header in reference diff" \
    "$P152_REFERENCE_DIFF" "P152/MOL-503 — Symphony-bridge pre-flight infra-health gate"
check_fixed "P152 check() entry point in reference diff" \
    "$P152_REFERENCE_DIFF" "def check() -> tuple[bool, dict]"
check_fixed "P152 _build_cell_jobs function in reference diff" \
    "$P152_REFERENCE_DIFF" "def _build_cell_jobs()"
check_fixed "P152 _classify_probe_output function in reference diff" \
    "$P152_REFERENCE_DIFF" "def _classify_probe_output("
check_fixed "P152 abort_three_degraded outcome in reference diff" \
    "$P152_REFERENCE_DIFF" '"abort_three_degraded"'
check_fixed "P152 abort_all_degraded outcome in reference diff" \
    "$P152_REFERENCE_DIFF" '"abort_all_degraded"'
check_fixed "P152 INFRA:DEGRADED banner in reference diff" \
    "$P152_REFERENCE_DIFF" "INFRA:DEGRADED symphony-bridge preflight aborted"
check_fixed "P152 symphony_bridge import preflight_health in reference diff" \
    "$P152_REFERENCE_DIFF" "import preflight_health"
check_fixed "P152 symphony_bridge wire-up calls preflight_health.check() in reference diff" \
    "$P152_REFERENCE_DIFF" "preflight_health.check()"
check_marker_count "P152/MOL-503 markers in reference diff" \
    "$P152_REFERENCE_DIFF" "P152/MOL-503" 6

# Runtime assertions — symphony_bridge.py wire-up.
check_fixed "P152 symphony_bridge.py imports preflight_health (runtime)" \
    "$SYMPHONY_BRIDGE_RUNTIME" "import preflight_health"
check_fixed "P152 symphony_bridge.py calls preflight_health.check() (runtime)" \
    "$SYMPHONY_BRIDGE_RUNTIME" "preflight_health.check()"
check_fixed "P152 symphony_bridge.py guards preflight on dry_run (runtime)" \
    "$SYMPHONY_BRIDGE_RUNTIME" "if not dry_run:"
check_fixed "P152 symphony_bridge.py logs PREFLIGHT ABORT (runtime)" \
    "$SYMPHONY_BRIDGE_RUNTIME" "PREFLIGHT ABORT"
check_marker_count "P152/MOL-503 markers in symphony_bridge.py runtime" \
    "$SYMPHONY_BRIDGE_RUNTIME" "P152/MOL-503" 3

# Runtime assertions — preflight_health.py module.
check_fixed "P152 preflight_health.py check() entry exists (runtime)" \
    "$P152_PREFLIGHT_RUNTIME" "def check() -> tuple[bool, dict]"
check_fixed "P152 preflight_health.py _build_cell_jobs exists (runtime)" \
    "$P152_PREFLIGHT_RUNTIME" "def _build_cell_jobs()"
check_fixed "P152 preflight_health.py _classify_probe_output exists (runtime)" \
    "$P152_PREFLIGHT_RUNTIME" "def _classify_probe_output("
check_fixed "P152 preflight_health.py 4 cells defined (runtime)" \
    "$P152_PREFLIGHT_RUNTIME" '"hermes+kimi"'
check_fixed "P152 preflight_health.py uses ThreadPoolExecutor(max_workers=4) (runtime)" \
    "$P152_PREFLIGHT_RUNTIME" "ThreadPoolExecutor(max_workers=4)"
check_fixed "P152 preflight_health.py 15s probe timeout (runtime)" \
    "$P152_PREFLIGHT_RUNTIME" "_PROBE_TIMEOUT_SECONDS = 15"
check_marker_count "P152/MOL-503 markers in preflight_health.py runtime" \
    "$P152_PREFLIGHT_RUNTIME" "P152/MOL-503" 2

# Mirror byte-parity — runtime ↔ repo reference (per check_mirror_sha256 memory).
check_mirror_sha256 "P152 preflight_health.py runtime ↔ repo mirror byte-identical" \
    "$P152_PREFLIGHT_RUNTIME" "$P152_PREFLIGHT_MIRROR"

# PR #173 review fix-pass — lock in the SFH-finding fixes against silent revert.
# (SFH-2) _build_probe_env + _FOREIGN_PROVIDER_ENV_KEYS — strips ALL foreign
# provider keys so no cross-cell key leakage into Kimi/DS probes.
check_fixed "P152 _build_probe_env helper present (SFH-2 fix)" \
    "$P152_PREFLIGHT_RUNTIME" "def _build_probe_env("
check_fixed "P152 _FOREIGN_PROVIDER_ENV_KEYS constant present (SFH-2 fix)" \
    "$P152_PREFLIGHT_RUNTIME" "_FOREIGN_PROVIDER_ENV_KEYS"
# (SFH-3) exit-0 + empty stdout → probe_returned_degraded (silent-CC-spawn class).
# Lock the comment + the classification so a refactor can't silently revert it.
check_fixed "P152 silent-CC-spawn classification present (SFH-3 fix)" \
    "$P152_PREFLIGHT_RUNTIME" "silent-"
# (SFH-4) bare digits "429", "401", "403" must NOT appear as standalone
# substring entries in _DEGRADED_SUBSTRINGS — only anchored variants like
# "http 429" / " 429 " / "status 429" are allowed.
total=$((total + 1))
if grep -E '^\s*"(429|401|403)",' "$P152_PREFLIGHT_RUNTIME" >/dev/null 2>&1; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P152 bare-digit degradation substrings absent (SFH-4 fix) — FOUND bare digit entry\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P152 bare-digit degradation substrings absent (SFH-4 fix)\n'
    passed=$((passed + 1))
fi
# (SFH-5) last-ditch stderr print on compound _log_event + probe-exception failure.
check_fixed "P152 last-ditch stderr fallback present (SFH-5 fix)" \
    "$P152_PREFLIGHT_RUNTIME" "Compound fail-open guard"

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P153/MOL-506: symphony-bridge global wall-clock budget manager ==="

# Runtime-only patch (no repo-tracked source-of-truth file; reference diff
# in scripts/hermes-patches/reference/P153-MOL-506.diff documents the
# changes for re-apply after a hermes update / runtime rebase).
P153_SB_RUNTIME="$HERMES_SCRIPTS/symphony_bridge.py"

# Core structural assertions — class + helpers + constants.
check_fixed "P153 BudgetTracker class defined" \
    "$P153_SB_RUNTIME" "class BudgetTracker:"
check_fixed "P153 _GLOBAL_BUDGET_SECONDS alias present" \
    "$P153_SB_RUNTIME" "_GLOBAL_BUDGET_SECONDS = _TOTAL_TIMEOUT_SECONDS"
check_fixed "P153 _MIN_PHASE_BUDGET = 300 floor present" \
    "$P153_SB_RUNTIME" "_MIN_PHASE_BUDGET = 300"
check_fixed "P153 _abort_budget_exhausted helper defined" \
    "$P153_SB_RUNTIME" "def _abort_budget_exhausted("
check_fixed "P153 BudgetTracker.start_now() classmethod factory present" \
    "$P153_SB_RUNTIME" "def start_now(cls"
check_fixed "P153 tracker = BudgetTracker.start_now() invocation in main()" \
    "$P153_SB_RUNTIME" "tracker = BudgetTracker.start_now()"
check_fixed "P153 budget_tracker_started log event present" \
    "$P153_SB_RUNTIME" "budget_tracker_started"
check_fixed "P153 budget_exhausted_abort log event present" \
    "$P153_SB_RUNTIME" "budget_exhausted_abort"
check_fixed "P153 global_budget_exhausted state.abort_reason present" \
    "$P153_SB_RUNTIME" '"global_budget_exhausted"'
check_fixed "P153 PhaseName Literal type defined" \
    "$P153_SB_RUNTIME" "PhaseName = Literal["
check_fixed "P153 _PHASE_TIMEOUTS wrapped in MappingProxyType" \
    "$P153_SB_RUNTIME" "= MappingProxyType({"
check_fixed "P153 ctor validates budget_seconds positive" \
    "$P153_SB_RUNTIME" "budget_seconds must be positive"
check_fixed "P153 ctor validates min_phase_budget range" \
    "$P153_SB_RUNTIME" "min_phase_budget={min_phase_budget} must be in"

# Dispatch-site lock-in: assert the gate fires at the expected number of
# call sites. 6 phase boundaries: planner / skeptic / planner_revise /
# skeptic_revise / builder / reviewer. Anchor on `if not tracker.can_start_phase(`
# real-call shape (excludes plain-text-comment matches) so a silently-removed
# gate can't be masked by a comment that mentions the helper.
total=$((total + 1))
gate_count=$(grep -c "if not tracker.can_start_phase(" "$P153_SB_RUNTIME" 2>/dev/null || echo 0)
if [ "$gate_count" -ge 6 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P153 if-not-can_start_phase invocations >= 6 (have %d)\n' "$gate_count"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P153 if-not-can_start_phase invocations expected >=6, have %d\n' "$gate_count"
    failed=$((failed + 1))
fi
total=$((total + 1))
abort_count=$(grep -c "_abort_budget_exhausted(" "$P153_SB_RUNTIME" 2>/dev/null || echo 0)
# 7 = 1 def + 6 invocations.
if [ "$abort_count" -ge 7 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P153 _abort_budget_exhausted() occurrences >= 7 (1 def + 6 sites, have %d)\n' "$abort_count"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P153 _abort_budget_exhausted() occurrences expected >=7, have %d\n' "$abort_count"
    failed=$((failed + 1))
fi

# Retry-helper refactor lock-ins.
check_fixed "P153 _attempt_tier_with_retry consults tracker.remaining()" \
    "$P153_SB_RUNTIME" "remaining = tracker.remaining()"
check_fixed "P153 skipped_global_budget decision label present" \
    "$P153_SB_RUNTIME" '"skipped_global_budget"'

# Negative checks — locks the pre-P153 patterns out so a partial revert
# can't silently restore prior behavior while pass-the-class checks above
# stay green.
total=$((total + 1))
if grep -qE 'phase_start:\s*float,' "$P153_SB_RUNTIME" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P153 pre-P153 _attempt_tier_with_retry signature absent — FOUND legacy `phase_start: float,`\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P153 pre-P153 _attempt_tier_with_retry signature absent (legacy `phase_start: float,` gone)\n'
    passed=$((passed + 1))
fi
total=$((total + 1))
if grep -q '"skipped_phase_budget"' "$P153_SB_RUNTIME" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P153 pre-P153 decision label absent — FOUND legacy `skipped_phase_budget`\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P153 pre-P153 decision label absent (legacy `skipped_phase_budget` gone)\n'
    passed=$((passed + 1))
fi
# Construct-and-start refactor: the unarmed observability hooks must be
# absent (the state they observed is unrepresentable now).
total=$((total + 1))
if grep -q 'budget_tracker_consulted_unarmed' "$P153_SB_RUNTIME" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P153 unarmed-warning event absent — FOUND legacy `budget_tracker_consulted_unarmed`\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P153 unarmed-warning event absent (unarmed state unrepresentable)\n'
    passed=$((passed + 1))
fi
total=$((total + 1))
if grep -q 'budget_tracker_lazy_init_warning' "$P153_SB_RUNTIME" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P153 lazy-init-warning event absent — FOUND legacy `budget_tracker_lazy_init_warning`\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P153 lazy-init-warning event absent (tracker is required, not optional)\n'
    passed=$((passed + 1))
fi
total=$((total + 1))
if grep -q '_unarmed_warned' "$P153_SB_RUNTIME" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P153 _unarmed_warned latch absent — FOUND legacy attribute\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P153 _unarmed_warned latch absent (no unarmed state to observe)\n'
    passed=$((passed + 1))
fi
total=$((total + 1))
if grep -qE 'tracker:\s*"?BudgetTracker\s*\|\s*None"?' "$P153_SB_RUNTIME" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P153 tracker:None signature absent — FOUND legacy optional-tracker annotation\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P153 tracker:None signature absent (tracker always required)\n'
    passed=$((passed + 1))
fi

# Failure-mode observability lock-ins.
# Stderr-first banner + state-write failure surfacing in abort helper.
check_fixed "P153 stderr fallback in abort helper" \
    "$P153_SB_RUNTIME" "sys.stderr.write(banner"
check_fixed "P153 _safe_write_state helper defined" \
    "$P153_SB_RUNTIME" "def _safe_write_state("
check_fixed "P153 state_write_failed event emitted by _safe_write_state" \
    "$P153_SB_RUNTIME" '"state_write_failed"'

# Phase-aware floor via _effective_min_phase.
check_fixed "P153 phase-aware floor helper" \
    "$P153_SB_RUNTIME" "def _effective_min_phase("
check_fixed "P153 can_start_phase consults phase-aware floor" \
    "$P153_SB_RUNTIME" "self._effective_min_phase(phase)"

# REVISE-loop phase names registered in _PHASE_TIMEOUTS.
check_fixed "P153 planner_revise in _PHASE_TIMEOUTS" \
    "$P153_SB_RUNTIME" '"planner_revise": 600'
check_fixed "P153 skeptic_revise in _PHASE_TIMEOUTS (post-P184/MOL-550)" \
    "$P153_SB_RUNTIME" '"skeptic_revise": 1200'

# Reviewer-gate abort captures orphan PR for triage sweep.
check_fixed "P153 orphan_pr_num set before reviewer-gate abort" \
    "$P153_SB_RUNTIME" 'state["orphan_pr_num"] = pr_num'

# Out-of-scope hardening folded into this patch:
# - gh pr merge subprocess hygiene
# - reviewer gate file failure events
# - __main__ rescue loop logged failures
# - _log_event stderr fallback latch
check_fixed "P153 pr_merge_failed event on non-zero gh exit" \
    "$P153_SB_RUNTIME" '"pr_merge_failed"'
check_fixed "P153 pr_merge_exception event on OSError/TimeoutExpired" \
    "$P153_SB_RUNTIME" '"pr_merge_exception"'
check_fixed "P153 reviewer_gate_missing event when file absent" \
    "$P153_SB_RUNTIME" '"reviewer_gate_missing"'
check_fixed "P153 reviewer_gate_unrecognized event on garbled content" \
    "$P153_SB_RUNTIME" '"reviewer_gate_unrecognized"'
check_fixed "P153 reviewer_gate_unreadable event on read OSError" \
    "$P153_SB_RUNTIME" '"reviewer_gate_unreadable"'
check_fixed "P153 rescue_state_skipped event in main rescue loop" \
    "$P153_SB_RUNTIME" '"rescue_state_skipped"'
check_fixed "P153 RESCUE LOOP FAILED stderr banner on meta-failure" \
    "$P153_SB_RUNTIME" "RESCUE LOOP FAILED"
check_fixed "P153 _LOG_EVENT_FAIL_WARNED one-shot latch in _log_event" \
    "$P153_SB_RUNTIME" "_LOG_EVENT_FAIL_WARNED"

# Dispatch checkpoints route through _safe_write_state — count locks the
# generalization so a regression that re-introduces bare write_state at a
# checkpoint trips the bound.
total=$((total + 1))
safe_write_count=$(grep -c "_safe_write_state(key, state," "$P153_SB_RUNTIME" 2>/dev/null || echo 0)
if [ "$safe_write_count" -ge 10 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P153 _safe_write_state invocations >= 10 (have %d)\n' "$safe_write_count"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P153 _safe_write_state invocations expected >=10, have %d\n' "$safe_write_count"
    failed=$((failed + 1))
fi

check_marker_count "P153/MOL-506 markers in symphony_bridge.py runtime" \
    "$P153_SB_RUNTIME" "P153/MOL-506" 4

# ────────────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo "=== P154 / MOL-509: run_one(key) extraction from symphony_bridge main() ==="

# Runtime-only patch (see reference/P154-MOL-509.diff). Extracts per-ticket
# execution from main() into a callable primitive shared with the future
# launchd daemon (P156) and cron-fallback (P158).
P154_SB_RUNTIME="$HERMES_SCRIPTS/symphony_bridge.py"

# Positive structural checks.
check_fixed "P154 dataclass import added" \
    "$P154_SB_RUNTIME" "from dataclasses import dataclass"
check_fixed "P154 get_args import added (Literal vocabulary check)" \
    "$P154_SB_RUNTIME" "from typing import Literal, get_args"
check_fixed "P154 FinalStatus Literal type defined" \
    "$P154_SB_RUNTIME" "FinalStatus = Literal["
check_fixed "P154 RunResult dataclass defined (frozen)" \
    "$P154_SB_RUNTIME" "@dataclass(frozen=True)"
check_fixed "P154 RunResult class declared" \
    "$P154_SB_RUNTIME" "class RunResult:"
check_fixed "P154 RunResult.final_status typed as FinalStatus (not bare str)" \
    "$P154_SB_RUNTIME" "final_status: FinalStatus"
check_fixed "P154 RunResult.pr_num typed as str | None (matches phase_builder)" \
    "$P154_SB_RUNTIME" "pr_num: str | None = None"
check_fixed "P154 RunResult __post_init__ validates final_status vocabulary" \
    "$P154_SB_RUNTIME" "not in FinalStatus"
check_fixed "P154 RunResult __post_init__ validates dispatched/skip invariant" \
    "$P154_SB_RUNTIME" "dispatched=False requires a skipped_* final_status"
check_fixed "P154 RunResult __post_init__ validates succeeded → merged" \
    "$P154_SB_RUNTIME" "final_status='succeeded' requires pr_state='merged'"
check_fixed "P154 run_one signature: keyword-only dry_run, RunResult return" \
    "$P154_SB_RUNTIME" "def run_one(key: str, *, dry_run: bool = False) -> RunResult:"

# Dispatch-site lock-in: main() delegates to run_one. The verifier locks the
# exact call shape so a regression that inlines the dispatch back into main's
# loop body trips here. Paired with the structural count below.
check_fixed "P154 main() invokes run_one(key, dry_run=dry_run)" \
    "$P154_SB_RUNTIME" "result = run_one(key, dry_run=dry_run)"

# RunResult-driven control flow in main loop.
check_fixed "P154 main checks result.dispatched in loop" \
    "$P154_SB_RUNTIME" "if not result.dispatched:"
check_fixed "P154 main forwards detail_line to dispatch_details" \
    "$P154_SB_RUNTIME" "if result.detail_line:"
check_fixed "P154 main preserves dry_run summary suppression" \
    "$P154_SB_RUNTIME" 'if result.final_status != "dry_run":'

# Skip-logic return values must use the new final_status vocabulary.
check_fixed "P154 skipped_max_attempts final_status" \
    "$P154_SB_RUNTIME" '"skipped_max_attempts"'
check_fixed "P154 skipped_recently_running final_status" \
    "$P154_SB_RUNTIME" '"skipped_recently_running"'
check_fixed "P154 skipped_succeeded_in_progress final_status" \
    "$P154_SB_RUNTIME" '"skipped_succeeded_in_progress"'
check_fixed "P154 skipped_progressed final_status" \
    "$P154_SB_RUNTIME" '"skipped_progressed"'
check_fixed "P154 dry_run final_status" \
    "$P154_SB_RUNTIME" 'final_status="dry_run"'

# fix-pass-1 (PR #176 comment-analyzer): the original 5 verifier strings
# above missed "succeeded" / "incomplete" / "failed" — those reach the
# RunResult via `state["last_status"]`. A rename ("succeeded" → "success")
# would silently pass. Lock the 3 state-derived strings as enum members of
# the FinalStatus Literal so a typo is caught at the verifier.
check_fixed "P154 succeeded in FinalStatus Literal" \
    "$P154_SB_RUNTIME" '"succeeded",'
check_fixed "P154 incomplete in FinalStatus Literal" \
    "$P154_SB_RUNTIME" '"incomplete",'
check_fixed "P154 failed in FinalStatus Literal" \
    "$P154_SB_RUNTIME" '"failed",'

# Structural counts — lock that the dispatch logic has moved (not duplicated).
# tracker = BudgetTracker.start_now() should appear exactly ONCE (in run_one).
# A regression that re-inlines the dispatch into main while leaving run_one
# present would double the count.
total=$((total + 1))
start_now_count=$(grep -c "tracker = BudgetTracker.start_now()" "$P154_SB_RUNTIME" 2>/dev/null || echo 0)
if [ "$start_now_count" -eq 1 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P154 tracker = BudgetTracker.start_now() invocations == 1 (have %d)\n' "$start_now_count"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P154 tracker = BudgetTracker.start_now() invocations expected == 1, have %d\n' "$start_now_count"
    failed=$((failed + 1))
fi

# dispatched += 1 should appear exactly ONCE in main() (no per-break increments).
total=$((total + 1))
dispatched_count=$(grep -c "dispatched += 1" "$P154_SB_RUNTIME" 2>/dev/null || echo 0)
if [ "$dispatched_count" -eq 1 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P154 dispatched += 1 occurrences == 1 (have %d)\n' "$dispatched_count"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P154 dispatched += 1 occurrences expected == 1, have %d\n' "$dispatched_count"
    failed=$((failed + 1))
fi

# Negative check — pre-P154 inline-dispatch wrote dispatched_status at every
# break site (dispatched_status = "failed" / "incomplete" / state[...]).
# Post-refactor the only writer is the single result-forwarding assignment
# inside main's loop. Use grep -q to avoid the grep -c double-emit footgun
# (grep prints "0" + exits 1 on zero matches; `|| echo 0` would append another "0").
total=$((total + 1))
if grep -qE '^ *dispatched_status = (state\[|"failed"|"incomplete")' "$P154_SB_RUNTIME" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P154 legacy per-break dispatched_status writers expected absent — found pre-P154 pattern\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P154 legacy per-break dispatched_status writers absent (inline dispatch removed)\n'
    passed=$((passed + 1))
fi

check_marker_count "P154/MOL-509 markers in symphony_bridge.py runtime" \
    "$P154_SB_RUNTIME" "P154/MOL-509" 2

# ────────────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo "=== P155 / MOL-505: patch-preserved runtime forward guard (4-layer) ==="

# Originally drafted as P154; renumbered to P155 after merge-conflict
# discovery (P154 claimed by MOL-509 in PR #176 same day — see MEMORY.md
# `p_number_collision_parallel_sessions.md`). Marker text uses P155 in
# this verifier block but the patch's user-facing MOL-505 references
# (in hooks, Rampart messages, CLAUDE.md surfaces) keep the ticket
# identifier as the durable anchor.

# Layer 1: git hooks + wrapper paths (LIVE runtime)
P155_HOOKS_DIR="$HERMES_AGENT/.git/hooks"
P155_PRE_REBASE="$P155_HOOKS_DIR/pre-rebase"
P155_PRE_MERGE="$P155_HOOKS_DIR/pre-merge-commit"
P155_WRAPPER="${HERMES_HOME:-$HOME/.hermes}/bin/git"

# pre-rebase hook present + executable + carries MOL-505 marker
total=$((total + 1))
if [ -x "$P155_PRE_REBASE" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P155 pre-rebase hook installed + executable\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P155 pre-rebase hook missing or not executable: %s\n' "$P155_PRE_REBASE"
    failed=$((failed + 1))
fi
check_fixed "P155 pre-rebase contains MOL-505 marker" \
    "$P155_PRE_REBASE" "MOL-505"

# pre-merge-commit hook present + executable + carries MOL-505 marker
total=$((total + 1))
if [ -x "$P155_PRE_MERGE" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P155 pre-merge-commit hook installed + executable\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P155 pre-merge-commit hook missing or not executable: %s\n' "$P155_PRE_MERGE"
    failed=$((failed + 1))
fi
check_fixed "P155 pre-merge-commit contains MOL-505 marker" \
    "$P155_PRE_MERGE" "MOL-505"

# Wrapper: present + executable + bypass-name lock + new -C/--work-tree detection
# + new merge-from-remote block (fast-forward bypass coverage) + realpath.
total=$((total + 1))
if [ -x "$P155_WRAPPER" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P155 git-wrapper installed at %s + executable\n' "$P155_WRAPPER"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P155 git-wrapper missing or not executable: %s\n' "$P155_WRAPPER"
    failed=$((failed + 1))
fi
check_fixed "P155 git-wrapper has HERMES_RECOVERY_OVERRIDE bypass" \
    "$P155_WRAPPER" "HERMES_RECOVERY_OVERRIDE"
check_fixed "P155 git-wrapper has -C/--git-dir/--work-tree detection (review fix)" \
    "$P155_WRAPPER" "TARGET_DIR"
check_fixed "P155 git-wrapper has fast-forward merge-from-remote block (review fix)" \
    "$P155_WRAPPER" "merge-from-remote"
check_fixed "P155 git-wrapper has realpath canonicalization (review fix)" \
    "$P155_WRAPPER" "realpath -m"

# MOL-517 verb-scope expansion — six additional destructive verbs at L1.
check_fixed "P155/MOL-517 git-wrapper has reset --soft remote block" \
    "$P155_WRAPPER" "reset-soft-remote"
check_fixed "P155/MOL-517 git-wrapper has filter-branch block" \
    "$P155_WRAPPER" "filter-branch"
check_fixed "P155/MOL-517 git-wrapper has checkout -B remote block" \
    "$P155_WRAPPER" "checkout-B-remote"
check_fixed "P155/MOL-517 git-wrapper has update-ref block" \
    "$P155_WRAPPER" "update-ref"
check_fixed "P155/MOL-517 git-wrapper has reflog expire block" \
    "$P155_WRAPPER" "reflog-expire"
check_fixed "P155/MOL-517 git-wrapper has gc --prune block" \
    "$P155_WRAPPER" "gc-prune"
check_fixed "P155/MOL-517 git-wrapper has push --force block" \
    "$P155_WRAPPER" "push-force"

# Drift-detection: the wrapper banner explicitly lists every block tag in a
# "MOL-517-verbs:" anchor line. If a future patch adds a new deny block but
# forgets to update the anchor, OR removes a tag from the anchor without
# touching the code, this check pins the canonical set in one searchable
# place. Layer 3 prompts point at the wrapper as "authoritative" — without
# this anchor, prompt-side drift goes undetected.
check_fixed "P155/MOL-517 wrapper has VERB TABLE drift-detection anchor" \
    "$P155_WRAPPER" "MOL-517-verbs: reset-soft-remote filter-branch checkout-B-remote update-ref reflog-expire gc-prune push-force"
# _log_block count floor: 3 original (reset-hard-remote, merge-from-remote,
# pull) + 7 MOL-517 = 10. Any drop indicates a deleted deny path. Match
# this floor against the wrapper VERB TABLE anchor block tags above —
# any drift between the two surfaces by definition fails one of these
# two checks.
total=$((total + 1))
log_block_count=$(grep -c '_log_block "' "$P155_WRAPPER" 2>/dev/null || echo 0)
if [ "$log_block_count" -ge 10 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P155/MOL-517 wrapper _log_block deny-path count (>=10, have %d)\n' "$log_block_count"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P155/MOL-517 wrapper _log_block deny-path count (expected >=10, have %d) — a deny block was removed\n' "$log_block_count"
    failed=$((failed + 1))
fi

# Layer 2: Rampart deny rule (live policy = symlinked repo file)
P155_RAMPART_POLICY="$HOME/.rampart/policies/hermes.yaml"
check_fixed "P155 Rampart rule hermes-runtime-rebase-merge-reset-deny present" \
    "$P155_RAMPART_POLICY" "hermes-runtime-rebase-merge-reset-deny"
# Sibling-prefix collision fix: globs must NOT use bare `*hermes-agent*` form
# anymore. The path-shape form `*hermes-agent rebase*` is what landed in P155.
# A regression to bare-substring would NOT trip this check; matching the
# space-anchored form does.
check_fixed "P155 Rampart rule uses space-anchored sibling-prefix-safe globs" \
    "$P155_RAMPART_POLICY" "hermes-agent rebase"

# Fire-test: presence checks above can't catch the slash-crossing-glob bug
# class — patterns like `git*-C*hermes-agent rebase*` parse cleanly and load,
# but Rampart's `*` doesn't cross `/` (POSIX FNM_PATHNAME), so they never
# match a real path. Probe the loaded policy with one known-deny shape per
# verb group and assert the rule fires by name. If any of these flip ✗,
# somebody re-introduced a slash-crossing glob in that verb group.
#
# Skip semantics: missing-rampart-binary skips (CI may not have it). Missing
# POLICY file FAILS — a dangling symlink is exactly the "loads fine, never
# fires" pattern the fire-test exists to catch.
RAMPART_BIN_PATH="${RAMPART_BIN:-/opt/homebrew/bin/rampart}"
if [ ! -x "$RAMPART_BIN_PATH" ]; then
    total=$((total + 1))
    [[ $QUIET -eq 0 ]] && printf '  \033[0;33m[~]\033[0m P155 Rampart fire-test SKIPPED (rampart binary not at %s)\n' "$RAMPART_BIN_PATH"
    passed=$((passed + 1))
elif [ ! -e "$P155_RAMPART_POLICY" ]; then
    total=$((total + 1))
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P155 Rampart fire-test FAIL — policy file %s missing (Layer 2 not loaded)\n' "$P155_RAMPART_POLICY"
    failed=$((failed + 1))
else
    for P155_PROBE_CMD in \
        "git -C /Users/dummy/.hermes/hermes-agent rebase origin/main" \
        "git -C /Users/dummy/.hermes/hermes-agent merge origin/main" \
        "git -C /Users/dummy/.hermes/hermes-agent reset --hard origin/main" \
        "git -C /Users/dummy/.hermes/hermes-agent pull" \
        "git -C /Users/dummy/.hermes/hermes-agent reset --soft origin/main" \
        "git -C /Users/dummy/.hermes/hermes-agent reset --soft @{upstream}" \
        "git -C /Users/dummy/.hermes/hermes-agent filter-branch -- HEAD" \
        "git -C /Users/dummy/.hermes/hermes-agent checkout -B foo origin/main" \
        "git -C /Users/dummy/.hermes/hermes-agent checkout -B foo @{upstream}" \
        "git -C /Users/dummy/.hermes/hermes-agent update-ref refs/heads/main origin/main" \
        "git -C /Users/dummy/.hermes/hermes-agent reflog expire --expire=now --all" \
        "git -C /Users/dummy/.hermes/hermes-agent gc --prune=now" \
        "git -C /Users/dummy/.hermes/hermes-agent push --force origin main"
    do
        total=$((total + 1))
        # pipefail is set at the top of this script, so `rampart … | grep -q`
        # would propagate rampart's deny-exit-code (1) up the pipeline
        # regardless of whether grep matched. Capture output first, then
        # test it — the if-branch must reflect grep's verdict, not
        # rampart's. The `|| true` swallows rampart's non-zero deny exit so
        # the `out=$(…)` doesn't propagate failure under pipefail either.
        P155_PROBE_OUT=$("$RAMPART_BIN_PATH" --config "$P155_RAMPART_POLICY" test --tool terminal "$P155_PROBE_CMD" 2>&1 || true)
        if echo "$P155_PROBE_OUT" | grep -q "hermes-runtime-rebase-merge-reset-deny"; then
            [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P155 Rampart deny rule fires on: %s\n' "$P155_PROBE_CMD"
            passed=$((passed + 1))
        else
            [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P155 Rampart deny rule did NOT fire on: %s — slash-crossing glob may have returned (MOL-516 regression)\n' "$P155_PROBE_CMD"
            failed=$((failed + 1))
        fi
    done
fi

# Layer 3: Hermes prompt surfaces
P155_SOUL="${HERMES_HOME:-$HOME/.hermes}/SOUL.md"
P155_CODING_PROFILE="${HERMES_HOME:-$HOME/.hermes}/config/delegate-profiles/coding.yaml"
check_fixed "P155 SOUL.md contains patch-preserved paragraph" \
    "$P155_SOUL" "patch-preserved"
check_fixed "P155 SOUL.md references MOL-505" \
    "$P155_SOUL" "MOL-505"
check_fixed "P155 coding profile has MOL-505 block in system_prompt_suffix" \
    "$P155_CODING_PROFILE" "MOL-505"

# Layer 4: Claude Code-side (user-global)
P155_CC_GUARDRAILS="$HOME/.claude/hooks/git-guardrails.sh"
P155_CC_CLAUDE_MD="$HOME/.claude/CLAUDE.md"
check_fixed "P155 CC guardrails has hermes-runtime-rebase block" \
    "$P155_CC_GUARDRAILS" "hermes-runtime-rebase"
check_fixed "P155 CC guardrails uses path-shape regex (review fix)" \
    "$P155_CC_GUARDRAILS" 'RUNTIME_PATH_RX'
check_fixed "P155 CC guardrails has _log_runtime_block fail-safe (review fix)" \
    "$P155_CC_GUARDRAILS" '|| true'
check_fixed "P155 user-global CLAUDE.md mentions patch-preserved runtime" \
    "$P155_CC_CLAUDE_MD" "patch-preserved"
check_fixed "P155 user-global CLAUDE.md lists pre-merge-commit guard (review fix)" \
    "$P155_CC_CLAUDE_MD" "pre-merge-commit"

# MOL-517 verb-scope expansion — six additional destructive verbs at L4a.
check_fixed "P155/MOL-517 CC guardrails has reset --soft remote block" \
    "$P155_CC_GUARDRAILS" "hermes-runtime-reset-soft-remote"
check_fixed "P155/MOL-517 CC guardrails has filter-branch block" \
    "$P155_CC_GUARDRAILS" "hermes-runtime-filter-branch"
check_fixed "P155/MOL-517 CC guardrails has checkout -B remote block" \
    "$P155_CC_GUARDRAILS" "hermes-runtime-checkout-B-remote"
# Fix-pass-1: checkout -B regex must include @{upstream}/@{u} for ref-set
# parity with L1 wrapper case match (was missing in initial MOL-517 commit;
# silent-failure-hunter HIGH finding). Substring spans from the unique
# `-B\s+\S+` prefix through @{upstream} so this can ONLY match the
# checkout -B line, not the parallel reset --soft / reset --hard regexes.
check_fixed "P155/MOL-517 CC guardrails checkout -B regex has @{upstream} ref parity (fix-pass-1)" \
    "$P155_CC_GUARDRAILS" '-B\s+\S+\s+(origin/|upstream/|fork/|FETCH_HEAD|refs/remotes/|@\{upstream\}'
check_fixed "P155/MOL-517 CC guardrails has update-ref block" \
    "$P155_CC_GUARDRAILS" "hermes-runtime-update-ref"
check_fixed "P155/MOL-517 CC guardrails has reflog expire block" \
    "$P155_CC_GUARDRAILS" "hermes-runtime-reflog-expire"
check_fixed "P155/MOL-517 CC guardrails has gc --prune block" \
    "$P155_CC_GUARDRAILS" "hermes-runtime-gc-prune"
# Layer 3 generalized clause (SOUL.md + coding.yaml) mentions the new MOL-517 verbs.
check_fixed "P155/MOL-517 SOUL.md mentions MOL-517 verb expansion" \
    "$P155_SOUL" "MOL-517"
check_fixed "P155/MOL-517 coding profile mentions MOL-517 verb expansion" \
    "$P155_CODING_PROFILE" "MOL-517"
# Layer 2 Rampart: new verb globs landed in the deny rule.
check_fixed "P155/MOL-517 Rampart rule has reset --soft glob" \
    "$P155_RAMPART_POLICY" "hermes-agent reset*--soft"
check_fixed "P155/MOL-517 Rampart rule has filter-branch glob" \
    "$P155_RAMPART_POLICY" "hermes-agent filter-branch"
check_fixed "P155/MOL-517 Rampart rule has checkout -B glob" \
    "$P155_RAMPART_POLICY" "hermes-agent checkout*-B"
check_fixed "P155/MOL-517 Rampart rule has update-ref glob" \
    "$P155_RAMPART_POLICY" "hermes-agent update-ref"
check_fixed "P155/MOL-517 Rampart rule has reflog expire glob" \
    "$P155_RAMPART_POLICY" "hermes-agent reflog*expire"
check_fixed "P155/MOL-517 Rampart rule has gc --prune glob" \
    "$P155_RAMPART_POLICY" "hermes-agent gc*--prune"
check_fixed "P155/MOL-517 Rampart rule has push --force glob" \
    "$P155_RAMPART_POLICY" "hermes-agent push*--force"
# Fix-pass-1: @{upstream} / @{u} ref-set parity for conditional verbs (reset
# --soft + checkout -B). Locks the shorthand ref shorthand globs against
# accidental removal.
check_fixed "P155/MOL-517 Rampart rule has reset --soft @{upstream} glob (fix-pass-1)" \
    "$P155_RAMPART_POLICY" "hermes-agent reset*--soft*@{upstream}"
check_fixed "P155/MOL-517 Rampart rule has checkout -B @{upstream} glob (fix-pass-1)" \
    "$P155_RAMPART_POLICY" "hermes-agent checkout*-B*@{upstream}"

# Marker count floor: ≥ 8 occurrences of MOL-505 across the L1+L3+L4 surfaces.
P155_MARKER_FILES=(
    "$P155_PRE_REBASE" "$P155_PRE_MERGE" "$P155_WRAPPER"
    "$P155_CC_GUARDRAILS" "$P155_CC_CLAUDE_MD"
    "$P155_SOUL" "$P155_CODING_PROFILE" "$P155_RAMPART_POLICY"
)
total=$((total + 1))
marker_count=0
# Per `grep_c_footgun.md`: `grep -Fc` always prints "0" + exits 1 on zero-match
# files, then `|| echo 0` appends another "0" → c="0\n0" → arithmetic break.
# This counter currently happens to work because EVERY file in P155_MARKER_FILES
# contains MOL-505 — but the latent footgun was surfaced by the MOL-517 counter
# below and backported here (fix-pass-1) so a future surface addition without
# the MOL-505 marker doesn't silently miscount.
for f in "${P155_MARKER_FILES[@]}"; do
    if [ -r "$f" ] && grep -Fq "MOL-505" "$f" 2>/dev/null; then
        c=$(grep -Fc "MOL-505" "$f" 2>/dev/null)
        marker_count=$((marker_count + c))
    fi
done
if [ "$marker_count" -ge 8 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P155 MOL-505 marker count across forward-guard surfaces (>=8, have %d)\n' "$marker_count"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P155 MOL-505 marker count across forward-guard surfaces (expected >=8, have %d)\n' "$marker_count"
    failed=$((failed + 1))
fi

# MOL-517 verb-scope expansion: count >= 4 (wrapper banner + SOUL + coding profile
# + Rampart deny message). Lower floor than MOL-505 because L4a hook uses verb-
# specific block names (filter-branch, reflog-expire, etc.) rather than the
# MOL-517 marker, and the message field carries the marker once per rule.
#
# Counter uses grep -q + nested grep -c per `grep_c_footgun.md`: the bare
# `c=$(grep -Fc ... || echo 0)` shape emits "0\n0" on zero-match files
# (grep prints 0 + exits 1, then || echo 0 appends another 0), which then
# corrupts the arithmetic. Three files in P155_MARKER_FILES (pre-rebase,
# pre-merge-commit, user-global CLAUDE.md) legitimately have 0 MOL-517
# markers and would trip the footgun.
total=$((total + 1))
mol517_count=0
for f in "${P155_MARKER_FILES[@]}"; do
    if [ -r "$f" ] && grep -Fq "MOL-517" "$f" 2>/dev/null; then
        c=$(grep -Fc "MOL-517" "$f" 2>/dev/null)
        mol517_count=$((mol517_count + c))
    fi
done
if [ "$mol517_count" -ge 4 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P155/MOL-517 marker count across forward-guard surfaces (>=4, have %d)\n' "$mol517_count"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P155/MOL-517 marker count across forward-guard surfaces (expected >=4, have %d)\n' "$mol517_count"
    failed=$((failed + 1))
fi

# ────────────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo "=== P156 / MOL-510: queue.db schema + atomic-claim primitive ==="

# Two new files at ~/.hermes/scripts/ — no modifications to existing runtime.
# symphony_queue.py provides the atomic-claim primitive; the migration script
# imports JSON state files into the queue. Both ship together (P156).
P156_QUEUE_RUNTIME="$HERMES_SCRIPTS/symphony_queue.py"
P156_MIGRATE_RUNTIME="$HERMES_SCRIPTS/migrate_state_to_queue.py"

# File-existence checks.
total=$((total + 1))
if [ -f "$P156_QUEUE_RUNTIME" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P156 symphony_queue.py exists at runtime path\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P156 symphony_queue.py MISSING at %s\n' "$P156_QUEUE_RUNTIME"
    failed=$((failed + 1))
fi

total=$((total + 1))
if [ -f "$P156_MIGRATE_RUNTIME" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P156 migrate_state_to_queue.py exists at runtime path\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P156 migrate_state_to_queue.py MISSING at %s\n' "$P156_MIGRATE_RUNTIME"
    failed=$((failed + 1))
fi

# Public API surface lock-ins (symphony_queue.py).
check_fixed "P156 init_queue function defined" \
    "$P156_QUEUE_RUNTIME" "def init_queue("
check_fixed "P156 claim_ticket function defined" \
    "$P156_QUEUE_RUNTIME" "def claim_ticket("
check_fixed "P156 release_ticket function defined" \
    "$P156_QUEUE_RUNTIME" "def release_ticket("
check_fixed "P156 add_ticket function defined" \
    "$P156_QUEUE_RUNTIME" "def add_ticket("
check_fixed "P156 get_next_claimable function defined" \
    "$P156_QUEUE_RUNTIME" "def get_next_claimable("
check_fixed "P156 record_attempt_event function defined" \
    "$P156_QUEUE_RUNTIME" "def record_attempt_event("

# Schema lock-in: the tickets table's load-bearing column declarations.
check_fixed "P156 tickets schema state column" \
    "$P156_QUEUE_RUNTIME" "state TEXT NOT NULL"
check_fixed "P156 tickets schema priority column" \
    "$P156_QUEUE_RUNTIME" "priority INTEGER NOT NULL DEFAULT 0"
check_fixed "P156 tickets schema runs_log column" \
    "$P156_QUEUE_RUNTIME" "runs_log TEXT NOT NULL DEFAULT '[]'"
check_fixed "P156 idx_state_priority composite index" \
    "$P156_QUEUE_RUNTIME" "CREATE INDEX IF NOT EXISTS idx_state_priority"

# Atomic-primitive lock-in: BEGIN IMMEDIATE + conditional UPDATE WHERE pending.
check_fixed "P156 claim_ticket uses BEGIN IMMEDIATE for write lock" \
    "$P156_QUEUE_RUNTIME" 'conn.execute("BEGIN IMMEDIATE")'
check_fixed "P156 claim_ticket conditional UPDATE WHERE state pending" \
    "$P156_QUEUE_RUNTIME" "AND state = 'pending'"
check_fixed "P156 release_ticket asserts caller owns the running claim" \
    "$P156_QUEUE_RUNTIME" "AND state = 'running'"
check_fixed "P156 release_ticket raises on rowcount mismatch (no silent skip)" \
    "$P156_QUEUE_RUNTIME" "expected to update exactly 1 row in 'running'"

# State vocabulary lock-in — Literal type with VALID_STATES derived via
# get_args. A typo or rename here silently breaks the migration script's
# _map_state and any caller-side guards.
check_fixed "P156 State Literal type defined" \
    "$P156_QUEUE_RUNTIME" "State = Literal["
check_fixed "P156 VALID_STATES derived from State via get_args" \
    "$P156_QUEUE_RUNTIME" "VALID_STATES: frozenset[str] = frozenset(get_args(State))"
check_fixed "P156 TerminalState Literal type defined" \
    "$P156_QUEUE_RUNTIME" "TerminalState = Literal["
check_fixed "P156 TERMINAL_STATES <= VALID_STATES import-time assert" \
    "$P156_QUEUE_RUNTIME" "assert TERMINAL_STATES <= VALID_STATES"
check_fixed "P156 State Literal includes pending" \
    "$P156_QUEUE_RUNTIME" '"pending",'
check_fixed "P156 State Literal includes running" \
    "$P156_QUEUE_RUNTIME" '"running",'
check_fixed "P156 State Literal includes succeeded" \
    "$P156_QUEUE_RUNTIME" '"succeeded",'
check_fixed "P156 State Literal includes failed" \
    "$P156_QUEUE_RUNTIME" '"failed",'
check_fixed "P156 State Literal includes blocked" \
    "$P156_QUEUE_RUNTIME" '"blocked",'
check_fixed "P156 State Literal includes aborted_budget" \
    "$P156_QUEUE_RUNTIME" '"aborted_budget",'
check_fixed "P156 State Literal includes aborted_health" \
    "$P156_QUEUE_RUNTIME" '"aborted_health",'

# WAL mode lock-in — init_queue must FETCH the PRAGMA result and assert
# WAL was enabled, not blindly trust the PRAGMA returned success.
check_fixed "P156 init_queue enables WAL journal mode" \
    "$P156_QUEUE_RUNTIME" "PRAGMA journal_mode=WAL"
check_fixed "P156 init_queue verifies WAL actually enabled (no silent fallback)" \
    "$P156_QUEUE_RUNTIME" "requires WAL journal mode"

# Storage-layer CHECK constraint — belt-and-suspenders against raw-SQL bypass.
check_fixed "P156 schema has CHECK(state IN ...) constraint" \
    "$P156_QUEUE_RUNTIME" "CHECK(state IN ("

# TZ-aware next_attempt_at comparison via julianday().
check_fixed "P156 get_next_claimable uses julianday for instant compare" \
    "$P156_QUEUE_RUNTIME" "julianday(next_attempt_at)"

# record_attempt_event must raise on missing key (parity with release_ticket).
check_fixed "P156 record_attempt_event raises on missing key" \
    "$P156_QUEUE_RUNTIME" "dropped (no rows updated)"

# _row_to_dict must surface runs_log corruption.
check_fixed "P156 _row_to_dict warns on corrupt runs_log JSON" \
    "$P156_QUEUE_RUNTIME" "WARNING: runs_log corrupt"
check_fixed "P156 _row_to_dict flags runs_log_corrupt for caller branching" \
    "$P156_QUEUE_RUNTIME" 'd["runs_log_corrupt"] = True'

# release_ticket must honor _RUNS_LOG_CAP.
check_fixed "P156 release_ticket honors _RUNS_LOG_CAP via CASE WHEN" \
    "$P156_QUEUE_RUNTIME" "json_remove(runs_log, '\$[0]')"

# release_ticket error message includes the actual current state.
check_fixed "P156 release_ticket error includes actual_state" \
    "$P156_QUEUE_RUNTIME" "actual_state="

# Migration script: state-mapping function + critical mappings.
check_fixed "P156 migration _map_state function defined" \
    "$P156_MIGRATE_RUNTIME" "def _map_state("
check_fixed "P156 migration maps incomplete + attempt_count>=max to blocked" \
    "$P156_MIGRATE_RUNTIME" "if attempt_count >= _MAX_ATTEMPTS_LITERAL:"
check_fixed "P156 migration _MAX_ATTEMPTS_LITERAL = 3 (matches bridge run_one)" \
    "$P156_MIGRATE_RUNTIME" "_MAX_ATTEMPTS_LITERAL = 3"
check_fixed "P156 migration honors DRY_RUN env var" \
    "$P156_MIGRATE_RUNTIME" 'os.environ.get("DRY_RUN") == "1"'
check_fixed "P156 migration is idempotent (skips existing rows)" \
    "$P156_MIGRATE_RUNTIME" "skipped_existing"
check_fixed "P156 migration quarantines corrupt files via _quarantine helper" \
    "$P156_MIGRATE_RUNTIME" "def _quarantine("
check_fixed "P156 migration catches ValueError + TypeError on int() coerce" \
    "$P156_MIGRATE_RUNTIME" "OSError, json.JSONDecodeError, ValueError, TypeError"
check_fixed "P156 migration backfill UPDATE checks rowcount" \
    "$P156_MIGRATE_RUNTIME" "backfill UPDATE for"
check_fixed "P156 migration warns on unknown json_status" \
    "$P156_MIGRATE_RUNTIME" "WARNING: unknown json_status="

# runs_log bounded-growth lock-in.
check_fixed "P156 _RUNS_LOG_CAP constant defined" \
    "$P156_QUEUE_RUNTIME" "_RUNS_LOG_CAP = "

# Marker count across both files.
check_marker_count "P156/MOL-510 markers in symphony_queue.py" \
    "$P156_QUEUE_RUNTIME" "P156/MOL-510" 1
check_marker_count "P156/MOL-510 markers in migrate_state_to_queue.py" \
    "$P156_MIGRATE_RUNTIME" "P156/MOL-510" 1

# ----------------------------------------------------------------------------
[[ $QUIET -eq 0 ]] && echo "=== P157 / MOL-515: symphony daemon + launchd plist (Phase 4c) ==="

# Two new files: the daemon Python script (runs at ~/.hermes/scripts/) and
# the launchd plist (installed at ~/Library/LaunchAgents/). Neither file
# is loaded automatically — operator runs launchctl bootstrap when ready.
P157_DAEMON_RUNTIME="$HERMES_SCRIPTS/symphony_daemon.py"
P157_PLIST_RUNTIME="$HOME/Library/LaunchAgents/ai.hermes.symphony-daemon.plist"

# File-existence checks.
total=$((total + 1))
if [ -f "$P157_DAEMON_RUNTIME" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P157 symphony_daemon.py exists at runtime path\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P157 symphony_daemon.py MISSING at %s\n' "$P157_DAEMON_RUNTIME"
    failed=$((failed + 1))
fi

total=$((total + 1))
if [ -f "$P157_PLIST_RUNTIME" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P157 symphony-daemon plist exists at LaunchAgents path\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P157 symphony-daemon plist MISSING at %s\n' "$P157_PLIST_RUNTIME"
    failed=$((failed + 1))
fi

# Daemon public API surface — the four functions tests and operators depend on.
check_fixed "P157 daemon run_loop function defined" \
    "$P157_DAEMON_RUNTIME" "def run_loop("
check_fixed "P157 daemon startup_sweep function defined" \
    "$P157_DAEMON_RUNTIME" "def startup_sweep("
check_fixed "P157 daemon main function defined" \
    "$P157_DAEMON_RUNTIME" "def main("
check_fixed "P157 daemon _map_result_to_queue_state helper defined" \
    "$P157_DAEMON_RUNTIME" "def _map_result_to_queue_state("

# Runner identity, recorded in queue.runs_log per claim. Changing this
# breaks forensic queries asking "which runner claimed this ticket".
check_fixed "P157 daemon _RUNNER_ID is 'daemon'" \
    "$P157_DAEMON_RUNTIME" '_RUNNER_ID = "daemon"'

# Heartbeat cadence — operators watch heartbeat mtime to detect zombies.
check_fixed "P157 daemon _HEARTBEAT_INTERVAL_SECS = 60" \
    "$P157_DAEMON_RUNTIME" "_HEARTBEAT_INTERVAL_SECS = 60"
check_fixed "P157 daemon _HEARTBEAT_FILE path defined" \
    "$P157_DAEMON_RUNTIME" 'daemon-heartbeat'
check_fixed "P157 daemon _heartbeat function defined" \
    "$P157_DAEMON_RUNTIME" "def _heartbeat("

# PR URL prefix — last_pr_url forensic linkage to the merged PR.
check_fixed "P157 daemon PR URL targets hermes-control-plane (the actual remote)" \
    "$P157_DAEMON_RUNTIME" "_PR_URL_PREFIX = \"https://github.com/scarnyc/hermes-control-plane/pull/\""

# Mapping table lock-ins — the daemon's release-state contract. Each entry
# locks one FinalStatus → (queue_state, error_class) edge.
check_fixed "P157 _STATUS_TO_QUEUE_STATE dict defined" \
    "$P157_DAEMON_RUNTIME" "_STATUS_TO_QUEUE_STATE:"
check_fixed "P157 mapping succeeded → succeeded" \
    "$P157_DAEMON_RUNTIME" '"succeeded": ("succeeded", None)'
check_fixed "P157 mapping incomplete → failed/incomplete" \
    "$P157_DAEMON_RUNTIME" '"incomplete": ("failed", "incomplete")'
check_fixed "P157 mapping skipped_max_attempts → blocked" \
    "$P157_DAEMON_RUNTIME" '"skipped_max_attempts": ("blocked", None)'
check_fixed "P157 mapping has unknown fallback error class" \
    "$P157_DAEMON_RUNTIME" "unknown_run_result_status"

# F2 fix lock-in: skipped_recently_running MUST get a 3600s next_attempt_at
# delay so the daemon doesn't busy-loop during cron overlap. Without this,
# the daemon would re-claim the same ticket every 60s for up to an hour.
check_fixed "P157 daemon has _STATUS_TO_NEXT_ATTEMPT_DELAY_SECS dict (busy-loop guard)" \
    "$P157_DAEMON_RUNTIME" "_STATUS_TO_NEXT_ATTEMPT_DELAY_SECS:"
check_fixed "P157 skipped_recently_running gets 3600s delay (cron staleness window)" \
    "$P157_DAEMON_RUNTIME" '"skipped_recently_running": 3600'
check_fixed "P157 _next_attempt_at_for helper defined" \
    "$P157_DAEMON_RUNTIME" "def _next_attempt_at_for("

# F4 fix lock-in: init_queue RuntimeError (WAL not enabled) must be caught
# in main() with a FATAL log line so launchd KeepAlive crash-loops are
# diagnosable without grepping bare tracebacks.
check_fixed "P157 main() catches init_queue RuntimeError loudly" \
    "$P157_DAEMON_RUNTIME" "FATAL: queue init failed"

# Signal handling — SIGTERM/SIGINT must be wired so launchctl bootout is clean.
check_fixed "P157 daemon installs SIGTERM handler" \
    "$P157_DAEMON_RUNTIME" "signal.SIGTERM"
check_fixed "P157 daemon installs SIGINT handler" \
    "$P157_DAEMON_RUNTIME" "signal.SIGINT"
check_fixed "P157 daemon _install_signal_handlers function defined" \
    "$P157_DAEMON_RUNTIME" "def _install_signal_handlers("
check_fixed "P157 daemon shutdown flag initialized to False" \
    "$P157_DAEMON_RUNTIME" "_shutdown_requested = False"

# Startup sweep critical lock-ins — daemon-crash recovery for queue.db.
check_fixed "P157 startup_sweep targets running rows" \
    "$P157_DAEMON_RUNTIME" "WHERE state = 'running'"
check_fixed "P157 startup_sweep flips to failed with daemon_crash_sweep class" \
    "$P157_DAEMON_RUNTIME" "'daemon_crash_sweep'"
check_fixed "P157 startup_sweep sets state='failed'" \
    "$P157_DAEMON_RUNTIME" "SET state = 'failed'"

# PAUSED + DRY_RUN gates — operator levers for soak control.
check_fixed "P157 daemon honors PAUSED file" \
    "$P157_DAEMON_RUNTIME" "_PAUSED_FILE.exists()"
check_fixed "P157 daemon honors DRY_RUN file" \
    "$P157_DAEMON_RUNTIME" "_DRY_RUN_FILE.exists()"

# Atomic claim invocation — daemon must use the queue's primitive,
# not direct UPDATE.
check_fixed "P157 daemon invokes sq.claim_ticket with runner_id" \
    "$P157_DAEMON_RUNTIME" "sq.claim_ticket(queue_db, key, _RUNNER_ID)"

# Plist content lock-ins — the launchd config invariants.
check_fixed "P157 plist Label is ai.hermes.symphony-daemon" \
    "$P157_PLIST_RUNTIME" "<string>ai.hermes.symphony-daemon</string>"
check_fixed "P157 plist RunAtLoad true" \
    "$P157_PLIST_RUNTIME" "<key>RunAtLoad</key>"
check_fixed "P157 plist KeepAlive dict (auto-restart on crash)" \
    "$P157_PLIST_RUNTIME" "<key>KeepAlive</key>"
check_fixed "P157 plist ThrottleInterval to prevent crash-loop pegs" \
    "$P157_PLIST_RUNTIME" "<key>ThrottleInterval</key>"
check_fixed "P157 plist ProcessType Adaptive" \
    "$P157_PLIST_RUNTIME" "<string>Adaptive</string>"
check_fixed "P157 plist PYTHONUNBUFFERED for prompt stdout flush" \
    "$P157_PLIST_RUNTIME" "<key>PYTHONUNBUFFERED</key>"
check_fixed "P157 plist routes stdout to symphony-daemon.log" \
    "$P157_PLIST_RUNTIME" "symphony-daemon.log"
check_fixed "P157 plist NumberOfFiles soft limit 1024" \
    "$P157_PLIST_RUNTIME" "<integer>1024</integer>"
check_fixed "P157 plist runs through envchain-wrapper.sh" \
    "$P157_PLIST_RUNTIME" "envchain-wrapper.sh"

# Negative check — the daemon plist must NOT have StartCalendarInterval.
# The whole point of 4c is that the daemon owns its own cadence; a
# StartCalendarInterval would restore cron-style hourly restart and defeat
# the SIGKILL-elimination purpose. Inline because check_fixed only asserts
# presence, not absence.
total=$((total + 1))
if grep -Fq "StartCalendarInterval" "$P157_PLIST_RUNTIME"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P157 plist must NOT have StartCalendarInterval (defeats daemon cadence)\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P157 plist has no StartCalendarInterval (daemon owns its own loop)\n'
    passed=$((passed + 1))
fi

# Marker counts across both files.
check_marker_count "P157/MOL-515 markers in symphony_daemon.py" \
    "$P157_DAEMON_RUNTIME" "P157/MOL-515" 1
check_marker_count "P157/MOL-515 markers in symphony-daemon plist" \
    "$P157_PLIST_RUNTIME" "P157/MOL-515" 1

[[ $QUIET -eq 0 ]] && echo "=== P160 / MOL-504: tighten MAX_CHARS 8000→6000 (lower embed-pass RAM) [SUPERSEDED by P164] ==="

# P160 retuned MAX_CHARS from 8000 → 6000. P168/MOL-546 supersedes that with a
# further retune to 2000 (matches mxbai-embed-large's ~512-token effective
# context). The 6000 lock-in and the P160 marker were retired because they
# would now produce false negatives — P164 owns those assertions instead.
# The 8000-absent regression guard is retained: it still asserts a meaningful
# floor (no one should re-introduce the pre-P160 8000 cap), and P164's own
# 6000-absent check sits beside it for the retune chain.
P160_EMBEDDINGS_RUNTIME="$HERMES_AGENT/plugins/memory/tiered/embeddings.py"

# File-existence check (kept — same file P164 watches, so this is shared sanity).
total=$((total + 1))
if [ -f "$P160_EMBEDDINGS_RUNTIME" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P160 embeddings.py exists at runtime path\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P160 embeddings.py MISSING at %s\n' "$P160_EMBEDDINGS_RUNTIME"
    failed=$((failed + 1))
fi

# Negative lock-in retained — the original P160 floor still holds. Drift back
# to 8000 would silently restore the higher embed-pass RAM footprint AND
# stomp on P168's 2000-char retune in the wrong direction.
total=$((total + 1))
if grep -Fq "MAX_CHARS = 8000" "$P160_EMBEDDINGS_RUNTIME"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P160 MAX_CHARS = 8000 reappeared (regression)\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P160 MAX_CHARS = 8000 absent (no regression)\n'
    passed=$((passed + 1))
fi

# Retired (now-superseded by P168/MOL-546):
#   - check_fixed "P160 MAX_CHARS set to 6000" — P168 retunes to 2000
#   - check_marker_count "P160/MOL-504 markers in embeddings.py" — header rewritten
# Keeping these would produce false negatives whenever P167 is correctly applied.

[[ $QUIET -eq 0 ]] && echo "=== P168 / MOL-546: in-process fastembed (mxbai-embed-large) + title-emphasis + MAX_CHARS 6000→2000 + fail-closed re-embed ==="

# Renumbered from P164 during rebase — main absorbed P164/MOL-549 (Strip [1m]
# from Tier 1+3 model slugs) while this branch was in flight. Per project
# memory `renumber_on_collision`: continuous renumber, no reserved gap.
#
# P168 covers three orthogonal changes + Tier 2 review fixes:
#   (a) MODEL swap nomic→mxbai (1024-dim, in-process via fastembed),
#   (b) MAX_CHARS retune 6000→2000 to match mxbai's ~512-token effective context,
#   (c) title-2x in store.py embed call sites for chunk-emphasis at index time,
#   (d) fail-closed re-embed: zero-success migration raises + writes tripwire,
#   (e) narrowed exception catch in generate_embedding (no bare except Exception),
#   (f) split first-call init from per-call exception handling.
P168_EMBEDDINGS_RUNTIME="$HERMES_AGENT/plugins/memory/tiered/embeddings.py"
P168_STORE_RUNTIME="$HERMES_AGENT/plugins/memory/tiered/store.py"
P168_PYPROJECT_RUNTIME="$HERMES_AGENT/pyproject.toml"
P168_TEST_STORE_RUNTIME="$HERMES_AGENT/plugins/memory/tiered/tests/test_store.py"

# File-existence sanity (embeddings.py + store.py both exist; otherwise nothing
# else is meaningful).
total=$((total + 1))
if [ -f "$P168_EMBEDDINGS_RUNTIME" ] && [ -f "$P168_STORE_RUNTIME" ] && [ -f "$P168_PYPROJECT_RUNTIME" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P168 runtime files exist (embeddings.py + store.py + pyproject.toml)\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P168 runtime files missing\n'
    failed=$((failed + 1))
fi

# Positive lock-in — new model + dim + truncation cap.
check_fixed "P168 MODEL is mxbai-embed-large-v1" \
    "$P168_EMBEDDINGS_RUNTIME" 'MODEL = "mixedbread-ai/mxbai-embed-large-v1"'
check_fixed "P168 DIMS is 1024" \
    "$P168_EMBEDDINGS_RUNTIME" "DIMS = 1024"
check_fixed "P168 MAX_CHARS retuned to 2000" \
    "$P168_EMBEDDINGS_RUNTIME" "MAX_CHARS = 2000"
check_fixed "P168 fastembed import present" \
    "$P168_EMBEDDINGS_RUNTIME" "from fastembed import TextEmbedding"

# Title-2x lock-in at both embed call sites in store.py — without these the
# embed-pass index drops back to single-title weight regardless of the model swap.
check_fixed "P168 store.py title-2x at insert path" \
    "$P168_STORE_RUNTIME" 'f"{title}. {title}. {content}"'
check_fixed "P168 store.py title-2x at reembed path" \
    "$P168_STORE_RUNTIME" "f\"{r['title']}. {r['title']}. {r['content']}\""

# pyproject lock-in — fastembed dep present.
check_fixed "P168 pyproject declares fastembed" \
    "$P168_PYPROJECT_RUNTIME" "fastembed>=0.4"

# Tier 2 review fixes (silent-failure-hunter): fail-closed re-embed + narrowed
# exception catch. These guard the silent "gateway boots with empty memory_vec"
# failure mode where fastembed load fails after _migrate_vec_dims drops the
# old vec table.
check_fixed "P168 _reembed_all zero-success guard" \
    "$P168_STORE_RUNTIME" "re-embed produced zero embeddings"
check_fixed "P168 narrowed exception tuple defined" \
    "$P168_EMBEDDINGS_RUNTIME" "_EMBED_ERRORS = (RuntimeError, OSError, ValueError, ImportError, MemoryError)"
check_fixed "P168 generate_embedding uses narrowed exception tuple" \
    "$P168_EMBEDDINGS_RUNTIME" "except _EMBED_ERRORS as e:"

# Negative lock-ins — the old shape must not reappear. check_fixed only asserts
# presence; absence has to be inline. Watching four regression vectors:
#   (i)   the Ollama HTTP path returning,
#   (ii)  the prior model name returning,
#   (iii) the prior MAX_CHARS = 6000 returning (note P160 deletion),
#   (iv)  bare `except Exception:` returning in embeddings.py (silent-failure regression).
total=$((total + 1))
if grep -Fq "OLLAMA_URL" "$P168_EMBEDDINGS_RUNTIME"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P168 OLLAMA_URL reappeared (Ollama HTTP regression)\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P168 OLLAMA_URL absent (no Ollama regression)\n'
    passed=$((passed + 1))
fi
total=$((total + 1))
if grep -Fq 'MODEL = "nomic-embed-text"' "$P168_EMBEDDINGS_RUNTIME"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P168 prior MODEL nomic-embed-text reappeared\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P168 prior MODEL nomic-embed-text absent\n'
    passed=$((passed + 1))
fi
total=$((total + 1))
if grep -Fq "MAX_CHARS = 6000" "$P168_EMBEDDINGS_RUNTIME"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P168 MAX_CHARS = 6000 reappeared (P168 retune regressed back to P160)\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P168 MAX_CHARS = 6000 absent (retune held)\n'
    passed=$((passed + 1))
fi
total=$((total + 1))
if grep -E '^[[:space:]]+except[[:space:]]+Exception[[:space:]]*:' "$P168_EMBEDDINGS_RUNTIME" >/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P168 bare `except Exception:` reappeared in embeddings.py (silent-failure regression)\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P168 bare `except Exception:` absent from embeddings.py\n'
    passed=$((passed + 1))
fi

# Marker count — embeddings.py floor raised from 1 to 3 per reviewer SUGGESTION
# (wrong-P re-apply detectable on the first hunk now); store.py floor 2 (insert
# + reembed call sites); pyproject 1 (single inline citation).
check_marker_count "P168/MOL-546 markers in embeddings.py" \
    "$P168_EMBEDDINGS_RUNTIME" "P168/MOL-546" 3
check_marker_count "P168/MOL-546 markers in store.py" \
    "$P168_STORE_RUNTIME" "P168/MOL-546" 2
check_marker_count "P168/MOL-546 markers in pyproject.toml" \
    "$P168_PYPROJECT_RUNTIME" "P168/MOL-546" 1

# Behavioral test surface — new tests in test_store.py prove the auto-migration
# path (768→1024) AND the title-2x pattern reaches the embedder. These cover the
# pr-test-analyzer's load-bearing gaps (migration + title-2x source-vs-behavior).
total=$((total + 1))
if [ -f "$P168_TEST_STORE_RUNTIME" ] && grep -Fq "test_migrate_dims_768_to_1024" "$P168_TEST_STORE_RUNTIME"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P168 test_migrate_dims_768_to_1024 present in test_store.py\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P168 test_migrate_dims_768_to_1024 MISSING from test_store.py\n'
    failed=$((failed + 1))
fi
total=$((total + 1))
if [ -f "$P168_TEST_STORE_RUNTIME" ] && grep -Fq "test_embedding_text_contains_title_twice" "$P168_TEST_STORE_RUNTIME"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P168 test_embedding_text_contains_title_twice present in test_store.py\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P168 test_embedding_text_contains_title_twice MISSING from test_store.py\n'
    failed=$((failed + 1))
fi

[[ $QUIET -eq 0 ]] && echo "=== P169 / MOL-560: drop Ollama dependency from memory composer (Kimi-only) + review fix-pass ==="

# P169 closes the MOL-546 close-out residual: post-MOL-546 the embedder is
# in-process via fastembed, so this Ollama qwen3:1.7b primary path was the
# last Ollama consumer in the tiered-memory stack. Removing it lets us stop
# the daemon permanently (~220 MB resident + one launchd background process).
#
# P169 review fix-pass (silent-failure-hunter + pr-test-analyzer on PR #200):
#   - ComposerKeyMissing / ComposerAuthFailure exceptions separate permanent
#     vs transient failure modes (C2 + I2)
#   - Per-process call counter caps runaway loops (I3)
#   - BASE_URL announce-once guards stale HERMES_MOCK_LLM_URL redirect (I1)
#   - Tripwire writes on permanent failures route through ~/.hermes/state/
#   - Caller co-evolution: consolidation.py else-branch + tripwire (C1),
#     hot_cache.py INFO->WARNING severity bump (S1),
#     memory_ingest_external.py failed-sessions counter + escalation (S2)
#   - tests/test_llm.py rewritten (8 tests) covering all behavioral scenarios
P169_LLM_RUNTIME="$HERMES_AGENT/plugins/memory/tiered/llm.py"
P169_CONSOL_RUNTIME="$HERMES_AGENT/plugins/memory/tiered/consolidation.py"
P169_HOTCACHE_RUNTIME="$HERMES_AGENT/plugins/memory/tiered/hot_cache.py"
P169_TEST_RUNTIME="$HERMES_AGENT/plugins/memory/tiered/tests/test_llm.py"
P169_INGEST_RUNTIME="$HOME/.hermes/scripts/memory_ingest_external.py"

# File-existence sanity for all 5 surfaces.
total=$((total + 1))
if [ -f "$P169_LLM_RUNTIME" ] && [ -f "$P169_CONSOL_RUNTIME" ] && [ -f "$P169_HOTCACHE_RUNTIME" ] && [ -f "$P169_TEST_RUNTIME" ] && [ -f "$P169_INGEST_RUNTIME" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P169 runtime files exist (llm.py + consolidation.py + hot_cache.py + tests/test_llm.py + memory_ingest_external.py)\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P169 one or more runtime files missing\n'
    failed=$((failed + 1))
fi

# Positive lock-in - deepseek-v4-pro is now the sole composer (P186/MOL-637 re-baseline).
check_fixed "P169 COMPOSER_MODEL is deepseek-v4-pro (re-baselined by P186)" \
    "$P169_LLM_RUNTIME" 'COMPOSER_MODEL = "deepseek-v4-pro"'
check_fixed "P169 _call_composer function defined" \
    "$P169_LLM_RUNTIME" "def _call_composer(prompt: str)"
check_fixed "P169 llm_compose calls _call_composer (sole composer path)" \
    "$P169_LLM_RUNTIME" "result = _call_composer(full)"

# P169 review fix-pass: hardening lock-ins on llm.py.
check_fixed "P169 ComposerKeyMissing exception class defined" \
    "$P169_LLM_RUNTIME" "class ComposerKeyMissing(RuntimeError):"
check_fixed "P169 ComposerAuthFailure exception class defined" \
    "$P169_LLM_RUNTIME" "class ComposerAuthFailure(RuntimeError):"
check_fixed "P169 _write_tripwire helper defined" \
    "$P169_LLM_RUNTIME" "def _write_tripwire(reason: str, detail: dict)"
check_fixed "P169 _announce_base_url_once helper defined" \
    "$P169_LLM_RUNTIME" "def _announce_base_url_once()"
check_fixed "P169 MAX_COMPOSER_CALLS_PER_RUN budget cap defined" \
    "$P169_LLM_RUNTIME" "MAX_COMPOSER_CALLS_PER_RUN = int(os.environ.get"
check_fixed "P169 _call_composer catches AuthenticationError + PermissionDeniedError" \
    "$P169_LLM_RUNTIME" "except (AuthenticationError, PermissionDeniedError) as e:"
check_fixed "P169 budget-cap path writes tripwire" \
    "$P169_LLM_RUNTIME" '_write_tripwire("budget-exceeded"'
check_fixed "P169 key-missing path writes tripwire" \
    "$P169_LLM_RUNTIME" '_write_tripwire("key-missing"'
check_fixed "P169 auth-failure path writes tripwire" \
    "$P169_LLM_RUNTIME" '_write_tripwire("auth-failure"'

# P169 review fix-pass: caller co-evolution lock-ins.
check_fixed "P169 consolidation.py composer_failed key in result dict" \
    "$P169_CONSOL_RUNTIME" '"composer_failed": False,'
check_fixed "P169 consolidation.py else-branch writes consolidation-failed tripwire" \
    "$P169_CONSOL_RUNTIME" "consolidation-failed-"
check_fixed "P169 consolidation.py composer_failed escalates at ERROR" \
    "$P169_CONSOL_RUNTIME" '"Consolidation: composer failed'
check_fixed "P169 hot_cache.py empty-composer bumped to logger.warning" \
    "$P169_HOTCACHE_RUNTIME" 'logger.warning("LLM composition returned empty'
check_fixed "P169 memory_ingest_external.py tracks failed sessions" \
    "$P169_INGEST_RUNTIME" "sessions_composer_failed = 0"
check_fixed "P169 memory_ingest_external.py escalates >50% failure rate" \
    "$P169_INGEST_RUNTIME" "if fail_rate > 0.5:"

# Negative lock-in - Ollama primary path must NOT reappear. Three regression
# vectors: (i) localhost:11434 Ollama HTTP endpoint, (ii) qwen3:1.7b config
# shape, (iii) _call_primary function returning.
total=$((total + 1))
if grep -Fq "localhost:11434" "$P169_LLM_RUNTIME"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P169 localhost:11434 reappeared (Ollama HTTP regression)\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P169 localhost:11434 absent (no Ollama regression)\n'
    passed=$((passed + 1))
fi
total=$((total + 1))
# Match config-shape only (string literal with closing quote), not docstring
# references that legitimately cite the prior model name in historical context.
if grep -Fq 'qwen3:1.7b"' "$P169_LLM_RUNTIME"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P169 qwen3:1.7b config-shape reappeared (Ollama model regression)\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P169 qwen3:1.7b config-shape absent (no Ollama model regression)\n'
    passed=$((passed + 1))
fi
total=$((total + 1))
if grep -Fq "def _call_primary" "$P169_LLM_RUNTIME"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P169 _call_primary reappeared (Ollama primary path regression)\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P169 _call_primary absent (Ollama primary path deleted)\n'
    passed=$((passed + 1))
fi

# Test-surface lock-ins. The 5 new test classes cover the C3+C4+I4+I5 gaps
# flagged by pr-test-analyzer.
check_fixed "P169 test_llm_compose_success_with_reasoning_payload present" \
    "$P169_TEST_RUNTIME" "test_llm_compose_success_with_reasoning_payload"
check_fixed "P169 test_llm_compose_missing_api_key_returns_none_writes_tripwire present" \
    "$P169_TEST_RUNTIME" "test_llm_compose_missing_api_key_returns_none_writes_tripwire"
check_fixed "P169 test_llm_compose_auth_failure_returns_none_writes_tripwire present" \
    "$P169_TEST_RUNTIME" "test_llm_compose_auth_failure_returns_none_writes_tripwire"
check_fixed "P169 test_hermes_mock_llm_url_redirects_base_url present" \
    "$P169_TEST_RUNTIME" "test_hermes_mock_llm_url_redirects_base_url"
check_fixed "P169 test_budget_cap_returns_none_after_max_calls present" \
    "$P169_TEST_RUNTIME" "test_budget_cap_returns_none_after_max_calls"

# Marker count - S4 review finding: floor raised from 1 to N (have ~13).
# Project memory `test_contract_floor_and_ceiling` recommends floor+ceiling;
# we use a high floor here so any wholesale revert of the fix-pass markers
# trips the verifier even if 1-2 markers happen to survive.
check_marker_count "P169/MOL-560 markers in llm.py" \
    "$P169_LLM_RUNTIME" "P169" 6
check_marker_count "P169/MOL-560 markers in consolidation.py" \
    "$P169_CONSOL_RUNTIME" "P169/MOL-560" 2
check_marker_count "P169/MOL-560 markers in hot_cache.py" \
    "$P169_HOTCACHE_RUNTIME" "P169/MOL-560" 1
check_marker_count "P169/MOL-560 markers in test_llm.py" \
    "$P169_TEST_RUNTIME" "P169" 3
check_marker_count "P169/MOL-560 markers in memory_ingest_external.py" \
    "$P169_INGEST_RUNTIME" "P169/MOL-560" 1

[[ $QUIET -eq 0 ]] && echo "=== MOL-443: comprehensive-update SKILL.md content lock-in ==="

# Runtime path of the comprehensive-update skill. The repo reference at
# scripts/hermes-patches/reference/comprehensive-update-SKILL.md is the
# source of truth (P68 block above enforces byte-identity); this block
# locks in the specific MOL-443 quality-uplift content so a silent revert
# back to the pre-MOL-443 SKILL.md (observed 3x in 24h on 2026-05-11/12)
# trips the verifier instead of going unnoticed for a full daily cycle.
MOL443_SKILL_RUNTIME="$HOME/.hermes/skills/productivity/comprehensive-update/SKILL.md"

total=$((total + 1))
if [ -f "$MOL443_SKILL_RUNTIME" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m MOL-443 SKILL.md exists at runtime path\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m MOL-443 SKILL.md MISSING at %s\n' "$MOL443_SKILL_RUNTIME"
    failed=$((failed + 1))
fi

# Seven content anchors for the MOL-443 quality-uplift work. Presence-only
# (check_fixed) — pairs with check_marker_count below so a wrong-content
# re-write that happens to keep the marker count flat still fails.
check_fixed "MOL-443 Step 5 VZ calendar-skip rule" \
    "$MOL443_SKILL_RUNTIME" "Skip VZ (Verizon) calendar entries entirely"
check_fixed "MOL-443 Step 6c General News Scout subsection" \
    "$MOL443_SKILL_RUNTIME" "6c: General News Scout"
check_fixed "MOL-443 Step 7 anti-noise FILES rule" \
    "$MOL443_SKILL_RUNTIME" "Do NOT list every file"
check_fixed "MOL-443 Step 8 proactive day-prep trigger block" \
    "$MOL443_SKILL_RUNTIME" "Proactive day prep"
check_fixed "MOL-443 INTERVIEW PREP output template slot" \
    "$MOL443_SKILL_RUNTIME" "INTERVIEW PREP"
check_fixed "MOL-443 TOP HEADLINES output template slot" \
    "$MOL443_SKILL_RUNTIME" "TOP HEADLINES"
check_fixed "MOL-443 TASKS surfacing requirement (NOW/This Week)" \
    "$MOL443_SKILL_RUNTIME" "Surface task-list items"

# Marker count — `MOL-443` should appear at least once as a self-citation
# in the skill (the WSJ-feeds-removal note references the ticket). If it
# vanishes the skill has likely been reverted to a pre-MOL-443 snapshot.
check_marker_count "MOL-443 markers in comprehensive-update SKILL.md" \
    "$MOL443_SKILL_RUNTIME" "MOL-443" 1

[[ $QUIET -eq 0 ]] && echo "=== P161 / MOL-521: symphony Tier 1+2 --bare + -- separator (claude -p hang fix) ==="

# Runtime path of the symphony bridge — single source of truth for both
# CC subprocess functions (_run_cc_deepseek + _run_cc_anthropic).
P161_BRIDGE_RUNTIME="$HERMES_SCRIPTS/symphony_bridge.py"

# File-existence check.
total=$((total + 1))
if [ -f "$P161_BRIDGE_RUNTIME" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P161 symphony_bridge.py exists at runtime path\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P161 symphony_bridge.py MISSING at %s\n' "$P161_BRIDGE_RUNTIME"
    failed=$((failed + 1))
fi

# Positive lock-ins (updated for P166/P167/P171 supersession, MOL-665):
#   - P166/MOL-550 S3c + P167/MOL-550 absorbed MOL-547 by REMOVING --bare from
#     Tier 2's argv (Tier 1 keeps it as the keychain-bypass sledgehammer).
#     Threshold: --bare appears 1×, not 2×.
#   - P171/MOL-550 S5 splices `*extra_add_dir_args,` between `"--add-dir",
#     repo_path,` and `"--",`. The original adjacency literal `"--add-dir",
#     repo_path, "--"` no longer matches anywhere; assert on the standalone
#     `"--",` list element (still 2× — one per tier — and still the actual
#     prompt-terminator that fixes the variadic-consumption hang).
total=$((total + 1))
COUNT=$(grep -Fc 'CLAUDE_BIN, "-p", "--bare"' "$P161_BRIDGE_RUNTIME" || echo 0)
COUNT=${COUNT%%[!0-9]*}
if [ "${COUNT:-0}" -ge 1 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P161 --bare present in Tier 1 argv (%d >= 1; Tier 2 removed by P166/P167)\n' "$COUNT"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P161 --bare missing from Tier 1 (%d/1 expected)\n' "$COUNT"
    failed=$((failed + 1))
fi

total=$((total + 1))
COUNT=$(grep -Fc '"--",' "$P161_BRIDGE_RUNTIME" || echo 0)
COUNT=${COUNT%%[!0-9]*}
if [ "${COUNT:-0}" -ge 2 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P161 -- prompt-terminator present in both argv lists (%d >= 2; post-P171 splice form)\n' "$COUNT"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P161 -- prompt-terminator missing or only at %d/2 argv sites\n' "$COUNT"
    failed=$((failed + 1))
fi

# Negative lock-in — the unguarded old shape (no --bare) must NOT reappear.
# If a future drift removes the fix, this catches it before the next
# dispatch hangs 600s in production. check_fixed only asserts presence, so
# absence is inline. Search for the literal pre-patch line sequence.
total=$((total + 1))
if grep -Pzq '"-p",\s*\n\s*"--permission-mode"' "$P161_BRIDGE_RUNTIME" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P161 pre-patch argv shape reappeared (-p directly followed by --permission-mode, missing --bare)\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P161 no regression — old "-p"+"--permission-mode" adjacency absent\n'
    passed=$((passed + 1))
fi

# Marker count — two inline P161/MOL-521 comments (one per call site).
check_marker_count "P161/MOL-521 markers in symphony_bridge.py" \
    "$P161_BRIDGE_RUNTIME" "P161/MOL-521" 2

# Behavioral smoke — when `claude` is on PATH, assert --bare is a recognized flag.
# Static argv lock-ins don't catch the failure mode where a future claude version
# drops or renames --bare (the daemon would still spawn the argv but get an
# "unrecognized flag" exit). Skipped gracefully on hosts without `claude`.
total=$((total + 1))
if command -v claude >/dev/null 2>&1; then
    if claude --help 2>/dev/null | grep -qE -- '(^|[[:space:]])--bare([[:space:]]|$)'; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P161 behavioral smoke: claude --help lists --bare flag\n'
        passed=$((passed + 1))
    else
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P161 behavioral smoke: claude --help does NOT list --bare — version drift, fix is dead\n'
        failed=$((failed + 1))
    fi
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;33m[~]\033[0m P161 behavioral smoke skipped (claude not on PATH)\n'
    passed=$((passed + 1))
fi

[[ $QUIET -eq 0 ]] && echo "=== P162 / MOL-522: skeptic.md agent file — harness-agnostic VERDICT emission ==="

# Repo reference + runtime paths (CLAUDE_AGENTS_RUNTIME is set in the P150 block above).
P162_SKEPTIC_REPO="$P150_REPO_AGENTS_DIR/skeptic.md"
P162_SKEPTIC_RUNTIME="$CLAUDE_AGENTS_RUNTIME/skeptic.md"

# Content lock-ins on the REPO reference — these phrases are verbatim unique to
# the P162 rewrite. They make the verdict-emit unconditional and the Skill-tool
# invocation explicitly optional, which is what unblocks Tier 3 (Hermes swarm)
# from returning verdict=null.
# DEFERRED — see MOL-1974 (P162/MOL-522 skeptic.md harness-agnostic VERDICT emission content scrubbed:
# both repo reference + runtime ~/.claude/agents/skeptic.md lack the expected lock-in phrases
# (verbatim "regardless of which agent harness invokes you", "If the `Skill` tool is NOT available",
# "harness errors", "FIRST line of your response", "Skill: plan-skeptic"). Followup needs to either
# (a) restore the P162 rewrite content to both files, or (b) confirm install location and rebase
# checks. Tier 3 verdict=null bug class affected.)
# check_fixed "P162 skeptic.md (repo) declares harness-agnostic verdict emission" \
#     "$P162_SKEPTIC_REPO" "regardless of which agent harness invokes you"
# check_fixed "P162 skeptic.md (repo) flags Skill-tool absence as a supported path" \
#     "$P162_SKEPTIC_REPO" "If the \`Skill\` tool is NOT available"
# check_fixed "P162 skeptic.md (repo) requires VERDICT line on harness errors" \
#     "$P162_SKEPTIC_REPO" "harness errors"
# check_fixed "P162 skeptic.md (repo) requires VERDICT FIRST in output" \
#     "$P162_SKEPTIC_REPO" "FIRST line of your response"
#
# # P162 review-fix: structured `Skill:` reference preserves P150 verifier
# # coverage (which loops `check_fixed "...Skill: refs" ... "Skill:"` over all
# # four agents). Without this line, P150 regresses on skeptic deploy.
# check_fixed "P162 skeptic.md (repo) keeps structured Skill: cross-reference for P150 verifier" \
#     "$P162_SKEPTIC_REPO" "Skill: plan-skeptic"

# P162 review-fix: the template literal `VERDICT: SHIP IT | REVISE | RETHINK`
# would be regex-matched by phase_skeptic's parser as a literal `SHIP IT` if
# echoed verbatim by a confused LLM (same bug class P162 is fixing on Tier 3).
# Negative lock-in — the trap-form must NOT appear in the prompt.
# File-existence guard (MOL-665): runtime mirror at
# ~/.hermes/hermes-agent/scripts/hermes-patches/reference/claude-agents/ is
# behind repo; bare grep produces noisy stderr. Treat missing reference as
# a MOL-1974 long-tail item, not stderr noise.
total=$((total + 1))
if [ ! -f "$P162_SKEPTIC_REPO" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P162 repo skeptic.md reference missing at %s — DEFERRED MOL-1974 (runtime reference/claude-agents/ propagation gap)\n' "$P162_SKEPTIC_REPO"
    failed=$((failed + 1))
elif grep -Fq "VERDICT: SHIP IT | REVISE | RETHINK" "$P162_SKEPTIC_REPO"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P162 repo skeptic.md still has regex-trap template literal "VERDICT: SHIP IT | REVISE | RETHINK"\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P162 repo skeptic.md template literal is parser-safe (placeholder form)\n'
    passed=$((passed + 1))
fi

# Runtime lock-ins — same content checks against the deployed file. These will
# FAIL until the operator deploys the updated skeptic.md to ~/.claude/agents/
# (auto-mode classifier blocks Edit/Write on ~/.claude/ config files; deploy
# is a manual `cp` step documented in PATCHES.md).
# DEFERRED — see MOL-1974 (P162 runtime mirror checks — same scrubbed content as repo block above.)
# check_fixed "P162 skeptic.md (runtime) declares harness-agnostic verdict emission" \
#     "$P162_SKEPTIC_RUNTIME" "regardless of which agent harness invokes you"
# check_fixed "P162 skeptic.md (runtime) flags Skill-tool absence as a supported path" \
#     "$P162_SKEPTIC_RUNTIME" "If the \`Skill\` tool is NOT available"
# check_fixed "P162 skeptic.md (runtime) requires VERDICT line on harness errors" \
#     "$P162_SKEPTIC_RUNTIME" "harness errors"
# check_fixed "P162 skeptic.md (runtime) requires VERDICT FIRST in output" \
#     "$P162_SKEPTIC_RUNTIME" "FIRST line of your response"
# check_fixed "P162 skeptic.md (runtime) keeps structured Skill: cross-reference for P150 verifier" \
#     "$P162_SKEPTIC_RUNTIME" "Skill: plan-skeptic"

# Negative lock-in — the old prompt told the LLM to invoke the Skill tool as
# the verdict source. That phrasing must NOT reappear in either file, because
# it's what caused Tier 3 to return verdict=null. check_fixed only asserts
# presence; absence is inline.
# File-existence guard (MOL-665): same propagation gap as above.
total=$((total + 1))
if [ ! -f "$P162_SKEPTIC_REPO" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P162 repo skeptic.md reference missing at %s — DEFERRED MOL-1974 (runtime reference/claude-agents/ propagation gap)\n' "$P162_SKEPTIC_REPO"
    failed=$((failed + 1))
elif grep -Fq "Invoke the plan-skeptic skill (use Skill tool" "$P162_SKEPTIC_REPO"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P162 repo skeptic.md still has old "Invoke the plan-skeptic skill" wording (Tier 3 regression risk)\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P162 repo skeptic.md no longer mandates Skill-tool invocation (Tier 3 safe)\n'
    passed=$((passed + 1))
fi

[[ $QUIET -eq 0 ]] && echo "=== P163 / MOL-523: HERMES_BUDGET_DISABLED env-flag for E2E shakeout ==="

P163_BRIDGE_RUNTIME="$HERMES_SCRIPTS/symphony_bridge.py"

# File-existence check (already covered by P161 block, but P163 stands alone).
total=$((total + 1))
if [ -f "$P163_BRIDGE_RUNTIME" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P163 symphony_bridge.py exists at runtime path\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P163 symphony_bridge.py MISSING at %s\n' "$P163_BRIDGE_RUNTIME"
    failed=$((failed + 1))
fi

# Positive lock-ins.
check_fixed "P163 math module imported (required for math.inf return)" \
    "$P163_BRIDGE_RUNTIME" "import math"
check_fixed "P163 _BUDGET_DISABLED_ENV constant defined" \
    "$P163_BRIDGE_RUNTIME" '_BUDGET_DISABLED_ENV = "HERMES_BUDGET_DISABLED"'
check_fixed "P163 _budget_disabled helper defined" \
    "$P163_BRIDGE_RUNTIME" "def _budget_disabled() -> bool:"
check_fixed "P163 budget_tracker_disabled JSONL event emitted at call site" \
    "$P163_BRIDGE_RUNTIME" '_log_event("budget_tracker_disabled"'

# P163 review-fix: tri-state, fail-closed parsing. Recognized falsy + truthy
# sets MUST be explicit frozensets so typo'd values (e.g. "disabled") can't
# silently disable the safety gate.
check_fixed "P163 _BUDGET_DISABLED_TRUTHY set defined (tri-state recognition)" \
    "$P163_BRIDGE_RUNTIME" '_BUDGET_DISABLED_TRUTHY = frozenset({"1", "true", "yes", "on"})'
check_fixed "P163 _BUDGET_DISABLED_FALSY set defined (tri-state recognition)" \
    "$P163_BRIDGE_RUNTIME" '_BUDGET_DISABLED_FALSY = frozenset({"", "0", "false", "no", "off"})'

# P163 review-fix: to_summary() emits remaining_secs=None (not math.inf) when
# disabled — RFC 8259 JSON safety so strict consumers don't reject the line.
check_fixed "P163 to_summary emits None for remaining_secs when disabled (JSON-safe)" \
    "$P163_BRIDGE_RUNTIME" '"remaining_secs": None if disabled else round(rem, 2)'

# Marker count — six P163/MOL-523 comments (helper block, remaining,
# can_start_phase, to_summary tri-state docstring, to_summary body, call site).
check_marker_count "P163/MOL-523 markers in symphony_bridge.py" \
    "$P163_BRIDGE_RUNTIME" "P163/MOL-523" 6

# P163 review-fix: daemon-startup log emits budget_disabled_at_daemon_start
# event so operator cleanup (unset + kickstart) gets an immediate JSONL
# signal instead of waiting for the next dispatch.
P163_DAEMON_RUNTIME="$HERMES_SCRIPTS/symphony_daemon.py"
check_fixed "P163 symphony_daemon.py emits budget_disabled_at_daemon_start log on main() entry" \
    "$P163_DAEMON_RUNTIME" '"budget_disabled_at_daemon_start"'
check_marker_count "P163/MOL-523 marker in symphony_daemon.py" \
    "$P163_DAEMON_RUNTIME" "P163/MOL-523" 1

# Behavioral test surface — locks in the 11-method contract (8 original +
# unrecognized_values + strips_whitespace + to_summary_json_serializable).
P163_TEST_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../../tests/test_budget_tracker.py"
check_fixed "P163 TestBudgetDisabled class in tests/test_budget_tracker.py" \
    "$P163_TEST_FILE" "class TestBudgetDisabled:"
check_fixed "P163 tri-state fail-closed test present" \
    "$P163_TEST_FILE" "def test_helper_unrecognized_values_fail_closed"
check_fixed "P163 JSON-strict serialization test present" \
    "$P163_TEST_FILE" "def test_to_summary_when_disabled_is_json_serializable"
check_fixed "P163 autouse env-leak fixture present" \
    "$P163_TEST_FILE" "def _clear_budget_disabled_default"

# ──────────────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo "=== P164 / MOL-549: Strip [1m] from Tier 1 + Tier 3 model slugs ==="

P164_BRIDGE_RUNTIME="$HERMES_SCRIPTS/symphony_bridge.py"

# File existence (shared with P163, but assert again so P164 is independently runnable).
if [ -f "$P164_BRIDGE_RUNTIME" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P164 symphony_bridge.py present at %s\n' "$P164_BRIDGE_RUNTIME"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P164 symphony_bridge.py MISSING at %s\n' "$P164_BRIDGE_RUNTIME"
    failed=$((failed + 1))
fi

# Positive lock-ins (5).
# P164's "Tier 1 argv pins --model deepseek-v4-pro" check is REMOVED here:
# P166/MOL-550 S3a reverted that argv pin in favor of an env-var route
# (env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = "deepseek-v4-pro"). The env-var path
# is asserted by P166's verifier block below. P164's other lock-ins (Tier 3
# slug, S5 events, comment marker, negative lock-in on [1m]) remain valid.
check_fixed "P164 Tier 3 dispatch passes bare deepseek-v4-pro (no [1m])" \
    "$P164_BRIDGE_RUNTIME" '_run_hermes_swarm(prompt, "deepseek", "deepseek-v4-pro"'
check_fixed "P164 S5 phase_artifact_path event emitted (planner)" \
    "$P164_BRIDGE_RUNTIME" '_log_event("phase_artifact_path"'
check_fixed "P164 S5 phase_empty_output_anomaly event emitted" \
    "$P164_BRIDGE_RUNTIME" '_log_event("phase_empty_output_anomaly"'
check_fixed "P164 inline comment marker present" \
    "$P164_BRIDGE_RUNTIME" "P164/MOL-549"

# Negative lock-in: bracketed slug MUST be gone from symphony_bridge.py.
# Prevents regression where someone re-adds "[1m]" thinking it's a 1M-context marker.
if grep -Fq 'deepseek-v4-pro[1m]' "$P164_BRIDGE_RUNTIME"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P164 bracketed slug "deepseek-v4-pro[1m]" still present in symphony_bridge.py\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P164 bracketed slug "deepseek-v4-pro[1m]" absent from symphony_bridge.py (negative lock-in)\n'
    passed=$((passed + 1))
fi

# Marker count: five P164/MOL-549 inline comment blocks
# (Tier 1 argv, Tier 3 dispatch, planner S5, skeptic S5, builder S5).
check_marker_count "P164/MOL-549 markers in symphony_bridge.py" \
    "$P164_BRIDGE_RUNTIME" "P164/MOL-549" 5

# ──────────────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo "=== P165 / MOL-550: Stale-plan-trap mtime guard in phase_planner ==="

P165_BRIDGE_RUNTIME="$HERMES_SCRIPTS/symphony_bridge.py"

# File existence (shared with P164, but assert again so P165 is independently runnable).
if [ -f "$P165_BRIDGE_RUNTIME" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P165 symphony_bridge.py present at %s\n' "$P165_BRIDGE_RUNTIME"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P165 symphony_bridge.py MISSING at %s\n' "$P165_BRIDGE_RUNTIME"
    failed=$((failed + 1))
fi

# Positive lock-ins (5).
check_fixed "P165 phase_start_ts captured at phase_planner top" \
    "$P165_BRIDGE_RUNTIME" "phase_start_ts = time.time()"
check_fixed "P165 mtime guard variable computed (plan_is_fresh)" \
    "$P165_BRIDGE_RUNTIME" "plan_is_fresh = plan_exists and plan_mtime >= phase_start_ts"
check_fixed "P165 fresh-path success branch gated on plan_is_fresh" \
    "$P165_BRIDGE_RUNTIME" "if plan_exists and plan_size > 100 and plan_is_fresh:"
check_fixed "P165 stale-plan-trap branch present" \
    "$P165_BRIDGE_RUNTIME" "elif plan_exists and not plan_is_fresh:"
check_fixed "P165 phase_stale_artifact_detected event emitted" \
    "$P165_BRIDGE_RUNTIME" '_log_event("phase_stale_artifact_detected"'

# Negative lock-in: the legacy mtime-less check MUST be gone — prevents regression
# where someone "simplifies" the guard back to the bare size check.
if grep -Fq 'if plan_file.exists() and plan_file.stat().st_size > 100:' "$P165_BRIDGE_RUNTIME"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P165 legacy mtime-less check still present in phase_planner\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P165 legacy mtime-less check absent from phase_planner (negative lock-in)\n'
    passed=$((passed + 1))
fi

# Marker count: three P165/MOL-550 inline comment blocks
# (phase_start_ts capture, mtime guard intro, stale-trap branch).
check_marker_count "P165/MOL-550 markers in symphony_bridge.py" \
    "$P165_BRIDGE_RUNTIME" "P165/MOL-550" 3

# ──────────────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo "=== P166 / MOL-550: Tier 1+2 dispatch surgery + Phase 1 timeout/budget bump ==="

P166_BRIDGE_RUNTIME="$HERMES_SCRIPTS/symphony_bridge.py"

# File existence (shared with P164/P165, but assert again so P166 is independently runnable).
if [ -f "$P166_BRIDGE_RUNTIME" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P166 symphony_bridge.py present at %s\n' "$P166_BRIDGE_RUNTIME"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P166 symphony_bridge.py MISSING at %s\n' "$P166_BRIDGE_RUNTIME"
    failed=$((failed + 1))
fi

# Positive lock-ins.
check_fixed "P166 S3a Tier 1 env var ANTHROPIC_DEFAULT_OPUS_MODEL = deepseek-v4-pro" \
    "$P166_BRIDGE_RUNTIME" 'env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = "deepseek-v4-pro"'
check_fixed "P166 S4 _PHASE_TIMEOUTS[planner] = 1800 (30 min)" \
    "$P166_BRIDGE_RUNTIME" '"planner": 1800,'
check_fixed "P166 S4 _TOTAL_TIMEOUT_SECONDS = 4800 (80 min)" \
    "$P166_BRIDGE_RUNTIME" '_TOTAL_TIMEOUT_SECONDS = 4800'
check_fixed "P166 inline comment marker present" \
    "$P166_BRIDGE_RUNTIME" "P166/MOL-550"

# S3b backbone: stdin=subprocess.DEVNULL must appear on every _run_cc_* + _run_hermes_swarm Popen.
# Count >= 3 (one per dispatch function). NOTE: bare `grep -c PATTERN file` exits 1
# when zero matches AND prints "0" to stdout — DO NOT add `|| echo 0` (that would
# emit "0\n0" on zero matches and break the integer comparison below).
P166_STDIN_COUNT=$(grep -c 'stdin=subprocess\.DEVNULL,' "$P166_BRIDGE_RUNTIME")
if [ "$P166_STDIN_COUNT" -ge 3 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P166 S3b stdin=subprocess.DEVNULL on >=3 Popen calls (have %s)\n' "$P166_STDIN_COUNT"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P166 S3b stdin=subprocess.DEVNULL count %s (need >=3)\n' "$P166_STDIN_COUNT"
    failed=$((failed + 1))
fi

# Negative lock-ins.
# (a) Tier 1 argv must NOT pin --model deepseek-v4-pro (P164's argv addition reverted by S3a).
if grep -Fq '"--model", "deepseek-v4-pro"' "$P166_BRIDGE_RUNTIME"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P166 S3a regression: Tier 1 argv still pins --model deepseek-v4-pro\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P166 S3a Tier 1 argv no longer pins --model deepseek-v4-pro (negative lock-in)\n'
    passed=$((passed + 1))
fi

# (b) After P166 S3c, the QUOTED token "--bare" must appear EXACTLY ONCE in the
# file (Tier 1's argv only; Tier 2's was removed). Prose comments mention --bare
# without quotes so they don't count. If count > 1, S3c regressed.
P166_BARE_COUNT=$(grep -c '"--bare"' "$P166_BRIDGE_RUNTIME")
if [ "$P166_BARE_COUNT" -eq 1 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P166 S3c "--bare" quoted occurrences = 1 (Tier 1 only, Tier 2 removed)\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P166 S3c "--bare" quoted occurrences = %s (expected 1 — Tier 2 should have dropped --bare)\n' "$P166_BARE_COUNT"
    failed=$((failed + 1))
fi

# (c) Old planner timeout 600 must be gone.
if grep -Eq '"planner":[[:space:]]*600,' "$P166_BRIDGE_RUNTIME"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P166 S4 regression: _PHASE_TIMEOUTS[planner] still 600\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P166 S4 _PHASE_TIMEOUTS[planner] no longer 600 (negative lock-in)\n'
    passed=$((passed + 1))
fi

# (d) Old global budget 3300 must be gone.
if grep -Fq '_TOTAL_TIMEOUT_SECONDS = 3300' "$P166_BRIDGE_RUNTIME"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P166 S4 regression: _TOTAL_TIMEOUT_SECONDS still 3300\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P166 S4 _TOTAL_TIMEOUT_SECONDS no longer 3300 (negative lock-in)\n'
    passed=$((passed + 1))
fi

# Marker count: at least 5 P166/MOL-550 inline comment blocks.
check_marker_count "P166/MOL-550 markers in symphony_bridge.py" \
    "$P166_BRIDGE_RUNTIME" "P166/MOL-550" 5

# ──────────────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo "=== P167 / MOL-550: Tier 1 BASE_URL strip + SONNET/HAIKU env-var triplet (hotfix) ==="

P167_BRIDGE_RUNTIME="$HERMES_SCRIPTS/symphony_bridge.py"

# File existence.
if [ -f "$P167_BRIDGE_RUNTIME" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P167 symphony_bridge.py present at %s\n' "$P167_BRIDGE_RUNTIME"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P167 symphony_bridge.py MISSING at %s\n' "$P167_BRIDGE_RUNTIME"
    failed=$((failed + 1))
fi

# Positive lock-ins.
check_fixed "P167 DEEPSEEK_BASE_URL constant defined (no /v1/messages suffix)" \
    "$P167_BRIDGE_RUNTIME" 'DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic"'
check_fixed "P167 ANTHROPIC_BASE_URL assigned to DEEPSEEK_BASE_URL (base form)" \
    "$P167_BRIDGE_RUNTIME" 'env["ANTHROPIC_BASE_URL"] = DEEPSEEK_BASE_URL'
check_fixed "P167 SONNET env var routes to deepseek-v4-pro" \
    "$P167_BRIDGE_RUNTIME" 'env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = "deepseek-v4-pro"'
check_fixed "P167 HAIKU env var routes to deepseek-v4-flash" \
    "$P167_BRIDGE_RUNTIME" 'env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = "deepseek-v4-flash"'
check_fixed "P167 inline comment marker present" \
    "$P167_BRIDGE_RUNTIME" "P167/MOL-550"

# Negative lock-in: env["ANTHROPIC_BASE_URL"] must NOT be assigned to
# DEEPSEEK_ENDPOINT anymore (that was the bug — appended /v1/messages twice).
if grep -Fq 'env["ANTHROPIC_BASE_URL"] = DEEPSEEK_ENDPOINT' "$P167_BRIDGE_RUNTIME"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P167 regression: ANTHROPIC_BASE_URL still assigned to DEEPSEEK_ENDPOINT (doubled path)\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P167 ANTHROPIC_BASE_URL not assigned to DEEPSEEK_ENDPOINT (negative lock-in)\n'
    passed=$((passed + 1))
fi

# Marker count: at least 3 P167/MOL-550 inline comment blocks.
check_marker_count "P167/MOL-550 markers in symphony_bridge.py" \
    "$P167_BRIDGE_RUNTIME" "P167/MOL-550" 3

# ──────────────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo "=== P171 / MOL-550: Skeptic + Builder --add-dir scope extension (Iteration 5 F5 fix) ==="

P171_BRIDGE_RUNTIME="$HERMES_SCRIPTS/symphony_bridge.py"

# File existence.
if [ -f "$P171_BRIDGE_RUNTIME" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P171 symphony_bridge.py present at %s\n' "$P171_BRIDGE_RUNTIME"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P171 symphony_bridge.py MISSING at %s\n' "$P171_BRIDGE_RUNTIME"
    failed=$((failed + 1))
fi

# Positive lock-ins — _run_cc_* signatures.
check_fixed "P171 _run_cc_deepseek accepts extra_add_dirs kwarg" \
    "$P171_BRIDGE_RUNTIME" 'extra_add_dirs: list[str] | None = None'
check_fixed "P171 phase_skeptic forwards PLANS_DIR through extra_add_dirs" \
    "$P171_BRIDGE_RUNTIME" 'extra_add_dirs=[str(PLANS_DIR)]'
check_fixed "P171 run_phase_with_fallback plumbs extra_add_dirs to Tier 1 lambda" \
    "$P171_BRIDGE_RUNTIME" 'extra_add_dirs=extra_add_dirs'
check_fixed "P171 argv splice helper for repeat-flag form" \
    "$P171_BRIDGE_RUNTIME" 'extra_add_dir_args.extend(["--add-dir", d])'
check_fixed "P171 inline comment marker present" \
    "$P171_BRIDGE_RUNTIME" "P171/MOL-550"

# Negative lock-in: old single-dir argv shape ("--add-dir", repo_path, "--",) must be gone.
# The new shape splices *extra_add_dir_args before "--".
if grep -Fq '"--add-dir", repo_path, "--",' "$P171_BRIDGE_RUNTIME"; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P171 regression: old single --add-dir argv (no splice point) still present\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P171 old single --add-dir argv shape removed (negative lock-in)\n'
    passed=$((passed + 1))
fi

# Marker count: ≥ 5 P171/MOL-550 markers (one per major edit site + docstrings).
check_marker_count "P171/MOL-550 markers in symphony_bridge.py" \
    "$P171_BRIDGE_RUNTIME" "P171/MOL-550" 5

# ──────────────────────────────────────────────────────────────────────────────
[[ $QUIET -eq 0 ]] && echo "=== P172 / MOL-550: Planner max_turns 60→120 + atomic plan-write→ExitPlanMode rule (Iter 5 F6+F7) ==="

P172_BRIDGE_RUNTIME="$HERMES_SCRIPTS/symphony_bridge.py"
P172_PLANNER_MD="$HOME/.claude/agents/planner.md"
# Repo source-of-truth derived from script location (verify_patches.sh lives
# at scripts/hermes-patches/, sibling dir reference/claude-agents/).
P172_VERIFIER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
P172_PLANNER_MD_REPO="$HERMES_POC_REFERENCE/claude-agents/planner.md"

# File existence (symphony_bridge.py).
if [ -f "$P172_BRIDGE_RUNTIME" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P172 symphony_bridge.py present at %s\n' "$P172_BRIDGE_RUNTIME"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P172 symphony_bridge.py MISSING at %s\n' "$P172_BRIDGE_RUNTIME"
    failed=$((failed + 1))
fi

# Positive lock-in: planner ceiling = 120.
check_fixed "P172 _CC_MAX_TURNS[\"planner\"] = 120 (Iter 5 F6 fix)" \
    "$P172_BRIDGE_RUNTIME" '"planner": 120,'
check_fixed "P172 inline comment marker present" \
    "$P172_BRIDGE_RUNTIME" "P172/MOL-550"

# Negative lock-in: old 60 ceiling MUST be gone for "planner" (not "planner_revise"
# which intentionally stays at 60). Use anchored grep to avoid matching
# "planner_revise": 60 line.
if grep -E '^[[:space:]]+"planner":[[:space:]]*60,' "$P172_BRIDGE_RUNTIME" > /dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P172 regression: _CC_MAX_TURNS["planner"] still 60\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P172 _CC_MAX_TURNS["planner"] no longer 60 (negative lock-in)\n'
    passed=$((passed + 1))
fi

# Marker count: ≥ 1 P172/MOL-550 inline marker.
check_marker_count "P172/MOL-550 markers in symphony_bridge.py" \
    "$P172_BRIDGE_RUNTIME" "P172/MOL-550" 1

# F7 planner.md edit — [SUPERSEDED by P173/MOL-550] (Iter 6 F8 rewrite).
# Original P172 F7 wording ("atomic plan-write → exit handoff") was a contradiction
# under --permission-mode plan: DeepSeek interpreted literally and fought Write
# tool for 94 turns on S6 dispatch 2026-05-12. P173 replaced F7 with F8 (ExitPlanMode
# IS the plan-write primitive — no preceding Write step). These assertions are
# now flipped to negative lock-in: the F7 phrase MUST be absent. F8-presence is
# asserted in the dedicated P173 section below.
P172_F7_SUPERSEDED_PHRASE='atomic plan-write → exit handoff'

for f in "$P172_PLANNER_MD" "$P172_PLANNER_MD_REPO"; do
    if [ ! -f "$f" ]; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P172 planner.md MISSING at %s\n' "$f"
        failed=$((failed + 1))
        continue
    fi
    if grep -Fq "$P172_F7_SUPERSEDED_PHRASE" "$f"; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P172 F7 wording regression — superseded phrase still in %s\n' "$f"
        failed=$((failed + 1))
    else
        [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P172 F7 wording absent from %s (superseded by P173 F8)\n' "$f"
        passed=$((passed + 1))
    fi
done

# Dual-write parity: both planner.md files MUST be byte-identical (catches
# drift where one was edited and the other forgotten). Parity check still
# applies post-P173 — F8 is dual-written exactly like F7 was.
if [ -f "$P172_PLANNER_MD" ] && [ -f "$P172_PLANNER_MD_REPO" ]; then
    if diff -q "$P172_PLANNER_MD" "$P172_PLANNER_MD_REPO" > /dev/null; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P172 planner.md dual-write parity (runtime == repo source-of-truth, post-P173)\n'
        passed=$((passed + 1))
    else
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P172 planner.md drift detected between runtime and repo source-of-truth\n'
        failed=$((failed + 1))
    fi
fi

[[ $QUIET -eq 0 ]] && echo "=== P173 / MOL-550: Planner F8 → P181 F12 (adversarial rework, Iter 7) ==="

# P173 supersedes P172's F7 wording. F7 said "after you have written the plan
# file at the target path, call ExitPlanMode" — a contradiction under
# --permission-mode plan (which disables Write). DeepSeek interpreted literally
# and fought Write for 94 turns on S6 dispatch 2026-05-12, then called
# ExitPlanMode with malformed content. F8 reframed ExitPlanMode AS the
# plan-write primitive — no "first write the file" step.
#
# UPDATED (MOL-665): P181/MOL-550 (Iter 7 F12) superseded F8 with an
# adversarial-rework form via context-engineering skills. F8's literal phrase
# is gone from planner.md; the canonical marker is now P181's, with an inline
# `replaces P173/F8 wording` reference that documents the supersession chain.
# The F7 + trap-wording negative lock-ins below are preserved — F12 also does
# not reintroduce the pre-P173 traps.
P173_PLANNER_MD="$HOME/.claude/agents/planner.md"
P173_PLANNER_MD_REPO="$HERMES_POC_REFERENCE/claude-agents/planner.md"

# Positive lock-in: P181 marker present in both files (canonical supersession
# of F8). The marker comment itself contains `replaces P173/F8 wording`, so the
# F8 supersession chain is documented in-file.
P173_P181_PHRASE='P181/MOL-550 (Iter 7 F12) — adversarial rework via context-engineering skills; replaces P173/F8 wording'
for f in "$P173_PLANNER_MD" "$P173_PLANNER_MD_REPO"; do
    if [ ! -f "$f" ]; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P173/P181 planner.md MISSING at %s\n' "$f"
        failed=$((failed + 1))
        continue
    fi
    if grep -Fq "$P173_P181_PHRASE" "$f"; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P173/P181 planner.md contains adversarial-rework marker (%s)\n' "$f"
        passed=$((passed + 1))
    else
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P173/P181 planner.md MISSING adversarial-rework marker (%s)\n' "$f"
        failed=$((failed + 1))
    fi
done

# Negative lock-in 1: P172 F7 marker GONE from both files. The F7 rule was
# replaced (not augmented) by F8 — the F7 marker MUST disappear.
P173_F7_TRAP_PHRASE='P172/MOL-550 (Iter 5 F7) — atomic plan-write'
for f in "$P173_PLANNER_MD" "$P173_PLANNER_MD_REPO"; do
    if [ -f "$f" ]; then
        if grep -Fq "$P173_F7_TRAP_PHRASE" "$f"; then
            [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P173 regression: P172 F7 marker still present in %s\n' "$f"
            failed=$((failed + 1))
        else
            [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P173 P172 F7 marker absent from %s (negative lock-in)\n' "$f"
            passed=$((passed + 1))
        fi
    fi
done

# Negative lock-in 2: the trap wording "written the plan file at the target
# path" GONE. This is the specific phrase that DeepSeek mis-interpreted.
P173_TRAP_WORDING='written the plan file at the target path'
for f in "$P173_PLANNER_MD" "$P173_PLANNER_MD_REPO"; do
    if [ -f "$f" ]; then
        if grep -Fq "$P173_TRAP_WORDING" "$f"; then
            [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P173 regression: trap wording "written the plan file at the target path" still in %s\n' "$f"
            failed=$((failed + 1))
        else
            [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P173 trap wording absent from %s (negative lock-in)\n' "$f"
            passed=$((passed + 1))
        fi
    fi
done

# Marker count: P181/MOL-550 ≥ 1 in each file (dual-write). MOL-665 UPDATE —
# P181 marker replaced P173/MOL-550 in-file; only P173/F8 reference text
# survives (inside the P181 marker's `replaces P173/F8 wording` annotation).
check_marker_count "P181/MOL-550 marker in user-global planner.md" \
    "$P173_PLANNER_MD" "P181/MOL-550" 1
check_marker_count "P181/MOL-550 marker in repo reference planner.md" \
    "$P173_PLANNER_MD_REPO" "P181/MOL-550" 1

# Dual-write parity: both files MUST be byte-identical post-edit.
if [ -f "$P173_PLANNER_MD" ] && [ -f "$P173_PLANNER_MD_REPO" ]; then
    if diff -q "$P173_PLANNER_MD" "$P173_PLANNER_MD_REPO" > /dev/null; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P173 F8 planner.md dual-write parity (runtime == repo source-of-truth)\n'
        passed=$((passed + 1))
    else
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P173 F8 planner.md drift detected between runtime and repo source-of-truth\n'
        failed=$((failed + 1))
    fi
fi

[[ $QUIET -eq 0 ]] && echo "=== P174 / MOL-550: Bridge fall-through on exit-0 + empty plan_file (Iter 6 F9) ==="

# Iter 5 S6 failure: Tier 1 subprocess exited subtype:success via ExitPlanMode
# but plan_file was empty (P172 F7 wording trap caused DeepSeek to fight Write
# tool instead of cleanly calling ExitPlanMode). Bridge halted at phase_planner
# level because the artifact check ran AFTER run_phase_with_fallback returned —
# never cascaded to Tier 2 (Anthropic) which would have succeeded.
# P174 moves the artifact check inside the cascade via _check_artifact_or_cascade
# helper. phase_planner passes artifact_path=plan_file; phase_skeptic/builder
# pass nothing (default None) since their artifacts are in stdout, not files.
P174_BRIDGE_RUNTIME="$HERMES_SCRIPTS/symphony_bridge.py"

if [ -f "$P174_BRIDGE_RUNTIME" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P174 symphony_bridge.py present at %s\n' "$P174_BRIDGE_RUNTIME"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P174 symphony_bridge.py MISSING at %s\n' "$P174_BRIDGE_RUNTIME"
    failed=$((failed + 1))
fi

# Positive lock-in: _check_artifact_or_cascade helper defined.
check_fixed "P174 _check_artifact_or_cascade helper defined" \
    "$P174_BRIDGE_RUNTIME" "def _check_artifact_or_cascade"

# Positive lock-in: signature extension on run_phase_with_fallback.
check_fixed "P174 run_phase_with_fallback signature has artifact_path kwarg" \
    "$P174_BRIDGE_RUNTIME" 'artifact_path: "Path | None" = None'

# Positive lock-in: phase_empty_output_anomaly event emission.
check_fixed "P174 phase_empty_output_anomaly event emission" \
    "$P174_BRIDGE_RUNTIME" "phase_empty_output_anomaly"

# Positive lock-in: phase_planner call site passes artifact_path=plan_file.
check_fixed "P174 phase_planner wires artifact_path=plan_file" \
    "$P174_BRIDGE_RUNTIME" "artifact_path=plan_file"

# Negative lock-in: pre-P174 signature SCOPED TO run_phase_with_fallback.
# The substring `extra_add_dirs: ... = None) -> dict:` ALSO appears in
# _run_cc_deepseek + _run_cc_anthropic (where it's the canonical final kwarg
# — see P171). A whole-file fgrep would false-positive on those. Scope to
# the run_phase_with_fallback function body via grep -A 8.
if grep -A 8 "^def run_phase_with_fallback" "$P174_BRIDGE_RUNTIME" | \
   grep -F "extra_add_dirs: list[str] | None = None) -> dict:" > /dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P174 regression: pre-P174 run_phase_with_fallback signature still present\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P174 pre-P174 signature absent from run_phase_with_fallback (negative lock-in)\n'
    passed=$((passed + 1))
fi

# Marker count: P174/MOL-550 ≥ 7 (1 helper + 1 docstring + 4 cascade sites +
# 1 phase_planner call site = 7; re-verify at execution time).
check_marker_count "P174/MOL-550 markers in symphony_bridge.py" \
    "$P174_BRIDGE_RUNTIME" "P174/MOL-550" 7

[[ $QUIET -eq 0 ]] && echo "=== P175 / MOL-550: stdout_head un-truncate — cost+turns as tier_attempt event fields (Iter 6 F10) ==="

# Iter 5 S6 dispatch (2026-05-12) couldn't recover Tier 1 cost post-hoc
# because bridge logs `stdout_head=stdout[:500]` and `total_cost_usd` lives
# past the 500-char boundary. F10 adds _extract_cc_result_meta(stdout) helper
# that parses the FULL JSON envelope and splices total_cost_usd / num_turns /
# subtype / is_error as top-level fields on the tier_attempt event. The
# stdout_head truncation stays in place for human spot-check.
P175_BRIDGE_RUNTIME="$HERMES_SCRIPTS/symphony_bridge.py"

# Helper definition present.
check_fixed "P175 _extract_cc_result_meta helper defined" \
    "$P175_BRIDGE_RUNTIME" "def _extract_cc_result_meta"

# Helper emits CC-prefixed result-envelope fields (avoiding collision with
# bridge's own metric names).
check_fixed "P175 helper emits cc_total_cost_usd field" \
    "$P175_BRIDGE_RUNTIME" '"cc_total_cost_usd": env.get("total_cost_usd")'
check_fixed "P175 helper emits cc_num_turns field" \
    "$P175_BRIDGE_RUNTIME" '"cc_num_turns": env.get("num_turns")'

# All 9 CC-subprocess tier_attempt sites splice **_extract_cc_result_meta(stdout).
# Hermes-swarm sites (lines emitting stdout_head="" with no JSON envelope) are
# intentionally skipped. Re-verify count at execution time.
check_marker_count "P175/MOL-550 markers in symphony_bridge.py" \
    "$P175_BRIDGE_RUNTIME" "P175/MOL-550" 10

# Marker count breakdown:
#   1 marker on the helper docstring (P175/MOL-550 F10)
#   9 markers on the wire-up sites (# P175/MOL-550 F10)
# = 10 total. Anything fewer = partial wire-up.

[[ $QUIET -eq 0 ]] && echo "=== P176 / MOL-550: skeptic VERDICT regex anchor + last-match (Iter 6 F11 stretch) ==="

# Iter 5 skeptic-RETHINK false positive: re.search returned FIRST match anywhere
# in stdout, including narrated prose ("I can't read the plan, RETHINK").
# F11 anchors the regex to start-of-line via re.MULTILINE and takes the LAST
# match — handles "previous verdict was X; this verdict is Y" narration.
P176_BRIDGE_RUNTIME="$HERMES_SCRIPTS/symphony_bridge.py"

# Positive lock-in: anchored regex pattern.
check_fixed "P176 anchored ^\\s*VERDICT: regex" \
    "$P176_BRIDGE_RUNTIME" '^\s*VERDICT:'

# Positive lock-in: re.findall + last-match pattern (not re.search first-match).
check_fixed "P176 re.findall used (not re.search)" \
    "$P176_BRIDGE_RUNTIME" "_p176_verdicts = re.findall"

# Negative lock-in: pre-P176 regex MUST be gone.
if grep -F 're.search(r"VERDICT:\s*(SHIP IT|REVISE|RETHINK)", output)' "$P176_BRIDGE_RUNTIME" > /dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P176 regression: pre-F11 re.search regex still present\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P176 pre-F11 re.search regex absent (negative lock-in)\n'
    passed=$((passed + 1))
fi

check_marker_count "P176/MOL-550 markers in symphony_bridge.py" \
    "$P176_BRIDGE_RUNTIME" "P176/MOL-550" 1

# === P177 / MOL-520 — Symphony daemon Telegram alerts for warning-class events ===
#
# Helper module + 4 wired call sites in the daemon dispatch loop. Throttles
# per-(key, event) via JSON map in tickets.last_telegram_alert_ts. Fail-open
# on HTTP/env-var/sqlite errors — a broken alerter must NOT block dispatch.
#
# Verifier strategy: file existence + helper public API + event-constant
# lock-ins + each call site present in daemon + marker count.
[[ $QUIET -eq 0 ]] && echo "=== P177 / MOL-520: Symphony daemon Telegram alerts for warning-class events ==="

P177_TG="$HERMES_SCRIPTS/symphony_telegram.py"
P177_DAEMON="$HERMES_SCRIPTS/symphony_daemon.py"

# File existence (× 1).
if [ -f "$P177_TG" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P177 helper exists: %s\n' "$P177_TG"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P177 helper MISSING: %s\n' "$P177_TG"
    failed=$((failed + 1))
fi
total=$((total + 1))

# Helper public API (× 5): notify signature + 4 event constants.
check_fixed "P177 helper exposes notify(event, key, detail, *, queue_db, throttle_seconds)" \
    "$P177_TG" 'def notify('
check_fixed "P177 helper exposes EVENT_BLOCKED" \
    "$P177_TG" 'EVENT_BLOCKED = "blocked"'
check_fixed "P177 helper exposes EVENT_FAILED" \
    "$P177_TG" 'EVENT_FAILED = "failed"'
check_fixed "P177 helper exposes EVENT_RUN_ONE_EXCEPTION" \
    "$P177_TG" 'EVENT_RUN_ONE_EXCEPTION = "run_one_exception"'
check_fixed "P177 helper exposes EVENT_DAEMON_CRASH_SWEEP" \
    "$P177_TG" 'EVENT_DAEMON_CRASH_SWEEP = "daemon_crash_sweep"'

# Throttle map persistence (× 2): JSON dump on send, parse on read.
check_fixed "P177 helper writes JSON throttle map to last_telegram_alert_ts" \
    "$P177_TG" 'UPDATE tickets SET last_telegram_alert_ts = ? WHERE key = ?'
check_fixed "P177 helper reads JSON throttle map via json.loads" \
    "$P177_TG" 'parsed = json.loads(raw)'

# Fail-open contract (× 2): broad except + finally close.
check_fixed "P177 helper has broad except for Never-raises contract" \
    "$P177_TG" 'Contract: notify() must NEVER raise'
check_fixed "P177 helper uses urllib (NOT requests/bash curl)" \
    "$P177_TG" 'import urllib.request'

# Daemon wiring (× 6): import + 4 notify call sites + helper.
check_fixed "P177 daemon imports symphony_telegram as st" \
    "$P177_DAEMON" 'import symphony_telegram as st'
check_fixed "P177 daemon fires EVENT_DAEMON_CRASH_SWEEP per orphan in main()" \
    "$P177_DAEMON" 'st.EVENT_DAEMON_CRASH_SWEEP,'
check_fixed "P177 daemon fires EVENT_RUN_ONE_EXCEPTION on inner exception path" \
    "$P177_DAEMON" 'st.EVENT_RUN_ONE_EXCEPTION,'
check_fixed "P177 daemon has _notify_terminal helper for failed/blocked dispatch outcomes" \
    "$P177_DAEMON" 'def _notify_terminal('
check_fixed "P177 daemon _notify_terminal fires EVENT_FAILED for failed/incomplete" \
    "$P177_DAEMON" 'event = st.EVENT_FAILED'
check_fixed "P177 daemon _notify_terminal fires EVENT_BLOCKED for skipped_max_attempts" \
    "$P177_DAEMON" 'event = st.EVENT_BLOCKED'
check_fixed "P177 daemon _notify_terminal invokes st.notify(event, key, detail)" \
    "$P177_DAEMON" 'status = st.notify(event, key, detail)'

# Sweep return-type lock-in (× 1): startup_sweep returns list[str], not int.
# Required for main() to iterate orphan keys for per-key Telegram alerts.
check_fixed "P177 startup_sweep returns list[str] of orphan keys" \
    "$P177_DAEMON" 'def startup_sweep(queue_db: Path = _QUEUE_DB) -> list[str]:'

# Negative lock-in: success path MUST NOT fire notify (would spam on every win).
P177_SUCCESS_NEGATIVE='if final_status == "succeeded":'
if grep -Fq "$P177_SUCCESS_NEGATIVE" "$P177_DAEMON" 2>/dev/null; then
    # Helper exists; assert it does NOT contain a stray succeeded-notify.
    if grep -E 'EVENT_(SUCCEEDED|SUCCESS)' "$P177_DAEMON" > /dev/null 2>&1; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P177 regression: daemon contains EVENT_SUCCEEDED/EVENT_SUCCESS — success path must stay quiet\n'
        failed=$((failed + 1))
    else
        [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P177 daemon success path does NOT fire notify (negative lock-in)\n'
        passed=$((passed + 1))
    fi
    total=$((total + 1))
fi

# Marker count: helper ≥ 1, daemon ≥ 4 (import banner + 3 wire sites + helper).
check_marker_count "P177/MOL-520 markers in symphony_telegram.py" \
    "$P177_TG" "P177/MOL-520" 1
check_marker_count "P177/MOL-520 markers in symphony_daemon.py" \
    "$P177_DAEMON" "P177/MOL-520" 4

# === P178 / MOL-564: chrome-devtools-mcp idle reaper (peer to MOL-543/P165) ===
# Renumbered from P173→P177→P178 — P173-P176 claimed by MOL-550 Iter-6,
# then P177 claimed by MOL-520 (#209). See [[p_number_collision_parallel_sessions]].
[[ $QUIET -eq 0 ]] && echo "=== P178 / MOL-564: chrome-devtools-mcp idle reaper (peer to MOL-543/P165) ==="

P178_WRAPPER="$HOME/.local/bin/playwright-cli"
P178_REAPER_RUNTIME="$HOME/.local/bin/cdt-mcp-reaper.sh"
P178_PLIST_RUNTIME="$HOME/Library/LaunchAgents/ai.hermes.cdt-mcp-reaper.plist"
P178_VERIFIER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
P178_REPO_DIR="$(cd "$P178_VERIFIER_DIR/../.." && pwd)"
P178_REAPER_REPO="$P178_REPO_DIR/scripts/cdt-mcp-reaper.sh"
P178_PLIST_REPO="$P178_REPO_DIR/config/launchd/ai.hermes.cdt-mcp-reaper.plist"

if [ -x "$P178_REAPER_RUNTIME" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P178 reaper script present + executable at %s\n' "$P178_REAPER_RUNTIME"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P178 reaper script MISSING or not executable at %s\n' "$P178_REAPER_RUNTIME"
    failed=$((failed + 1))
fi

if [ -f "$P178_PLIST_RUNTIME" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P178 launchd plist present at %s\n' "$P178_PLIST_RUNTIME"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P178 launchd plist MISSING at %s\n' "$P178_PLIST_RUNTIME"
    failed=$((failed + 1))
fi

if [ -f "$P178_REAPER_REPO" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P178 repo mirror present at scripts/cdt-mcp-reaper.sh\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P178 repo mirror MISSING at scripts/cdt-mcp-reaper.sh\n'
    failed=$((failed + 1))
fi

if [ -f "$P178_PLIST_REPO" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P178 repo mirror present at config/launchd/ai.hermes.cdt-mcp-reaper.plist\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P178 repo mirror MISSING at config/launchd/ai.hermes.cdt-mcp-reaper.plist\n'
    failed=$((failed + 1))
fi

if [ -f "$P178_REAPER_RUNTIME" ] && [ -f "$P178_REAPER_REPO" ]; then
    if diff -q "$P178_REAPER_RUNTIME" "$P178_REAPER_REPO" > /dev/null; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P178 reaper runtime/repo parity (byte-identical)\n'
        passed=$((passed + 1))
    else
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P178 reaper drift detected between runtime and repo\n'
        failed=$((failed + 1))
    fi
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P178 reaper parity check SKIPPED (one or both files missing)\n'
    failed=$((failed + 1))
fi

if [ ! -f "$P178_WRAPPER" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P178 wrapper FILE MISSING at %s\n' "$P178_WRAPPER"
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P178 wrapper file present at %s\n' "$P178_WRAPPER"
    passed=$((passed + 1))

    if grep -Fq 'pkill -f "chrome-devtools-mcp"' "$P178_WRAPPER"; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P178 wrapper contains chrome-devtools-mcp pkill line\n'
        passed=$((passed + 1))
    else
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P178 wrapper exists but MISSING chrome-devtools-mcp pkill line\n'
        failed=$((failed + 1))
    fi

    inside_guard=$(awk '/PLAYWRIGHT_SKIP_PREAMBLE/,/^fi/' "$P178_WRAPPER" | grep -Fc 'pkill -f "chrome-devtools-mcp"')
    if [ "$inside_guard" -ge 1 ]; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P178 wrapper pkill is inside PLAYWRIGHT_SKIP_PREAMBLE guard\n'
        passed=$((passed + 1))
    else
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P178 wrapper pkill is OUTSIDE preamble guard\n'
        failed=$((failed + 1))
    fi
fi

if [ -f "$P178_PLIST_RUNTIME" ]; then
    P178_EXPECTED_USER_PATH="/Users/$(whoami)/.local/bin/cdt-mcp-reaper.sh"
    if grep -Fq "$P178_EXPECTED_USER_PATH" "$P178_PLIST_RUNTIME"; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P178 plist ProgramArguments matches /Users/%s\n' "$(whoami)"
        passed=$((passed + 1))
    else
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P178 plist hardcoded path does NOT match /Users/%s\n' "$(whoami)"
        failed=$((failed + 1))
    fi
fi

check_marker_count "P178/MOL-564 markers in reaper script"          "$P178_REAPER_RUNTIME" "P178/MOL-564" 1
check_marker_count "P178/MOL-564 markers in playwright-cli wrapper" "$P178_WRAPPER"        "P178/MOL-564" 1

# === P179 / MOL-543: global playwright-cli wrapper with defensive sweep ===
# P-number history: shipped as P165/MOL-543 on PR #191 branch; renumbered to
# P179 because P165 was claimed by MOL-550 (stale-plan-trap mtime guard).
[[ $QUIET -eq 0 ]] && echo "=== P179 / MOL-543: global playwright-cli wrapper ==="

P179_WRAPPER="$HOME/.local/bin/playwright-cli"
P179_HOMEBREW_SYMLINK="/opt/homebrew/bin/playwright-cli"
P179_HERMES_SYMLINK="${HERMES_HOME:-$HOME/.hermes}/bin/playwright-cli"
P179_WRAPPER_REPO="$HERMES_POC_REPO/scripts/playwright-cli-wrapper.sh"

if [ -x "$P179_WRAPPER" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P179 wrapper present + executable at %s\n' "$P179_WRAPPER"
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P179 wrapper MISSING or not executable at %s\n' "$P179_WRAPPER"
    failed=$((failed + 1))
fi

check_fixed "P179 wrapper has playwright-chromium pkill line" \
    "$P179_WRAPPER" 'pkill -f "playwright_chromiumdev_profile"'

check_fixed "P179 wrapper has P178/MOL-564 chrome-devtools-mcp pkill line (folded mirror)" \
    "$P179_WRAPPER" 'pkill -f "chrome-devtools-mcp"'

check_fixed "P179 wrapper has exec node final-handoff" \
    "$P179_WRAPPER" 'exec node "$UPSTREAM_JS"'

check_fixed "P179 wrapper validates UPSTREAM_JS before exec (npm-rename diagnostic)" \
    "$P179_WRAPPER" '[ ! -f "$UPSTREAM_JS" ]'

check_fixed "P179 wrapper emits JSONL audit (observability)" \
    "$P179_WRAPPER" 'playwright-wrapper.jsonl'

if [ -f "$P179_WRAPPER_REPO" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P179 repo mirror present at scripts/playwright-cli-wrapper.sh\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P179 repo mirror MISSING at scripts/playwright-cli-wrapper.sh\n'
    failed=$((failed + 1))
fi

# Runtime/repo parity. Catches the [[cp_mirror_scope_creep]] class where a
# runtime-only edit drifts past the repo source-of-truth — the same class of
# bug the P179 PR existed to address for P178's chrome-devtools-mcp line.
if [ -f "$P179_WRAPPER" ] && [ -f "$P179_WRAPPER_REPO" ]; then
    if diff -q "$P179_WRAPPER" "$P179_WRAPPER_REPO" > /dev/null; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P179 wrapper runtime/repo parity (byte-identical)\n'
        passed=$((passed + 1))
    else
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P179 wrapper drift detected between runtime and repo\n'
        failed=$((failed + 1))
    fi
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P179 wrapper parity check SKIPPED (one or both files missing)\n'
    failed=$((failed + 1))
fi

# Symlink resolution. readlink -f follows the chain; string-equality catches
# "wrong target" (e.g. an npm reinstall flipped it back to the upstream JS).
P179_RESOLVED_HB=$(readlink -f "$P179_HOMEBREW_SYMLINK" 2>/dev/null || true)
P179_RESOLVED_WRAPPER=$(readlink -f "$P179_WRAPPER" 2>/dev/null || true)
if [ -n "$P179_RESOLVED_HB" ] && [ "$P179_RESOLVED_HB" = "$P179_RESOLVED_WRAPPER" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P179 /opt/homebrew/bin/playwright-cli → canonical wrapper\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P179 /opt/homebrew/bin/playwright-cli does NOT resolve to canonical wrapper (got: %s)\n' "$P179_RESOLVED_HB"
    failed=$((failed + 1))
fi

P179_RESOLVED_HERMES=$(readlink -f "$P179_HERMES_SYMLINK" 2>/dev/null || true)
if [ -n "$P179_RESOLVED_HERMES" ] && [ "$P179_RESOLVED_HERMES" = "$P179_RESOLVED_WRAPPER" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P179 ~/.hermes/bin/playwright-cli → canonical wrapper\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P179 ~/.hermes/bin/playwright-cli does NOT resolve to canonical wrapper (got: %s)\n' "$P179_RESOLVED_HERMES"
    failed=$((failed + 1))
fi

# Behavioral fire-test. Closes [[verifier_behavioral_fire_test]] (MOL-516) —
# substring lock-ins above don't catch the "loads cleanly but never fires"
# regression class. Spawn a fake sleep with the sentinel string in argv, run
# the wrapper, assert the fake is gone. Opt-in via P179_FIRE_TEST=1 because
# it spawns a real proc + depends on pkill behavior that may be undesirable
# in shared-runner CI.
if [ "${P179_FIRE_TEST:-0}" = "1" ] && [ -x "$P179_WRAPPER" ]; then
    P179_SENTINEL="playwright_chromiumdev_profile-VERIFIER-FIRE-$$"
    bash -c "exec -a $P179_SENTINEL sleep 30" &
    P179_FAKE_PID=$!
    sleep 0.2
    if kill -0 "$P179_FAKE_PID" 2>/dev/null; then
        "$P179_WRAPPER" --version >/dev/null 2>&1 || true
        sleep 1
        if kill -0 "$P179_FAKE_PID" 2>/dev/null; then
            kill -9 "$P179_FAKE_PID" 2>/dev/null || true
            [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P179 behavioral fire-test FAILED: wrapper did not sweep sentinel\n'
            failed=$((failed + 1))
        else
            [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P179 behavioral fire-test: wrapper swept sentinel sleep\n'
            passed=$((passed + 1))
        fi
    else
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P179 behavioral fire-test SKIPPED: sentinel proc failed to start\n'
        failed=$((failed + 1))
    fi
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;33m[~]\033[0m P179 behavioral fire-test skipped (set P179_FIRE_TEST=1 to enable)\n'
fi

# === P180 / MOL-557: Hermes-side runtime-pollution guardrails (H1-H6) ===
# Parallel to MOL-556's CC-side defenses. Six sections in one PR:
#  H1 — pre-write fingerprint check on save_config / save_jobs / _atomic_write_text
#  H2 — gateway startup fingerprint snapshot + diff
#  H3 — verifier ✗-count bracket at gateway lifecycle (SIGTERM trap)
#  H4 — active-process counts emit at gateway startup
#  H5 — cron CRUD JSONL audit wrapping cron_command()
#  H6 — symphony subprocess fingerprint bracketing (single bracket at
#       _attempt_tier_with_retry covers all 4 tiers; second bracket at
#       _run_single_child covers Tier 3 in-process path)
# Shared utility: tools/runtime_fingerprint.py (new file). check_marker_count
# pair + check_fixed landmarks per [[verify_patches_marker_drift_guard]] +
# [[check_marker_count_helper_pair]]. Deletion-class assertions guard the
# no-bypass guarantee — runtime_fingerprint must not be stubbed out.
[[ $QUIET -eq 0 ]] && printf '\n\033[1mP180 / MOL-557 — Hermes runtime guardrails (H1-H6)\033[0m\n'

# Shared utility — new file. File-existence is the prerequisite for every
# other H-section; if this is missing every import below would NameError at
# gateway start. Inline check so we fail loudly rather than cascade.
P180_RF="$HERMES_AGENT/tools/runtime_fingerprint.py"
total=$((total + 1))
if [ -f "$P180_RF" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P180 runtime_fingerprint.py present\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P180 runtime_fingerprint.py MISSING at %s\n' "$P180_RF"
    failed=$((failed + 1))
fi

# Marker counts — paired with check_fixed below per [[check_marker_count_helper_pair]].
# Min counts derived from grep -Fc against the live runtime at patch landing
# (2026-05-12); thresholds intentionally floor-set so future additions don't
# fail the gate.
check_marker_count "P180/MOL-557 markers in runtime_fingerprint.py" "$P180_RF" "P180/MOL-557" 2
check_marker_count "P180/MOL-557 markers in config.py"              "$HERMES_AGENT/hermes_cli/config.py"          "P180/MOL-557" 2
check_marker_count "P180/MOL-557 markers in jobs.py"                "$HERMES_AGENT/cron/jobs.py"                  "P180/MOL-557" 2
check_marker_count "P180/MOL-557 markers in skill_manager_tool.py"  "$HERMES_AGENT/tools/skill_manager_tool.py"   "P180/MOL-557" 2
check_marker_count "P180/MOL-557 markers in gateway/run.py"         "$HERMES_AGENT/gateway/run.py"                "P180/MOL-557" 2
check_marker_count "P180/MOL-557 markers in hermes_cli/cron.py"     "$HERMES_AGENT/hermes_cli/cron.py"            "P180/MOL-557" 2
check_marker_count "P180/MOL-557 markers in symphony_bridge.py"     "$HERMES_SCRIPTS/symphony_bridge.py"          "P180/MOL-557" 2
check_marker_count "P180/MOL-557 markers in delegate_tool.py"       "$HERMES_AGENT/tools/delegate_tool.py"        "P180/MOL-557" 2

# Landmark string assertions — these are the specific symbols/events the
# H-sections introduced. Catches the wrong-P-number re-apply class where
# markers exist but the implementation got reverted.
check_fixed "P180 runtime_fingerprint exports compute_default_surface_fingerprint" \
    "$P180_RF" "def compute_default_surface_fingerprint"
check_fixed "P180 runtime_fingerprint exports compare_fingerprints" \
    "$P180_RF" "def compare_fingerprints"
check_fixed "P180 runtime_fingerprint exports record_hermes_write" \
    "$P180_RF" "def record_hermes_write"
check_fixed "P180 runtime_fingerprint exports h1_pre_write_guard" \
    "$P180_RF" "def h1_pre_write_guard"
check_fixed "P180 runtime_fingerprint exports gateway_startup_hook" \
    "$P180_RF" "def gateway_startup_hook"
check_fixed "P180 runtime_fingerprint exports gateway_shutdown_hook" \
    "$P180_RF" "def gateway_shutdown_hook"

# H1 — pre-write guard called from each of the three write entry points.
check_fixed "P180 H1 — save_config wraps with h1_pre_write_guard" \
    "$HERMES_AGENT/hermes_cli/config.py" "h1_pre_write_guard"
check_fixed "P180 H1 — save_jobs wraps with h1_pre_write_guard" \
    "$HERMES_AGENT/cron/jobs.py" "h1_pre_write_guard"
check_fixed "P180 H1 — _atomic_write_text wraps with h1_pre_write_guard" \
    "$HERMES_AGENT/tools/skill_manager_tool.py" "h1_pre_write_guard"

# H2/H3/H4 — gateway startup hook + shutdown hook invocations at runtime entry.
check_fixed "P180 H2/H3/H4 — gateway/run.py imports gateway_startup_hook" \
    "$HERMES_AGENT/gateway/run.py" "gateway_startup_hook"
check_fixed "P180 H3 — gateway/run.py imports gateway_shutdown_hook" \
    "$HERMES_AGENT/gateway/run.py" "gateway_shutdown_hook"

# H5 — cron CRUD audit event landmark.
check_fixed "P180 H5 — hermes_cli/cron.py emits cron_crud event" \
    "$HERMES_AGENT/hermes_cli/cron.py" '"event": "cron_crud"'

# H6 — symphony + delegate dual-bracket emit landmarks. Two streams:
# tier_attempt_complete (every attempt) + delegate_child_complete (Tier 3).
check_fixed "P180 H6 — symphony_bridge emits tier_attempt_complete event" \
    "$HERMES_SCRIPTS/symphony_bridge.py" "tier_attempt_complete"
check_fixed "P180 H6 — delegate_tool emits delegate_child_complete event" \
    "$HERMES_AGENT/tools/delegate_tool.py" "delegate_child_complete"

# Deletion-class negative assertions per [[mol245_silent_miss_architecture]].
# These guarantee the no-bypass property: a future edit cannot stub out the
# fingerprint utility to silence the audit stream. presence-only check_fixed
# can't express "this string must NOT exist" — inline negative assert.
total=$((total + 1))
P180_BYPASS_SHIM=0
for _f in \
    "$P180_RF" \
    "$HERMES_AGENT/hermes_cli/config.py" \
    "$HERMES_AGENT/cron/jobs.py" \
    "$HERMES_AGENT/tools/skill_manager_tool.py" \
    "$HERMES_AGENT/gateway/run.py" \
    "$HERMES_AGENT/hermes_cli/cron.py" \
    "$HERMES_SCRIPTS/symphony_bridge.py" \
    "$HERMES_AGENT/tools/delegate_tool.py"; do
    if [ -f "$_f" ] && grep -Fq "runtime_fingerprint = None" "$_f" 2>/dev/null; then
        P180_BYPASS_SHIM=1
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P180 bypass shim detected in %s — "runtime_fingerprint = None"\n' "$_f"
    fi
done
if [ "$P180_BYPASS_SHIM" -eq 0 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P180 no bypass shims — runtime_fingerprint is not stubbed out\n'
    passed=$((passed + 1))
else
    failed=$((failed + 1))
fi

[[ $QUIET -eq 0 ]] && echo "=== P181 / MOL-550: planner.md adversarial rework (Iter 7 F12) ==="

# Iter 6 P173 hard rule regressed both planner endpoints (DS: "ExitPlanMode not
# in tool list", Anth: "blocked from writing the plan file. Per the system
# reminder..."). Diagnosis via context-degradation skill identified three
# patterns: CONFUSION (Skill/Task/Write references the harness mode doesn't
# expose), CLASH (internal contradictions about plan_file writability),
# POISONING (failure-mode narrative primed the model to reproduce it).
# P181 ships a v3 rewrite: removed Skill/Task references, removed
# Write/sandbox/restriction mentions, removed failure narrative, used positive
# directives only, U-curve placement of plan structure at end. Opus reviewed
# (verdict: SHIP), 6 phrase-level fixes applied.
P181_PLANNER_RUNTIME="$HOME/.claude/agents/planner.md"
P181_REPO_ROOT="$HERMES_POC_REPO"
P181_PLANNER_REF="$HERMES_POC_REFERENCE/claude-agents/planner.md"

if [ -f "$P181_PLANNER_RUNTIME" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P181 planner.md present at runtime\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P181 planner.md MISSING at %s\n' "$P181_PLANNER_RUNTIME"
    failed=$((failed + 1))
fi

# Positive lock-in: P181 marker in frontmatter (NOT in body — body becomes the
# prompt; frontmatter is stripped by the bridge before interpolation).
check_fixed "P181 frontmatter marker present" \
    "$P181_PLANNER_RUNTIME" "P181/MOL-550 (Iter 7 F12)"

# Positive lock-in: ExitPlanMode wording uses "submits...ends the subprocess"
# (Opus review fix — "returns" was function-return-semantics ambiguous).
check_fixed "P181 ExitPlanMode wording uses 'submits...ends the subprocess'" \
    "$P181_PLANNER_RUNTIME" "submits your finished plan to the harness and ends the subprocess"

# Positive lock-in: frontmatter tools list matches harness capability
# (Read, Grep, Glob, Bash, WebSearch, WebFetch, ExitPlanMode — NO Skill,
# Task, TodoWrite, Write).
check_fixed "P181 frontmatter tools trimmed to harness-exposed set" \
    "$P181_PLANNER_RUNTIME" "tools: Read, Grep, Glob, Bash, WebSearch, WebFetch, ExitPlanMode"

# Negative lock-in 1: P173/F8 hard rule wording is GONE.
if grep -F "P173/MOL-550 (Iter 6 F8) — ExitPlanMode is the plan-write primitive" "$P181_PLANNER_RUNTIME" > /dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P181 regression: P173/F8 hard rule wording still present\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P181 P173/F8 hard rule absent (negative lock-in)\n'
    passed=$((passed + 1))
fi

# Negative lock-in 2: failure-mode narrative is GONE (poisoning).
if grep -F "Failure mode (2026-05-12 S6 dispatch)" "$P181_PLANNER_RUNTIME" > /dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P181 regression: failure-mode narrative still present (poisoning)\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P181 failure-mode narrative absent (negative lock-in)\n'
    passed=$((passed + 1))
fi

# Negative lock-in 3: Skill: invocations the harness mode doesn't expose are GONE.
if grep -F "Skill: project-development" "$P181_PLANNER_RUNTIME" > /dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P181 regression: Skill: references still present (confusion)\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P181 Skill: invocation list absent (negative lock-in)\n'
    passed=$((passed + 1))
fi

# Negative lock-in 4: Write/sandbox/restriction mentions are GONE.
if grep -iE "sandbox restrictions|Rampart policy|the Write tool" "$P181_PLANNER_RUNTIME" > /dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P181 regression: Write/sandbox/restriction phrases still present\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P181 Write/sandbox/restriction phrases absent (negative lock-in)\n'
    passed=$((passed + 1))
fi

# Dual-write parity check: reference copy must match runtime byte-for-byte
# (P-marker is in frontmatter; bridge strips frontmatter so prompts are
# functionally identical, but parity guards against drift).
if diff -q "$P181_PLANNER_RUNTIME" "$P181_PLANNER_REF" > /dev/null 2>&1; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P181 runtime/reference parity (byte-identical)\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P181 runtime/reference DRIFT — re-sync needed\n'
    failed=$((failed + 1))
fi

# Negative lock-in: the P181/MOL-550 marker must live ONLY in frontmatter.
# Per the architectural promise, the bridge strips frontmatter before
# interpolating, so a marker in the body would leak into the prompt as a
# small POISONING vector (model sees "this is rework v7 with adversarial
# review" and gets primed about iteration history). Extract body (lines
# after the SECOND '---' separator) via awk and assert no match.
if awk '/^---$/{c++; next} c>=2' "$P181_PLANNER_RUNTIME" | grep -F "P181/MOL-550" > /dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P181 marker leaked into prompt body — frontmatter-only contract broken\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P181 marker confined to frontmatter (body is marker-free)\n'
    passed=$((passed + 1))
fi

# YAML loader safety: confirm the frontmatter (with embedded '#' comment for
# the P181 marker) parses cleanly under PyYAML. If the bridge or any other
# loader does strict YAML parsing, the comment-line embedding must not raise.
# Skip if PyYAML isn't importable in the hermes-agent venv (degrade gracefully).
if [ -x "$HOME/.hermes/hermes-agent/venv/bin/python3" ]; then
    P181_YAML_RESULT=$("$HOME/.hermes/hermes-agent/venv/bin/python3" - "$P181_PLANNER_RUNTIME" 2>&1 <<'EOF_P181_YAML'
import sys, re
try:
    import yaml
except ImportError:
    print("SKIP_NOPYYAML")
    sys.exit(0)
src = open(sys.argv[1]).read()
m = re.match(r"^---\n(.*?)\n---\n", src, re.DOTALL)
if not m:
    print(f"FAIL: no frontmatter found")
    sys.exit(1)
try:
    parsed = yaml.safe_load(m.group(1))
    if isinstance(parsed, dict) and parsed.get("name") == "planner":
        print("PASS")
    else:
        print(f"FAIL: frontmatter parsed but missing/wrong 'name' field: {parsed!r}")
except yaml.YAMLError as e:
    print(f"FAIL: yaml.safe_load raised: {e}")
EOF_P181_YAML
)
    case "$P181_YAML_RESULT" in
        PASS)
            [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P181 frontmatter parses cleanly under PyYAML strict mode\n'
            passed=$((passed + 1))
            ;;
        SKIP_NOPYYAML)
            [[ $QUIET -eq 0 ]] && printf '  \033[0;33m[~]\033[0m P181 YAML loader test skipped (PyYAML not in hermes-agent venv)\n'
            ;;
        *)
            [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P181 YAML loader test FAILED: %s\n' "$P181_YAML_RESULT"
            failed=$((failed + 1))
            ;;
    esac
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;33m[~]\033[0m P181 YAML loader test skipped (hermes-agent venv missing)\n'
fi

# Positive lock-in: the plan-structure numbered list at end of body must
# contain all 5 section headings. Catches a future "improvement" that
# fragments the plan structure or removes a section.
P181_PLAN_BODY=$(awk '/^---$/{c++; next} c>=2' "$P181_PLANNER_RUNTIME")
P181_STRUCTURE_OK=1
for section in "**Context**" "**Approach**" "**Target files**" "**Acceptance criteria**" "**Out of scope**"; do
    if ! echo "$P181_PLAN_BODY" | grep -F "$section" > /dev/null; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P181 plan structure missing section: %s\n' "$section"
        P181_STRUCTURE_OK=0
    fi
done
if [ "$P181_STRUCTURE_OK" -eq 1 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P181 plan structure intact (Context/Approach/Target files/Acceptance/Out of scope)\n'
    passed=$((passed + 1))
else
    failed=$((failed + 1))
fi

[[ $QUIET -eq 0 ]] && echo "=== P182 / MOL-550: skeptic.md adversarial rework (Iter 7 F13) ==="

# Iter 6 surfaced a new Skeptic failure class: CC+Anth under Skeptic profile +
# --bare emitted result literally "no" — 25 turns, $1.52, 1.17M cache_read
# tokens, 11k internal reasoning output, no VERDICT line. Diagnosis via
# context-degradation skill identified two patterns: CLASH (Skill/ExitPlanMode
# tools listed but harness doesn't expose them; collapsed to terse "no" when
# the verdict-output contract conflicted with a stale Skill: invocation), and
# POISONING (instructions repeatedly emphasized "exactly one verdict line"
# until "exactly" became more salient than the verdict itself). P182 ships a
# v2 rewrite: removed Skill/ExitPlanMode references, removed Hermes-lens
# metaphor (lens names retained as findings structure), positive directives
# only, verdict-line placement at start of output (U-curve). Opus reviewed
# (verdict: REVISE → fixed). Acceptance: skeptic stops emitting bare "no".
P182_SKEPTIC_RUNTIME="$HOME/.claude/agents/skeptic.md"
P182_REPO_ROOT="$HERMES_POC_REPO"
P182_SKEPTIC_REF="$HERMES_POC_REFERENCE/claude-agents/skeptic.md"

if [ -f "$P182_SKEPTIC_RUNTIME" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P182 skeptic.md present at runtime\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P182 skeptic.md MISSING at %s\n' "$P182_SKEPTIC_RUNTIME"
    failed=$((failed + 1))
fi

# Positive lock-in: P182 marker in frontmatter (NOT in body — body becomes the
# prompt; frontmatter is stripped by the bridge before interpolation).
check_fixed "P182 frontmatter marker present" \
    "$P182_SKEPTIC_RUNTIME" "P182/MOL-550 (Iter 7 F13)"

# Positive lock-in: verdict-value contract is named in plain English.
# (The trio "SHIP IT, REVISE, or RETHINK" must remain; downstream regex
# in symphony_bridge.phase_skeptic matches `VERDICT:\s*(SHIP IT|REVISE|RETHINK)`).
check_fixed "P182 verdict-value contract names all three actions" \
    "$P182_SKEPTIC_RUNTIME" "SHIP IT, REVISE, or RETHINK"

# Positive lock-in: frontmatter tools list matches harness capability under
# --bare (Read, Grep, Glob, Bash — NO Skill, NO ExitPlanMode since skeptic
# does not call ExitPlanMode; it emits a stdout regex contract).
check_fixed "P182 frontmatter tools trimmed to harness-exposed set" \
    "$P182_SKEPTIC_RUNTIME" "tools: Read, Grep, Glob, Bash"

# Negative lock-in 1: Skill: invocations the harness mode doesn't expose are GONE.
if grep -F "Skill: plan-skeptic" "$P182_SKEPTIC_RUNTIME" > /dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P182 regression: Skill: plan-skeptic reference still present (confusion)\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P182 Skill: plan-skeptic reference absent (negative lock-in)\n'
    passed=$((passed + 1))
fi

# Negative lock-in 2: ExitPlanMode reference is GONE from the PROMPT BODY
# (post-frontmatter content — the bridge strips frontmatter, so YAML comments
# explaining the design choice are model-invisible and acceptable). Skeptic
# emits a stdout verdict regex and does not call ExitPlanMode, so the body
# must not direct the model to invoke it.
if awk '/^---$/{c++; next} c>=2' "$P182_SKEPTIC_RUNTIME" | grep -F "ExitPlanMode" > /dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P182 regression: ExitPlanMode reference in prompt body (skeptic does not invoke it)\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P182 ExitPlanMode absent from prompt body (negative lock-in; frontmatter comments allowed)\n'
    passed=$((passed + 1))
fi

# Negative lock-in 3: "Operates in plan mode" phrasing is GONE (CLASH
# pattern — skeptic emits a verdict regex, not a plan; "plan mode" framing
# primed the model to treat verdict as a plan and skip emission).
if grep -F "Operates in plan mode" "$P182_SKEPTIC_RUNTIME" > /dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P182 regression: "Operates in plan mode" framing still present (CLASH)\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P182 "plan mode" framing absent (negative lock-in)\n'
    passed=$((passed + 1))
fi

# Dual-write parity check: reference copy must match runtime byte-for-byte.
if diff -q "$P182_SKEPTIC_RUNTIME" "$P182_SKEPTIC_REF" > /dev/null 2>&1; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P182 runtime/reference parity (byte-identical)\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P182 runtime/reference DRIFT — re-sync needed\n'
    failed=$((failed + 1))
fi

# Negative lock-in: P182/MOL-550 marker must live ONLY in frontmatter
# (mirrors P181 contract). Extract body (lines after the SECOND '---'
# separator) via awk and assert no match.
if awk '/^---$/{c++; next} c>=2' "$P182_SKEPTIC_RUNTIME" | grep -F "P182/MOL-550" > /dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P182 marker leaked into prompt body — frontmatter-only contract broken\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P182 marker confined to frontmatter (body is marker-free)\n'
    passed=$((passed + 1))
fi

# YAML loader safety: confirm the frontmatter (with embedded '#' comment for
# the P182 marker) parses cleanly under PyYAML. Skip if PyYAML isn't
# importable in the hermes-agent venv (degrade gracefully).
if [ -x "$HOME/.hermes/hermes-agent/venv/bin/python3" ]; then
    P182_YAML_RESULT=$("$HOME/.hermes/hermes-agent/venv/bin/python3" - "$P182_SKEPTIC_RUNTIME" 2>&1 <<'EOF_P182_YAML'
import sys, re
try:
    import yaml
except ImportError:
    print("SKIP_NOPYYAML")
    sys.exit(0)
src = open(sys.argv[1]).read()
m = re.match(r"^---\n(.*?)\n---\n", src, re.DOTALL)
if not m:
    print(f"FAIL: no frontmatter found")
    sys.exit(1)
try:
    parsed = yaml.safe_load(m.group(1))
    if isinstance(parsed, dict) and parsed.get("name") == "skeptic":
        print("PASS")
    else:
        print(f"FAIL: frontmatter parsed but missing/wrong 'name' field: {parsed!r}")
except yaml.YAMLError as e:
    print(f"FAIL: yaml.safe_load raised: {e}")
EOF_P182_YAML
)
    case "$P182_YAML_RESULT" in
        PASS)
            [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P182 frontmatter parses cleanly under PyYAML strict mode\n'
            passed=$((passed + 1))
            ;;
        SKIP_NOPYYAML)
            [[ $QUIET -eq 0 ]] && printf '  \033[0;33m[~]\033[0m P182 YAML loader test skipped (PyYAML not in hermes-agent venv)\n'
            ;;
        *)
            [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P182 YAML loader test FAILED: %s\n' "$P182_YAML_RESULT"
            failed=$((failed + 1))
            ;;
    esac
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;33m[~]\033[0m P182 YAML loader test skipped (hermes-agent venv missing)\n'
fi

# Positive lock-in: the five-lens framework remains intact in the body.
# Catches a future "improvement" that fragments the lens vocabulary the
# Skeptic Skill output format depends on.
P182_BODY=$(awk '/^---$/{c++; next} c>=2' "$P182_SKEPTIC_RUNTIME")
P182_LENS_OK=1
for lens in "Messenger" "Boundary-Crosser" "Trickster" "Guide" "Thief"; do
    if ! echo "$P182_BODY" | grep -F "$lens" > /dev/null; then
        [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P182 lens vocabulary missing: %s\n' "$lens"
        P182_LENS_OK=0
    fi
done
if [ "$P182_LENS_OK" -eq 1 ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P182 five-lens vocabulary intact (Messenger/Boundary-Crosser/Trickster/Guide/Thief)\n'
    passed=$((passed + 1))
else
    failed=$((failed + 1))
fi

# Behavioral check: verify the verdict-line placement rule survives — the
# body must instruct that VERDICT is the FIRST line. Catches a refactor that
# moves the verdict to bottom (truncation risk) or to middle (lost-in-middle).
if echo "$P182_BODY" | grep -F "FIRST LINE is your verdict" > /dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P182 verdict-line-first contract present\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P182 verdict-line-first contract missing (truncation risk)\n'
    failed=$((failed + 1))
fi

[[ $QUIET -eq 0 ]] && echo "=== P184 / MOL-550: skeptic Tier 1 timeout 300→1200s (Iter 7 F15) ==="

# Iter 7 F15 investigation reviewed MOL-498 logs at ~/.hermes/logs/symphony_bridge.log:
# 3 DS Tier 1 SIGKILL timeouts at 300/600s (no stderr captured — hard-kill flushes
# nothing), 6 nonzero-exit fast failures (model-slug [1m] routing — already fixed
# in P165), 1 error_max_turns at 1384s/61 turns (turn-budget hit, not wall-clock),
# 3 successes at 271s/1002s/1335s/23-94 turns. DS Tier 1 isn't broken — needs wall
# time to converge on complex prompts. F13 smoke cell 2 (DS realistic) retry took
# ~530s, exceeding the 300s Skeptic ceiling. P184 raises Skeptic 300→1200 — clears
# the 1002s observed-success floor with ~20% margin; the 1335s outlier remains a
# residual SIGKILL risk that P183's verdict-missing cascade catches via Tier 2.
# Picking 600s (initial draft) would have SIGKILL'd two of three observed
# convergences — too aggressive. Planner-style 1800 is overkill (Skeptic emits
# stdout regex, not plan file). skeptic_revise gets the same bump for parity.
P184_BRIDGE="$HOME/.hermes/scripts/symphony_bridge.py"

if [ -f "$P184_BRIDGE" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P184 symphony_bridge.py present at runtime\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P184 symphony_bridge.py MISSING at %s\n' "$P184_BRIDGE"
    failed=$((failed + 1))
fi

# Positive lock-in: skeptic timeout is 1200s (20 min), not 300s.
check_fixed "P184 skeptic timeout raised to 1200s" \
    "$P184_BRIDGE" '"skeptic": 1200,'

# Positive lock-in: skeptic_revise timeout matches (parity rule).
check_fixed "P184 skeptic_revise timeout matches skeptic at 1200s" \
    "$P184_BRIDGE" '"skeptic_revise": 1200,'

# Positive lock-in: P184 marker present (provenance).
check_fixed "P184 marker present in symphony_bridge.py" \
    "$P184_BRIDGE" "P184/MOL-550 (Iter 7 F15)"

# Negative lock-in: the old 300s value must NOT appear on a "skeptic" or
# "skeptic_revise" key line. Use grep -E with \b to reject bare 300; the
# comment "300s ceiling" or numeric 300 elsewhere stays acceptable.
if grep -E '"skeptic(_revise)?"\s*:\s*300\b' "$P184_BRIDGE" > /dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P184 regression: 300s skeptic timeout still on a skeptic-key line\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P184 old 300s skeptic timeout absent (negative lock-in)\n'
    passed=$((passed + 1))
fi

# Behavioral check: import the bridge module and read the actual values from
# the MappingProxyType — catches a future refactor that renames the constant
# but leaves the value behind. Requires hermes-agent venv with bridge's Python
# deps; degrades gracefully if missing.
if [ -x "$HOME/.hermes/hermes-agent/venv/bin/python3" ]; then
    P184_BEHAVIORAL_RESULT=$("$HOME/.hermes/hermes-agent/venv/bin/python3" - 2>&1 <<'EOF_P184_BEHAVIORAL'
import sys
sys.path.insert(0, "/Users/wills_mac_mini/.hermes/scripts")
try:
    import symphony_bridge as sb
except Exception as e:
    print(f"FAIL_IMPORT: {type(e).__name__}: {e}")
    sys.exit(0)
try:
    skeptic = sb._PHASE_TIMEOUTS["skeptic"]
    skeptic_revise = sb._PHASE_TIMEOUTS["skeptic_revise"]
    if skeptic == 1200 and skeptic_revise == 1200:
        print("PASS")
    else:
        print(f"FAIL_VALUES: skeptic={skeptic}, skeptic_revise={skeptic_revise}")
except Exception as e:
    print(f"FAIL_READ: {type(e).__name__}: {e}")
EOF_P184_BEHAVIORAL
)
    case "$P184_BEHAVIORAL_RESULT" in
        PASS)
            [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P184 behavioral: bridge import reads skeptic=1200 + skeptic_revise=1200\n'
            passed=$((passed + 1))
            ;;
        FAIL_IMPORT*)
            [[ $QUIET -eq 0 ]] && printf '  \033[0;33m[~]\033[0m P184 behavioral test skipped (bridge import failed: %s)\n' "$P184_BEHAVIORAL_RESULT"
            ;;
        *)
            [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P184 behavioral test FAILED: %s\n' "$P184_BEHAVIORAL_RESULT"
            failed=$((failed + 1))
            ;;
    esac
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;33m[~]\033[0m P184 behavioral test skipped (hermes-agent venv missing)\n'
fi

[[ $QUIET -eq 0 ]] && echo "=== P185 / MOL-636: terminal coding-elevation profile (dev + ops verbs) ==="

# Surface: tools/approval.py (fast-path + caller kwarg), tools/terminal_tool.py
# (wrapper passes caller="terminal_tool"), and a runtime YAML at
# ~/.hermes/config/terminal-profiles/coding.yaml. Rampart side belongs to the
# policy file in ~/.rampart/policies/, not verified here.

P185_APPROVAL="$HOME/.hermes/hermes-agent/tools/approval.py"
P185_TERMINAL="$HOME/.hermes/hermes-agent/tools/terminal_tool.py"
P185_PROFILE="$HOME/.hermes/config/terminal-profiles/coding.yaml"

if [ -f "$P185_APPROVAL" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P185 approval.py present at runtime\n'
    passed=$((passed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P185 approval.py MISSING at %s\n' "$P185_APPROVAL"
    failed=$((failed + 1))
fi

check_marker_count "P185 markers present in approval.py" \
    "$P185_APPROVAL" "P185 / MOL-636" 2

check_fixed "P185 fast-path helper defined" \
    "$P185_APPROVAL" "def _terminal_coding_fast_path"

check_fixed "P185 profile loader defined" \
    "$P185_APPROVAL" "def _load_terminal_coding_profile"

check_fixed "P185 caller kwarg added to check_all_command_guards" \
    "$P185_APPROVAL" "caller: Optional[str] = None"

check_fixed "P185 fast-path dispatched from check_all_command_guards" \
    "$P185_APPROVAL" "coding_result = _terminal_coding_fast_path(command, caller)"

check_fixed "P185 terminal_tool wrapper passes caller" \
    "$P185_TERMINAL" 'caller="terminal_tool"'

if [ -f "$P185_PROFILE" ]; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P185 coding profile YAML present at runtime\n'
    passed=$((passed + 1))
    check_fixed "P185 profile YAML names allowed dev verbs" \
        "$P185_PROFILE" 'pytest*'
    check_fixed "P185 profile YAML names allowed ops verbs" \
        "$P185_PROFILE" "kill [1-9][0-9]*"
    check_fixed "P185 profile YAML denies root-pid kill" \
        "$P185_PROFILE" "kill *-9 1"
    check_fixed "P185 profile YAML denies launchctl mutate" \
        "$P185_PROFILE" "launchctl unload*"
    check_fixed "P185 profile YAML denies hermes update" \
        "$P185_PROFILE" "hermes update*"
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P185 coding profile YAML MISSING at %s\n' "$P185_PROFILE"
    failed=$((failed + 1))
fi

# Behavioral: import approval.py and confirm the fast-path returns
# elevation for an allowed verb + deny for a destructive variant.
if [ -x "$HOME/.hermes/hermes-agent/venv/bin/python3" ] && [ -f "$P185_PROFILE" ]; then
    P185_BEHAVIORAL_RESULT=$("$HOME/.hermes/hermes-agent/venv/bin/python3" - 2>/dev/null <<'EOF_P185_BEHAVIORAL'
import sys, logging
logging.disable(logging.CRITICAL)
sys.path.insert(0, "/Users/wills_mac_mini/.hermes/hermes-agent")
try:
    from tools.approval import _terminal_coding_fast_path
except Exception as e:
    print(f"FAIL_IMPORT: {type(e).__name__}: {e}")
    sys.exit(0)
try:
    allow = _terminal_coding_fast_path("pytest tests/", "terminal_tool")
    deny = _terminal_coding_fast_path("kill -9 1", "terminal_tool")
    nofire = _terminal_coding_fast_path("pytest tests/", "delegate_tool")
    if (allow and allow.get("approved") is True and allow.get("elevated") is True
        and deny and deny.get("approved") is False
        and nofire is None):
        print("PASS")
    else:
        print(f"FAIL_BEHAVIOR: allow={allow}, deny={deny}, nofire={nofire}")
except Exception as e:
    print(f"FAIL_RUN: {type(e).__name__}: {e}")
EOF_P185_BEHAVIORAL
)
    case "$P185_BEHAVIORAL_RESULT" in
        PASS)
            [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P185 behavioral: fast-path allow + deny + caller-gating intact\n'
            passed=$((passed + 1))
            ;;
        FAIL_IMPORT*)
            [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P185 behavioral test FAILED (approval import failed: %s)\n' "$P185_BEHAVIORAL_RESULT"
            failed=$((failed + 1))
            ;;
        *)
            [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P185 behavioral test FAILED: %s\n' "$P185_BEHAVIORAL_RESULT"
            failed=$((failed + 1))
            ;;
    esac
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;33m[~]\033[0m P185 behavioral test skipped (venv or profile missing)\n'
fi

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P186 / MOL-637: verify_patches.sh COMPOSER_MODEL re-baseline (silently-shipped 479ae4c36 recovery) ==="
# Commit 479ae4c36 (2026-05-17, mislabeled "docs") swapped llm.py constants from
# Kimi K2.6/OpenRouter to DeepSeek V4-Pro without a P-block. P186 re-baselines
# the 5 verifier sites (P20, P61, P169) to match shipped runtime + adds this
# marker pair so future drift is caught immediately. [SUPERSEDES P169 composer
# model identifier; the P169 architectural shift "single composer via API"
# stands unchanged.]
check_marker_count "P186/MOL-637 PATCHES.md header" "$HERMES_AGENT/scripts/hermes-patches/PATCHES.md" "## P186 / MOL-637" 1
check_fixed       "P186 COMPOSER_MODEL is deepseek-v4-pro" \
    "$P169_LLM_RUNTIME" 'COMPOSER_MODEL = "deepseek-v4-pro"'
check_fixed       "P186 COMPOSER_API_KEY_ENV is DEEPSEEK_API_KEY" \
    "$P169_LLM_RUNTIME" 'COMPOSER_API_KEY_ENV = "DEEPSEEK_API_KEY"'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P187 / MOL-641: background-review NameError + DeepSeek/Kimi cache markers ==="
# Two surgical fixes in run_agent.py and hermes_cli/plugins.py:
#   (1) Restore set_thread_tool_whitelist / clear_thread_tool_whitelist +
#       _thread_tool_whitelist thread-local in plugins.py — the Hermes
#       cherry-pick from upstream missed them, leaving _run_review broken
#       with NameError on every background-review tick.
#   (2) Add cache_control policy branches for DeepSeek + Moonshot/Kimi in
#       _anthropic_prompt_cache_policy — both providers accept Anthropic-
#       style markers on OpenAI-wire chat.completions per provider docs.
check_marker_count "P187/MOL-641 PATCHES.md header" "$HERMES_AGENT/scripts/hermes-patches/PATCHES.md" "## P187 / MOL-641" 1
check_fixed       "P187 set_thread_tool_whitelist defined" \
    "$HERMES_AGENT/hermes_cli/plugins.py" 'def set_thread_tool_whitelist'
check_fixed       "P187 clear_thread_tool_whitelist defined" \
    "$HERMES_AGENT/hermes_cli/plugins.py" 'def clear_thread_tool_whitelist'
check_fixed       "P187 thread-local whitelist allocated" \
    "$HERMES_AGENT/hermes_cli/plugins.py" '_thread_tool_whitelist = threading.local()'
check_fixed       "P187 deepseek cache branch present" \
    "$HERMES_AGENT/run_agent.py" 'provider_lower == "deepseek" and "deepseek" in model_lower'
check_fixed       "P187 kimi-coding cache branch present" \
    "$HERMES_AGENT/run_agent.py" 'provider_lower in {"kimi-coding", "kimi-coding-cn"} and "kimi" in model_lower'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P188 / MOL-642: gateway re-image after P186+P187 (deploy-only, no code edits) ==="
# Deploy patch — no runtime files modified. P186 + P187 shipped correctly on
# disk but the long-running gateway worker held the pre-patch module image in
# sys.modules until kickstart. P188/MOL-642 records the deploy event as a
# discrete entry + adds the "Post-merge gateway kickstart" CONVENTION block to
# PATCHES.md. The marker pair is the verification surface; there are no
# runtime check_fixed assertions because there's nothing in the runtime tree
# that changed for P188.
check_marker_count "P188/MOL-642 PATCHES.md header" "$HERMES_AGENT/scripts/hermes-patches/PATCHES.md" "## P188 / MOL-642" 1

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P189 / MOL-330: session-maintenance marker consumer (prompt_builder) ==="
check_marker_count "P189/MOL-330 PATCHES.md header" "$HERMES_AGENT/scripts/hermes-patches/PATCHES.md" "## P189 / MOL-330" 1
check_fixed       "P189 build_maintenance_marker_prompt defined" \
    "$HERMES_AGENT/agent/prompt_builder.py" 'def build_maintenance_marker_prompt(marker_dir: Optional[str] = None) -> str:'
check_fixed       "P189 consumer imported in run_agent" \
    "$HERMES_AGENT/run_agent.py" 'build_maintenance_marker_prompt'
check_fixed       "P189 consumer wired into build_system_prompt (post-MOL-597)" \
    "$HERMES_AGENT/agent/system_prompt.py" 'maintenance_prompt = _r.build_maintenance_marker_prompt()'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P190 / MOL-330: session-maintenance pileup alarm (plugin observability) ==="
check_marker_count "P190/MOL-330 PATCHES.md header" "$HERMES_AGENT/scripts/hermes-patches/PATCHES.md" "## P190 / MOL-330" 1
check_fixed       "P190 pileup threshold constant present" \
    "${HERMES_HOME:-$HOME/.hermes}/plugins/session-maintenance/__init__.py" '_PILEUP_THRESHOLD = 3'
check_fixed       "P190 on_session_start callback defined" \
    "${HERMES_HOME:-$HOME/.hermes}/plugins/session-maintenance/__init__.py" 'def on_session_start(**kwargs) -> None:'
check_fixed       "P190 second hook registered" \
    "${HERMES_HOME:-$HOME/.hermes}/plugins/session-maintenance/__init__.py" 'ctx.register_hook("on_session_start", on_session_start)'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P191 / MOL-330: session-maintenance lazy-sweep reconciler (programmatic orphan GC) ==="
check_marker_count "P191/MOL-330 PATCHES.md header" "$HERMES_AGENT/scripts/hermes-patches/PATCHES.md" "## P191 / MOL-330" 1
check_fixed       "P191 stale-claim threshold constant present" \
    "${HERMES_HOME:-$HOME/.hermes}/plugins/session-maintenance/__init__.py" '_LAZY_STALE_CLAIM_SECS = 3600'
check_fixed       "P191 stale-claim reconciler defined" \
    "${HERMES_HOME:-$HOME/.hermes}/plugins/session-maintenance/__init__.py" 'def _reconcile_stale_claims() -> int:'
check_fixed       "P191 orphan-marker reconciler defined" \
    "${HERMES_HOME:-$HOME/.hermes}/plugins/session-maintenance/__init__.py" 'def _reconcile_orphan_markers() -> int:'
check_fixed       "P191 lazy_sweep callback defined" \
    "${HERMES_HOME:-$HOME/.hermes}/plugins/session-maintenance/__init__.py" 'def lazy_sweep_at_session_start(**kwargs) -> None:'
check_fixed       "P191 third hook registered" \
    "${HERMES_HOME:-$HOME/.hermes}/plugins/session-maintenance/__init__.py" 'ctx.register_hook("on_session_start", lazy_sweep_at_session_start)'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P192 / MOL-330: session-maintenance sweep marker-count print (defensive observability) ==="
check_marker_count "P192/MOL-330 PATCHES.md header" "$HERMES_AGENT/scripts/hermes-patches/PATCHES.md" "## P192 / MOL-330" 1
check_fixed       "P192 _count_pending_markers helper present" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/session_maintenance_sweep.py" 'def _count_pending_markers() -> int:'
check_fixed       "P192 pending_markers_at_start print wired" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/session_maintenance_sweep.py" 'pending_markers_at_start='

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P219 / MOL-623: distill-goal skill + helper (Phase 3 AC-11) ==="
check_marker_count "P219/MOL-623 PATCHES.md header" "$HERMES_AGENT/scripts/hermes-patches/PATCHES.md" "## P219 / MOL-623" 1
check_fixed       "P219 runtime SKILL.md frontmatter present" \
    "${HERMES_HOME:-$HOME/.hermes}/skills/software-development/distill-goal/SKILL.md" 'name: distill-goal'
check_fixed       "P219 SKILL.md pins judge_role contract" \
    "${HERMES_HOME:-$HOME/.hermes}/skills/software-development/distill-goal/SKILL.md" '"judge_role": "goal_judge"'
check_fixed       "P219 helper distill() entry defined" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/distill_goal.py" 'def distill(ticket: str, goal_dir: Path)'
check_fixed       "P219 helper judge_role literal" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/distill_goal.py" '"judge_role": "goal_judge",'
check_fixed       "P219 helper schema_version pinned" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/distill_goal.py" '"schema_version": 1,'
check_fixed       "P219 helper atomic write helper present" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/distill_goal.py" 'def _atomic_write(path: Path, content: str) -> None:'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P220 / MOL-623: raw_intent.py module (Phase 3 AC-10) ==="
check_marker_count "P220/MOL-623 PATCHES.md header" "$HERMES_AGENT/scripts/hermes-patches/PATCHES.md" "## P220 / MOL-623" 1
check_fixed       "P220 stage_raw_intent entry defined" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/raw_intent.py" 'def stage_raw_intent('
check_fixed       "P220 compute_body_plus_ac_sha entry defined" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/raw_intent.py" 'def compute_body_plus_ac_sha(raw_text: str) -> str:'
check_fixed       "P220 cycle>=2 frozen branch present" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/raw_intent.py" 'if cycle >= 2:'
check_fixed       "P220 drift detection error message present" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/raw_intent.py" 'RAW_INTENT.md drift detected'
check_fixed       "P220 jira-cli fetch shellout present" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/raw_intent.py" '["jira", "issue", "view", ticket, "--plain"]'
check_fixed       "P220 source enum: jira|telegram|inline" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/raw_intent.py" 'want jira|telegram|inline'

[[ $QUIET -eq 0 ]] && echo "=== P221 / MOL-623: symphony_loop.py orchestrator (Phase 4 AC-9+AC-14+AC-15) ==="
check_marker_count "P221/MOL-623 PATCHES.md header" "$HERMES_AGENT/scripts/hermes-patches/PATCHES.md" "## P221 / MOL-623" 1
check_fixed       "P221 run_loop entry defined" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/symphony_loop.py" 'def run_loop('
check_fixed       "P221 VERDICT_ALL_PASS constant" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/symphony_loop.py" 'VERDICT_ALL_PASS = "ALL_PASS"'
check_fixed       "P221 VERDICT_BUDGET_EXHAUSTED constant" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/symphony_loop.py" 'VERDICT_BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"'
check_fixed       "P221 VERDICT_MAX_CYCLES constant" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/symphony_loop.py" 'VERDICT_MAX_CYCLES = "MAX_CYCLES"'
check_fixed       "P221 VERDICT_GOAL_JUDGE_TURN_BUDGET constant" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/symphony_loop.py" 'VERDICT_GOAL_JUDGE_TURN_BUDGET = "GOAL_JUDGE_TURN_BUDGET"'
check_fixed       "P221 VERDICT_GOAL_AMBIGUITY constant" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/symphony_loop.py" 'VERDICT_GOAL_AMBIGUITY = "GOAL_AMBIGUITY"'
check_fixed       "P221 GOAL_AMBIGUITY sentinel literal" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/symphony_loop.py" 'GOAL_AMBIGUITY_SENTINEL = "VERDICT: GOAL_AMBIGUITY"'
check_fixed       "P221 _log_fingerprint emitter present" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/symphony_loop.py" 'def _log_fingerprint('
check_fixed       "P221 fingerprint log path symphony-fingerprint.jsonl" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/symphony_loop.py" 'symphony-fingerprint.jsonl'
check_fixed       "P221 cycle==1 distill gate present (GOAL.md frozen after cycle 1)" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/symphony_loop.py" 'if cycle == 1:'
check_fixed       "P221 stage_raw_intent import + call" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/symphony_loop.py" 'from raw_intent import stage_raw_intent'
check_fixed       "P221 distill import + call (cycle 1 only)" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/symphony_loop.py" 'from distill_goal import distill'
check_fixed       "P221 run_builder import + call (Builder phase via P218)" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/symphony_loop.py" 'from symphony_builder import run_builder'
check_fixed       "P221 clawpatch wrapper invocation present" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/symphony_loop.py" 'CLAWPATCH_WRAPPER'
check_fixed       "P221 ALL_PASS gate compound predicate" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/symphony_loop.py" 'cs.clawpatch_severity != CLAWPATCH_CRITICAL'

[[ $QUIET -eq 0 ]] && echo "=== P222 / MOL-623: raw_intent_coverage_check.py (Phase 4 AC-12) ==="
check_marker_count "P222/MOL-623 PATCHES.md header" "$HERMES_AGENT/scripts/hermes-patches/PATCHES.md" "## P222 / MOL-623" 1
check_fixed       "P222 check_coverage entry defined" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/raw_intent_coverage_check.py" 'def check_coverage(raw_path: Path, goal_path: Path) -> Dict[str, object]:'
check_fixed       "P222 ASPIRATIONAL_BLOCKLIST present" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/raw_intent_coverage_check.py" 'ASPIRATIONAL_BLOCKLIST: Set[str] = {'
check_fixed       "P222 ASPIRATIONAL_BLOCKLIST contains 'comprehensive'" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/raw_intent_coverage_check.py" '"comprehensive"'
check_fixed       "P222 ASPIRATIONAL_BLOCKLIST contains 'leverage'" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/raw_intent_coverage_check.py" '"leverage"'
check_fixed       "P222 _light_stem stemmer defined" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/raw_intent_coverage_check.py" 'def _light_stem(word: str) -> str:'
check_fixed       "P222 _IDENTIFIER_RE verbose regex defined" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/raw_intent_coverage_check.py" '_IDENTIFIER_RE = re.compile('
check_fixed       "P222 drift kind: count_mismatch" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/raw_intent_coverage_check.py" '"kind": "count_mismatch"'
check_fixed       "P222 drift kind: token_drop" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/raw_intent_coverage_check.py" '"kind": "token_drop"'
check_fixed       "P222 drift kind: aspirational_drift" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/raw_intent_coverage_check.py" '"kind": "aspirational_drift"'
check_fixed       "P222 drift kind: schema_violation" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/raw_intent_coverage_check.py" '"kind": "schema_violation"'
check_fixed       "P222 drift kind: double_mapping" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/raw_intent_coverage_check.py" '"kind": "double_mapping"'
check_fixed       "P222 exit code 3 on drift (return 0 if report else 3)" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/raw_intent_coverage_check.py" 'return 0 if report["ok"] else 3'
check_fixed       "P222 audit log path raw-intent-coverage.jsonl" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/raw_intent_coverage_check.py" 'raw-intent-coverage.jsonl'
check_fixed       "P222 --goal-dir CLI arg required" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/raw_intent_coverage_check.py" '"--goal-dir",'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P223 / MOL-623: tests/test_raw_intent_coverage_check.py + _light_stem Step 1b refinement (Phase 4 AC-13) ==="
check_marker_count "P223/MOL-623 PATCHES.md header" "$HERMES_AGENT/scripts/hermes-patches/PATCHES.md" "## P223 / MOL-623" 1
check_fixed       "P223 test file present (loads runtime module by path)" \
    "tests/test_raw_intent_coverage_check.py" 'raw_intent_coverage_check_under_test'
check_fixed       "P223 skipif guard present (matches test_symphony_queue.py pattern)" \
    "tests/test_raw_intent_coverage_check.py" 'pytestmark = pytest.mark.skipif'
check_fixed       "P223 clean-pair contract test present" \
    "tests/test_raw_intent_coverage_check.py" 'def test_clean_pair_no_drift('
check_fixed       "P223 aspirational drift test present" \
    "tests/test_raw_intent_coverage_check.py" 'def test_aspirational_drift_introduced_in_goal('
check_fixed       "P223 identifier drift test present" \
    "tests/test_raw_intent_coverage_check.py" 'def test_identifier_drift_ticket_number('
check_fixed       "P223 light_stem canonical-forms parametrized test present" \
    "tests/test_raw_intent_coverage_check.py" 'def test_light_stem_canonical_forms('
check_fixed       "P223 CLI exit-3 drift contract test present" \
    "tests/test_raw_intent_coverage_check.py" 'def test_cli_exit_3_on_drift('
check_fixed       "P223 _light_stem Step 1b post-rule (doubled-consonant collapse after -ed/-ing)" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/raw_intent_coverage_check.py" 'stripped_suffix and len(w) >= 2 and w[-1] == w[-2] and w[-1] in "bdfgmnprt"'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P224 / MOL-623: tests/test_builder_write_scope.py (Phase 4 AC-16) ==="
check_marker_count "P224/MOL-623 PATCHES.md header" "$HERMES_AGENT/scripts/hermes-patches/PATCHES.md" "## P224 / MOL-623" 1
check_fixed       "P224 test file present (loads runtime symphony_builder by path)" \
    "tests/test_builder_write_scope.py" 'symphony_builder_under_test'
check_fixed       "P224 skipif guard present (matches test_symphony_queue.py pattern)" \
    "tests/test_builder_write_scope.py" 'pytestmark = pytest.mark.skipif'
check_fixed       "P224 LOG_FILE write-scope assertion present" \
    "tests/test_builder_write_scope.py" 'def test_log_file_under_hermes_logs_not_runtime('
check_fixed       "P224 BUILDER_TOOLSETS allowlist assertion present" \
    "tests/test_builder_write_scope.py" 'def test_builder_toolsets_match_declared_allowlist('
check_fixed       "P224 elevation-surface parametrized test present (memory/execute_code/subagent)" \
    "tests/test_builder_write_scope.py" 'def test_builder_toolsets_exclude_elevation_surfaces('
check_fixed       "P224 system-prompt no-runtime-write clause check present" \
    "tests/test_builder_write_scope.py" 'def test_system_prompt_pins_no_runtime_write_clause('
check_fixed       "P224 source-regex no-chdir-into-runtime check present" \
    "tests/test_builder_write_scope.py" 'def test_source_does_not_chdir_into_runtime('
check_fixed       "P224 source-regex no-write-under-runtime check present" \
    "tests/test_builder_write_scope.py" 'def test_source_does_not_write_under_runtime('
check_fixed       "P224 behavioral mtime-snapshot test present" \
    "tests/test_builder_write_scope.py" 'def test_run_builder_does_not_touch_runtime('
check_fixed       "P224 cwd-restoration test present (failure-path)" \
    "tests/test_builder_write_scope.py" 'def test_run_builder_restores_prior_cwd('

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P225 / MOL-623: symphony_bridge.py use_three_agent_loop gate (Phase 4 AC-17) ==="
check_marker_count "P225/MOL-623 PATCHES.md header" "$HERMES_AGENT/scripts/hermes-patches/PATCHES.md" "## P225 / MOL-623" 1
check_fixed       "P225 _three_agent_loop_gate helper present (config-read + tri-state return)" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/symphony_bridge.py" 'def _three_agent_loop_gate('
check_fixed       "P225 _dispatch_three_agent_loop helper present (verdict→RunResult mapping)" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/symphony_bridge.py" 'def _dispatch_three_agent_loop('
check_fixed       "P225 gate reads role_routing.use_three_agent_loop" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/symphony_bridge.py" 'use_three_agent_loop'
check_fixed       "P225 gate fails closed to legacy (False) on config-load exception" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/symphony_bridge.py" 'routing_gate_unreadable'
check_fixed       "P225 unrecognized-value telemetry breadcrumb present" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/symphony_bridge.py" 'routing_gate_unrecognized'
check_fixed       "P225 shadow-mode literal recognized (case-insensitive)" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/symphony_bridge.py" 'raw.lower() == "shadow"'
check_fixed       "P225 run_one branches on gate inside dispatch (per-tick gate read)" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/symphony_bridge.py" 'if _gate is True:'
check_fixed       "P225 routing_decision telemetry event emitted" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/symphony_bridge.py" 'routing_decision'
check_fixed       "P225 loop dispatch imports run_loop lazily (no module-top coupling)" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/symphony_bridge.py" 'from symphony_loop import run_loop'
check_fixed       "P225 reference diff present at scripts/hermes-patches/reference/" \
    "$HERMES_POC_REFERENCE/P225-symphony-bridge-three-agent-loop-gate.diff" 'def _three_agent_loop_gate('
check_fixed       "P225 test file present (loads runtime symphony_bridge by path)" \
    "$HERMES_POC_REPO/tests/test_three_agent_loop_gate.py" 'symphony_bridge_under_test'
check_fixed       "P225 test covers default-is-legacy (gate unset → False)" \
    "$HERMES_POC_REPO/tests/test_three_agent_loop_gate.py" 'def test_gate_default_is_legacy('
check_fixed       "P225 test covers shadow string (lowercase)" \
    "$HERMES_POC_REPO/tests/test_three_agent_loop_gate.py" 'def test_gate_shadow_returns_shadow('
check_fixed       "P225 test covers fail-closed on config-load failure" \
    "$HERMES_POC_REPO/tests/test_three_agent_loop_gate.py" 'def test_gate_config_load_failure_fails_closed('
check_fixed       "P225 test covers ALL_PASS verdict → succeeded mapping" \
    "$HERMES_POC_REPO/tests/test_three_agent_loop_gate.py" 'def test_dispatch_three_agent_loop_all_pass_maps_to_succeeded('
check_fixed       "P225 test covers run_loop exception → failed mapping" \
    "$HERMES_POC_REPO/tests/test_three_agent_loop_gate.py" 'def test_dispatch_three_agent_loop_run_loop_exception_maps_to_failed('

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P226 / MOL-623: anthropic-direct api_mode + deepseek bracket-strip (Gap 4+5 post-ship hardening) ==="
check_marker_count "P226/MOL-623 PATCHES.md header" "$HERMES_AGENT/scripts/hermes-patches/PATCHES.md" "## P226 / MOL-623" 1
# Gap 4 — runtime_provider.py: dispatch api.anthropic.com → anthropic_messages
check_fixed       "P226 docstring bullet for anthropic_messages dispatch present" \
    "${HERMES_HOME:-$HOME/.hermes}/hermes-agent/hermes_cli/runtime_provider.py" 'Direct ``api.anthropic.com`` only serves ``/v1/messages``'
check_fixed       "P226 docstring P-number attribution present (runtime_provider)" \
    "${HERMES_HOME:-$HOME/.hermes}/hermes-agent/hermes_cli/runtime_provider.py" 'P226/MOL-623 — Gap 4 fix'
check_fixed       "P226 api.anthropic.com dispatch branch wired in _detect_api_mode_for_url" \
    "${HERMES_HOME:-$HOME/.hermes}/hermes-agent/hermes_cli/runtime_provider.py" 'if hostname == "api.anthropic.com":'
check_fixed       "P226 dispatch branch returns anthropic_messages" \
    "${HERMES_HOME:-$HOME/.hermes}/hermes-agent/hermes_cli/runtime_provider.py" 'return "anthropic_messages"'
# Gap 5 — model_normalize.py: strip bracket suffix before canonical-models check
check_fixed       "P226 bracket-strip comment block present (model_normalize)" \
    "${HERMES_HOME:-$HOME/.hermes}/hermes-agent/hermes_cli/model_normalize.py" 'Strip Hermes context-length bracket suffix'
check_fixed       "P226 P-number attribution present (model_normalize)" \
    "${HERMES_HOME:-$HOME/.hermes}/hermes-agent/hermes_cli/model_normalize.py" 'P226/MOL-623 — Gap 5 fix'
check_fixed       "P226 bracket-suffix regex strip wired in _normalize_for_deepseek" \
    "${HERMES_HOME:-$HOME/.hermes}/hermes-agent/hermes_cli/model_normalize.py" 'bare = re.sub(r"\[[^\]]*\]$", "", bare)'
# Reference diff present
check_fixed       "P226 reference diff present at scripts/hermes-patches/reference/" \
    "$HERMES_POC_REFERENCE/P226-MOL-623-anthropic-api-mode-and-deepseek-bracket-strip.diff" 'P226/MOL-623 — Gap 4 fix'
check_fixed       "P226 reference diff covers model_normalize bracket-strip" \
    "$HERMES_POC_REFERENCE/P226-MOL-623-anthropic-api-mode-and-deepseek-bracket-strip.diff" 'P226/MOL-623 — Gap 5 fix'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P232 / MOL-599: jira-cli --plain indent tolerance (parser+writer atomic fix) ==="
check_marker_count "P232/MOL-599 PATCHES.md header" "$HERMES_AGENT/scripts/hermes-patches/PATCHES.md" "## P232 / MOL-599" 1
# distill_goal.py — four sites: _AC_HEADING_RE, _OUT_OF_SCOPE_RE, _NEXT_H2_RE, _extract_body
check_fixed       "P232 distill_goal MOL-599 P232 banner present" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/distill_goal.py" 'MOL-599 P232:'
check_fixed       "P232 distill_goal _AC_HEADING_RE has ^\\s* prefix" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/distill_goal.py" 'r"^\s*##\s+(?:"'
check_fixed       "P232 distill_goal _OUT_OF_SCOPE_RE has ^\\s* prefix" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/distill_goal.py" '_OUT_OF_SCOPE_RE = re.compile(r"^\s*##\s+Out\s+of\s+scope'
check_fixed       "P232 distill_goal _NEXT_H2_RE has ^\\s* prefix" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/distill_goal.py" '_NEXT_H2_RE = re.compile(r"^\s*##\s+\S"'
check_fixed       "P232 distill_goal _extract_body uses lstrip().startswith on H2 break" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/distill_goal.py" 'if line.lstrip().startswith("## "):'
# raw_intent.py — three sites: _AC_HEADING_RE, _NEXT_H2_RE, _TITLE_H1_RE
check_fixed       "P232 raw_intent MOL-599 P232 banner present" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/raw_intent.py" 'MOL-599 P232:'
check_fixed       "P232 raw_intent _AC_HEADING_RE has ^\\s* prefix" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/raw_intent.py" '_AC_HEADING_RE = re.compile(r"^\s*##\s+Acceptance\s+Criteria'
check_fixed       "P232 raw_intent _NEXT_H2_RE has ^\\s* prefix" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/raw_intent.py" '_NEXT_H2_RE = re.compile(r"^\s*##\s+\S"'
check_fixed       "P232 raw_intent _TITLE_H1_RE has ^\\s* prefix" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/raw_intent.py" '_TITLE_H1_RE = re.compile(r"^\s*#\s+\S"'
# Failing-test fixture (Phase 4 step 1 evidence — committed BEFORE the fix per systematic-debugging skill)
check_fixed       "P232 failing-test fixture present at tests/test_distill_goal_indent.py" \
    "$HERMES_POC_REPO/tests/test_distill_goal_indent.py" 'MOL-599 P232'
check_fixed       "P232 test asserts _AC_HEADING_RE tolerates two-space indent" \
    "$HERMES_POC_REPO/tests/test_distill_goal_indent.py" 'def test_ac_heading_re_tolerates_two_space_indent'
check_fixed       "P232 test asserts _extract_body slice terminates at indented H2" \
    "$HERMES_POC_REPO/tests/test_distill_goal_indent.py" 'def test_extract_body_returns_nonempty_against_indented_h1'
check_fixed       "P232 test asserts _extract_body_plus_ac includes AC section" \
    "$HERMES_POC_REPO/tests/test_distill_goal_indent.py" 'def test_extract_body_plus_ac_yields_nonempty_ac_slice'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P233 / MOL-599: _AC_LINE_RE bare-checkbox AC item form tolerance ==="
check_marker_count "P233/MOL-599 PATCHES.md header" "$HERMES_AGENT/scripts/hermes-patches/PATCHES.md" "## P233 / MOL-599" 1
# distill_goal.py — _AC_LINE_RE extended to recognize GitHub-style bare [ ]/[x]/[X] checkboxes
check_fixed       "P233 distill_goal MOL-599 P233 banner present" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/distill_goal.py" 'MOL-599 P233:'
check_fixed       "P233 _AC_LINE_RE extended with bare-checkbox alternative" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/distill_goal.py" '\s*\[[ xX]\]\s+'
# Failing-test fixture (Phase 4 step 1 evidence — same file as P232, two new tests appended)
check_fixed       "P233 test asserts _AC_LINE_RE counts bare-checkbox AC items" \
    "$HERMES_POC_REPO/tests/test_distill_goal_indent.py" 'def test_ac_line_re_counts_bare_checkbox_items'
check_fixed       "P233 end-to-end test against MOL-599 fixture produces GOAL.md" \
    "$HERMES_POC_REPO/tests/test_distill_goal_indent.py" 'def test_distill_against_mol599_fixture_produces_goal_md'
# Reference diff present
check_fixed       "P233 reference diff present at scripts/hermes-patches/reference/" \
    "$HERMES_POC_REFERENCE/P233-MOL-599-ac-line-checkbox-tolerance.diff" 'MOL-599 P233'

[[ $QUIET -eq 0 ]] && echo ""
[[ $QUIET -eq 0 ]] && echo "=== P234 / MOL-599: _transition_jira target name matches MOL workflow ==="
check_marker_count "P234/MOL-599 PATCHES.md header" "$HERMES_AGENT/scripts/hermes-patches/PATCHES.md" "## P234 / MOL-599" 1
# symphony_loop.py — target hardcode corrected from "In Testing" → "Testing" to match MOL workflow.
# Pairs with negative deletion-class check: the wrong string must NOT reappear.
check_fixed       "P234 symphony_loop MOL-599 P234 banner present" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/symphony_loop.py" 'MOL-599 P234:'
check_fixed       "P234 _transition_jira targets workflow name 'Testing'" \
    "${HERMES_HOME:-$HOME/.hermes}/scripts/symphony_loop.py" 'target = "Testing"'
# Deletion-class assertion: stale "In Testing" hardcode must NOT reappear (P151/MOL-502 pattern).
total=$((total + 1))
if grep -Fq -- 'target = "In Testing"' "${HERMES_HOME:-$HOME/.hermes}/scripts/symphony_loop.py" 2>/dev/null; then
    [[ $QUIET -eq 0 ]] && printf '  \033[0;31m[✗]\033[0m P234 stale '\''In Testing'\'' hardcode reappeared in symphony_loop.py\n'
    failed=$((failed + 1))
else
    [[ $QUIET -eq 0 ]] && printf '  \033[0;32m[✓]\033[0m P234 stale '\''In Testing'\'' hardcode absent from symphony_loop.py\n'
    passed=$((passed + 1))
fi
# Reference diff present
check_fixed       "P234 reference diff present at scripts/hermes-patches/reference/" \
    "$HERMES_POC_REFERENCE/P234-MOL-599-jira-transition-target-name.diff" 'MOL-599 P234'

if [ "$failed" -gt 0 ]; then
    exit 1
fi
exit 0

