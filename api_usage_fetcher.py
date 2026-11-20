"""Helpers for fetching Claude usage from the OAuth usage API (no PTY scraping).

Library of pure(-ish) functions used by collect_all.py, the multi-account
runner: token-refresh decision (needs_refresh_ms), refresh-grant POST
(refresh_access_token), usage GET (_http_get_usage), response mapping
(map_usage_response) and snapshot assembly (build_snapshot). No CLI entry
point and no file reading — credentials live in the DB (accounts table) and
the caller persists rotated tokens there.

Method modeled on better-ccflare (packages/providers/src/usage-fetcher.ts).
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

USAGE_URL = 'https://api.anthropic.com/api/oauth/usage'
TOKEN_URL = 'https://platform.claude.com/v1/oauth/token'
MESSAGES_URL = 'https://api.anthropic.com/v1/messages'
# Rate-limit reset ("restart"): the official claim endpoint the Claude Code CLI
# calls (observed in the 2.1.329 bundle). `juniper_tide` is Anthropic's program
# name for the free subscription limit resets; status for it rides along in the
# usage response only when the GET carries at_wall=1.
RESET_URL_TEMPLATE = 'https://api.anthropic.com/api/organizations/{org_uuid}/reset_rate_limits'
RESET_PROGRAM = 'juniper_tide'
# cedar_ember = per-account reset grants ("Full reset", per-model resets); the
# claim names one grant and carries an idempotency request id. Patterns are the
# CLI's own validation regexes.
CEDAR_PROGRAM = 'cedar_ember'
_GRANT_ID_RE = re.compile(r'[a-z0-9_-]{1,40}')
_REQUEST_ID_RE = re.compile(r'[A-Za-z0-9_-]{1,64}')
AT_WALL_QUERY = '?at_wall=1&skip_spend=1'
# Cheapest model; starting the 5h (session) window works with ANY model and Haiku
# does not touch the 7d Sonnet/Opus windows. Overridable for forward-compat.
PRIMER_MODEL = os.environ.get('PRIMER_MODEL', 'claude-haiku-4-5')
# OAuth (subscription) tokens require the first system block to be exactly this
# Claude Code preamble — Anthropic enforces it on the Messages API.
PRIMER_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."
PRIMER_TEXT = 'Hi'
# Public Claude Code OAuth client id (same one better-ccflare ships as default).
CLIENT_ID = os.environ.get('CLAUDE_OAUTH_CLIENT_ID',
                           '9d1c250a-e61b-44d9-88ed-5944d1962f5e')
# Only used for the User-Agent header; keep loosely in sync with the CLI.
CLI_VERSION = os.environ.get('CLAUDE_CLI_VERSION', '2.1.329')
# The reset feature (status read with at_wall, and the claim) is the ONLY code
# that uses the UA below. Everything that existed before it — the collector's
# usage polling, the 5h primer, the profile read — keeps sending
# `claude-code/{CLI_VERSION}` exactly as it always has.
#
# The reset endpoints need the format the CLI itself sends (bV() in the 2.1.329
# bundle): the server derives the client "surface" AND the CLI version from it.
# `claude-code/<ver>` → cedar_ember ineligible "surface" (no reset grants); a
# too-old version → "cli_version". Keep CLI_VERSION current — it is the one
# version number behind both user-agents.
#
# Keeping the two apart is also what protects the collector: the usage endpoint
# is rate-limited per account AND per surface. Measured live (2026-11-20): with
# one account's CLI surface 429'd after a burst of reset status reads, the same
# read sent as `claude-code/<ver>` in the same second returned 200.
CLI_SURFACE_UA = f'claude-cli/{CLI_VERSION} (external, cli)'
# User-Agent for the token endpoint (platform.claude.com/v1/oauth/token). It is
# fronted by Cloudflare with two traps: spoofing `claude-code/<ver>` trips
# anti-abuse (429), and urllib's default `Python-urllib/<ver>` is blocked at the
# edge (403). better-ccflare works because Bun sends `Bun/<ver>` — mirror that.
TOKEN_ENDPOINT_UA = os.environ.get('TOKEN_ENDPOINT_UA', 'Bun/1.1.34')
HTTP_TIMEOUT = 15
REFRESH_MARGIN_MS = 120_000  # refresh when less than 2 min of token life left

# Indirection so tests can stub HTTP without touching the network.
_urlopen = urllib.request.urlopen

# Legacy flat windows (pre-2026-06-30). Used as a fallback when the response has
# no `limits` array. (api field, quota type, model) — model windows are mutually
# exclusive in the legacy shape (the absent one is null), first non-null wins.
WINDOW_MAP = [
    ('five_hour', 'session', ''),
    ('seven_day', 'weekly', ''),
    ('seven_day_sonnet', 'model_specific', 'sonnet'),
    ('seven_day_opus', 'model_specific', 'opus'),
]

# The 2026-06-30 restructure (shipped with the new Sonnet) made `limits` the
# canonical source: a list of {kind, group, percent, resets_at, is_active, ...}.
# `percent` is utilization, so percent_remaining = 100 - percent. Session and the
# all-models weekly cap are stable kinds; any other group=='weekly' entry is a
# per-model window (kind like 'weekly_sonnet'/'weekly_opus') — mapped structurally
# so a reintroduced per-model cap is picked up without a code change.
_LIMIT_KIND_SESSION = 'session'
_LIMIT_KIND_WEEKLY_ALL = 'weekly_all'


class UsageApiError(Exception):
    """Any failure of the API fetch path (caller records it per account).

    `status` carries the HTTP status code when the failure was an HTTP error
    (e.g. 401 = dead/invalid access token → caller can refresh and retry),
    else None. `oauth_error` carries the OAuth error code from a token-endpoint
    error body (e.g. 'invalid_grant' when the grant expired — Anthropic grants
    die ~28 days after authorization), else None.
    """

    def __init__(self, message, status=None, oauth_error=None):
        super().__init__(message)
        self.status = status
        self.oauth_error = oauth_error


def _make_quota(quota_type: str, model: str, percent_used: float,
                resets_raw: Any, now: datetime) -> Dict[str, Any]:
    """Build one quota dict. `percent_used` is utilization; a malformed resets_at
    drops the reset fields but keeps the percentage (same degradation the PTY
    parser had)."""
    quota: Dict[str, Any] = {
        'type': quota_type,
        'percent_remaining': round(100.0 - float(percent_used), 3),
    }
    if model:
        quota['model'] = model
    if isinstance(resets_raw, str) and resets_raw:
        try:
            resets = datetime.fromisoformat(resets_raw.replace('Z', '+00:00'))
            if resets.tzinfo is None:
                resets = resets.replace(tzinfo=timezone.utc)
            resets = resets.astimezone(timezone.utc)
            quota['resets_at'] = resets.strftime('%Y-%m-%dT%H:%M:%SZ')
            quota['time_remaining_seconds'] = max(
                0, int((resets - now).total_seconds()))
        except ValueError:
            pass
    return quota


def _scoped_model_name(item: Dict[str, Any]) -> Optional[str]:
    """Model name of a scoped limit entry (2026-07-01+ `weekly_scoped` shape).

    Reads `scope.model.display_name` (e.g. "Fable"; `id` observed null so far,
    used as fallback). Lowercased to match the legacy stored names ('sonnet').
    """
    scope = item.get('scope')
    model = scope.get('model') if isinstance(scope, dict) else None
    if not isinstance(model, dict):
        return None
    for key in ('display_name', 'id'):
        name = model.get(key)
        if isinstance(name, str) and name.strip():
            return name.strip().lower()
    return None


def scoped_model_display_name(api: Dict[str, Any]) -> Optional[str]:
    """Display name of the model behind the per-model weekly limit (e.g. "Fable").

    Reset grants do not name the model they refill — they only list the windows
    they clear — so the UI takes the name from the `weekly_scoped` entry of the
    `limits` array. None when there is no such entry, or when several entries
    name different models (ambiguous → the UI falls back to a generic name).
    """
    limits = api.get('limits')
    if not isinstance(limits, list):
        return None
    names = set()
    for item in limits:
        if not isinstance(item, dict) or item.get('kind') != 'weekly_scoped':
            continue
        scope = item.get('scope')
        model = scope.get('model') if isinstance(scope, dict) else None
        name = model.get('display_name') if isinstance(model, dict) else None
        if isinstance(name, str) and name.strip():
            names.add(name.strip())
    return names.pop() if len(names) == 1 else None


def _map_from_limits(limits: List[Any], now: datetime) -> List[Dict[str, Any]]:
    """Map the canonical `limits` array (2026-06-30+) to quota dicts.

    session ← kind/group 'session'; weekly ← kind 'weekly_all'; model_specific ←
    any other group=='weekly' entry. The model name comes from the entry's
    `scope.model` block (kind 'weekly_scoped', 2026-07-01+), falling back to the
    kind suffix (kind like 'weekly_sonnet'). When several per-model entries
    exist Sonnet wins (legacy card convention), else first.
    """
    session: Optional[Dict[str, Any]] = None
    weekly: Optional[Dict[str, Any]] = None
    model_candidates: List[Dict[str, Any]] = []
    for item in limits:
        if not isinstance(item, dict):
            continue
        percent = item.get('percent')
        if not isinstance(percent, (int, float)):
            continue
        kind = item.get('kind')
        group = item.get('group')
        resets_raw = item.get('resets_at')
        if (group == 'session' or kind == _LIMIT_KIND_SESSION) and session is None:
            session = _make_quota('session', '', percent, resets_raw, now)
        elif kind == _LIMIT_KIND_WEEKLY_ALL and weekly is None:
            weekly = _make_quota('weekly', '', percent, resets_raw, now)
        elif group == 'weekly' and isinstance(kind, str):
            model = _scoped_model_name(item) or (
                kind[len('weekly_'):] if kind.startswith('weekly_') else kind)
            model_candidates.append(_make_quota('model_specific', model, percent, resets_raw, now))

    quotas: List[Dict[str, Any]] = []
    if session:
        quotas.append(session)
    if weekly:
        quotas.append(weekly)
    if model_candidates:
        quotas.append(next((m for m in model_candidates if m.get('model') == 'sonnet'),
                           model_candidates[0]))
    return quotas


def _map_from_flat_windows(api: Dict[str, Any], now: datetime) -> List[Dict[str, Any]]:
    """Legacy fallback: map the flat top-level windows (WINDOW_MAP)."""
    quotas: List[Dict[str, Any]] = []
    have_model_quota = False
    for key, quota_type, model in WINDOW_MAP:
        window = api.get(key)
        if not isinstance(window, dict):
            continue
        utilization = window.get('utilization')
        if not isinstance(utilization, (int, float)):
            continue
        if quota_type == 'model_specific':
            if have_model_quota:
                continue
            have_model_quota = True
        quotas.append(_make_quota(quota_type, model, utilization,
                                  window.get('resets_at'), now))
    return quotas


def map_usage_response(api: Dict[str, Any],
                       now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Map the oauth/usage JSON to the quota dicts the dashboard stores.

    Prefers the canonical `limits` array (2026-06-30+ shape); falls back to the
    legacy flat windows when `limits` is absent/empty or doesn't yield the two
    required windows. Unknown windows and nulls are skipped.
    """
    now = now or datetime.now(timezone.utc)
    limits = api.get('limits')
    if isinstance(limits, list) and limits:
        quotas = _map_from_limits(limits, now)
        types = {q['type'] for q in quotas}
        if 'session' in types and 'weekly' in types:
            return quotas
    return _map_from_flat_windows(api, now)


