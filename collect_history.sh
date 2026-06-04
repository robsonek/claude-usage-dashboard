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

# Warn (don't fail) on fetch problems — the JSON is still written so we can inspect later
if echo "$USAGE_JSON" | grep -q '"error"'; then
    log "WARN: fetcher returned error: $(echo "$USAGE_JSON" | head -c 200)"
elif echo "$USAGE_JSON" | grep -q '"quotas": \[\]'; then
    log "WARN: fetcher returned empty quotas"
fi

# Save to JSON file (history / reproducibility)
echo "$USAGE_JSON" > "$DAY_DIR/$TIME.json"

# Save to SQLite database
if ! echo "$USAGE_JSON" | "$VENV_PYTHON" "$SCRIPT_DIR/insert_to_db.py"; then
    log "ERROR: insert_to_db.py failed"
    exit 1
fi
