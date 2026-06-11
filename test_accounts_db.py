"""Tests for accounts table schema, CRUD, token encryption, account_id wiring."""
import os
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

import config
import crypto_util
from database import UsageDatabase


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'TOKEN_ENCRYPTION_KEY', Fernet.generate_key().decode())
    crypto_util._reset_cache()
    d = UsageDatabase(str(tmp_path / 'test.db'))
    yield d
    d.close()


def test_accounts_table_exists(db):
    cur = db.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='accounts'")
    assert cur.fetchone() is not None


def test_snapshots_has_account_id_column(db):
    cur = db.conn.execute("PRAGMA table_info(snapshots)")
    cols = {r['name'] for r in cur.fetchall()}
    assert 'account_id' in cols


def _add(db, **kw):
    base = dict(label='Main', email='a@x.com', account_type='max',
                access_token='AT-1', refresh_token='RT-1', expires_at=1000)
    base.update(kw)
    return db.add_or_update_account(**base)


def test_add_account_encrypts_tokens(db):
    acc_id = _add(db)
    row = db.conn.execute(
        "SELECT access_token, refresh_token FROM accounts WHERE id=?", (acc_id,)).fetchone()
    assert row['access_token'] != 'AT-1'  # stored encrypted
    assert crypto_util.decrypt(row['access_token']) == 'AT-1'


def test_list_accounts_omits_tokens(db):
    _add(db)
    accounts = db.list_accounts()
    assert len(accounts) == 1
    assert accounts[0]['email'] == 'a@x.com'
    assert 'access_token' not in accounts[0]


def test_get_pollable_accounts_decrypts_tokens(db):
    _add(db)
    pollable = db.get_pollable_accounts()
    assert pollable[0]['access_token'] == 'AT-1'
    assert pollable[0]['refresh_token'] == 'RT-1'


def test_pollable_skips_undecryptable_row_and_records_error(db):
    """Audit point 7: one account whose stored token can't be decrypted (corrupt
    blob, or encrypted under a different key) must not blow up the whole poll. It
    is skipped from the pollable list and flagged via last_error, so every other
    account still gets collected and the UI shows the failure."""
    good = _add(db, email='good@x.com')
    bad = _add(db, email='bad@x.com')
    db.conn.execute("UPDATE accounts SET access_token='not-a-valid-fernet-token' WHERE id=?",
                    (bad,))
    db.conn.commit()
    pollable = db.get_pollable_accounts()
    assert {p['email'] for p in pollable} == {'good@x.com'}
    by_id = {a['id']: a for a in db.list_accounts()}
    assert by_id[bad]['last_error'] is not None
    assert by_id[good]['last_error'] is None


def test_pollable_missing_key_still_raises(db, monkeypatch):
    """A globally missing/wrong key is a different failure from one corrupt row:
    it must still propagate (CryptoError) so collect_all exits 3 (hard ERROR),
    not get silently swallowed as 'no accounts'."""
    _add(db, email='a@x.com')
    monkeypatch.setattr(config, 'TOKEN_ENCRYPTION_KEY', None)
    crypto_util._reset_cache()
    with pytest.raises(crypto_util.CryptoError):
        db.get_pollable_accounts()


def test_upsert_by_email_updates_not_duplicates(db):
    first = _add(db, access_token='AT-1')
    second = _add(db, access_token='AT-2', label='Renamed')
    assert first == second  # same row
    assert len(db.list_accounts()) == 1
    assert db.get_pollable_accounts()[0]['access_token'] == 'AT-2'


def test_update_account_tokens(db):
    acc_id = _add(db)
    db.update_account_tokens(acc_id, 'AT-NEW', 'RT-NEW', 2000)
    p = db.get_pollable_accounts()[0]
    assert (p['access_token'], p['refresh_token'], p['expires_at']) == ('AT-NEW', 'RT-NEW', 2000)


def test_set_active_excludes_from_pollable(db):
    acc_id = _add(db)
    db.set_account_active(acc_id, False)
    assert db.get_pollable_accounts() == []
    assert len(db.list_accounts()) == 1  # still listed for UI


