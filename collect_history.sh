#!/bin/bash
# Script for collecting Claude usage history
# Runs via systemd timer / cron every 5 minutes

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$SCRIPT_DIR/data"
VENV_PYTHON="$SCRIPT_DIR/venv/bin/python"

log() { echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" >&2; }

# TODAY gates the once-per-day retention cleanup; DATA_DIR must exist for the
# lock file (collect_all.py creates its own per-day backup dirs underneath).
TODAY=$(date +%Y-%m-%d)
mkdir -p "$DATA_DIR"

if [ ! -x "$VENV_PYTHON" ]; then
    log "ERROR: venv Python not found at $VENV_PYTHON"
    exit 1
fi

# Skip if a previous run is still going (a slow /usage fetch shouldn't overlap the
# next 5-min tick and double-insert). Lock lives under data/ (gitignored).
exec 9>"$DATA_DIR/.collect.lock"
if ! flock -n 9; then
    log "WARN: another collection run is in progress, skipping"
    exit 0
fi

# Poll all active accounts (collect_all.py handles refresh, fetch, per-account
# snapshot insert + JSON backup, and per-account error isolation).
FETCH_ERROR=0
"$VENV_PYTHON" "$SCRIPT_DIR/collect_all.py"
FETCH_STATUS=$?
case $FETCH_STATUS in
    0) : ;;                                   # all accounts OK
    1) log "WARN: no active accounts to poll" ;;
    2) log "WARN: at least one account failed (see above)"; FETCH_ERROR=1 ;;
    *) log "ERROR: collect_all.py exited $FETCH_STATUS"; exit 1 ;;
esac

# Retention cleanup, at most once per day (date-marker gated). Runs inside the
# existing flock. Housekeeping only: a failure is logged but never fails the
# collection run, and the marker is written only on success so a failed run
# retries on the next tick.
CLEANUP_MARKER="$DATA_DIR/.cleanup_done"
if [ "$(cat "$CLEANUP_MARKER" 2>/dev/null)" != "$TODAY" ]; then
    if "$VENV_PYTHON" "$SCRIPT_DIR/cleanup_old_data.py"; then
        echo "$TODAY" > "$CLEANUP_MARKER"
    else
        log "WARN: cleanup_old_data.py failed (non-fatal)"
    fi
fi

# Surface a persistent fetch error to the timer/monitoring (artifacts already saved).
if [ "$FETCH_ERROR" -ne 0 ]; then
    exit 2
fi
