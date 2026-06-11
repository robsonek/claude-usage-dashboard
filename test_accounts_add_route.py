"""Audit points 4 & 8: the OAuth add-account 'complete' step must require a
matching state (CSRF/code-injection defence) and a non-empty profile email
(otherwise repeated re-auth silently mints duplicate accounts).
"""
import os

os.environ.setdefault('ALLOW_DEFAULT_CREDENTIALS', '1')

import pytest
from cryptography.fernet import Fernet

import app as app_module
import config
import crypto_util
import oauth_flow
from database import UsageDatabase


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'TOKEN_ENCRYPTION_KEY', Fernet.generate_key().decode())
    crypto_util._reset_cache()
    db = UsageDatabase(str(tmp_path / 'route.db'))
    monkeypatch.setattr(app_module, '_db', db)  # isolate from the real usage.db
    app_module.app.config.update(TESTING=True)
    c = app_module.app.test_client()
    with c.session_transaction() as sess:
        sess['logged_in'] = True
        sess['oauth_verifier'] = 'verifier-xyz'
        sess['oauth_state'] = 'STATE123'
    yield c
    db.close()


def _stub_oauth(monkeypatch, email='new@x.com', account_type='max'):
    monkeypatch.setattr(oauth_flow, 'exchange_code',
                        lambda code, verifier, now_ms=None: {
                            'access_token': 'AT', 'refresh_token': 'RT',
                            'expires_at': 9_999_999_999_999})
    monkeypatch.setattr(oauth_flow, 'fetch_profile',
                        lambda at: {'email': email, 'account_type': account_type})


def test_complete_rejects_bare_code_without_state(client, monkeypatch):
    _stub_oauth(monkeypatch)
    r = client.post('/accounts/add', data={'action': 'complete', 'code': 'rawcode'})
    assert r.status_code == 400
    assert 'state' in r.get_json()['error'].lower()
    assert app_module._db.list_accounts() == []  # nothing created


def test_complete_rejects_mismatched_state(client, monkeypatch):
    _stub_oauth(monkeypatch)
    r = client.post('/accounts/add', data={'action': 'complete', 'code': 'rawcode#WRONG'})
    assert r.status_code == 400
    assert 'state' in r.get_json()['error'].lower()
    assert app_module._db.list_accounts() == []


def test_complete_requires_email(client, monkeypatch):
    _stub_oauth(monkeypatch, email=None)
    r = client.post('/accounts/add', data={'action': 'complete', 'code': 'rawcode#STATE123'})
    assert r.status_code == 400
    assert 'mail' in r.get_json()['error'].lower()
    assert app_module._db.list_accounts() == []  # no email-less duplicate row


def test_complete_happy_path_creates_account(client, monkeypatch):
    """Matching state + a real email still succeeds — the guards don't break the
    normal add flow."""
    _stub_oauth(monkeypatch, email='ok@x.com')
    r = client.post('/accounts/add', data={'action': 'complete', 'code': 'rawcode#STATE123'})
    assert r.status_code == 200
    body = r.get_json()
    assert body['email'] == 'ok@x.com'
    accts = app_module._db.list_accounts()
    assert len(accts) == 1 and accts[0]['email'] == 'ok@x.com'