def test_rename_and_record_poll_and_delete(db):
    acc_id = _add(db)
    db.rename_account(acc_id, 'New Label')
    db.record_account_poll(acc_id, error='boom')
    acc = db.list_accounts()[0]
    assert acc['label'] == 'New Label'
    assert acc['last_error'] == 'boom'
    assert acc['last_polled_at'] is not None
    db.delete_account(acc_id)
    assert db.list_accounts() == []


def test_get_default_account_id_first_active(db):
    a = _add(db, email='a@x.com')
    b = _add(db, email='b@x.com')
    db.set_account_active(a, False)
    assert db.get_default_account_id() == b


def test_get_default_account_id_falls_back_when_all_disabled(db):
    """Audit point 9a: with accounts present but ALL disabled, scope to a real
    account (lowest id) rather than returning None — None makes get_current /
    get_history merge every account's snapshots into one garbage series."""
    a = _add(db, email='a@x.com')
    b = _add(db, email='b@x.com')
    db.set_account_active(a, False)
    db.set_account_active(b, False)
    assert db.get_default_account_id() == a


def test_get_default_account_id_none_when_no_accounts(db):
    """Truly empty (legacy single-account era) still returns None → unscoped."""
    assert db.get_default_account_id() is None


def SNAP(acc_id, pct, captured=None, resets=None):
    """Build a weekly-quota snapshot. captured_at defaults to now (so the 168h
    get_history cutoff always covers it — no time-bomb), resets_at a few days
    ahead so _resets_at_is_plausible stays happy."""
    cap = captured or datetime.now(timezone.utc)
    res = resets or (cap + timedelta(days=4))
    return {
        'account_id': acc_id, 'account_type': 'max', 'email': 'a@x.com',
        'captured_at': cap.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'quotas': [{'type': 'weekly', 'percent_remaining': pct,
                    'resets_at': res.strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'time_remaining_seconds': 100}],
    }


def test_insert_snapshot_persists_account_id(db):
    acc = _add(db)
    sid = db.insert_snapshot(SNAP(acc, 50))
    row = db.conn.execute("SELECT account_id FROM snapshots WHERE id=?", (sid,)).fetchone()
    assert row['account_id'] == acc


def test_get_current_filters_by_account(db):
    a = _add(db, email='a@x.com')
    b = _add(db, email='b@x.com')
    db.insert_snapshot(dict(SNAP(a, 11), email='a@x.com'))
    db.insert_snapshot(dict(SNAP(b, 22), email='b@x.com'))
    cur_a = db.get_current(account_id=a)
    assert cur_a['limits']['weekly']['percent_remaining'] == 11
    cur_b = db.get_current(account_id=b)
    assert cur_b['limits']['weekly']['percent_remaining'] == 22


def test_get_history_filters_by_account(db):
    a = _add(db, email='a@x.com')
    b = _add(db, email='b@x.com')
    db.insert_snapshot(dict(SNAP(a, 11), email='a@x.com'))
    db.insert_snapshot(dict(SNAP(b, 22), email='b@x.com'))
    hist_a = db.get_history(account_id=a)
    assert len(hist_a) == 1
    assert hist_a[0]['limits']['weekly']['percent_remaining'] == 11


def _psa(db, account_id):
    """Stored period_start_at (as tz-aware datetime) for the latest weekly quota
    of account_id."""
    row = db.conn.execute("""
        SELECT q.period_start_at
        FROM quotas q JOIN snapshots s ON s.id = q.snapshot_id
        WHERE s.account_id = ? AND q.quota_type = 'weekly'
        ORDER BY s.captured_at DESC, q.id DESC LIMIT 1
    """, (account_id,)).fetchone()
    raw = row['period_start_at']
    return datetime.fromisoformat(str(raw).replace('Z', '+00:00').replace(' ', 'T'))


