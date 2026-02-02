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


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON: {e}", file=sys.stderr)
        sys.exit(1)

    if 'error' in data:
        print(f"Skipping error record: {data.get('details', 'unknown')}", file=sys.stderr)
        sys.exit(0)

    try:
        db = UsageDatabase(config.DB_FILE)
        snapshot_id = db.insert_snapshot(data)
        db.close()
    except Exception as e:
        print(f"Database error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
