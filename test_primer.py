"""Tests for the Haiku session primer: DB accessor + prime_account orchestration.

All offline — usage/primer/refresh HTTP is monkeypatched via api_usage_fetcher.
"""
import pytest
from cryptography.fernet import Fernet

import config
import crypto_util
import primer
from database import UsageDatabase

FAR = 9_999_999_999_999
PAST = "2020-01-01T00:00:00+00:00"
FUTURE = "2999-01-01T00:00:00+00:00"


def _sample(five_reset):
    """A usage response with the 5h window resetting at five_reset (active iff future)."""
    return {
        "five_hour": {"utilization": 0.0, "resets_at": five_reset},
        "seven_day": {"utilization": 10.0, "resets_at": FUTURE},
        "seven_day_sonnet": {"utilization": 0.0, "resets_at": FUTURE},
    }


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'TOKEN_ENCRYPTION_KEY', Fernet.generate_key().decode())
    crypto_util._reset_cache()
    d = UsageDatabase(str(tmp_path / 'p.db'))
    yield d
    d.close()


def _add(db, email='a@x.com', expires_at=FAR):
    return db.add_or_update_account(
        label=email, email=email, account_type='max',
        access_token='AT', refresh_token='RT', expires_at=expires_at)


def test_get_account_for_primer_decrypts_tokens(db):
    a = _add(db)
    acc = db.get_account_for_primer(a)
    assert acc['id'] == a
    assert acc['access_token'] == 'AT'      # decrypted
    assert acc['refresh_token'] == 'RT'
    assert acc['account_type'] == 'max'


def test_get_account_for_primer_missing_returns_none(db):
    assert db.get_account_for_primer(424242) is None
