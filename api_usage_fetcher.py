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
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

USAGE_URL = 'https://api.anthropic.com/api/oauth/usage'
TOKEN_URL = 'https://platform.claude.com/v1/oauth/token'
# Public Claude Code OAuth client id (same one better-ccflare ships as default).
CLIENT_ID = os.environ.get('CLAUDE_OAUTH_CLIENT_ID',
                           '9d1c250a-e61b-44d9-88ed-5944d1962f5e')
# Only used for the User-Agent header; keep loosely in sync with the CLI.
CLI_VERSION = os.environ.get('CLAUDE_CLI_VERSION', '2.1.169')
HTTP_TIMEOUT = 15
REFRESH_MARGIN_MS = 120_000  # refresh when less than 2 min of token life left

# Indirection so tests can stub HTTP without touching the network.
_urlopen = urllib.request.urlopen

# (api field, quota type, model) — model windows are mutually exclusive in the
# API (the absent one is null), first non-null wins.
WINDOW_MAP = [
    ('five_hour', 'session', ''),
    ('seven_day', 'weekly', ''),
    ('seven_day_sonnet', 'model_specific', 'sonnet'),
    ('seven_day_opus', 'model_specific', 'opus'),
]


class UsageApiError(Exception):
    """Any failure of the API fetch path (caller records it per account)."""


def map_usage_response(api: Dict[str, Any],
                       now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Map the oauth/usage JSON to the quota dicts usage_fetcher.py produces.

    Unknown windows and nulls are skipped; a malformed resets_at drops the reset
    fields but keeps the percentage (same degradation the PTY parser has).
    """
    now = now or datetime.now(timezone.utc)
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

        quota: Dict[str, Any] = {
            'type': quota_type,
            'percent_remaining': round(100.0 - float(utilization), 3),
        }
        if model:
            quota['model'] = model

        resets_raw = window.get('resets_at')
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

        quotas.append(quota)

    return quotas


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
    # NO User-Agent here: the token endpoint (platform.claude.com/v1/oauth/token)
    # 429-flags requests that spoof `claude-code/<ver>` without the real CLI
    # fingerprint. better-ccflare sends only Content-Type on this POST and works.
    # (The usage endpoint below DOES accept the UA — keep it there.)
    req = urllib.request.Request(TOKEN_URL, data=body, method='POST', headers={
        'Content-Type': 'application/json',
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
    except Exception as e:
        raise UsageApiError(f'usage request failed: {type(e).__name__}: {e}') from e