def needs_refresh_ms(expires_at_ms, now_ms=None) -> bool:
    if not isinstance(expires_at_ms, (int, float)):
        return True
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    return now_ms >= expires_at_ms - REFRESH_MARGIN_MS


def _read_oauth_error_body(e) -> tuple:
    """(detail, oauth_error) from a token-endpoint HTTPError body.

    Standard shape: {"error": "invalid_grant", "error_description": "..."}.
    A non-JSON body (e.g. a Cloudflare block page) falls back to the HTTP
    reason with no oauth_error.
    """
    try:
        body = json.loads(e.read().decode())
        detail = body.get('error_description') or body.get('error') or e.reason
        return str(detail)[:200], body.get('error')
    except Exception:
        return str(e.reason), None


def refresh_access_token(refresh_token: str, now_ms=None) -> Dict[str, Any]:
    """POST the refresh grant; return new {access_token, refresh_token, expires_at}.
    Does not touch any file — caller persists to the DB."""
    body = json.dumps({
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
        'client_id': CLIENT_ID,
    }).encode()
    # Token endpoint UA must be neither the spoofed `claude-code/<ver>` (429) nor
    # urllib's default `Python-urllib` (Cloudflare 403). Use Bun's UA like ccflare.
    req = urllib.request.Request(TOKEN_URL, data=body, method='POST', headers={
        'Content-Type': 'application/json',
        'User-Agent': TOKEN_ENDPOINT_UA,
    })
    try:
        with _urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            token = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # The ERROR body carries no secrets (unlike the request payload or a
        # success body) — surface its error_description so last_error / the UI
        # can say "Refresh token expired" instead of a bare status code.
        detail, oauth_error = _read_oauth_error_body(e)
        raise UsageApiError(f'token refresh failed: HTTP {e.code}: {detail}',
                            status=e.code, oauth_error=oauth_error) from e
    except Exception as e:
        # Never include the request payload here: it carries tokens.
        raise UsageApiError(f'token refresh failed: {type(e).__name__}: {e}') from e
    access_token = token.get('access_token')
    if not access_token:
        raise UsageApiError('token refresh response has no access_token')
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    return {
        'access_token': access_token,
        'refresh_token': token.get('refresh_token') or refresh_token,
        'expires_at': int(now_ms + token.get('expires_in', 0) * 1000),
    }


