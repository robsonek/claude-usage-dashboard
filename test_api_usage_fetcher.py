"""Tests for api_usage_fetcher: mapping, token refresh, snapshot assembly.

All offline — HTTP is monkeypatched via api_usage_fetcher._urlopen.
"""
import json
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


# ---- refactored multi-account helpers ----

def test_needs_refresh_ms_margin():
    assert auf.needs_refresh_ms(1_000_000, now_ms=1_000_000 - 60_000) is True   # 60s < 2min
    assert auf.needs_refresh_ms(1_000_000, now_ms=1_000_000 - 600_000) is False  # 10min left


def test_refresh_access_token_returns_new_tokens(monkeypatch):
    monkeypatch.setattr(auf, '_urlopen',
                        lambda req, timeout=None: FakeResponse(
                            {'access_token': 'AT2', 'refresh_token': 'RT2', 'expires_in': 28800}))
    out = auf.refresh_access_token('RT1', now_ms=1_000_000)
    assert out == {'access_token': 'AT2', 'refresh_token': 'RT2',
                   'expires_at': 1_000_000 + 28800 * 1000}


def test_refresh_access_token_keeps_old_refresh_if_absent(monkeypatch):
    monkeypatch.setattr(auf, '_urlopen',
                        lambda req, timeout=None: FakeResponse(
                            {'access_token': 'AT2', 'expires_in': 100}))
    out = auf.refresh_access_token('RT1', now_ms=0)
    assert out['refresh_token'] == 'RT1'  # rotation optional


def test_build_snapshot_from_account_and_api():
    account = {'id': 7, 'email': 'a@x.com', 'account_type': 'max'}
    snap = auf.build_snapshot(PROD_SAMPLE, account, now=NOW)
    assert snap['account_id'] == 7
    assert snap['email'] == 'a@x.com'
    assert snap['account_type'] == 'max'
    assert snap['source'] == 'api'
    assert len(snap['quotas']) == 3
    datetime.strptime(snap['captured_at'], '%Y-%m-%dT%H:%M:%SZ')


def test_build_snapshot_incomplete_raises():
    with pytest.raises(auf.UsageApiError):
        auf.build_snapshot({'five_hour': None, 'seven_day': None},
                           {'id': 1, 'email': 'a@x.com', 'account_type': 'max'}, now=NOW)


# ---- HTTP stubbing helper ----

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


def test_send_haiku_primer_posts_hi_with_oauth_headers(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured['url'] = req.full_url
        captured['method'] = req.get_method()
        captured['body'] = json.loads(req.data.decode())
        captured['auth'] = req.get_header('Authorization')
        captured['beta'] = req.get_header('Anthropic-beta')   # urllib title-cases keys
        captured['ua'] = req.get_header('User-agent')
        return FakeResponse({'id': 'msg_1', 'role': 'assistant'})

    monkeypatch.setattr(auf, '_urlopen', fake_urlopen)
    out = auf.send_haiku_primer('TOK')
    assert out['id'] == 'msg_1'
    assert captured['method'] == 'POST'
    assert captured['url'] == auf.MESSAGES_URL
    assert captured['body']['model'] == auf.PRIMER_MODEL
    assert captured['body']['messages'][0]['content'] == 'Hi'
    assert captured['body']['system'] == auf.PRIMER_SYSTEM
    assert captured['auth'] == 'Bearer TOK'
    assert captured['beta'] == 'oauth-2025-04-20'
    assert captured['ua'].startswith('claude-code/')


def test_send_haiku_primer_maps_http_error_status(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(auf.MESSAGES_URL, 403, 'Forbidden', {}, None)

    monkeypatch.setattr(auf, '_urlopen', boom)
    with pytest.raises(auf.UsageApiError) as ei:
        auf.send_haiku_primer('TOK')
    assert ei.value.status == 403
