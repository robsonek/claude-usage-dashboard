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