def build_snapshot(api: Dict[str, Any], account: Dict[str, Any],
                   now: Optional[datetime] = None) -> Dict[str, Any]:
    """Assemble a snapshot dict (insert_snapshot format) for one account."""
    now = now or datetime.now(timezone.utc)
    quotas = map_usage_response(api, now=now)
    types = {q['type'] for q in quotas}
    if 'session' not in types or 'weekly' not in types:
        raise UsageApiError('incomplete usage response, windows: %s' % sorted(api.keys()))
    return {
        'account_id': account.get('id'),
        'account_type': account.get('account_type') or 'unknown',
        'email': account.get('email'),
        'quotas': quotas,
        'captured_at': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'source': 'api',
    }


def _http_get_usage(access_token: str, at_wall: bool = False) -> Dict[str, Any]:
    # Only the at_wall read (reset status) uses the CLI surface; the plain read
    # is unchanged — see CLI_SURFACE_UA.
    url = USAGE_URL + AT_WALL_QUERY if at_wall else USAGE_URL
    req = urllib.request.Request(url, headers={
        'Authorization': f'Bearer {access_token}',
        'anthropic-beta': 'oauth-2025-04-20',
        'User-Agent': CLI_SURFACE_UA if at_wall else f'claude-code/{CLI_VERSION}',
        'Accept': 'application/json',
    })
    try:
        with _urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # Carry the status so the caller can react (401 = dead token → refresh+retry).
        raise UsageApiError(
            f'usage request failed: HTTPError: HTTP Error {e.code}: {e.reason}',
            status=e.code) from e
    except Exception as e:
        raise UsageApiError(f'usage request failed: {type(e).__name__}: {e}') from e


