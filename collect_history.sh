#!/bin/bash
# Script for collecting Claude usage history
# Runs via systemd timer / cron every 5 minutes

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$SCRIPT_DIR/data"
VENV_PYTHON="$SCRIPT_DIR/venv/bin/python"

log() { echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" >&2; }

# Create directory for today's data
TODAY=$(date +%Y-%m-%d)
TIME=$(date +%H-%M)
DAY_DIR="$DATA_DIR/$TODAY"
mkdir -p "$DAY_DIR"

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

# Fetch data using usage_fetcher.py — stderr goes to the cron log, not /dev/null.
# CLAUDE_USAGE_RAW_DIR: fetcher dumps raw PTY bytes here only for incomplete/glitched
# readings, so we can diagnose what went wrong after the fact.
export CLAUDE_USAGE_RAW_DIR="$DATA_DIR/raw_debug"
USAGE_JSON=$("$VENV_PYTHON" "$SCRIPT_DIR/usage_fetcher.py")
FETCH_STATUS=$?

if [ $FETCH_STATUS -ne 0 ] || [ -z "$USAGE_JSON" ]; then
    log "ERROR: usage_fetcher.py exited with $FETCH_STATUS, output=${#USAGE_JSON} bytes"
    exit 1
fi

# Warn (don't fail yet) on fetch problems — the JSON is still written so we can
# inspect later. A hard fetcher error (e.g. auth) is persistent, so we record it
# and exit non-zero at the very end (after artifacts are saved) so the timer unit
# / monitoring actually sees the failure instead of a misleading success.
FETCH_ERROR=0
if echo "$USAGE_JSON" | grep -q '"error"'; then
    log "WARN: fetcher returned error: $(echo "$USAGE_JSON" | head -c 200)"
    FETCH_ERROR=1
elif echo "$USAGE_JSON" | grep -q '"quotas": \[\]'; then
    log "WARN: fetcher returned empty quotas"
fi

# Save to JSON file (history / reproducibility) — write atomically so a crash or
# overlap can't leave a half-written/corrupt file.
TMP_JSON="$DAY_DIR/.$TIME.$$.json"
printf '%s\n' "$USAGE_JSON" > "$TMP_JSON" && mv -f "$TMP_JSON" "$DAY_DIR/$TIME.json"

# Save to SQLite database (insert_to_db.py skips pure-error records itself)
if ! echo "$USAGE_JSON" | "$VENV_PYTHON" "$SCRIPT_DIR/insert_to_db.py"; then
    log "ERROR: insert_to_db.py failed"
    exit 1
fi

# Surface a persistent fetch error to the timer/monitoring (artifacts already saved).
if [ "$FETCH_ERROR" -ne 0 ]; then
    exit 2
fi
