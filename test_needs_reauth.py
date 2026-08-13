"""Grant-expiry handling: OAuth error body surfaced, needs_reauth auto-flag,
grant age tracking.

Background: Anthropic OAuth grants expire ~28 days after authorization
(HTTP 400 {"error": "invalid_grant", "error_description": "Refresh token
expired"}). Before this feature a dead account was retried every cron tick
forever (token-endpoint hammering, per-IP 429 risk) and the UI only said
"read error". Now: the error body reaches last_error, three consecutive
invalid_grant refresh failures pause polling via accounts.needs_reauth, and
authorized_at powers a grant-age warning in the UI. All offline — HTTP is
monkeypatched via the _urlopen seam.
"""
import io
import json
import os
import urllib.error
from datetime import datetime, timedelta, timezone

os.environ.setdefault('ALLOW_DEFAULT_CREDENTIALS', '1')

import pytest
from cryptography.fernet import Fernet

import api_usage_fetcher as auf
import collect_all
import config
import crypto_util
import database
from database import UsageDatabase

PROD_SAMPLE = {
    "five_hour": {"utilization": 10.0, "resets_at": "2026-06-11T08:40:00+00:00"},
    "seven_day": {"utilization": 58.0, "resets_at": "2026-06-15T11:00:00+00:00"},
}

FAR = 9_999_999_999_999


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'TOKEN_ENCRYPTION_KEY', Fernet.generate_key().decode())
    crypto_util._reset_cache()
    d = UsageDatabase(str(tmp_path / 'reauth.db'))
    yield d
    d.close()


def _add(db, email='a@x.com', expires_at=FAR):
    return db.add_or_update_account(
        label=email, email=email, account_type='team',
        access_token='AT', refresh_token='RT', expires_at=expires_at)


def _http_error(code, body: bytes):
    return urllib.error.HTTPError('https://x/token', code, 'Bad Request',
                                  {}, io.BytesIO(body))


# ---- 1) refresh_access_token surfaces the OAuth error body ----

def test_refresh_error_carries_oauth_error_and_description(monkeypatch):
    body = b'{"error": "invalid_grant", "error_description": "Refresh token expired"}'

    def boom(req, timeout=None):
        raise _http_error(400, body)

    monkeypatch.setattr(auf, '_urlopen', boom)
    with pytest.raises(auf.UsageApiError) as ei:
        auf.refresh_access_token('RT1')
    assert ei.value.status == 400
    assert ei.value.oauth_error == 'invalid_grant'
    assert 'Refresh token expired' in str(ei.value)


def test_refresh_error_nonjson_body_still_maps_status(monkeypatch):
    def boom(req, timeout=None):
        raise _http_error(403, b'<html>edge says no</html>')

    monkeypatch.setattr(auf, '_urlopen', boom)
    with pytest.raises(auf.UsageApiError) as ei:
        auf.refresh_access_token('RT1')
    assert ei.value.status == 403
    assert ei.value.oauth_error is None


def test_refresh_network_error_has_no_oauth_error(monkeypatch):
    def boom(req, timeout=None):
        raise TimeoutError('timed out')

    monkeypatch.setattr(auf, '_urlopen', boom)
    with pytest.raises(auf.UsageApiError) as ei:
        auf.refresh_access_token('RT1')
    assert ei.value.oauth_error is None


# ---- 2) DB: authorized_at + failure counter + needs_reauth ----

def test_add_account_sets_authorized_at(db):
    acc_id = _add(db)
    acc = db.list_accounts()[0]
    assert acc['id'] == acc_id
    assert acc['authorized_at'] is not None
    assert acc['auth_failures'] == 0
    assert acc['needs_reauth'] == 0


def test_reauth_upsert_refreshes_authorized_at_and_clears_flags(db):
    acc_id = _add(db)
    db.conn.execute(
        "UPDATE accounts SET authorized_at='2026-07-01 00:00:00+00:00',"
        " auth_failures=3, needs_reauth=1 WHERE id=?", (acc_id,))
    db.conn.commit()
    assert _add(db) == acc_id  # upsert by email, same row
    acc = db.list_accounts()[0]
    assert acc['needs_reauth'] == 0
    assert acc['auth_failures'] == 0
    assert acc['authorized_at'] > '2026-07-01'  # refreshed to now


def test_record_auth_failure_trips_needs_reauth_at_threshold(db):
    acc_id = _add(db)
    assert db.record_auth_failure(acc_id) is False
    assert db.record_auth_failure(acc_id) is False
    assert db.record_auth_failure(acc_id) is True
    acc = db.list_accounts()[0]
    assert acc['needs_reauth'] == 1
    assert acc['auth_failures'] == 3


def test_successful_poll_resets_failure_counter(db):
    acc_id = _add(db)
    db.record_auth_failure(acc_id)
    db.record_auth_failure(acc_id)
    db.record_account_poll(acc_id, error=None)
    assert db.list_accounts()[0]['auth_failures'] == 0
    db.record_auth_failure(acc_id)
    db.record_auth_failure(acc_id)
    assert db.list_accounts()[0]['needs_reauth'] == 0  # streak broken, not tripped


def test_failed_poll_does_not_reset_counter(db):
    acc_id = _add(db)
    db.record_auth_failure(acc_id)
    db.record_account_poll(acc_id, error='boom')
    assert db.list_accounts()[0]['auth_failures'] == 1


def test_pollable_accounts_exclude_needs_reauth(db):
    _add(db, 'dead@x.com')
    alive = _add(db, 'alive@x.com')
    for _ in range(database.REAUTH_FAILURE_THRESHOLD):
        db.record_auth_failure(db.list_accounts()[0]['id'])
    pollable = db.get_pollable_accounts()
    assert [a['id'] for a in pollable] == [alive]


