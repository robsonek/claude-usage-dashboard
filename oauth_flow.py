"""Add a Claude account via the Claude Code OAuth flow (manual code paste).

Flow: build_authorize_url() → user authorizes at claude.ai → Anthropic shows a
`code#state` string → exchange_code() swaps it (with the PKCE verifier) for
tokens → fetch_profile() reads email + plan. Mirrors better-ccflare's
AnthropicOAuthProvider and the claude CLI itself.
"""
import base64
import hashlib
import json
import secrets
import time
import urllib.request
from urllib.parse import urlencode

from api_usage_fetcher import CLIENT_ID, TOKEN_URL, CLI_VERSION, TOKEN_ENDPOINT_UA

# Mirror the working better-ccflare reference exactly. The token endpoint sits
# behind Cloudflare: spoofing `claude-code/<ver>` → 429 (anti-abuse), urllib's
# default `Python-urllib` → 403 (edge block). ccflare works because Bun sends
# `Bun/<ver>` — so we send TOKEN_ENDPOINT_UA on the exchange. Authorize host
# stays claude.ai (ccflare uses it and works; the db6645f claude.com/cai switch
# was a misdiagnosis).
AUTHORIZE_URL = 'https://claude.ai/oauth/authorize'
REDIRECT_URI = 'https://platform.claude.com/oauth/code/callback'
PROFILE_URL = 'https://api.anthropic.com/api/oauth/profile'
SCOPES = ['org:create_api_key', 'user:profile', 'user:inference',
          'user:sessions:claude_code', 'user:mcp_servers', 'user:file_upload']

_urlopen = urllib.request.urlopen


class OAuthError(Exception):
    """Failure during the OAuth add-account flow."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def generate_pkce() -> dict:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return {'verifier': verifier, 'challenge': challenge}


def generate_state() -> str:
    return _b64url(secrets.token_bytes(32))


def build_authorize_url(challenge: str, state: str) -> str:
    params = {
        'code': 'true',
        'client_id': CLIENT_ID,
        'response_type': 'code',
        'redirect_uri': REDIRECT_URI,
        'scope': ' '.join(SCOPES),
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
        'state': state,
    }
    return f'{AUTHORIZE_URL}?{urlencode(params)}'


def exchange_code(code_input: str, verifier: str, now_ms=None) -> dict:
    """Swap a pasted `code#state` (or bare code) + PKCE verifier for tokens."""
    parts = code_input.strip().split('#')
    code = parts[0]
    state = parts[1] if len(parts) > 1 else ''
    body = json.dumps({
        'grant_type': 'authorization_code',
        'code': code,
        'state': state,
        'code_verifier': verifier,
        'redirect_uri': REDIRECT_URI,
        'client_id': CLIENT_ID,
    }).encode()
    # Token endpoint UA must be neither the spoofed `claude-code/<ver>` (429) nor
    # urllib's default `Python-urllib` (Cloudflare 403). Use Bun's UA like ccflare.
    req = urllib.request.Request(TOKEN_URL, data=body, method='POST', headers={
        'Content-Type': 'application/json',
        'User-Agent': TOKEN_ENDPOINT_UA,
    })
    try:
        with _urlopen(req, timeout=15) as resp:
            token = json.loads(resp.read().decode())
    except Exception as e:
        raise OAuthError(f'code exchange failed: {type(e).__name__}: {e}') from e
    if not token.get('access_token') or not token.get('refresh_token'):
        raise OAuthError('token response missing access/refresh token')
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    return {
        'access_token': token['access_token'],
        'refresh_token': token['refresh_token'],
        'expires_at': int(now_ms + token.get('expires_in', 0) * 1000),
    }


def fetch_profile(access_token: str) -> dict:
    """Read account email + plan from the OAuth profile endpoint."""
    req = urllib.request.Request(PROFILE_URL, headers={
        'Authorization': f'Bearer {access_token}',
        'anthropic-beta': 'oauth-2025-04-20',
        'User-Agent': f'claude-code/{CLI_VERSION}',
        'Accept': 'application/json',
    })
    try:
        with _urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        raise OAuthError(f'profile fetch failed: {type(e).__name__}: {e}') from e
    account = data.get('account') or {}
    org = data.get('organization') or {}
    # Individual plans surface as account-level flags; Team/Enterprise only show
    # up as organization.organization_type (e.g. "claude_team") with both flags
    # False. Derive from the org type as a fallback so Team isn't "unknown".
    if account.get('has_claude_max'):
        account_type = 'max'
    elif account.get('has_claude_pro'):
        account_type = 'pro'
    else:
        org_type = org.get('organization_type') or ''
        account_type = (org_type[len('claude_'):] if org_type.startswith('claude_')
                        else org_type) or 'unknown'
    return {'email': account.get('email'), 'account_type': account_type}
