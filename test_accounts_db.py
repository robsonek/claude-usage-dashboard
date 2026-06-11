"""Tests for accounts table schema, CRUD, token encryption, account_id wiring."""
import os

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


SNAP = lambda acc_id, pct: {
    'account_id': acc_id, 'account_type': 'max', 'email': 'a@x.com',
    'captured_at': '2026-06-11T07:00:00Z',
    'quotas': [{'type': 'weekly', 'percent_remaining': pct,
                'resets_at': '2026-06-15T11:00:00Z', 'time_remaining_seconds': 100}],
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
