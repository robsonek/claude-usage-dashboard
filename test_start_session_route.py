"""Route tests for the per-account 'Start 5h' op (accounts_op -> start_session)."""
import os

os.environ.setdefault('ALLOW_DEFAULT_CREDENTIALS', '1')

import pytest
from cryptography.fernet import Fernet

import app as app_module
import config
import crypto_util
import primer
import api_usage_fetcher as auf
from database import UsageDatabase

FAR = 9_999_999_999_999


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'TOKEN_ENCRYPTION_KEY', Fernet.generate_key().decode())
    crypto_util._reset_cache()
    db = UsageDatabase(str(tmp_path / 'route.db'))
    monkeypatch.setattr(app_module, '_db', db)
    app_module.app.config.update(TESTING=True)
    c = app_module.app.test_client()
    with c.session_transaction() as sess:
        sess['logged_in'] = True
    yield c
    db.close()


def _add(db):
    return db.add_or_update_account(label='a', email='a@x.com', account_type='max',
                                    access_token='AT', refresh_token='RT', expires_at=FAR)


def test_start_session_started(client, monkeypatch):
    a = _add(app_module._db)
    monkeypatch.setattr(primer, 'prime_account',
                        lambda db, account: {'started': True, 'resets_at': '2999-01-01T00:00:00Z'})
    r = client.post(f'/accounts/{a}/start_session')
    assert r.status_code == 200
    body = r.get_json()
    assert body['ok'] is True and body['started'] is True
    assert body['resets_at'].startswith('2999')


def test_start_session_already_active(client, monkeypatch):
    a = _add(app_module._db)
    monkeypatch.setattr(primer, 'prime_account',
                        lambda db, account: {'started': False, 'resets_at': '2999-01-01T00:00:00Z'})
    r = client.post(f'/accounts/{a}/start_session')
    assert r.status_code == 200
    assert r.get_json()['started'] is False


def test_start_session_unknown_account_404(client):
    r = client.post('/accounts/999999/start_session')
    assert r.status_code == 404


def test_start_session_api_error_maps_502(client, monkeypatch):
    a = _add(app_module._db)

    def boom(db, account):
        raise auf.UsageApiError('primer failed', status=403)

    monkeypatch.setattr(primer, 'prime_account', boom)
    r = client.post(f'/accounts/{a}/start_session')
    assert r.status_code == 502
    assert 'error' in r.get_json()


def test_start_session_requires_login(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'TOKEN_ENCRYPTION_KEY', Fernet.generate_key().decode())
    crypto_util._reset_cache()
    db = UsageDatabase(str(tmp_path / 'noauth.db'))
    monkeypatch.setattr(app_module, '_db', db)
    c = app_module.app.test_client()           # no logged_in session
    r = c.post('/accounts/1/start_session')
    assert r.status_code in (302, 401)         # redirected to /login by @login_required
    db.close()
