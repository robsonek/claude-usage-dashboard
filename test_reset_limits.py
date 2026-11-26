"""Tests for the rate-limit reset (juniper_tide) support.

Layer 1: api_usage_fetcher — at_wall usage GET, juniper_tide status mapping,
reset_rate_limits claim POST. All offline — HTTP is monkeypatched via
api_usage_fetcher._urlopen (same convention as test_api_usage_fetcher.py).
"""
import json
import urllib.error

import pytest

import api_usage_fetcher as auf


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


# ---- _http_get_usage at_wall variant ----

def test_get_usage_at_wall_appends_query(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured['url'] = req.full_url
        return FakeResponse({'five_hour': None})

    monkeypatch.setattr(auf, '_urlopen', fake_urlopen)
    auf._http_get_usage('TOK', at_wall=True)
    assert captured['url'] == auf.USAGE_URL + '?at_wall=1&skip_spend=1'


def test_get_usage_sends_cli_surface_user_agent(monkeypatch):
    # Live finding (2026-11-20): the server derives the client "surface" from
    # the UA. `claude-code/<ver>` → cedar_ember ineligible_reason "surface";
    # `claude-cli/<ver> (external, cli)` → grants returned (Team included).
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured['ua'] = req.get_header('User-agent')
        return FakeResponse({})

    monkeypatch.setattr(auf, '_urlopen', fake_urlopen)
    auf._http_get_usage('TOK', at_wall=True)
    assert captured['ua'] == f'claude-cli/{auf.CLI_VERSION} (external, cli)'
    assert auf.CLI_SURFACE_UA == captured['ua']


def test_get_usage_default_url_unchanged(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured['url'] = req.full_url
        return FakeResponse({'five_hour': None})

    monkeypatch.setattr(auf, '_urlopen', fake_urlopen)
    auf._http_get_usage('TOK')
    assert captured['url'] == auf.USAGE_URL


# ---- map_reset_status ----

def test_map_reset_status_available():
    api = {'juniper_tide': {
        'eligible': True, 'ineligible_reason': None, 'available': True,
        'next_available_at': None}}
    st = auf.map_reset_status(api)
    assert st == {'eligible': True, 'available': True,
                  'next_available_at': None, 'ineligible_reason': None}


def test_map_reset_status_unavailable_with_next():
    api = {'juniper_tide': {
        'eligible': True, 'available': False,
        'next_available_at': '2026-11-22T10:00:00Z'}}
    st = auf.map_reset_status(api)
    assert st['available'] is False
    assert st['next_available_at'] == '2026-11-22T10:00:00Z'


def test_map_reset_status_absent_field_returns_none():
    assert auf.map_reset_status({'five_hour': {}}) is None


def test_map_reset_status_malformed_returns_none():
    # Experimental API: a non-object or fieldless block must degrade to None,
    # never raise into the collector.
    assert auf.map_reset_status({'juniper_tide': 'nope'}) is None
    assert auf.map_reset_status({'juniper_tide': None}) is None


# ---- claim_rate_limit_reset ----

def test_claim_posts_program_with_oauth_headers(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured['url'] = req.full_url
        captured['method'] = req.get_method()
        captured['body'] = json.loads(req.data.decode())
        captured['auth'] = req.get_header('Authorization')
        captured['beta'] = req.get_header('Anthropic-beta')
        captured['ua'] = req.get_header('User-agent')
        return FakeResponse({'result': 'ok',
                             'next_available_at': '2026-11-27T10:00:00Z'})

    monkeypatch.setattr(auf, '_urlopen', fake_urlopen)
    out = auf.claim_rate_limit_reset('TOK', 'org-uuid-1')
    assert out == {'result': 'ok', 'next_available_at': '2026-11-27T10:00:00Z'}
    assert captured['url'] == \
        'https://api.anthropic.com/api/organizations/org-uuid-1/reset_rate_limits'
    assert captured['method'] == 'POST'
    assert captured['body'] == {'program': 'juniper_tide'}
    assert captured['auth'] == 'Bearer TOK'
    assert captured['beta'] == 'oauth-2025-04-20'
    assert captured['ua'] == auf.CLI_SURFACE_UA


def test_claim_maps_http_error_status(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError('u', 429, 'Too Many', {}, None)

    monkeypatch.setattr(auf, '_urlopen', boom)
    with pytest.raises(auf.UsageApiError) as ei:
        auf.claim_rate_limit_reset('TOK', 'org-uuid-1')
    assert ei.value.status == 429


def test_claim_requires_org_uuid():
    with pytest.raises(auf.UsageApiError):
        auf.claim_rate_limit_reset('TOK', '')


# ---- cedar_ember (reset grants: "Full reset", per-model "Fable" resets) ----

FULL_GRANT = {
    'id': 'full-reset-2611', 'label': 'Full reset', 'type': 'full',
    'resets_total': 1, 'resets_left': 1,
    'starts_at': None, 'ends_at': '2026-12-10T00:00:00Z',
    'clears': ['five_hour', 'seven_day', 'seven_day_overage_included'],
    'paused': False, 'usable_now': True, 'use_requires_limit': False,
    'gives_back_weekly': False,
}
FABLE_GRANT = {
    'id': 'launch-2610-fable', 'label': 'Fable reset', 'type': 'model',
    'resets_total': 1, 'resets_left': 1,
    'ends_at': '2027-03-09T00:00:00Z',
    'clears': ['five_hour', 'seven_day_overage_included'],
    'paused': False, 'usable_now': True, 'gives_back_weekly': True,
}
CEDAR_ELIGIBLE = {
    'eligible': True, 'ineligible_reason': None, 'at_limit': False,
    'grants': [FULL_GRANT, FABLE_GRANT], 'next_grant_id': 'full-reset-2611',
}


def test_map_cedar_status_lists_grants():
    st = auf.map_cedar_status({'cedar_ember': CEDAR_ELIGIBLE})
    assert st['eligible'] is True
    assert st['next_grant_id'] == 'full-reset-2611'
    assert [g['id'] for g in st['grants']] == ['full-reset-2611', 'launch-2610-fable']
    full = st['grants'][0]
    assert full == {
        'id': 'full-reset-2611', 'label': 'Full reset', 'type': 'full',
        'resets_left': 1, 'resets_total': 1, 'usable_now': True, 'paused': False,
        'ends_at': '2026-12-10T00:00:00Z',
        'clears': ['five_hour', 'seven_day', 'seven_day_overage_included'],
        'gives_back_weekly': False, 'percent_used': {},
    }


def test_map_cedar_status_ineligible_surface():
    st = auf.map_cedar_status({'cedar_ember': {
        'eligible': False, 'ineligible_reason': 'surface', 'grants': []}})
    assert st['eligible'] is False
    assert st['ineligible_reason'] == 'surface'
    assert st['grants'] == []


def test_map_cedar_status_skips_malformed_grant():
    # Same rule as the CLI: a grant without a valid id is dropped, not fatal.
    st = auf.map_cedar_status({'cedar_ember': dict(
        CEDAR_ELIGIBLE, grants=[{'label': 'no id'}, FULL_GRANT])})
    assert [g['id'] for g in st['grants']] == ['full-reset-2611']


def test_map_cedar_status_absent_or_malformed_returns_none():
    assert auf.map_cedar_status({}) is None
    assert auf.map_cedar_status({'cedar_ember': None}) is None
    assert auf.map_cedar_status({'cedar_ember': 'x'}) is None


def test_claim_cedar_posts_grant_and_request_id(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured['url'] = req.full_url
        captured['body'] = json.loads(req.data.decode())
        captured['ua'] = req.get_header('User-agent')
        return FakeResponse({'result': 'reset', 'resets_left': 0,
                             'cleared': ['five_hour']})

    monkeypatch.setattr(auf, '_urlopen', fake_urlopen)
    out = auf.claim_cedar_reset('TOK', 'org-1', 'full-reset-2611', 'req_abc-1')
    assert out['result'] == 'reset'
    assert captured['url'] == \
        'https://api.anthropic.com/api/organizations/org-1/reset_rate_limits'
    assert captured['body'] == {'program': 'cedar_ember',
                                'grant_id': 'full-reset-2611',
                                'request_id': 'req_abc-1'}
    assert captured['ua'] == auf.CLI_SURFACE_UA


@pytest.mark.parametrize('grant_id,request_id', [
    ('Full-Reset', 'r1'),          # uppercase not allowed in grant ids (CLI regex)
    ('a' * 41, 'r1'),              # too long
    ('ok-grant', 'bad id!'),       # request id charset
    ('', 'r1'),
])
def test_claim_cedar_rejects_malformed_ids(monkeypatch, grant_id, request_id):
    monkeypatch.setattr(auf, '_urlopen', lambda *a, **k: pytest.fail('must not POST'))
    with pytest.raises(auf.UsageApiError):
        auf.claim_cedar_reset('TOK', 'org-1', grant_id, request_id)


# ---- Layer 2: accounts.organization_uuid persistence ----

import config          # noqa: E402
import crypto_util     # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from database import UsageDatabase      # noqa: E402

FAR = 9_999_999_999_999


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'TOKEN_ENCRYPTION_KEY', Fernet.generate_key().decode())
    crypto_util._reset_cache()
    d = UsageDatabase(str(tmp_path / 'r.db'))
    yield d
    d.close()


def _add(db, email='a@x.com', org_uuid=None):
    return db.add_or_update_account(
        label=email, email=email, account_type='max',
        access_token='AT', refresh_token='RT', expires_at=FAR,
        organization_uuid=org_uuid)


def test_account_stores_organization_uuid(db):
    a = _add(db, org_uuid='org-1')
    assert db.get_account_for_primer(a)['organization_uuid'] == 'org-1'


def test_account_org_uuid_defaults_to_none(db):
    a = _add(db)
    assert db.get_account_for_primer(a)['organization_uuid'] is None


def test_reauth_without_org_uuid_keeps_existing(db):
    a = _add(db, org_uuid='org-1')
    a2 = _add(db, org_uuid=None)           # upsert by same email, no uuid known
    assert a2 == a
    assert db.get_account_for_primer(a)['organization_uuid'] == 'org-1'


def test_reauth_with_org_uuid_updates(db):
    a = _add(db, org_uuid='org-1')
    _add(db, org_uuid='org-2')
    assert db.get_account_for_primer(a)['organization_uuid'] == 'org-2'


def test_set_account_org_uuid_backfills(db):
    a = _add(db)
    db.set_account_org_uuid(a, 'org-9')
    assert db.get_account_for_primer(a)['organization_uuid'] == 'org-9'


# ---- Layer 3: oauth_flow.fetch_profile exposes the organization uuid ----

import oauth_flow  # noqa: E402


def test_fetch_profile_returns_organization_uuid(monkeypatch):
    monkeypatch.setattr(oauth_flow, '_urlopen',
                        lambda req, timeout=None: FakeResponse({
                            'account': {'email': 'a@x.com', 'has_claude_max': True},
                            'organization': {'uuid': 'org-uuid-7',
                                             'organization_type': 'claude_max'}}))
    p = oauth_flow.fetch_profile('TOK')
    assert p['organization_uuid'] == 'org-uuid-7'
    assert p['email'] == 'a@x.com'


def test_fetch_profile_org_uuid_none_when_absent(monkeypatch):
    monkeypatch.setattr(oauth_flow, '_urlopen',
                        lambda req, timeout=None: FakeResponse({
                            'account': {'email': 'a@x.com'}}))
    assert oauth_flow.fetch_profile('TOK')['organization_uuid'] is None


# ---- Layer 4: resetter orchestration (mirrors test_primer.py style) ----

import resetter  # noqa: E402

FUTURE = "2999-01-01T00:00:00+00:00"


def _usage(juniper=None):
    """A minimal usage response; juniper injects the juniper_tide block."""
    api = {
        "five_hour": {"utilization": 99.0, "resets_at": FUTURE},
        "seven_day": {"utilization": 10.0, "resets_at": FUTURE},
    }
    if juniper is not None:
        api['juniper_tide'] = juniper
    return api


AVAILABLE = {'eligible': True, 'available': True, 'next_available_at': None}
UNAVAILABLE = {'eligible': True, 'available': False,
               'next_available_at': '2026-11-22T10:00:00Z'}


def test_get_reset_status_reads_at_wall(db, monkeypatch):
    a = _add(db, org_uuid='org-1')
    account = db.get_account_for_primer(a)
    captured = {}

    def usage(token, at_wall=False):
        captured['at_wall'] = at_wall
        return _usage(AVAILABLE)

    monkeypatch.setattr(resetter.auf, '_http_get_usage', usage)
    st = resetter.get_reset_status(db, account)
    assert captured['at_wall'] is True
    assert st['juniper']['available'] is True
    assert st['cedar'] is None


def test_get_reset_status_includes_cedar_grants(db, monkeypatch):
    a = _add(db, org_uuid='org-1')
    account = db.get_account_for_primer(a)
    api = _usage()
    api['cedar_ember'] = CEDAR_ELIGIBLE
    monkeypatch.setattr(resetter.auf, '_http_get_usage',
                        lambda token, at_wall=False: api)
    st = resetter.get_reset_status(db, account)
    assert st['juniper'] is None
    assert [g['id'] for g in st['cedar']['grants']] == \
        ['full-reset-2611', 'launch-2610-fable']


def _cedar_usage(grants=None):
    api = _usage()
    api['cedar_ember'] = dict(CEDAR_ELIGIBLE,
                              grants=grants if grants is not None
                              else [FULL_GRANT, FABLE_GRANT])
    return api


def test_reset_grant_claims_cedar(db, monkeypatch):
    import re
    a = _add(db, org_uuid='org-1')
    account = db.get_account_for_primer(a)
    claimed = {}

    monkeypatch.setattr(resetter.auf, '_http_get_usage',
                        lambda token, at_wall=False: _cedar_usage())

    def claim(token, org, grant_id, request_id):
        claimed.update(org=org, grant=grant_id, req=request_id)
        return {'result': 'reset', 'resets_left': 0, 'cleared': ['five_hour']}

    monkeypatch.setattr(resetter.auf, 'claim_cedar_reset', claim)
    monkeypatch.setattr(resetter.auf, 'claim_rate_limit_reset',
                        lambda *a: pytest.fail('juniper must not be claimed'))
    out = resetter.reset_account(db, account, grant_id='launch-2610-fable')
    assert out['reset'] is True
    assert out['result'] == 'reset'
    assert out['cleared'] == ['five_hour']
    assert claimed['org'] == 'org-1'
    assert claimed['grant'] == 'launch-2610-fable'
    assert re.fullmatch(r'[A-Za-z0-9_-]{1,64}', claimed['req'])
    assert db.get_current(account_id=a) is not None      # post-claim snapshot


def test_reset_grant_not_usable_is_never_claimed(db, monkeypatch):
    a = _add(db, org_uuid='org-1')
    account = db.get_account_for_primer(a)
    spent = dict(FULL_GRANT, resets_left=0, usable_now=False)
    monkeypatch.setattr(resetter.auf, '_http_get_usage',
                        lambda token, at_wall=False: _cedar_usage([spent]))
    monkeypatch.setattr(resetter.auf, 'claim_cedar_reset',
                        lambda *a: pytest.fail('must not claim a spent grant'))
    out = resetter.reset_account(db, account, grant_id='full-reset-2611')
    assert out['reset'] is False
    assert out['result'] == 'unavailable'


def test_reset_unknown_grant_is_never_claimed(db, monkeypatch):
    a = _add(db, org_uuid='org-1')
    account = db.get_account_for_primer(a)
    monkeypatch.setattr(resetter.auf, '_http_get_usage',
                        lambda token, at_wall=False: _cedar_usage())
    monkeypatch.setattr(resetter.auf, 'claim_cedar_reset',
                        lambda *a: pytest.fail('must not claim an unlisted grant'))
    out = resetter.reset_account(db, account, grant_id='made-up-grant')
    assert out['reset'] is False
    assert out['result'] == 'unavailable'


def test_reset_claims_when_available(db, monkeypatch):
    a = _add(db, org_uuid='org-1')
    account = db.get_account_for_primer(a)
    claimed = {}

    monkeypatch.setattr(resetter.auf, '_http_get_usage',
                        lambda token, at_wall=False: _usage(AVAILABLE))

    def claim(token, org_uuid):
        claimed['org'] = org_uuid
        return {'result': 'reset', 'next_available_at': '2026-11-27T10:00:00Z'}

    monkeypatch.setattr(resetter.auf, 'claim_rate_limit_reset', claim)
    out = resetter.reset_account(db, account)
    assert out['reset'] is True
    assert out['result'] == 'reset'
    assert out['next_available_at'] == '2026-11-27T10:00:00Z'
    assert claimed['org'] == 'org-1'
    assert db.get_current(account_id=a) is not None   # post-claim snapshot


def test_reset_blocked_when_unavailable(db, monkeypatch):
    a = _add(db, org_uuid='org-1')
    account = db.get_account_for_primer(a)
    claims = {'n': 0}

    monkeypatch.setattr(resetter.auf, '_http_get_usage',
                        lambda token, at_wall=False: _usage(UNAVAILABLE))
    monkeypatch.setattr(resetter.auf, 'claim_rate_limit_reset',
                        lambda token, org: claims.__setitem__('n', claims['n'] + 1))

    out = resetter.reset_account(db, account)
    assert out['reset'] is False
    assert out['result'] == 'unavailable'
    assert out['next_available_at'] == '2026-11-22T10:00:00Z'
    assert claims['n'] == 0                 # never claims when not available


def test_reset_never_claims_when_status_absent(db, monkeypatch):
    # Team accounts answer juniper_tide=null (their resets are cedar_ember
    # grants). With no status block there is no reset on offer, so nothing is
    # POSTed — a blind claim would only be refused, from the collector's IP.
    a = _add(db, org_uuid='org-1')
    account = db.get_account_for_primer(a)

    monkeypatch.setattr(resetter.auf, '_http_get_usage',
                        lambda token, at_wall=False: _usage())
    monkeypatch.setattr(resetter.auf, 'claim_rate_limit_reset',
                        lambda *a: pytest.fail('must not claim without a status'))
    monkeypatch.setattr(resetter.oauth_flow, 'fetch_profile',
                        lambda *a: pytest.fail('must not resolve the org either'))
    out = resetter.reset_account(db, account)
    assert out == {'reset': False, 'result': 'unavailable',
                   'next_available_at': None, 'cleared': []}


def test_reset_limit_ui_has_no_blind_claim():
    """With neither grants nor a juniper_tide status the UI reports that no
    reset is available instead of offering an attempt."""
    html = _read('templates/accounts.html')
    assert 'Try reset' not in html
    assert 'No limit resets are available for this account' in html


def test_reset_backfills_org_uuid_from_profile(db, monkeypatch):
    a = _add(db)                             # no org uuid stored
    account = db.get_account_for_primer(a)
    claimed = {}

    monkeypatch.setattr(resetter.auf, '_http_get_usage',
                        lambda token, at_wall=False: _usage(AVAILABLE))
    monkeypatch.setattr(resetter.oauth_flow, 'fetch_profile',
                        lambda token: {'email': 'a@x.com', 'account_type': 'max',
                                       'organization_uuid': 'org-lazy'})
    monkeypatch.setattr(resetter.auf, 'claim_rate_limit_reset',
                        lambda token, org: claimed.update(org=org) or
                        {'result': 'reset', 'next_available_at': None})

    out = resetter.reset_account(db, account)
    assert out['reset'] is True
    assert claimed['org'] == 'org-lazy'
    # persisted for next time — no profile fetch needed again
    assert db.get_account_for_primer(a)['organization_uuid'] == 'org-lazy'


def test_reset_passes_through_non_reset_result(db, monkeypatch):
    a = _add(db, org_uuid='org-1')
    account = db.get_account_for_primer(a)

    monkeypatch.setattr(resetter.auf, '_http_get_usage',
                        lambda token, at_wall=False: _usage(AVAILABLE))
    monkeypatch.setattr(resetter.auf, 'claim_rate_limit_reset',
                        lambda token, org: {'result': 'not_limited',
                                            'next_available_at': None})
    out = resetter.reset_account(db, account)
    assert out['reset'] is False
    assert out['result'] == 'not_limited'


def test_reset_success_survives_postclaim_read_failure(db, monkeypatch):
    a = _add(db, org_uuid='org-1')
    account = db.get_account_for_primer(a)
    reads = {'n': 0}

    def usage(token, at_wall=False):
        reads['n'] += 1
        if reads['n'] == 1:
            return _usage(AVAILABLE)
        raise resetter.auf.UsageApiError('post-claim read boom')

    monkeypatch.setattr(resetter.auf, '_http_get_usage', usage)
    monkeypatch.setattr(resetter.auf, 'claim_rate_limit_reset',
                        lambda token, org: {'result': 'reset',
                                            'next_available_at': None})
    out = resetter.reset_account(db, account)
    assert out['reset'] is True             # claim succeeded; read failure is best-effort


# ---- Layer 5: routes (mirrors test_start_session_route.py style) ----

import os  # noqa: E402

os.environ.setdefault('ALLOW_DEFAULT_CREDENTIALS', '1')

import app as app_module  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'TOKEN_ENCRYPTION_KEY', Fernet.generate_key().decode())
    crypto_util._reset_cache()
    d = UsageDatabase(str(tmp_path / 'route.db'))
    monkeypatch.setattr(app_module, '_db', d)
    app_module.app.config.update(TESTING=True)
    c = app_module.app.test_client()
    with c.session_transaction() as sess:
        sess['logged_in'] = True
    yield c
    d.close()


def test_reset_limit_route_success(client, monkeypatch):
    a = _add(app_module._db, org_uuid='org-1')
    monkeypatch.setattr(resetter, 'reset_account',
                        lambda db, account, grant_id=None: {'reset': True, 'result': 'reset',
                                             'next_available_at': '2026-11-27T10:00:00Z'})
    r = client.post(f'/accounts/{a}/reset_limit')
    assert r.status_code == 200
    body = r.get_json()
    assert body['ok'] is True and body['reset'] is True
    assert body['result'] == 'reset'


def test_reset_limit_route_unavailable(client, monkeypatch):
    a = _add(app_module._db, org_uuid='org-1')
    monkeypatch.setattr(resetter, 'reset_account',
                        lambda db, account, grant_id=None: {'reset': False, 'result': 'unavailable',
                                             'next_available_at': '2026-11-22T10:00:00Z'})
    r = client.post(f'/accounts/{a}/reset_limit')
    assert r.status_code == 200
    body = r.get_json()
    assert body['reset'] is False
    assert body['next_available_at'] == '2026-11-22T10:00:00Z'


def test_reset_limit_route_unknown_account_404(client):
    r = client.post('/accounts/999999/reset_limit')
    assert r.status_code == 404


def test_reset_limit_route_api_error_maps_502(client, monkeypatch):
    a = _add(app_module._db, org_uuid='org-1')

    def boom(db, account, grant_id=None):
        raise auf.UsageApiError('claim failed', status=429)

    monkeypatch.setattr(resetter, 'reset_account', boom)
    r = client.post(f'/accounts/{a}/reset_limit')
    assert r.status_code == 502
    assert 'error' in r.get_json()


def test_reset_limit_route_unexpected_error_hides_message(client, monkeypatch):
    a = _add(app_module._db, org_uuid='org-1')

    def boom(db, account, grant_id=None):
        raise RuntimeError('secret-token-xyz must not leak')

    monkeypatch.setattr(resetter, 'reset_account', boom)
    r = client.post(f'/accounts/{a}/reset_limit')
    assert r.status_code == 500
    body = r.get_json()
    assert 'secret-token-xyz' not in body['error']
    assert 'RuntimeError' in body['error']


def test_reset_status_route(client, monkeypatch):
    a = _add(app_module._db, org_uuid='org-1')
    monkeypatch.setattr(resetter, 'get_reset_status',
                        lambda db, account: {
                            'juniper': dict(AVAILABLE, ineligible_reason=None),
                            'cedar': auf.map_cedar_status({'cedar_ember': CEDAR_ELIGIBLE})})
    r = client.get(f'/accounts/{a}/reset_status')
    assert r.status_code == 200
    st = r.get_json()['status']
    assert st['juniper']['available'] is True
    assert st['cedar']['grants'][0]['id'] == 'full-reset-2611'


def test_reset_limit_route_passes_grant_id(client, monkeypatch):
    a = _add(app_module._db, org_uuid='org-1')
    seen = {}

    def fake_reset(db, account, grant_id=None):
        seen['grant'] = grant_id
        return {'reset': True, 'result': 'reset', 'next_available_at': None,
                'cleared': ['five_hour']}

    monkeypatch.setattr(resetter, 'reset_account', fake_reset)
    r = client.post(f'/accounts/{a}/reset_limit', data={'grant_id': 'launch-2610-fable'})
    assert r.status_code == 200
    assert seen['grant'] == 'launch-2610-fable'
    assert r.get_json()['cleared'] == ['five_hour']


def test_reset_status_route_none_when_unsupported(client, monkeypatch):
    a = _add(app_module._db, org_uuid='org-1')
    monkeypatch.setattr(resetter, 'get_reset_status', lambda db, account: None)
    r = client.get(f'/accounts/{a}/reset_status')
    assert r.status_code == 200
    assert r.get_json()['status'] is None


def test_add_account_stores_org_uuid(client, monkeypatch):
    monkeypatch.setattr(app_module.oauth_flow, 'exchange_code',
                        lambda code, verifier: {'access_token': 'AT',
                                                'refresh_token': 'RT',
                                                'expires_at': FAR})
    monkeypatch.setattr(app_module.oauth_flow, 'fetch_profile',
                        lambda tok: {'email': 'n@x.com', 'account_type': 'max',
                                     'organization_uuid': 'org-new'})
    with client.session_transaction() as sess:
        sess['oauth_verifier'] = 'v'
        sess['oauth_state'] = 'st'
    r = client.post('/accounts/add',
                    data={'action': 'complete', 'code': 'abc#st', 'label': 'n'})
    assert r.status_code == 200
    acc_id = r.get_json()['id']
    assert app_module._db.get_account_for_primer(acc_id)['organization_uuid'] == 'org-new'


# ---- Layer 6: accounts template (mirrors test_accounts_template.py style) ----

BASE = os.path.dirname(os.path.abspath(__file__))


def _read(rel):
    with open(os.path.join(BASE, rel), encoding='utf-8') as f:
        return f.read()


def test_reset_limit_button_present():
    html = _read('templates/accounts.html')
    assert 'resetLimit({{ a.id }})' in html
    assert '>Limit resets<' in html


def test_reset_limit_js_handler_checks_status_first():
    """The handler must read reset_status before claiming — a reset is a limited
    resource, so the confirm dialog has to show real availability, and an
    unavailable reset must never be claimed."""
    html = _read('templates/accounts.html')
    assert 'async function resetLimit(' in html
    assert '/reset_status' in html
    assert '/reset_limit' in html


def test_reset_limit_ui_offers_cedar_grants():
    """Reset grants (cedar_ember) must be offered by name and claimed with their
    grant_id; labels are rendered via textContent (never innerHTML-interpolated)."""
    html = _read('templates/accounts.html')
    assert 'function chooseDialog(' in html
    assert 'grant_id' in html
    assert 'st.cedar' in html
    assert '${o.label}' not in html          # option labels never interpolated into HTML


def test_reset_success_survives_snapshot_db_failure(db, monkeypatch):
    """PR review: a consumed reset must never be reported as an error because the
    best-effort snapshot write failed (e.g. SQLite locked)."""
    import sqlite3
    a = _add(db, org_uuid='org-1')
    account = db.get_account_for_primer(a)
    monkeypatch.setattr(resetter.auf, '_http_get_usage',
                        lambda token, at_wall=False: _cedar_usage())
    monkeypatch.setattr(resetter.auf, 'claim_cedar_reset',
                        lambda tok, org, gid, rid: {'result': 'reset', 'cleared': []})

    def locked(snapshot):
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(db, 'insert_snapshot', locked)
    out = resetter.reset_account(db, account, grant_id='full-reset-2611')
    assert out['reset'] is True


def test_reset_limit_ui_dismisses_checking_toast():
    """PR review: the sticky 'Checking…' toast must be closed once the status
    read finishes, so cancelling a dialog leaves no spinner behind."""
    html = _read('templates/accounts.html')
    assert "const checking = showToast('Checking reset availability…', 'load', {sticky: true});" in html
    assert 'dismissToast(checking)' in html


def test_reset_limit_ui_groups_equivalent_grants():
    """Grants that do the same thing (claude.ai's "Fable reset · 2") are one
    choice that claims the first-expiring member; the picker also explains the
    weekly give-back and shows the expiry time, not just the day.

    Live finding (2026-11-20): the two Fable grants carry different labels
    ("New model launch: one Fable reset for ..." / "... one more Fable reset at
    6 months' tenure") and every grant has type "full", so the group key is
    what a grant clears, never its label or type."""
    html = _read('templates/accounts.html')
    assert 'function groupResetGrants(' in html
    assert 'groupResetGrants(usable)' in html
    assert 'JSON.stringify([[...g.clears].sort(), g.gives_back_weekly])' in html
    assert 'g.gives_back_weekly' in html
    assert "gives back ${model || 'this model'}'s usage only" in html
    assert "`${model || 'Model'} reset`" in html      # "Fable reset", as claude.ai names it
    assert 'Refills your 5-hour and weekly limits.' in html
    assert "hour: 'numeric'" in html.split('const fmtExpiry')[1].split('};')[0]


# ---- model name for per-model resets ("Fable reset") ----

def _scoped(name):
    return {'kind': 'weekly_scoped', 'group': 'weekly', 'percent': 79,
            'scope': {'model': {'id': None, 'display_name': name}, 'surface': None}}


def test_scoped_model_display_name_from_limits():
    # Live shape (2026-11-20): grants only list the windows they clear; the
    # model name lives on the weekly_scoped entry of `limits`.
    api = {'limits': [{'kind': 'session', 'scope': None},
                      {'kind': 'weekly_all', 'scope': None}, _scoped('Fable')]}
    assert auf.scoped_model_display_name(api) == 'Fable'


@pytest.mark.parametrize('api', [
    {}, {'limits': None}, {'limits': 'x'}, {'limits': [{'kind': 'weekly_all', 'scope': None}]},
    {'limits': [{'kind': 'weekly_scoped', 'scope': None}]},
    {'limits': [{'kind': 'weekly_scoped', 'scope': {'model': {'display_name': '  '}}}]},
    {'limits': [_scoped('Fable'), _scoped('Opus')]},      # ambiguous → generic name
])
def test_scoped_model_display_name_none_when_absent_or_ambiguous(api):
    assert auf.scoped_model_display_name(api) is None


def test_get_reset_status_carries_scoped_model(db, monkeypatch):
    a = _add(db, org_uuid='org-1')
    account = db.get_account_for_primer(a)
    api = _cedar_usage()
    api['limits'] = [_scoped('Fable')]
    monkeypatch.setattr(resetter.auf, '_http_get_usage',
                        lambda token, at_wall=False: api)
    assert resetter.get_reset_status(db, account)['scoped_model'] == 'Fable'


# ---- confirm step after picking a grant ----

def test_map_cedar_status_keeps_percent_used():
    grant = dict(FULL_GRANT, percent_used={'five_hour': 52, 'seven_day': 83.4,
                                           'junk': 'x', 'flag': True})
    st = auf.map_cedar_status({'cedar_ember': dict(CEDAR_ELIGIBLE, grants=[grant])})
    assert st['grants'][0]['percent_used'] == {'five_hour': 52, 'seven_day': 83.4}
    bad = dict(FULL_GRANT, percent_used='nope')
    st = auf.map_cedar_status({'cedar_ember': dict(CEDAR_ELIGIBLE, grants=[bad])})
    assert st['grants'][0]['percent_used'] == {}


def test_reset_limit_ui_confirms_after_picking_a_grant():
    """Picking a row in the grant picker must not spend a reset by itself: a
    confirm step (the "Start 5h" convention) sits between the pick and the POST."""
    html = _read('templates/accounts.html')
    handler = html.split('async function resetLimit(')[1].split('async function delAcc(')[0]
    pick = handler.index('await chooseDialog(')
    confirm = handler.index('title: `Use ${resetName(g)}?`')
    post = handler.index('/reset_limit')
    assert pick < confirm < post
    assert 'Cannot be undone.' in handler


# ---- per-account rate limit on the usage endpoint ----

def test_reset_status_route_429_asks_to_wait(client, monkeypatch):
    # Live finding (2026-11-20): about ten status reads in a quarter of an hour
    # got one account 429'd, which also blocked the collector's polling for it.
    a = _add(app_module._db, org_uuid='org-1')

    def boom(db, account):
        raise auf.UsageApiError('usage request failed: HTTPError: HTTP Error 429', status=429)

    monkeypatch.setattr(resetter, 'get_reset_status', boom)
    r = client.get(f'/accounts/{a}/reset_status')
    assert r.status_code == 502
    body = r.get_json()
    assert 'rate-limiting' in body['error'] and 'Wait a few minutes' in body['error']
    assert body['rate_limited'] is True


def test_reset_limit_route_429_asks_to_wait(client, monkeypatch):
    a = _add(app_module._db, org_uuid='org-1')

    def boom(db, account, grant_id=None):
        raise auf.UsageApiError('reset claim failed', status=429)

    monkeypatch.setattr(resetter, 'reset_account', boom)
    r = client.post(f'/accounts/{a}/reset_limit')
    assert r.status_code == 502
    assert 'Wait a few minutes' in r.get_json()['error']


def test_reset_limit_ui_reuses_a_recent_status():
    """Opening and cancelling the picker must not cost a usage request each
    time: a recent status is reused, and dropped once a claim is attempted."""
    html = _read('templates/accounts.html')
    handler = html.split('async function resetLimit(')[1].split('async function delAcc(')[0]
    assert '_resetStatusCache.get(id)' in handler
    assert '_resetStatusCache.set(id' in handler
    assert handler.index('_resetStatusCache.delete(id)') < handler.index('/reset_limit')


def test_reset_limit_ui_stops_sending_after_a_429():
    """After a rate-limited answer the page must not send again for that
    account until the block has had time to lift."""
    html = _read('templates/accounts.html')
    handler = html.split('async function resetLimit(')[1].split('async function delAcc(')[0]
    assert handler.index('_resetBlockedUntil.get(id)') < handler.index('/reset_status')
    assert handler.count('noteResetRateLimit(id, d)') == 2
    assert 'Nothing was sent.' in handler


def test_existing_requests_keep_their_user_agent_format(monkeypatch):
    # The pre-existing requests keep the `claude-code/<ver>` format; only the
    # reset status read and the claim use the CLI surface. Both take their
    # version from the one CLI_VERSION constant.
    # Live finding (2026-11-20): the usage rate limit is per account and per
    # User-Agent surface, so the collector's plain read sharing the CLI surface
    # with the reset status read got an account's polling 429'd.
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured['ua'] = req.get_header('User-agent')
        return FakeResponse({})

    monkeypatch.setattr(auf, '_urlopen', fake_urlopen)
    auf._http_get_usage('TOK')
    assert captured['ua'] == f'claude-code/{auf.CLI_VERSION}'
    auf._http_get_usage('TOK', at_wall=True)
    assert captured['ua'] == auf.CLI_SURFACE_UA == f'claude-cli/{auf.CLI_VERSION} (external, cli)'
    auf.send_haiku_primer('TOK')
    assert captured['ua'] == f'claude-code/{auf.CLI_VERSION}'


def test_reset_limit_ui_says_when_every_reset_is_spent():
    """Live finding (2026-11-26): the server keeps listing spent grants
    (resets_left 0) until they expire. The page must say they are used up,
    not that they cannot be used "right now"."""
    html = _read('templates/accounts.html')
    handler = html.split('async function resetLimit(')[1].split('async function delAcc(')[0]
    assert 'grants.every(g => g.resets_left < 1)' in handler
    assert 'resets on this account have already been used.' in handler
    assert 'but none can be used right now.' in handler      # kept for paused / not-yet-usable grants
