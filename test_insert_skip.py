"""Insert guard for the collector.

A glitched PTY render of `claude /usage` sometimes parses no quotas at all (only
the terminal-init escape burst was captured, or Claude itself showed "Could not
refresh usage data"). insert_to_db used to persist those as a snapshot row with
zero quota rows — useless for the history charts and, when it's the latest row,
it makes /api/current backfill *every* quota as stale (↩ prev). should_insert()
filters both pure-error records and empty-quota readings so they never reach the
DB. The JSON backup + raw_debug dump are still written by collect_history.sh.

Run: python3 test_insert_skip.py
"""
from insert_to_db import should_insert


def test_keeps_record_with_quotas():
    data = {'account_type': 'max', 'quotas': [{'type': 'session', 'percent_remaining': 80}]}
    assert should_insert(data) is True


def test_skips_pure_error_record():
    assert should_insert({'error': 'Authentication error', 'auth_error_type': 'token_expired'}) is False


def test_skips_empty_quota_list():
    # The empty-capture / "Could not refresh usage data" case.
    assert should_insert({'account_type': 'unknown', 'quotas': []}) is False


def test_skips_missing_quotas_key():
    assert should_insert({'account_type': 'max'}) is False


if __name__ == '__main__':
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:
                failures += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    raise SystemExit(failures)
