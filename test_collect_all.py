"""Tests for collect_all: per-account refresh+fetch+insert, error isolation."""
import json

import pytest
from cryptography.fernet import Fernet

import config
import crypto_util
import collect_all
from database import UsageDatabase

PROD_SAMPLE = {
    "five_hour": {"utilization": 10.0, "resets_at": "2026-06-11T08:40:00+00:00"},
    "seven_day": {"utilization": 58.0, "resets_at": "2026-06-15T11:00:00+00:00"},
    "seven_day_sonnet": {"utilization": 0.0, "resets_at": "2026-06-15T11:00:00+00:00"},
}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'TOKEN_ENCRYPTION_KEY', Fernet.generate_key().decode())
    crypto_util._reset_cache()
    d = UsageDatabase(str(tmp_path / 'c.db'))
    yield d
    d.close()


def _add(db, email, expires_at):
    return db.add_or_update_account(
        label=email, email=email, account_type='max',
        access_token='AT', refresh_token='RT', expires_at=expires_at)


def test_polls_each_active_account_and_inserts(db, tmp_path, monkeypatch):
    far = 9_999_999_999_999
    a = _add(db, 'a@x.com', far)
    b = _add(db, 'b@x.com', far)
    monkeypatch.setattr(collect_all.auf, '_http_get_usage', lambda token: PROD_SAMPLE)
    monkeypatch.setattr(collect_all, 'DATA_DIR', str(tmp_path / 'data'))
    rc = collect_all.run(db)
    assert rc == 0
    assert db.get_current(account_id=a) is not None
    assert db.get_current(account_id=b) is not None


def test_refreshes_expired_token_and_persists(db, tmp_path, monkeypatch):
    a = _add(db, 'a@x.com', 0)  # already expired
    monkeypatch.setattr(collect_all.auf, 'refresh_access_token',
                        lambda rt, now_ms=None: {'access_token': 'AT2',
                            'refresh_token': 'RT2', 'expires_at': 9_999_999_999_999})
    monkeypatch.setattr(collect_all.auf, '_http_get_usage', lambda token: PROD_SAMPLE)
    monkeypatch.setattr(collect_all, 'DATA_DIR', str(tmp_path / 'data'))
    assert collect_all.run(db) == 0
    p = db.get_pollable_accounts()[0]
    assert p['access_token'] == 'AT2' and p['refresh_token'] == 'RT2'


def test_one_account_failing_does_not_block_others(db, tmp_path, monkeypatch):
    far = 9_999_999_999_999
    a = _add(db, 'a@x.com', far)
    b = _add(db, 'b@x.com', far)
    raise_list = []
    def usage(token):
        raise_list.append(1)
        if len(raise_list) == 1:
            raise collect_all.auf.UsageApiError('boom')
        return PROD_SAMPLE
    monkeypatch.setattr(collect_all.auf, '_http_get_usage', usage)
    monkeypatch.setattr(collect_all, 'DATA_DIR', str(tmp_path / 'data'))
    rc = collect_all.run(db)
    assert rc == 2  # at least one account failed
    accounts = {acc['email']: acc for acc in db.list_accounts()}
    assert accounts['a@x.com']['last_error'] is not None
    assert db.get_current(account_id=b) is not None


def test_no_accounts_returns_one(db, monkeypatch, tmp_path):
    monkeypatch.setattr(collect_all, 'DATA_DIR', str(tmp_path / 'data'))
    assert collect_all.run(db) == 1  # nothing to poll
