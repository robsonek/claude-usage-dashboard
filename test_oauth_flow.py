"""Tests for oauth_flow: PKCE, authorize URL, code exchange, profile fetch.
HTTP mocked via oauth_flow._urlopen."""
import base64
import hashlib
import json
from urllib.parse import urlparse, parse_qs

import pytest

import oauth_flow as of


class FakeResponse:
    def __init__(self, payload):
        self._p = payload
    def read(self):
        return json.dumps(self._p).encode()
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_generate_pkce_challenge_matches_verifier():
    pkce = of.generate_pkce()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(pkce['verifier'].encode()).digest()).rstrip(b'=').decode()
    assert pkce['challenge'] == expected


def test_authorize_url_has_required_params():
    url = of.build_authorize_url('CHAL', 'STATE')
    q = parse_qs(urlparse(url).query)
    assert q['client_id'] == [of.CLIENT_ID]
    assert q['code_challenge'] == ['CHAL']
    assert q['code_challenge_method'] == ['S256']
    assert q['state'] == ['STATE']
    assert q['redirect_uri'] == [of.REDIRECT_URI]
    assert q['response_type'] == ['code']


def test_exchange_code_splits_state_and_posts(monkeypatch):
    captured = {}
    def fake_urlopen(req, timeout=None):
        captured['url'] = req.full_url
        captured['body'] = json.loads(req.data.decode())
        return FakeResponse({'access_token': 'AT', 'refresh_token': 'RT',
                             'expires_in': 28800})
    monkeypatch.setattr(of, '_urlopen', fake_urlopen)
    result = of.exchange_code('CODE123#STATE456', 'VERIFIER', now_ms=1_000_000)
    assert captured['url'] == of.TOKEN_URL
    assert captured['body']['code'] == 'CODE123'
    assert captured['body']['state'] == 'STATE456'
    assert captured['body']['grant_type'] == 'authorization_code'
    assert captured['body']['code_verifier'] == 'VERIFIER'
    assert captured['body']['redirect_uri'] == of.REDIRECT_URI
    assert result == {'access_token': 'AT', 'refresh_token': 'RT',
                      'expires_at': 1_000_000 + 28800 * 1000}


def test_exchange_code_without_state(monkeypatch):
    monkeypatch.setattr(of, '_urlopen',
                        lambda req, timeout=None: FakeResponse(
                            {'access_token': 'AT', 'refresh_token': 'RT', 'expires_in': 1}))
    result = of.exchange_code('BARECODE', 'V', now_ms=0)
    assert result['access_token'] == 'AT'


def test_exchange_code_error_raises(monkeypatch):
    import urllib.error
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 400, 'Bad', None, None)
    monkeypatch.setattr(of, '_urlopen', boom)
    with pytest.raises(of.OAuthError):
        of.exchange_code('C#S', 'V')


def test_fetch_profile_maps_account_type(monkeypatch):
    monkeypatch.setattr(of, '_urlopen', lambda req, timeout=None: FakeResponse(
        {'account': {'email': 'me@x.com', 'has_claude_max': True, 'has_claude_pro': False}}))
    prof = of.fetch_profile('AT')
    assert prof == {'email': 'me@x.com', 'account_type': 'max'}


def test_fetch_profile_pro_and_unknown(monkeypatch):
    monkeypatch.setattr(of, '_urlopen', lambda req, timeout=None: FakeResponse(
        {'account': {'email': 'p@x.com', 'has_claude_max': False, 'has_claude_pro': True}}))
    assert of.fetch_profile('AT')['account_type'] == 'pro'
    monkeypatch.setattr(of, '_urlopen', lambda req, timeout=None: FakeResponse(
        {'account': {'email': 'u@x.com', 'has_claude_max': False, 'has_claude_pro': False}}))
    assert of.fetch_profile('AT')['account_type'] == 'unknown'
