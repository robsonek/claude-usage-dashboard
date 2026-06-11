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
