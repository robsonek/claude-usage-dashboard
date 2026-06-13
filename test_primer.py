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


def test_prime_starts_window_when_inactive(db, monkeypatch):
    a = _add(db)
    account = db.get_account_for_primer(a)
    calls = {'usage': 0, 'primer': 0}

    def usage(token):
        calls['usage'] += 1
        return _sample(PAST) if calls['usage'] == 1 else _sample(FUTURE)

    def send(token, model=None):
        calls['primer'] += 1
        return {'id': 'msg_1'}

    monkeypatch.setattr(primer.auf, '_http_get_usage', usage)
    monkeypatch.setattr(primer.auf, 'send_haiku_primer', send)

    result = primer.prime_account(db, account)
    assert result['started'] is True
    assert result['resets_at'] == '2999-01-01T00:00:00Z'
    assert calls['primer'] == 1            # exactly one message sent
    assert calls['usage'] == 2             # guard GET + post-send GET
    assert db.get_current(account_id=a) is not None  # snapshot was inserted


def test_prime_blocks_when_window_active(db, monkeypatch):
    a = _add(db)
    account = db.get_account_for_primer(a)
    sent = {'n': 0}

    monkeypatch.setattr(primer.auf, '_http_get_usage', lambda token: _sample(FUTURE))
    monkeypatch.setattr(primer.auf, 'send_haiku_primer',
                        lambda token, model=None: sent.__setitem__('n', sent['n'] + 1))

    result = primer.prime_account(db, account)
    assert result['started'] is False
    assert result['resets_at'] == '2999-01-01T00:00:00Z'
    assert sent['n'] == 0                  # no message sent when already active


def test_prime_retries_primer_after_401(db, monkeypatch):
    a = _add(db)
    account = db.get_account_for_primer(a)
    usage_n = {'n': 0}
    primer_n = {'n': 0}

    def usage(token):
        usage_n['n'] += 1
        return _sample(PAST) if usage_n['n'] == 1 else _sample(FUTURE)

    def send(token, model=None):
        primer_n['n'] += 1
        if primer_n['n'] == 1:
            raise primer.auf.UsageApiError('primer 401', status=401)
        return {'id': 'msg_1'}

    monkeypatch.setattr(primer.auf, '_http_get_usage', usage)
    monkeypatch.setattr(primer.auf, 'send_haiku_primer', send)
    monkeypatch.setattr(primer.auf, 'refresh_access_token',
                        lambda rt, now_ms=None: {'access_token': 'AT2',
                            'refresh_token': 'RT2', 'expires_at': FAR})

    result = primer.prime_account(db, account)
    assert result['started'] is True
    assert primer_n['n'] == 2                              # retried after refresh
    assert db.get_account_for_primer(a)['access_token'] == 'AT2'  # rotated token persisted


def test_prime_returns_started_even_if_postsend_read_fails(db, monkeypatch):
    a = _add(db)
    account = db.get_account_for_primer(a)
    usage_n = {'n': 0}

    def usage(token):
        usage_n['n'] += 1
        if usage_n['n'] == 1:
            return _sample(PAST)                         # guard: inactive -> proceed to send
        raise primer.auf.UsageApiError('post-send read boom')  # post-send GET fails

    monkeypatch.setattr(primer.auf, '_http_get_usage', usage)
    monkeypatch.setattr(primer.auf, 'send_haiku_primer', lambda token, model=None: {'id': 'm'})

    result = primer.prime_account(db, account)
    assert result['started'] is True       # send succeeded -> started, despite read failure
    assert result['resets_at'] is None     # no post-send usage to report
