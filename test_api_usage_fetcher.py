"""Tests for api_usage_fetcher: mapping, credentials, refresh, CLI contract.

All offline — HTTP is monkeypatched via api_usage_fetcher._urlopen.
"""
import json
import os
import time
import urllib.error
from datetime import datetime, timezone

import pytest

import api_usage_fetcher as auf

# Real prod response captured 2026-06-11 (account: Max)
PROD_SAMPLE = {
    "five_hour": {"utilization": 0.0, "resets_at": "2026-06-11T08:40:00.235311+00:00"},
    "seven_day": {"utilization": 58.0, "resets_at": "2026-06-15T10:59:59.235340+00:00"},
    "seven_day_oauth_apps": None,
    "seven_day_opus": None,
    "seven_day_sonnet": {"utilization": 0.0, "resets_at": "2026-06-15T11:00:00.235353+00:00"},
    "seven_day_cowork": None,
    "seven_day_omelette": None,
    "tangelo": None,
    "iguana_necktie": None,
    "omelette_promotional": None,
    "cinder_cove": None,
    "extra_usage": {"is_enabled": True, "monthly_limit": 20000, "used_credits": 5353.0,
                    "utilization": 26.765, "currency": "USD", "disabled_reason": None},
}

NOW = datetime(2026, 6, 11, 6, 0, 0, tzinfo=timezone.utc)


def test_maps_three_quotas_from_prod_sample():
    quotas = auf.map_usage_response(PROD_SAMPLE, now=NOW)
    by_type = {(q['type'], q.get('model', '')): q for q in quotas}
    assert set(by_type) == {('session', ''), ('weekly', ''), ('model_specific', 'sonnet')}


def test_percent_remaining_is_100_minus_utilization():
    quotas = auf.map_usage_response(PROD_SAMPLE, now=NOW)
    by_type = {(q['type'], q.get('model', '')): q for q in quotas}
    assert by_type[('session', '')]['percent_remaining'] == 100.0
    assert by_type[('weekly', '')]['percent_remaining'] == 42.0


def test_resets_at_normalized_and_time_remaining_computed():
    quotas = auf.map_usage_response(PROD_SAMPLE, now=NOW)
    session = next(q for q in quotas if q['type'] == 'session')
    assert session['resets_at'] == '2026-06-11T08:40:00Z'
    assert session['time_remaining_seconds'] == 2 * 3600 + 40 * 60


def test_opus_window_used_when_sonnet_null():
    sample = dict(PROD_SAMPLE, seven_day_sonnet=None,
                  seven_day_opus={"utilization": 12.5, "resets_at": "2026-06-15T11:00:00+00:00"})
    quotas = auf.map_usage_response(sample, now=NOW)
    model = next(q for q in quotas if q['type'] == 'model_specific')
    assert model['model'] == 'opus'
    assert model['percent_remaining'] == 87.5


def test_missing_model_window_yields_no_model_quota():
    sample = dict(PROD_SAMPLE, seven_day_sonnet=None, seven_day_opus=None)
    quotas = auf.map_usage_response(sample, now=NOW)
    assert all(q['type'] != 'model_specific' for q in quotas)


def test_unknown_and_null_windows_ignored():
    sample = dict(PROD_SAMPLE)
    sample['brand_new_window'] = {"utilization": 99.0, "resets_at": "2026-06-15T11:00:00+00:00"}
    quotas = auf.map_usage_response(sample, now=NOW)
    assert len(quotas) == 3  # session, weekly, model_specific — nothing extra


def test_garbled_resets_at_drops_reset_but_keeps_percent():
    sample = dict(PROD_SAMPLE,
                  five_hour={"utilization": 10.0, "resets_at": "not-a-date"})
    quotas = auf.map_usage_response(sample, now=NOW)
    session = next(q for q in quotas if q['type'] == 'session')
    assert session['percent_remaining'] == 90.0
    assert 'resets_at' not in session
    assert 'time_remaining_seconds' not in session


def test_reset_in_past_clamps_time_remaining_to_zero():
    sample = dict(PROD_SAMPLE,
                  five_hour={"utilization": 10.0, "resets_at": "2026-06-11T05:59:00+00:00"})
    quotas = auf.map_usage_response(sample, now=NOW)
    session = next(q for q in quotas if q['type'] == 'session')
    assert session['time_remaining_seconds'] == 0


# ---- credentials & refresh decision ----

CREDS = {
    "claudeAiOauth": {
        "accessToken": "sk-ant-oat01-AAA",
        "refreshToken": "sk-ant-ort01-BBB",
        "expiresAt": 1780000000000,
        "scopes": ["user:inference", "user:profile"],
        "subscriptionType": "max",
        "rateLimitTier": "default",
    },
    "someOtherTopLevelKey": {"keep": "me"},
}


@pytest.fixture
def creds_file(tmp_path, monkeypatch):
    path = tmp_path / '.credentials.json'
    path.write_text(json.dumps(CREDS))
    monkeypatch.setattr(auf, 'CREDENTIALS_FILE', str(path))
    return path


def test_load_credentials_reads_oauth_section(creds_file):
    creds = auf.load_credentials()
    assert creds['claudeAiOauth']['accessToken'] == 'sk-ant-oat01-AAA'


