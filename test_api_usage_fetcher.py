"""Tests for api_usage_fetcher: mapping, credentials, refresh, CLI contract.

All offline — HTTP is monkeypatched via api_usage_fetcher._urlopen.
"""
import json
import os
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