def map_reset_status(api: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize the `juniper_tide` block of an at_wall usage response.

    Returns {eligible, available, next_available_at, ineligible_reason} or None
    when the block is absent or malformed — the reset feature is experimental
    server-side, so an unreadable block must degrade to "unknown", never raise.
    """
    block = api.get('juniper_tide')
    if not isinstance(block, dict):
        return None
    next_at = block.get('next_available_at')
    reason = block.get('ineligible_reason')
    return {
        'eligible': bool(block.get('eligible')),
        'available': bool(block.get('available')),
        'next_available_at': next_at if isinstance(next_at, str) else None,
        'ineligible_reason': reason if isinstance(reason, str) else None,
    }


def _grant_dict(item: Any) -> Optional[Dict[str, Any]]:
    """One cedar_ember grant, normalized; None when it has no valid id (the
    claim needs it — the CLI drops such grants the same way)."""
    if not isinstance(item, dict):
        return None
    gid = item.get('id')
    if not isinstance(gid, str) or not _GRANT_ID_RE.fullmatch(gid):
        return None

    def _int(key):
        v = item.get(key)
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0

    def _str(key):
        v = item.get(key)
        return v if isinstance(v, str) else None

    clears = item.get('clears')
    used = item.get('percent_used')
    return {
        'id': gid,
        'label': _str('label') or gid,
        'type': _str('type'),
        'resets_left': _int('resets_left'),
        'resets_total': _int('resets_total'),
        'usable_now': bool(item.get('usable_now')),
        'paused': bool(item.get('paused')),
        'ends_at': _str('ends_at'),
        'clears': [c for c in clears if isinstance(c, str)] if isinstance(clears, list) else [],
        'gives_back_weekly': bool(item.get('gives_back_weekly')),
        # Current utilization of each window, so the confirm step can show what
        # a reset would actually give back right now.
        'percent_used': {k: v for k, v in used.items()
                         if isinstance(k, str) and isinstance(v, (int, float))
                         and not isinstance(v, bool)} if isinstance(used, dict) else {},
    }


def map_cedar_status(api: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize the `cedar_ember` block (reset grants such as "Full reset" or
    per-model resets) of an at_wall usage response.

    Returns {eligible, ineligible_reason, at_limit, next_grant_id, grants} or
    None when the block is absent/malformed. Malformed grants are skipped.
    """
    block = api.get('cedar_ember')
    if not isinstance(block, dict):
        return None
    raw = block.get('grants')
    grants = [g for g in (_grant_dict(i) for i in raw) if g] if isinstance(raw, list) else []
    reason = block.get('ineligible_reason')
    nxt = block.get('next_grant_id')
    return {
        'eligible': bool(block.get('eligible')),
        'ineligible_reason': reason if isinstance(reason, str) else None,
        'at_limit': bool(block.get('at_limit')),
        'next_grant_id': nxt if any(g['id'] == nxt for g in grants) else None,
        'grants': grants,
    }


def _post_reset(access_token: str, org_uuid: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST a reset claim to /api/organizations/<org>/reset_rate_limits.

    Same OAuth surface (incl. CLI_SURFACE_UA) as _http_get_usage. Raises
    UsageApiError(status=...) on an HTTP error (401 → caller refreshes and
    retries; 429 → claim throttled). An empty org_uuid is a caller bug surfaced
    as UsageApiError instead of POSTing to a malformed URL.
    """
    if not org_uuid:
        raise UsageApiError('cannot claim a reset without an organization uuid')
    body = json.dumps(payload).encode()
    url = RESET_URL_TEMPLATE.format(org_uuid=org_uuid)
    req = urllib.request.Request(url, data=body, method='POST', headers={
        'Authorization': f'Bearer {access_token}',
        'anthropic-beta': 'oauth-2025-04-20',
        'User-Agent': CLI_SURFACE_UA,
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    })
    try:
        with _urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise UsageApiError(
            f'reset claim failed: HTTPError: HTTP Error {e.code}: {e.reason}',
            status=e.code) from e
    except Exception as e:
        raise UsageApiError(f'reset claim failed: {type(e).__name__}: {e}') from e


def claim_rate_limit_reset(access_token: str, org_uuid: str) -> Dict[str, Any]:
    """Claim a juniper_tide (free subscription) reset."""
    return _post_reset(access_token, org_uuid, {'program': RESET_PROGRAM})


def claim_cedar_reset(access_token: str, org_uuid: str, grant_id: str,
                      request_id: str) -> Dict[str, Any]:
    """Claim one cedar_ember reset grant. The ids are validated with the CLI's
    own patterns before anything is sent — a bad id never reaches the API."""
    if not isinstance(grant_id, str) or not _GRANT_ID_RE.fullmatch(grant_id):
        raise UsageApiError('malformed reset grant id')
    if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
        raise UsageApiError('malformed reset request id')
    return _post_reset(access_token, org_uuid, {
        'program': CEDAR_PROGRAM, 'grant_id': grant_id, 'request_id': request_id})


def send_haiku_primer(access_token: str, model: Optional[str] = None) -> Dict[str, Any]:
    """POST a minimal 'Hi' to start the 5h session window. Returns the parsed body.

    Uses the same OAuth surface as _http_get_usage (Bearer token, oauth beta header,
    claude-code User-Agent — accepted on the usage endpoint, so reused here). The
    `system` preamble is mandatory for subscription OAuth tokens. Raises
    UsageApiError(status=...) on an HTTP error so the caller can refresh+retry on 401.
    Never logs the request/response body (it carries the bearer token).
    """
    body = json.dumps({
        'model': model or PRIMER_MODEL,
        'max_tokens': 1,
        'system': PRIMER_SYSTEM,
        'messages': [{'role': 'user', 'content': PRIMER_TEXT}],
    }).encode()
    req = urllib.request.Request(MESSAGES_URL, data=body, method='POST', headers={
        'Authorization': f'Bearer {access_token}',
        'anthropic-beta': 'oauth-2025-04-20',
        'anthropic-version': '2023-06-01',
        'User-Agent': f'claude-code/{CLI_VERSION}',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    })
    try:
        with _urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise UsageApiError(
            f'primer request failed: HTTPError: HTTP Error {e.code}: {e.reason}',
            status=e.code) from e
    except Exception as e:
        raise UsageApiError(f'primer request failed: {type(e).__name__}: {e}') from e
