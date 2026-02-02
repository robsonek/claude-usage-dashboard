#!/bin/bash
# Script for collecting Claude usage history
# Runs via systemd timer every 5 minutes

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$SCRIPT_DIR/data"
VENV_PYTHON="$SCRIPT_DIR/venv/bin/python"

# Create directory for today's data
TODAY=$(date +%Y-%m-%d)
TIME=$(date +%H-%M)
DAY_DIR="$DATA_DIR/$TODAY"
mkdir -p "$DAY_DIR"

# Fetch data using usage_fetcher.py
if [ -x "$VENV_PYTHON" ]; then
    USAGE_JSON=$("$VENV_PYTHON" "$SCRIPT_DIR/usage_fetcher.py" 2>/dev/null)

    # Save to JSON file (for backward compatibility)
    echo "$USAGE_JSON" > "$DAY_DIR/$TIME.json"

    # Save to SQLite database
    echo "$USAGE_JSON" | "$VENV_PYTHON" "$SCRIPT_DIR/insert_to_db.py" 2>/dev/null
else
    echo "Error: venv Python not found at $VENV_PYTHON" >&2
    exit 1
fi
