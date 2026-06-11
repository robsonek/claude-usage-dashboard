"""Audit point 6: snapshot timestamps must be ISO-8601 with 'T' + 'Z'.

The SQLite datetime adapter stores captured_at as 'YYYY-MM-DD HH:MM:SS+00:00'
(space separator, numeric offset). _format_timestamp used to hand that back
verbatim, and the dashboard parses it with `new Date(...)`. A space-separated
datetime is outside the ECMAScript Date.parse spec — Safari/iOS returns
Invalid Date, blanking the charts and freshness indicator. Both the history
and current-status read paths must normalize to the same ISO form _format_iso_z
already produces for resets_at.
"""
import re
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

import config
import crypto_util
from database import UsageDatabase

_ISO_Z = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'TOKEN_ENCRYPTION_KEY', Fernet.generate_key().decode())
    crypto_util._reset_cache()
    d = UsageDatabase(str(tmp_path / 'ts.db'))
    yield d
    d.close()


def _snap(captured):
    res = captured + timedelta(days=4)
    return {
        'account_type': 'max', 'email': 'a@x.com',
        'captured_at': captured.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'quotas': [{'type': 'weekly', 'percent_remaining': 50,
                    'resets_at': res.strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'time_remaining_seconds': 100}],
    }


def test_get_history_timestamp_is_iso_with_t_and_z(db):
    db.insert_snapshot(_snap(datetime.now(timezone.utc)))
    ts = db.get_history()[0]['timestamp']
    assert _ISO_Z.match(ts), f'not ISO-Z: {ts!r}'
    assert ' ' not in ts


def test_get_current_timestamp_is_iso_with_t_and_z(db):
    db.insert_snapshot(_snap(datetime.now(timezone.utc)))
    ts = db.get_current()['timestamp']
    assert _ISO_Z.match(ts), f'not ISO-Z: {ts!r}'
    assert ' ' not in ts
