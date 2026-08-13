#!/usr/bin/env python3
"""Poll every active account's usage and insert one snapshot each.

Replaces the single-account api_usage_fetcher.main() in collect_history.sh.
Sequential (2-3 accounts × ~1s); errors are isolated per account. Exit codes:
  0 — every active account polled OK
  1 — no active accounts to poll
  2 — at least one account failed (others still inserted)
  3 — crash (e.g. missing/wrong TOKEN_ENCRYPTION_KEY → CryptoError)
"""
import json
import os
import sys
from datetime import datetime, timezone

import account_session
import api_usage_fetcher as auf
import config
from database import UsageDatabase, REAUTH_FAILURE_THRESHOLD

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


def _capture_daily_raw(api, account_id):
    """Persist one raw usage response per account per UTC day to data/raw_debug/.

    Lets us inspect the live API shape as Anthropic evolves it — notably to map a
    per-model (model_specific) window from real data if/when it reappears in the
    `limits` array — without per-poll file churn. Best-effort: pruned by
    cleanup_old_data retention (by mtime), and never raises into the poll path.
    """
    try:
        raw_dir = os.path.join(DATA_DIR, 'raw_debug')
        now = datetime.now(timezone.utc)
        dest = os.path.join(raw_dir, f"usage-{account_id}-{now.strftime('%Y-%m-%d')}.json")
        if os.path.exists(dest):
            return  # already captured a sample for this account today
        os.makedirs(raw_dir, exist_ok=True)
        tmp = os.path.join(raw_dir, f".usage-{account_id}.{os.getpid()}.tmp")
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(api, f)
        os.replace(tmp, dest)
    except OSError as e:
        _log(f'WARN: raw capture failed for account {account_id}: {e}')


def _poll_one(db, account) -> bool:
    """Refresh-if-needed, fetch usage, insert snapshot. Returns True on success.

    Refresh + retry-once-on-401 is handled by account_session.call_with_401_retry.
    """
    acc_id = account['id']
    try:
        api = account_session.call_with_401_retry(db, account, auf._http_get_usage)
        _capture_daily_raw(api, acc_id)
        snapshot = auf.build_snapshot(api, account)
        db.insert_snapshot(snapshot)
        _write_backup(snapshot, acc_id)
        db.record_account_poll(acc_id, error=None)
        return True
    except Exception as e:
        _log(f'WARN: account {acc_id} ({account.get("email")}) failed: {e}')
        db.record_account_poll(acc_id, error=str(e)[:300])
        if getattr(e, 'oauth_error', None) == 'invalid_grant':
            # An expired/revoked grant never heals on its own — after a few
            # consecutive hits, pause the account instead of hammering the
            # token endpoint every tick until someone re-authorizes it.
            if db.record_auth_failure(acc_id):
                _log(f'WARN: account {acc_id} flagged needs_reauth after '
                     f'{REAUTH_FAILURE_THRESHOLD} consecutive invalid_grant '
                     'failures; polling paused until re-auth')
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
    try:
        with UsageDatabase(config.DB_FILE) as db:
            return run(db)
    except Exception as e:
        # e.g. CryptoError when TOKEN_ENCRYPTION_KEY is missing/wrong — return a
        # distinct code so collect_history.sh logs a hard ERROR (not the benign
        # exit-1 "no accounts" path).
        _log(f'ERROR: collect_all crashed: {type(e).__name__}: {e}')
        return 3


if __name__ == '__main__':
    sys.exit(main())
