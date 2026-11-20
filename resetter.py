"""Manual rate-limit reset ("restart"): claim one of Anthropic's reset programs.

Mirrors primer.py — trigger-agnostic orchestration used by the web button. The
flow matches what the Claude Code CLI does: read usage with at_wall=1 (the
response then carries the reset status blocks), then POST
/api/organizations/<uuid>/reset_rate_limits. Two programs exist:

- cedar_ember: per-account reset grants ("Full reset", per-model resets such
  as Fable). The claim names one grant_id. This is what Team accounts get.
- juniper_tide: the free subscription reset (no grant id).

Claiming consumes a limited reset, so availability is re-checked server-side
here right before the POST — a stale button click never wastes one.
"""
import uuid

import account_session
import api_usage_fetcher as auf
import oauth_flow


def _read_at_wall(db, account):
    return account_session.call_with_401_retry(
        db, account, lambda tok: auf._http_get_usage(tok, at_wall=True))


def get_reset_status(db, account):
    """{'juniper': mapped juniper_tide|None, 'cedar': mapped cedar_ember|None,
    'scoped_model': display name of the per-model weekly limit|None}.
    Raises auf.UsageApiError on an API failure."""
    api = _read_at_wall(db, account)
    return {'juniper': auf.map_reset_status(api), 'cedar': auf.map_cedar_status(api),
            'scoped_model': auf.scoped_model_display_name(api)}


def _ensure_org_uuid(db, account):
    """The claim URL needs the organization uuid; fetch + persist it lazily for
    accounts added before the column existed. The token was just used (and
    refreshed if needed) by the status read, so a plain profile GET suffices."""
    if account.get('organization_uuid'):
        return account['organization_uuid']
    try:
        profile = oauth_flow.fetch_profile(account['access_token'])
    except oauth_flow.OAuthError as e:
        raise auf.UsageApiError(f'could not resolve organization: {e}') from e
    org_uuid = profile.get('organization_uuid')
    if not org_uuid:
        raise auf.UsageApiError('account profile has no organization uuid')
    db.set_account_org_uuid(account['id'], org_uuid)
    account['organization_uuid'] = org_uuid
    return org_uuid


def _snapshot_after_reset(db, account):
    """Best-effort post-claim snapshot so the dashboard shows the freed window
    right away. The reset is already consumed at this point, so NO failure here
    (API read, or a DB write such as a locked SQLite) may turn the successful
    claim into an error — the next collector poll records the window anyway."""
    try:
        api2 = account_session.call_with_401_retry(db, account, auf._http_get_usage)
        db.insert_snapshot(auf.build_snapshot(api2, account))
        db.record_account_poll(account['id'], error=None)
    except Exception:  # noqa: BLE001
        pass


def _str_or_none(v):
    return v if isinstance(v, str) else None


def _claim_cedar(db, account, api, grant_id):
    status = auf.map_cedar_status(api)
    grant = next((g for g in (status or {}).get('grants', []) if g['id'] == grant_id), None)
    if not status or not status['eligible'] or grant is None \
            or not grant['usable_now'] or grant['paused'] or grant['resets_left'] < 1:
        return {'reset': False, 'result': 'unavailable', 'next_available_at': None,
                'cleared': []}
    org_uuid = _ensure_org_uuid(db, account)
    # One request id per click; the 401-retry re-sends the SAME id so the server
    # can dedupe a claim whose first attempt actually landed.
    request_id = uuid.uuid4().hex
    out = account_session.call_with_401_retry(
        db, account,
        lambda tok: auf.claim_cedar_reset(tok, org_uuid, grant_id, request_id))
    result = out.get('result') if isinstance(out.get('result'), str) else 'unknown'
    cleared = out.get('cleared')
    if result == 'reset':
        _snapshot_after_reset(db, account)
    # cedar_ember has no "next reset" time (its weekly_resets_at is the weekly
    # window's reset, not a grant's) — report None rather than mislabel it.
    return {'reset': result == 'reset', 'result': result,
            'next_available_at': None,
            'cleared': [c for c in cleared if isinstance(c, str)]
            if isinstance(cleared, list) else []}


def _claim_juniper(db, account, api):
    status = auf.map_reset_status(api)
    # status None = the server reported no juniper_tide block (Team accounts
    # answer null; their resets are cedar_ember grants). Nothing is on offer, so
    # nothing is claimed: a blind POST would only earn a refusal from the IP the
    # collector polls from.
    if status is None:
        return {'reset': False, 'result': 'unavailable',
                'next_available_at': None, 'cleared': []}
    if not status['available']:
        result = 'unavailable' if status['eligible'] else 'ineligible'
        return {'reset': False, 'result': result,
                'next_available_at': status['next_available_at'], 'cleared': []}
    org_uuid = _ensure_org_uuid(db, account)
    out = account_session.call_with_401_retry(
        db, account, lambda tok: auf.claim_rate_limit_reset(tok, org_uuid))
    result = out.get('result') if isinstance(out.get('result'), str) else 'unknown'
    if result == 'reset':
        _snapshot_after_reset(db, account)
    return {'reset': result == 'reset', 'result': result,
            'next_available_at': _str_or_none(out.get('next_available_at')),
            'cleared': []}


def reset_account(db, account, grant_id=None):
    """Claim a rate-limit reset for one account.

    grant_id given → claim that cedar_ember grant (only if the fresh status lists
    it as usable); otherwise → juniper_tide (only if the fresh status reports it
    available). Returns {'reset': bool, 'result':
    str, 'next_available_at': str|None, 'cleared': [window names]}. result is
    'reset' on success, 'unavailable'/'ineligible' from the local gate, or the
    claim endpoint's answer ('already_used', 'not_limited', 'cooldown', ...).
    Raises auf.UsageApiError on an API failure (route maps it to HTTP 502).
    """
    api = _read_at_wall(db, account)
    if grant_id:
        return _claim_cedar(db, account, api, grant_id)
    return _claim_juniper(db, account, api)
