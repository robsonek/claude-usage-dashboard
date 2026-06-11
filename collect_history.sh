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

# Fetch data from the oauth/usage HTTP API (api_usage_fetcher.py). On failure
# it prints an {"error": ...} JSON and exits non-zero — the JSON is still
# archived below for diagnosis (insert_to_db skips it) and the run exits 2 at
# the very end so the timer/monitoring sees the failure.
USAGE_JSON=$("$VENV_PYTHON" "$SCRIPT_DIR/api_usage_fetcher.py")
FETCH_STATUS=$?

if [ -z "$USAGE_JSON" ]; then
    log "ERROR: api_usage_fetcher.py exited with $FETCH_STATUS and produced no output"
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

# Save to SQLite database (insert_to_db.py skips pure-error AND empty-quota records
# itself — a glitched read with no quotas isn't persisted, only the JSON/raw_debug above)
if ! echo "$USAGE_JSON" | "$VENV_PYTHON" "$SCRIPT_DIR/insert_to_db.py"; then
    log "ERROR: insert_to_db.py failed"
    exit 1
fi

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