def test_migration_adds_columns_to_legacy_db(tmp_path, monkeypatch):
    """A pre-feature DB (no authorized_at/auth_failures/needs_reauth) must
    migrate on open and keep its rows pollable (needs_reauth defaults to 0)."""
    import sqlite3
    path = str(tmp_path / 'legacy.db')
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL,
            email TEXT, account_type TEXT, access_token TEXT NOT NULL,
            refresh_token TEXT NOT NULL, expires_at INTEGER NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1, created_at DATETIME,
            last_polled_at DATETIME, last_error TEXT)
    """)
    conn.execute("INSERT INTO accounts (label, email, access_token, refresh_token,"
                 " expires_at) VALUES ('L', 'l@x.com', 'x', 'y', 1)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, 'TOKEN_ENCRYPTION_KEY', Fernet.generate_key().decode())
    crypto_util._reset_cache()
    d = UsageDatabase(path)
    acc = d.list_accounts()[0]
    assert acc['needs_reauth'] == 0
    assert acc['auth_failures'] == 0
    assert acc['authorized_at'] is None  # unknown for legacy rows
    d.close()


# ---- 3) collector: invalid_grant streak pauses polling ----

def _raise_invalid_grant(rt, now_ms=None):
    raise auf.UsageApiError('token refresh failed: HTTP 400: Refresh token expired',
                            status=400, oauth_error='invalid_grant')


def test_three_invalid_grant_runs_flag_account_and_pause_polling(db, tmp_path, monkeypatch):
    _add(db, expires_at=0)  # forces proactive refresh on every poll
    monkeypatch.setattr(collect_all.auf, 'refresh_access_token', _raise_invalid_grant)
    monkeypatch.setattr(collect_all, 'DATA_DIR', str(tmp_path / 'data'))
    assert collect_all.run(db) == 2
    assert collect_all.run(db) == 2
    assert collect_all.run(db) == 2
    acc = db.list_accounts()[0]
    assert acc['needs_reauth'] == 1
    assert 'Refresh token expired' in acc['last_error']
    assert collect_all.run(db) == 1  # nothing pollable anymore — hammering stops


def test_non_invalid_grant_failures_never_flag(db, tmp_path, monkeypatch):
    _add(db, expires_at=FAR)

    def usage_403(token):
        raise auf.UsageApiError('usage request failed: HTTP 403', status=403)

    monkeypatch.setattr(collect_all.auf, '_http_get_usage', usage_403)
    monkeypatch.setattr(collect_all, 'DATA_DIR', str(tmp_path / 'data'))
    for _ in range(4):
        assert collect_all.run(db) == 2
    assert db.list_accounts()[0]['needs_reauth'] == 0


def test_successful_poll_between_failures_breaks_streak(db, tmp_path, monkeypatch):
    _add(db, expires_at=0)
    calls = {'n': 0}

    def flaky_refresh(rt, now_ms=None):
        calls['n'] += 1
        if calls['n'] == 3:
            return {'access_token': 'AT2', 'refresh_token': 'RT2', 'expires_at': 0}
        _raise_invalid_grant(rt)

    monkeypatch.setattr(collect_all.auf, 'refresh_access_token', flaky_refresh)
    monkeypatch.setattr(collect_all.auf, '_http_get_usage', lambda token: PROD_SAMPLE)
    monkeypatch.setattr(collect_all, 'DATA_DIR', str(tmp_path / 'data'))
    for _ in range(5):
        collect_all.run(db)
    # failures: runs 1,2 then success on 3 resets, then 4,5 fail → streak of 2
    assert db.list_accounts()[0]['needs_reauth'] == 0


# ---- 4) UI surface: /api/accounts fields + template badges ----

@pytest.fixture
def client(db, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, '_db', db)
    c = app_module.app.test_client()
    with c.session_transaction() as sess:
        sess['logged_in'] = True
    return c


def test_api_accounts_exposes_reauth_and_grant_age(db, client):
    acc_id = _add(db)
    old = (datetime.now(timezone.utc) - timedelta(days=26)).strftime(
        '%Y-%m-%d %H:%M:%S+00:00')
    db.conn.execute("UPDATE accounts SET authorized_at=?, needs_reauth=1 WHERE id=?",
                    (old, acc_id))
    db.conn.commit()
    data = client.get('/api/accounts').get_json()
    assert data[0]['needs_reauth'] == 1
    assert data[0]['grant_age_days'] == 26


def test_api_accounts_grant_age_none_for_legacy_rows(db, client):
    acc_id = _add(db)
    db.conn.execute("UPDATE accounts SET authorized_at=NULL WHERE id=?", (acc_id,))
    db.conn.commit()
    data = client.get('/api/accounts').get_json()
    assert data[0]['grant_age_days'] is None


def test_accounts_page_shows_reauth_badge_error_text_and_age_warning(db, client):
    acc_id = _add(db)
    old = (datetime.now(timezone.utc) - timedelta(days=26)).strftime(
        '%Y-%m-%d %H:%M:%S+00:00')
    db.conn.execute(
        "UPDATE accounts SET authorized_at=?, needs_reauth=1,"
        " last_error='token refresh failed: HTTP 400: Refresh token expired'"
        " WHERE id=?", (old, acc_id))
    db.conn.commit()
    html = client.get('/accounts').get_data(as_text=True)
    assert 'Re-auth required' in html
    assert 'Refresh token expired' in html   # actual error text, not just an icon
    assert 'acc-age warn' in html            # age >= 25d renders the warning class


def test_accounts_page_young_grant_has_no_warning(db, client):
    _add(db)  # authorized_at = now → age 0
    html = client.get('/accounts').get_data(as_text=True)
    assert 'acc-age warn' not in html
    assert 'Re-auth required' not in html
