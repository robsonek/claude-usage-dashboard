#!/usr/bin/env python3
"""
Helper script to insert usage data into SQLite database.
Reads JSON from stdin and inserts into the database.

Usage:
    echo '{"captured_at": "...", "quotas": [...]}' | python insert_to_db.py
"""
import json
import sys

import config
from database import UsageDatabase


def should_insert(data) -> bool:
    """Whether a fetched record is worth persisting.

    Skip pure-error records AND empty-quota readings: a glitched PTY render of
    `claude /usage` sometimes parses no quotas (only the terminal-init escape burst
    was captured, or Claude itself showed "Could not refresh usage data"). Storing
    those as a snapshot with zero quota rows adds nothing to the history charts and,
    when it's the latest row, makes /api/current backfill *every* quota as stale
    (↩ prev). The JSON backup + raw_debug dump are still written by collect_history.sh.
    """
    if 'error' in data:
        return False
    if not data.get('quotas'):
        return False
    return True


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON: {e}", file=sys.stderr)
        sys.exit(1)

    if not should_insert(data):
        reason = data.get('details', 'unknown') if 'error' in data else 'no quotas parsed'
        print(f"Skipping record ({reason})", file=sys.stderr)
        sys.exit(0)

    try:
        with UsageDatabase(config.DB_FILE) as db:
            db.insert_snapshot(data)
    except Exception as e:
        print(f"Database error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
