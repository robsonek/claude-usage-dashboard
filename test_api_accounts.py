"""Audit point 9b: /api/accounts recomputes 2 predictions per account on every
call (heartbeat-driven). It fetched the full 168h of history even though the
prediction only looks at the last 24h. Bounding the per-account history to ~25h
is a behaviour-preserving optimization — this characterization test guards the
response contract across that change.
"""
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault('ALLOW_DEFAULT_CREDENTIALS', '1')

import pytest
from cryptography.fernet import Fernet

import app as app_module
import config
import crypto_util
from database import UsageDatabase


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'TOKEN_ENCRYPTION_KEY', Fernet.generate_key().decode())
    crypto_util._reset_cache()
    db = UsageDatabase(str(tmp_path / 'apiacc.db'))
    monkeypatch.setattr(app_module, '_db', db)
    c = app_module.app.test_client()
    with c.session_transaction() as sess:
        sess['logged_in'] = True
    yield c, db
    db.close()


def _weekly_snap(acc, pct, captured):
    res = captured + timedelta(days=4)
    return {
        'account_id': acc, 'account_type': 'max', 'email': 'a@x.com',
        'captured_at': captured.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'quotas': [{'type': 'weekly', 'percent_remaining': pct,
                    'resets_at': res.strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'time_remaining_seconds': 100},
                   {'type': 'session', 'percent_remaining': pct,
                    'resets_at': (captured + timedelta(hours=3)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'time_remaining_seconds': 100}],
    }


def test_api_accounts_reports_latest_remaining_and_flags(client):
    c, db = client
    acc = db.add_or_update_account(label='A', email='a@x.com', account_type='max',
                                   access_token='AT', refresh_token='RT', expires_at=1)
    now = datetime.now(timezone.utc)
    for i, pct in enumerate((50, 45, 40)):
        db.insert_snapshot(_weekly_snap(acc, pct, now - timedelta(minutes=20 - i * 5)))

    r = c.get('/api/accounts')
    assert r.status_code == 200
    data = r.get_json()
    assert isinstance(data, list) and len(data) == 1
    item = data[0]
    assert item['email'] == 'a@x.com'
    assert item['weekly_remaining'] == 40        # latest reading
    assert item['session_remaining'] == 40
    assert isinstance(item['weekly_will_exceed'], bool)
    assert isinstance(item['session_will_exceed'], bool)
