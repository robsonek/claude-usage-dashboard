"""Fetch Claude usage straight from the OAuth usage API (no PTY scraping).

Primary collector path: GET https://api.anthropic.com/api/oauth/usage with the
OAuth access token Claude CLI keeps in ~/.claude/.credentials.json (refreshed
here when expired, same endpoint+client_id Claude Code uses). Output format is
identical to usage_fetcher.py, so insert_to_db.py / DB / dashboard are untouched.
collect_history.sh falls back to the PTY scraper when this exits non-zero.

Method modeled on better-ccflare (packages/providers/src/usage-fetcher.ts).
"""
import json
import os
import sys
import tempfile
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
CREDENTIALS_FILE = os.environ.get(
    'CLAUDE_CREDENTIALS_FILE', os.path.expanduser('~/.claude/.credentials.json'))
CLAUDE_CONFIG_FILE = os.environ.get(
    'CLAUDE_CONFIG_FILE', os.path.expanduser('~/.claude.json'))
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
    """Any failure of the API fetch path (caller falls back to PTY)."""


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


def load_credentials() -> Dict[str, Any]:
    """Read the whole credentials file (all top-level keys preserved for write-back)."""
    try:
        with open(CREDENTIALS_FILE, encoding='utf-8') as f:
            creds = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise UsageApiError(f'cannot read credentials file: {e}') from e
    oauth = creds.get('claudeAiOauth')
    if not isinstance(oauth, dict) or not oauth.get('accessToken'):
        raise UsageApiError('credentials file has no claudeAiOauth.accessToken')
    return creds


def needs_refresh(oauth: Dict[str, Any], now_ms: Optional[int] = None) -> bool:
    expires_at = oauth.get('expiresAt')
    if not isinstance(expires_at, (int, float)):
        return True
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    return now_ms >= expires_at - REFRESH_MARGIN_MS


def _atomic_write_credentials(creds: Dict[str, Any]) -> None:
    """Rewrite the credentials file atomically (tmp file + os.replace, mode 0600)."""
    directory = os.path.dirname(CREDENTIALS_FILE) or '.'
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix='.credentials.')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(creds, f)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, CREDENTIALS_FILE)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def refresh_credentials(creds: Dict[str, Any],
                        now_ms: Optional[int] = None) -> Dict[str, Any]:
    """Refresh the OAuth access token and persist rotated tokens back to disk.

    On any failure nothing is written — the caller exits non-zero and the PTY
    fallback (claude CLI) refreshes its own credentials as before.
    """
    oauth = creds['claudeAiOauth']
    refresh_token = oauth.get('refreshToken')
    if not refresh_token:
        raise UsageApiError('no refreshToken in credentials file')

    body = json.dumps({
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
        'client_id': CLIENT_ID,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method='POST', headers={
        'Content-Type': 'application/json',
        'User-Agent': f'claude-code/{CLI_VERSION}',
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

    oauth['accessToken'] = access_token
    if token.get('refresh_token'):
        oauth['refreshToken'] = token['refresh_token']
    if isinstance(token.get('expires_in'), (int, float)):
        oauth['expiresAt'] = int(now_ms + token['expires_in'] * 1000)

    try:
        _atomic_write_credentials(creds)
    except OSError as e:
        raise UsageApiError(f'cannot write credentials file: {e}') from e
    return oauth


def read_account_email() -> Optional[str]:
    """Best-effort e-mail from ~/.claude.json (oauthAccount.emailAddress)."""
    try:
        with open(CLAUDE_CONFIG_FILE, encoding='utf-8') as f:
            cfg = json.load(f)
        email = (cfg.get('oauthAccount') or {}).get('emailAddress')
        return email if isinstance(email, str) and '@' in email else None
    except (OSError, json.JSONDecodeError):
        return None


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


def fetch_usage() -> Dict[str, Any]:
    """Full API path: credentials → (refresh) → GET usage → snapshot dict."""
    creds = load_credentials()
    oauth = creds['claudeAiOauth']
    if needs_refresh(oauth):
        oauth = refresh_credentials(creds)

    api = _http_get_usage(oauth['accessToken'])
    quotas = map_usage_response(api)
    types = {q['type'] for q in quotas}
    if 'session' not in types or 'weekly' not in types:
        raise UsageApiError(
            'incomplete usage response, windows: %s' % sorted(api.keys()))

    return {
        'account_type': oauth.get('subscriptionType') or 'unknown',
        'email': read_account_email(),
        'quotas': quotas,
        'captured_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'source': 'api',
    }


def main() -> int:
    """CLI entry: snapshot JSON on stdout, exit 0; error JSON + exit 1 on failure.

    Non-zero exit is the fallback signal for collect_history.sh. Error details
    go to stderr (cron log); they never contain token material.
    """
    try:
        result = fetch_usage()
    except Exception as e:
        print(json.dumps({'error': 'API usage fetch failed', 'details': str(e)}))
        print(f'[api_usage_fetcher] {e}', file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