def test_period_start_at_isolated_per_account(db):
    """Interleaved inserts for two accounts whose weekly resets differ must not
    let account B inherit account A's reset anchor (account-blind 'prev' bug)."""
    a = _add(db, email='a@x.com')
    b = _add(db, email='b@x.com')
    now = datetime.now(timezone.utc)
    # Distinct reset deadlines per account, both plausibly in the future.
    reset_a = now + timedelta(days=2)
    reset_b = now + timedelta(days=5)
    # Two interleaved rounds: A then B, A then B (collect_all-style back-to-back).
    db.insert_snapshot(dict(SNAP(a, 50, captured=now, resets=reset_a), email='a@x.com'))
    db.insert_snapshot(dict(SNAP(b, 60, captured=now, resets=reset_b), email='b@x.com'))
    db.insert_snapshot(dict(SNAP(a, 45, captured=now + timedelta(minutes=5),
                                  resets=reset_a), email='a@x.com'))
    db.insert_snapshot(dict(SNAP(b, 55, captured=now + timedelta(minutes=5),
                                  resets=reset_b), email='b@x.com'))
    # Expected period_start = reset - 168h (weekly default), computed from each
    # account's OWN first row (same reset → inherit fallback from first insert).
    exp_a = reset_a - timedelta(hours=168)
    exp_b = reset_b - timedelta(hours=168)
    assert abs((_psa(db, a) - exp_a).total_seconds()) < 1
    assert abs((_psa(db, b) - exp_b).total_seconds()) < 1
    assert _psa(db, a) != _psa(db, b)  # not cross-contaminated


def test_fill_missing_quotas_isolated_per_account(db):
    """A dropped quota on account A must be carried forward from A's own earlier
    snapshot, never from account B."""
    a = _add(db, email='a@x.com')
    b = _add(db, email='b@x.com')
    now = datetime.now(timezone.utc)
    reset = now + timedelta(days=4)

    def snap_with_model(acc, weekly_pct, model_pct, captured):
        s = SNAP(acc, weekly_pct, captured=captured, resets=reset)
        if model_pct is not None:
            s['quotas'].append({
                'type': 'model_specific', 'model': 'sonnet',
                'percent_remaining': model_pct,
                'resets_at': reset.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'time_remaining_seconds': 100,
            })
        return s

    # Earlier A snapshot has model_specific=70; B snapshot has model_specific=99.
    db.insert_snapshot(dict(snap_with_model(a, 50, 70, now), email='a@x.com'))
    db.insert_snapshot(dict(snap_with_model(b, 60, 99, now + timedelta(minutes=1)),
                            email='b@x.com'))
    # A's LATEST snapshot drops model_specific entirely.
    db.insert_snapshot(dict(snap_with_model(a, 45, None, now + timedelta(minutes=2)),
                            email='a@x.com'))

    cur_a = db.get_current(account_id=a)
    ms = cur_a['limits']['model_specific']
    assert ms['stale'] is True
    assert ms['percent_remaining'] == 70  # from A's earlier row, not B's 99


def test_backfill_account_by_email_targets_only_null_matching(db):
    acc = _add(db, email='a@x.com')
    now = datetime.now(timezone.utc)
    # 1) NULL account, matching email -> should be backfilled
    db.insert_snapshot(dict(SNAP(None, 10, captured=now), email='a@x.com'))
    # 2) NULL account, different email -> untouched
    db.insert_snapshot(dict(SNAP(None, 20, captured=now + timedelta(minutes=1)),
                            email='other@x.com'))
    # 3) already-assigned account_id, matching email -> untouched
    other = _add(db, email='b@x.com')
    db.insert_snapshot(dict(SNAP(other, 30, captured=now + timedelta(minutes=2)),
                            email='a@x.com'))

    updated = db.backfill_account_by_email(acc, 'a@x.com')
    assert updated == 1  # only the NULL + a@x.com row

    rows = db.conn.execute(
        "SELECT email, account_id FROM snapshots ORDER BY captured_at ASC").fetchall()
    by = {(r['email'], r['account_id']) for r in rows}
    assert ('a@x.com', acc) in by          # the NULL row got acc
    assert ('other@x.com', None) in by     # different email stayed NULL
    assert ('a@x.com', other) in by        # already-assigned stayed put