def test_load_credentials_missing_file_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(auf, 'CREDENTIALS_FILE', str(tmp_path / 'nope.json'))
    with pytest.raises(auf.UsageApiError):
        auf.load_credentials()


def test_needs_refresh_false_for_fresh_token():
    now_ms = 1780000000000 - 3_600_000  # an hour before expiry
    assert auf.needs_refresh(CREDS['claudeAiOauth'], now_ms=now_ms) is False


def test_needs_refresh_true_within_margin_and_when_missing():
    near = 1780000000000 - 60_000  # 60s left < 120s margin
    assert auf.needs_refresh(CREDS['claudeAiOauth'], now_ms=near) is True
    assert auf.needs_refresh({'accessToken': 'x'}, now_ms=0) is True


# ---- token refresh + atomic write-back ----

class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_refresh_updates_tokens_and_preserves_other_fields(creds_file, monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured['url'] = req.full_url
        captured['body'] = json.loads(req.data.decode())
        return FakeResponse({'access_token': 'sk-ant-oat01-NEW',
                             'refresh_token': 'sk-ant-ort01-NEW',
                             'expires_in': 28800})

    monkeypatch.setattr(auf, '_urlopen', fake_urlopen)
    creds = auf.load_credentials()
    oauth = auf.refresh_credentials(creds, now_ms=1_000_000)

    assert captured['url'] == auf.TOKEN_URL
    assert captured['body']['grant_type'] == 'refresh_token'
    assert captured['body']['refresh_token'] == 'sk-ant-ort01-BBB'
    assert captured['body']['client_id'] == auf.CLIENT_ID

    assert oauth['accessToken'] == 'sk-ant-oat01-NEW'
    on_disk = json.loads(creds_file.read_text())
    assert on_disk['claudeAiOauth']['accessToken'] == 'sk-ant-oat01-NEW'
    assert on_disk['claudeAiOauth']['refreshToken'] == 'sk-ant-ort01-NEW'
    assert on_disk['claudeAiOauth']['expiresAt'] == 1_000_000 + 28800 * 1000
    # untouched fields survive the rewrite
    assert on_disk['someOtherTopLevelKey'] == {'keep': 'me'}
    assert on_disk['claudeAiOauth']['subscriptionType'] == 'max'
    assert oct(os.stat(creds_file).st_mode & 0o777) == oct(0o600)


def test_refresh_failure_raises_and_leaves_file_untouched(creds_file, monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 400, 'Bad Request', None, None)

    monkeypatch.setattr(auf, '_urlopen', fake_urlopen)
    creds = auf.load_credentials()
    before = creds_file.read_text()
    with pytest.raises(auf.UsageApiError):
        auf.refresh_credentials(creds)
    assert creds_file.read_text() == before


# ---- fetch_usage: full API path ----

@pytest.fixture
def claude_config(tmp_path, monkeypatch):
    cfg = tmp_path / '.claude.json'
    cfg.write_text(json.dumps({'oauthAccount': {'emailAddress': 'rob@example.com'}}))
    monkeypatch.setattr(auf, 'CLAUDE_CONFIG_FILE', str(cfg))
    return cfg


def _fresh_creds(creds_path):
    """Make the stored token look fresh so fetch_usage skips the refresh path."""
    data = json.loads(creds_path.read_text())
    data['claudeAiOauth']['expiresAt'] = int(time.time() * 1000) + 3_600_000
    creds_path.write_text(json.dumps(data))


def test_fetch_usage_returns_snapshot_format(creds_file, claude_config, monkeypatch):
    _fresh_creds(creds_file)
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured['url'] = req.full_url
        captured['auth'] = req.get_header('Authorization')
        captured['beta'] = req.get_header('Anthropic-beta')
        return FakeResponse(PROD_SAMPLE)

    monkeypatch.setattr(auf, '_urlopen', fake_urlopen)
    result = auf.fetch_usage()

    assert captured['url'] == auf.USAGE_URL
    assert captured['auth'] == 'Bearer sk-ant-oat01-AAA'
    assert captured['beta'] == 'oauth-2025-04-20'
    assert result['account_type'] == 'max'
    assert result['email'] == 'rob@example.com'
    assert result['source'] == 'api'
    assert len(result['quotas']) == 3
    # captured_at parses in insert_to_db's expected shape
    datetime.strptime(result['captured_at'], '%Y-%m-%dT%H:%M:%SZ')


def test_fetch_usage_missing_core_windows_raises(creds_file, claude_config, monkeypatch):
    _fresh_creds(creds_file)
    monkeypatch.setattr(auf, '_urlopen',
                        lambda req, timeout=None: FakeResponse({'five_hour': None,
                                                                'seven_day': None}))
    with pytest.raises(auf.UsageApiError):
        auf.fetch_usage()


def test_email_none_when_config_missing(creds_file, tmp_path, monkeypatch):
    _fresh_creds(creds_file)
    monkeypatch.setattr(auf, 'CLAUDE_CONFIG_FILE', str(tmp_path / 'absent.json'))
    monkeypatch.setattr(auf, '_urlopen',
                        lambda req, timeout=None: FakeResponse(PROD_SAMPLE))
    result = auf.fetch_usage()
    assert result['email'] is None
