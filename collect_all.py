#!/usr/bin/env python3
"""Poll every active account's usage and insert one snapshot each.

Replaces the single-account api_usage_fetcher.main() in collect_history.sh.
Sequential (2-3 accounts × ~1s); errors are isolated per account. Exit codes:
  0 — every active account polled OK
  1 — no active accounts to poll
  2 — at least one account failed (others still inserted)
"""
import json
import os
import sys
from datetime import datetime, timezone

import api_usage_fetcher as auf
import config
from database import UsageDatabase

DATA_DIR = config.DATA_DIR


def _log(msg):
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{stamp}] {msg}', file=sys.stderr)


def _write_backup(snapshot, account_id):
    """Mirror the JSON backup collect_history.sh used to write, one file/account."""
    try:
        now = datetime.now(timezone.utc)
        day_dir = os.path.join(DATA_DIR, now.strftime('%Y-%m-%d'))
        os.makedirs(day_dir, exist_ok=True)
        name = now.strftime('%H-%M') + f'-{account_id}.json'
        tmp = os.path.join(day_dir, f'.{name}.{os.getpid()}')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f)
        os.replace(tmp, os.path.join(day_dir, name))
    except OSError as e:
        _log(f'WARN: backup write failed for account {account_id}: {e}')


def _poll_one(db, account) -> bool:
    """Refresh-if-needed, fetch usage, insert snapshot. Returns True on success."""
    acc_id = account['id']
    try:
        access_token = account['access_token']
        if auf.needs_refresh_ms(account['expires_at']):
            new = auf.refresh_access_token(account['refresh_token'])
            db.update_account_tokens(acc_id, new['access_token'],
                                     new['refresh_token'], new['expires_at'])
            access_token = new['access_token']
        api = auf._http_get_usage(access_token)
        snapshot = auf.build_snapshot(api, account)
        db.insert_snapshot(snapshot)
        _write_backup(snapshot, acc_id)
        db.record_account_poll(acc_id, error=None)
        return True
    except Exception as e:
        _log(f'WARN: account {acc_id} ({account.get("email")}) failed: {e}')
        db.record_account_poll(acc_id, error=str(e)[:300])
        return False


def run(db) -> int:
    accounts = db.get_pollable_accounts()
    if not accounts:
        _log('no active accounts to poll')
        return 1
    failures = 0
    for account in accounts:
        if not _poll_one(db, account):
            failures += 1
    return 2 if failures else 0


def main() -> int:
    with UsageDatabase(config.DB_FILE) as db:
        return run(db)


if __name__ == '__main__':
    sys.exit(main())
