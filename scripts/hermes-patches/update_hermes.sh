#!/usr/bin/env bash
# MOL-158: Hermes Agent update with backup + clean pull
# Does NOT auto-apply patches — use agent-assisted manual patching after.
# Exits 2 on success to signal "patches need re-application".
#
# Usage: bash scripts/hermes-patches/update_hermes.sh
#
# Steps:
#   1. Pre-flight checks
#   2. Stop gateway
#   3. Backup (full tar + plugins dir, verified)
#   4. Confirm before destructive operations
#   5. Clean git pull
#   6. Reinstall Python dependencies
#   7. Copy envchain-wrapper.sh
#   8. Print next steps (exit 2 — patches missing)

set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_AGENT="$HERMES_HOME/hermes-agent"
BACKUP_DIR="$HERMES_HOME/backups"
REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"

red()   { printf '\033[0;31m%s\033[0m\n' "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
yellow(){ printf '\033[0;33m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

die() { red "ERROR: $*" >&2; exit 1; }

# -------------------------------------------------------------------
# 1. Pre-flight checks
# -------------------------------------------------------------------
bold "=== Pre-flight checks ==="

# P93/MOL-283: Block updates on patched trees.
# This script does `git checkout -- .` + `git clean -fd` which discards
# uncommitted local patches.  After migration to a clean upstream tree
# the fast-forward protection from founding commits disappears, so we
# need an explicit sentinel check.
PATCHED_MARKER="$HERMES_HOME/.patched-tree"
if [ -f "$PATCHED_MARKER" ]; then
    die "Patched tree detected ($PATCHED_MARKER exists).

This script discards all local patches with 'git checkout -- .'.
To upgrade safely, follow the MOL-283 migration workflow:
  https://deep-agent-one.atlassian.net/browse/MOL-283

If you are SURE you want to discard all patches:
  rm $PATCHED_MARKER
  bash scripts/hermes-patches/update_hermes.sh"
fi

[ -d "$HERMES_AGENT/.git" ] || die "$HERMES_AGENT is not a git repo"

cd "$HERMES_AGENT"

# Show current state
CURRENT_SHA=$(git rev-parse --short HEAD)
FETCH_ERR=$(git fetch origin main 2>&1 >/dev/null) || die "Failed to fetch from origin: $FETCH_ERR"
BEHIND=$(git rev-list --count HEAD..origin/main)
echo "  Current HEAD:  $CURRENT_SHA"
echo "  Commits behind: $BEHIND"

if [ "$BEHIND" -eq 0 ]; then
    green "Already up to date. Nothing to do."
    exit 0
fi

# Show modified files
MODIFIED=$(git diff --name-only)
if [ -n "$MODIFIED" ]; then
    yellow "  Modified files that will be discarded:"
    echo "$MODIFIED" | sed 's/^/    /'
fi

# Check for untracked dirs we need to preserve
if [ -d "plugins/memory/tiered" ]; then
    yellow "  plugins/memory/tiered/ exists (untracked) — will be preserved"
fi

echo ""

# -------------------------------------------------------------------
# 2. Stop gateway
# -------------------------------------------------------------------
bold "=== Stopping gateway ==="

if command -v hermes &>/dev/null; then
    hermes gateway stop 2>/dev/null && green "  Gateway stopped" || yellow "  Gateway was not running"
else
    yellow "  hermes command not found — skipping gateway stop"
fi

echo ""

# -------------------------------------------------------------------
# 3. Backup (with verification)
# -------------------------------------------------------------------
bold "=== Creating backup ==="

mkdir -p "$BACKUP_DIR"

BACKUP_FILE="$BACKUP_DIR/hermes-agent-$TIMESTAMP.tar.gz"
TAR_ERR=$(tar czf "$BACKUP_FILE" -C "$HERMES_HOME" hermes-agent/ 2>&1) || die "Backup failed: $TAR_ERR"
if [ -n "$TAR_ERR" ]; then
    yellow "  Backup warnings: $TAR_ERR"
fi

# Verify the archive is not empty/corrupt
tar tzf "$BACKUP_FILE" >/dev/null 2>&1 || die "Backup verification failed — archive is corrupt"
BACKUP_SIZE=$(stat -f%z "$BACKUP_FILE" 2>/dev/null || stat -c%s "$BACKUP_FILE" 2>/dev/null || echo 0)
if [ "$BACKUP_SIZE" -lt 1024 ]; then
    die "Backup file suspiciously small (${BACKUP_SIZE} bytes) — aborting"
fi
green "  Full backup: $BACKUP_FILE ($(du -h "$BACKUP_FILE" | cut -f1))"

# Separate backup for untracked plugins dir
if [ -d "plugins/memory/tiered" ]; then
    PLUGIN_BACKUP="$BACKUP_DIR/plugins-memory-tiered-$TIMESTAMP.tar.gz"
    tar czf "$PLUGIN_BACKUP" -C "$HERMES_AGENT" plugins/memory/tiered/ || die "Plugin backup failed"
    green "  Plugin backup: $PLUGIN_BACKUP"
fi

echo ""

# -------------------------------------------------------------------
# 4. Confirm before destructive operations
# -------------------------------------------------------------------
if [ -n "$MODIFIED" ]; then
    echo ""
    read -rp "Proceed with update? This will discard the above changes. [y/N] " confirm
    [[ "$confirm" =~ ^[Yy]$ ]] || die "Aborted by user"
    echo ""
fi

# -------------------------------------------------------------------
# 5. Clean & pull
# -------------------------------------------------------------------
bold "=== Clean git pull ==="

echo "  Discarding local changes..."
git checkout -- .

echo "  Cleaning untracked files (preserving plugins/)..."
git clean -fd --exclude=plugins/

echo "  Pulling from origin/main..."
if ! git pull --ff-only origin main; then
    red "  Fast-forward failed — local branch has diverged from origin/main."
    echo "  Local commits that would be lost:"
    git log --oneline origin/main..HEAD 2>/dev/null || true
    die "Cannot fast-forward. Resolve manually or restore from backup."
fi

# Clear stale bytecode
find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
echo "  Cleared __pycache__ directories"

NEW_SHA=$(git rev-parse --short HEAD)
NEW_VERSION=$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' hermes_cli/__init__.py 2>/dev/null || echo "unknown")
green "  Updated: $CURRENT_SHA → $NEW_SHA (v$NEW_VERSION)"

echo ""

# -------------------------------------------------------------------
# 6. Reinstall Python dependencies
# -------------------------------------------------------------------
bold "=== Installing Python dependencies ==="

VENV_PYTHON="$HERMES_AGENT/venv/bin/python3"
if [ -x "$VENV_PYTHON" ]; then
    if ! "$VENV_PYTHON" -m pip install -e ".[all]" --quiet 2>&1; then
        red "  pip install failed. Run manually for full output:"
        echo "    $VENV_PYTHON -m pip install -e '.[all]'"
        exit 1
    fi
    green "  Dependencies installed"
else
    yellow "  venv python not found at $VENV_PYTHON — install manually"
fi

echo ""

# -------------------------------------------------------------------
# 7. Copy envchain-wrapper.sh
# -------------------------------------------------------------------
bold "=== Post-update setup ==="

WRAPPER_SRC="$REPO_DIR/scripts/envchain-wrapper.sh"
WRAPPER_DST="$HERMES_HOME/scripts/envchain-wrapper.sh"

if [ -f "$WRAPPER_SRC" ]; then
    mkdir -p "$(dirname "$WRAPPER_DST")"
    cp "$WRAPPER_SRC" "$WRAPPER_DST"
    chmod +x "$WRAPPER_DST"
    green "  envchain-wrapper.sh copied to $WRAPPER_DST"
else
    yellow "  envchain-wrapper.sh not found at $WRAPPER_SRC"
fi

echo ""

# -------------------------------------------------------------------
# 8. Next steps (exit 2 — patches need re-application)
# -------------------------------------------------------------------
bold "=== Update complete ==="
echo ""
echo "  Backup:  $BACKUP_FILE"
echo "  Version: v$NEW_VERSION ($NEW_SHA)"
echo ""
yellow "  WARNING: Patches have been removed by the update."
yellow "  Hermes should NOT be started until patches are re-applied."
echo ""
echo "  Next steps:"
echo "    1. Re-apply patches (see scripts/hermes-patches/PATCHES.md)"
echo "    2. Verify: bash scripts/hermes-patches/verify_patches.sh"
echo "    3. Restart: hermes gateway run --replace"
echo ""
echo "  To rollback:"
echo "    cd $HERMES_HOME && tar xzf $BACKUP_FILE"
echo "    hermes gateway run --replace"

# Exit 2: update succeeded but patches need re-application.
# Distinct from exit 0 (nothing to do) and exit 1 (error).
exit 2
