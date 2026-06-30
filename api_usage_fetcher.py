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
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

USAGE_URL = 'https://api.anthropic.com/api/oauth/usage'
TOKEN_URL = 'https://platform.claude.com/v1/oauth/token'
MESSAGES_URL = 'https://api.anthropic.com/v1/messages'
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
CLI_VERSION = os.environ.get('CLAUDE_CLI_VERSION', '2.1.169')
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
    else None.
    """

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


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


def _map_from_limits(limits: List[Any], now: datetime) -> List[Dict[str, Any]]:
    """Map the canonical `limits` array (2026-06-30+) to quota dicts.

    session ← kind/group 'session'; weekly ← kind 'weekly_all'; model_specific ←
    any other group=='weekly' entry (kind like 'weekly_sonnet'). The status card is
    always Sonnet, so when several per-model entries exist Sonnet wins, else first.
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
            model = kind[len('weekly_'):] if kind.startswith('weekly_') else kind
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
    except Exception as e:
        # Never include the request/response payload here: it carries tokens.
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


def _http_get_usage(access_token: str) -> Dict[str, Any]:
    req = urllib.request.Request(USAGE_URL, headers={
        'Authorization': f'Bearer {access_token}',
        'anthropic-beta': 'oauth-2025-04-20',
        'User-Agent': f'claude-code/{CLI_VERSION}',
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
