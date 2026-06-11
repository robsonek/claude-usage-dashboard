# API Usage Fetcher (oauth/usage) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Zastąpić scraping PTY `claude /usage` bezpośrednim odczytem `GET https://api.anthropic.com/api/oauth/usage` (jak better-ccflare), z fallbackiem na dotychczasowy PTY przy każdej awarii ścieżki API.

**Architecture:** Nowy moduł `api_usage_fetcher.py` (stdlib-only: `urllib.request`, `json`) czyta token OAuth z `~/.claude/.credentials.json`, odświeża go przez `POST https://platform.claude.com/v1/oauth/token` gdy wygasa (atomowy zapis pliku credentiali z zachowaniem pozostałych pól), woła endpoint usage i mapuje odpowiedź na **dokładnie ten sam format snapshotu**, który produkuje `usage_fetcher.py` — dzięki temu `insert_to_db.py`, baza i dashboard nie wymagają żadnych zmian. `collect_history.sh` próbuje najpierw ścieżki API; niezerowy exit code → log WARN + fallback na PTY.

**Tech Stack:** Python 3.11 stdlib (urllib, json, tempfile), pytest (testy offline z monkeypatchem `_urlopen`), bash (collect_history.sh).

**Zweryfikowane na prodzie (2026-06-11):**

Realna odpowiedź `GET /api/oauth/usage` (konto Max):

```json
{"five_hour":{"utilization":0.0,"resets_at":"2026-06-11T08:40:00.235311+00:00"},"seven_day":{"utilization":58.0,"resets_at":"2026-06-15T10:59:59.235340+00:00"},"seven_day_oauth_apps":null,"seven_day_opus":null,"seven_day_sonnet":{"utilization":0.0,"resets_at":"2026-06-15T11:00:00.235353+00:00"},"seven_day_cowork":null,"seven_day_omelette":null,"tangelo":null,"iguana_necktie":null,"omelette_promotional":null,"cinder_cove":null,"extra_usage":{"is_enabled":true,"monthly_limit":20000,"used_credits":5353.0,"utilization":26.765,"currency":"USD","disabled_reason":null}}
```

Struktura `~/.claude/.credentials.json` → klucz `claudeAiOauth` z polami:
`accessToken`, `refreshToken`, `expiresAt` (epoch **ms**), `scopes`, `subscriptionType` (`"max"`), `rateLimitTier`.

Mapowanie pól:

| API | nasz format |
|---|---|
| `five_hour.utilization` | quota `session`, `percent_remaining = 100 - utilization` |
| `seven_day.utilization` | quota `weekly` |
| `seven_day_sonnet` / `seven_day_opus` (jedno z nich, drugie null) | quota `model_specific` + `model` |
| `*.resets_at` (ISO, mikrosekundy, `+00:00`) | `resets_at` znormalizowane do `%Y-%m-%dT%H:%M:%SZ` + `time_remaining_seconds` |
| `claudeAiOauth.subscriptionType` | `account_type` |
| `~/.claude.json` → `oauthAccount.emailAddress` | `email` (None gdy brak) |
| pozostałe okna (`seven_day_oauth_apps`, `seven_day_cowork`, `tangelo`, …) i `extra_usage` | **ignorowane** (schema bazy zna tylko 3 typy) |

Uwagi bezpieczeństwa:
- Refresh token **rotuje** przy odświeżeniu → zapis pliku credentiali musi być atomowy (`tempfile` + `os.replace`, chmod 0600) i zachować wszystkie nieznane pola.
- Jeśli refresh się nie powiedzie → **nic nie zapisujemy**, exit 1, fallback na PTY (CLI samo sobie odświeży token).
- Wyścig z równoległym odświeżeniem przez CLI nie występuje: na prodzie `claude` odpalany jest wyłącznie z `collect_history.sh` (fallback), a całość działa pod tym samym flockiem.
- Token NIE może trafiać do logów ani komunikatów błędów.

---

### Task 1: Mapowanie odpowiedzi API → format snapshotu (TDD)

**Files:**
- Create: `test_api_usage_fetcher.py`
- Create: `api_usage_fetcher.py`

- [ ] **Step 1: Write the failing tests** — `test_api_usage_fetcher.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest test_api_usage_fetcher.py -v` (lub `python3 -m pytest` jeśli brak venv lokalnie)
Expected: FAIL — `ModuleNotFoundError: No module named 'api_usage_fetcher'`

- [ ] **Step 3: Write the implementation** — `api_usage_fetcher.py` (na razie tylko mapowanie + stałe):

```python
"""Fetch Claude usage straight from the OAuth usage API (no PTY scraping).

Primary collector path: GET https://api.anthropic.com/api/oauth/usage with the
OAuth access token Claude CLI keeps in ~/.claude/.credentials.json (refreshed
here when expired, same endpoint+client_id Claude CLE uses). Output format is
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest test_api_usage_fetcher.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add api_usage_fetcher.py test_api_usage_fetcher.py
git commit -m "feat(fetcher): map oauth/usage API response to snapshot quota format"
```

### Task 2: Odczyt credentiali + decyzja o refreshu (TDD)

**Files:**
- Modify: `test_api_usage_fetcher.py` (dopisz testy)
- Modify: `api_usage_fetcher.py`

- [ ] **Step 1: Write the failing tests** — dopisz do `test_api_usage_fetcher.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest test_api_usage_fetcher.py -v -k "credentials or refresh"`
Expected: FAIL — `AttributeError: module 'api_usage_fetcher' has no attribute 'load_credentials'`

- [ ] **Step 3: Write the implementation** — dopisz do `api_usage_fetcher.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest test_api_usage_fetcher.py -v`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add api_usage_fetcher.py test_api_usage_fetcher.py
git commit -m "feat(fetcher): read Claude CLI OAuth credentials with expiry check"
```

### Task 3: Refresh tokenu + atomowy zapis credentiali (TDD)

**Files:**
- Modify: `test_api_usage_fetcher.py`
- Modify: `api_usage_fetcher.py`

- [ ] **Step 1: Write the failing tests** — dopisz do `test_api_usage_fetcher.py`:

```python
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


def test_refresh_updates_tokens_and_preserves_other_fields(creds_file, monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured['url'] = req.full_url
        captured['body'] = json.loads(req.data.decode())
        return FakeResponse({'access_token': 'sk-ant-oat01-NEW',
                             'refresh_token': 'sk-ant-ort01-NEW',
                             'expires_in': 28800})

    monkeypatch.setattr(auf, '_urlopen', fake_urlopen)
    creds = auf.load_credentials()
    oauth = auf.refresh_credentials(creds, now_ms=1_000_000)

    assert captured['url'] == auf.TOKEN_URL
    assert captured['body']['grant_type'] == 'refresh_token'
    assert captured['body']['refresh_token'] == 'sk-ant-ort01-BBB'
    assert captured['body']['client_id'] == auf.CLIENT_ID

    assert oauth['accessToken'] == 'sk-ant-oat01-NEW'
    on_disk = json.loads(creds_file.read_text())
    assert on_disk['claudeAiOauth']['accessToken'] == 'sk-ant-oat01-NEW'
    assert on_disk['claudeAiOauth']['refreshToken'] == 'sk-ant-ort01-NEW'
    assert on_disk['claudeAiOauth']['expiresAt'] == 1_000_000 + 28800 * 1000
    # untouched fields survive the rewrite
    assert on_disk['someOtherTopLevelKey'] == {'keep': 'me'}
    assert on_disk['claudeAiOauth']['subscriptionType'] == 'max'
    assert oct(os.stat(creds_file).st_mode & 0o777) == oct(0o600)


def test_refresh_failure_raises_and_leaves_file_untouched(creds_file, monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 400, 'Bad Request', None, None)

    import urllib.error
    monkeypatch.setattr(auf, '_urlopen', fake_urlopen)
    creds = auf.load_credentials()
    before = creds_file.read_text()
    with pytest.raises(auf.UsageApiError):
        auf.refresh_credentials(creds)
    assert creds_file.read_text() == before
```

(do importów testu dopisz na górze pliku: `import urllib.error` — i usuń lokalny import z testu)

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest test_api_usage_fetcher.py -v -k refresh_`
Expected: FAIL — `AttributeError: ... no attribute 'refresh_credentials'`

- [ ] **Step 3: Write the implementation** — dopisz do `api_usage_fetcher.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest test_api_usage_fetcher.py -v`
Expected: 14 passed

- [ ] **Step 5: Commit**

```bash
git add api_usage_fetcher.py test_api_usage_fetcher.py
git commit -m "feat(fetcher): OAuth token refresh with atomic credentials write-back"
```

### Task 4: Pobranie usage + email + składanie snapshotu (TDD)

**Files:**
- Modify: `test_api_usage_fetcher.py`
- Modify: `api_usage_fetcher.py`

- [ ] **Step 1: Write the failing tests** — dopisz do `test_api_usage_fetcher.py`:

```python
@pytest.fixture
def claude_config(tmp_path, monkeypatch):
    cfg = tmp_path / '.claude.json'
    cfg.write_text(json.dumps({'oauthAccount': {'emailAddress': 'rob@example.com'}}))
    monkeypatch.setattr(auf, 'CLAUDE_CONFIG_FILE', str(cfg))
    return cfg


def _fresh_creds(monkeypatch):
    """Make the stored token look fresh so fetch_usage skips the refresh path."""
    far_future = int(time.time() * 1000) + 3_600_000
    monkeypatch.setitem(CREDS['claudeAiOauth'], 'expiresAt', far_future)


def test_fetch_usage_returns_snapshot_format(creds_file, claude_config, monkeypatch):
    import time as _time
    _fresh_creds(monkeypatch)
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured['url'] = req.full_url
        captured['auth'] = req.get_header('Authorization')
        captured['beta'] = req.get_header('Anthropic-beta')
        return FakeResponse(PROD_SAMPLE)

    monkeypatch.setattr(auf, '_urlopen', fake_urlopen)
    result = auf.fetch_usage()

    assert captured['url'] == auf.USAGE_URL
    assert captured['auth'] == 'Bearer sk-ant-oat01-AAA'
    assert captured['beta'] == 'oauth-2025-04-20'
    assert result['account_type'] == 'max'
    assert result['email'] == 'rob@example.com'
    assert result['source'] == 'api'
    assert len(result['quotas']) == 3
    # captured_at parses in insert_to_db's expected shape
    datetime.strptime(result['captured_at'], '%Y-%m-%dT%H:%M:%SZ')


def test_fetch_usage_missing_core_windows_raises(creds_file, claude_config, monkeypatch):
    _fresh_creds(monkeypatch)
    monkeypatch.setattr(auf, '_urlopen',
                        lambda req, timeout=None: FakeResponse({'five_hour': None,
                                                                'seven_day': None}))
    with pytest.raises(auf.UsageApiError):
        auf.fetch_usage()


def test_email_none_when_config_missing(creds_file, tmp_path, monkeypatch):
    _fresh_creds(monkeypatch)
    monkeypatch.setattr(auf, 'CLAUDE_CONFIG_FILE', str(tmp_path / 'absent.json'))
    monkeypatch.setattr(auf, '_urlopen',
                        lambda req, timeout=None: FakeResponse(PROD_SAMPLE))
    result = auf.fetch_usage()
    assert result['email'] is None
```

(do importów testu dopisz `import time` na górze pliku)

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest test_api_usage_fetcher.py -v -k fetch_usage`
Expected: FAIL — `AttributeError: ... no attribute 'fetch_usage'`

- [ ] **Step 3: Write the implementation** — dopisz do `api_usage_fetcher.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest test_api_usage_fetcher.py -v`
Expected: 17 passed

- [ ] **Step 5: Commit**

```bash
git add api_usage_fetcher.py test_api_usage_fetcher.py
git commit -m "feat(fetcher): fetch_usage over oauth/usage API with snapshot output"
```

### Task 5: Kontrakt CLI (exit code + error JSON) (TDD)

**Files:**
- Modify: `test_api_usage_fetcher.py`
- Modify: `api_usage_fetcher.py`

- [ ] **Step 1: Write the failing tests** — dopisz do `test_api_usage_fetcher.py`:

```python
def test_main_success_prints_json_and_exits_zero(creds_file, claude_config,
                                                 monkeypatch, capsys):
    _fresh_creds(monkeypatch)
    monkeypatch.setattr(auf, '_urlopen',
                        lambda req, timeout=None: FakeResponse(PROD_SAMPLE))
    assert auf.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out['source'] == 'api'
    assert len(out['quotas']) == 3


def test_main_failure_prints_error_json_and_exits_one(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(auf, 'CREDENTIALS_FILE', str(tmp_path / 'absent.json'))
    assert auf.main() == 1
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out['error'] == 'API usage fetch failed'
    assert 'api_usage_fetcher' in captured.err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest test_api_usage_fetcher.py -v -k main_`
Expected: FAIL — `AttributeError: ... no attribute 'main'`

- [ ] **Step 3: Write the implementation** — dopisz na końcu `api_usage_fetcher.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass** (cały zestaw — też regresja istniejących)

Run: `venv/bin/python -m pytest -x -q`
Expected: wszystkie testy projektu przechodzą (19 nowych + dotychczasowe)

- [ ] **Step 5: Commit**

```bash
git add api_usage_fetcher.py test_api_usage_fetcher.py
git commit -m "feat(fetcher): CLI entrypoint with error-JSON contract for collector fallback"
```

### Task 6: Hybryda w collect_history.sh (API → fallback PTY)

**Files:**
- Modify: `collect_history.sh` (blok „Fetch data using usage_fetcher.py")

- [ ] **Step 1: Zmień blok fetch** — w `collect_history.sh` zastąp:

```bash
# Fetch data using usage_fetcher.py — stderr goes to the cron log, not /dev/null.
# CLAUDE_USAGE_RAW_DIR: fetcher dumps raw PTY bytes here only for incomplete/glitched
# readings, so we can diagnose what went wrong after the fact.
export CLAUDE_USAGE_RAW_DIR="$DATA_DIR/raw_debug"
USAGE_JSON=$("$VENV_PYTHON" "$SCRIPT_DIR/usage_fetcher.py")
FETCH_STATUS=$?
```

na:

```bash
# Fetch data: primary path is the oauth/usage HTTP API (api_usage_fetcher.py —
# fast, structured, no PTY render glitches). Any non-zero exit / empty output
# falls back to the PTY scraper (usage_fetcher.py), which also self-heals the
# OAuth credentials via claude CLI. Both run under this script's flock, so the
# two paths never race each other on the credentials file.
# CLAUDE_USAGE_RAW_DIR: PTY fetcher dumps raw bytes here only for incomplete/
# glitched readings, so we can diagnose what went wrong after the fact.
export CLAUDE_USAGE_RAW_DIR="$DATA_DIR/raw_debug"
USAGE_JSON=$("$VENV_PYTHON" "$SCRIPT_DIR/api_usage_fetcher.py")
FETCH_STATUS=$?
if [ $FETCH_STATUS -ne 0 ] || [ -z "$USAGE_JSON" ]; then
    log "WARN: API fetcher failed (exit $FETCH_STATUS), falling back to PTY claude /usage"
    USAGE_JSON=$("$VENV_PYTHON" "$SCRIPT_DIR/usage_fetcher.py")
    FETCH_STATUS=$?
fi
```

(reszta skryptu — walidacja `"error"`, zapis JSON, insert, retencja — bez zmian; działa identycznie dla obu źródeł)

- [ ] **Step 2: Sprawdź składnię i przebieg lokalnie**

Run: `bash -n collect_history.sh && ./collect_history.sh; echo "exit=$?"`
Expected: `bash -n` cicho; run lokalny: API path zadziała (mac ma credentiale w Keychain, więc spodziewany jest **fallback na PTY** z logiem WARN — to potwierdza działanie fallbacku) i `exit=0`, nowy plik w `data/$(date +%F)/`

- [ ] **Step 3: Commit**

```bash
git add collect_history.sh
git commit -m "feat(collector): prefer oauth/usage API fetch, PTY scraper as fallback"
```

### Task 7: Dokumentacja + bump wersji

**Files:**
- Modify: `config.py:8` (VERSION `1.0.5` → `1.1.0` — minor, nowa funkcja)
- Modify: `CLAUDE.md` (sekcja „Jak działa" + „Struktura projektu")
- Modify: `README.md` (jeśli opisuje mechanizm zbierania)

- [ ] **Step 1: Bump wersji** — w `config.py`: `VERSION = '1.1.0'`

- [ ] **Step 2: CLAUDE.md** — w „Struktura projektu" dodaj wiersz:

```
├── api_usage_fetcher.py   # Pobieranie danych z API oauth/usage (ścieżka główna)
```

a w „Jak działa" zmień punkt 2 na:

```
2. **collect_history.sh** - najpierw `api_usage_fetcher.py` (GET https://api.anthropic.com/api/oauth/usage
   tokenem OAuth z ~/.claude/.credentials.json, z auto-refreshem); przy błędzie fallback na
   `usage_fetcher.py` (PTY `claude /usage`). Wynik zapisuje do:
```

oraz dopisz krótką sekcję (po „Fallback braku odczytu"):

```
## Ścieżka API (api_usage_fetcher.py)

Główne źródło danych od v1.1.0: `GET https://api.anthropic.com/api/oauth/usage`
(nagłówki: `Authorization: Bearer <accessToken>`, `anthropic-beta: oauth-2025-04-20`,
`User-Agent: claude-code/<wersja>`). Token z `~/.claude/.credentials.json`
(`claudeAiOauth`), odświeżany gdy zostało <2 min życia przez
`POST https://platform.claude.com/v1/oauth/token` (client_id Claude Code) —
zapis pliku atomowy, rotowany refreshToken zachowywany, chmod 600. Mapowanie:
`five_hour`→session, `seven_day`→weekly, `seven_day_sonnet|opus`→model_specific;
`percent_remaining = 100 - utilization`. Endpoint jest nieoficjalny — przy każdej
awarii collector spada na PTY (`usage_fetcher.py`), więc zbieranie nie ma SPOF.
Snapshoty ze ścieżki API mają w JSON-backupie klucz `"source": "api"`.
Testy: `test_api_usage_fetcher.py` (offline, HTTP mockowany).
```

- [ ] **Step 3: Pełny test suite**

Run: `venv/bin/python -m pytest -q`
Expected: wszystko zielone

- [ ] **Step 4: Commit**

```bash
git add config.py CLAUDE.md README.md
git commit -m "docs: document API usage fetch path; bump version to 1.1.0"
```

### Task 8: Deploy na prod + weryfikacja end-to-end

**Files:** brak zmian kodu — rsync + obserwacja

- [ ] **Step 1: Surgical rsync zmienionych plików** (zgodnie z pamięcią o deployu — bez pełnego `rsync ./`):

```bash
rsync -avz api_usage_fetcher.py collect_history.sh config.py CLAUDE.md README.md test_api_usage_fetcher.py \
  robson@ai.onee.pl:/home/robson/claude-dashboard/
```

- [ ] **Step 2: Ręczny test fetchera na prodzie** (przed czekaniem na cron):

```bash
ssh robson@ai.onee.pl 'cd /home/robson/claude-dashboard && venv/bin/python api_usage_fetcher.py; echo "exit=$?"'
```

Expected: JSON z `"source": "api"`, 3 kwoty, `exit=0`

- [ ] **Step 3: Pełny przebieg collectora na prodzie**:

```bash
ssh robson@ai.onee.pl 'cd /home/robson/claude-dashboard && ./collect_history.sh; echo "exit=$?"; sqlite3 usage.db "SELECT s.captured_at, q.quota_type, q.model, q.percent_remaining, q.resets_at FROM snapshots s JOIN quotas q ON q.snapshot_id=s.id ORDER BY s.id DESC LIMIT 3;"'
```

Expected: `exit=0`, 3 świeże wiersze kwot zgodne z dashboardem

- [ ] **Step 4: Restart gunicorna** (bump wersji w UI — chip 1.1.0):

```bash
ssh robson@ai.onee.pl 'sudo systemctl restart claude-dashboard'
```

- [ ] **Step 5: Obserwacja jednego ticka crona** (cron co 5 min; sprawdź log + brak WARN o fallbacku):

```bash
ssh robson@ai.onee.pl 'sleep 360; tail -20 /home/robson/claude-dashboard/data/cron.log 2>/dev/null || grep CRON /var/log/syslog | tail -5; ls -lt /home/robson/claude-dashboard/data/$(date +%F)/ | head -4'
```

Expected: nowy snapshot bez WARN o fallbacku (albo z ustaloną lokalizacją logu crona)

- [ ] **Step 6: Commit + push** (repo lokalne; deploy już zrobiony):

```bash
git push
```

---

## Self-Review

- **Spec coverage:** mapowanie pól ✓ (Task 1), refresh tokenu z rotacją i atomowym zapisem ✓ (Task 3), email z ~/.claude.json ✓ (Task 4), kontrakt fallbacku ✓ (Task 5+6), brak zmian w DB/dashboardzie ✓ (format wyjścia identyczny, weryfikowany testem `captured_at`/kluczy), walidacja na realnej odpowiedzi z prod ✓ (PROD_SAMPLE), wersjonowanie przy commicie ✓ (Task 7), deploy chirurgiczny + weryfikacja ✓ (Task 8).
- **Placeholder scan:** brak TBD/TODO; każdy krok ma kod lub dokładną komendę.
- **Type consistency:** `map_usage_response(api, now)`, `load_credentials()`, `needs_refresh(oauth, now_ms)`, `refresh_credentials(creds, now_ms)`, `fetch_usage()`, `main()` — nazwy spójne między taskami; testy używają `auf.<nazwa>` zgodnie z definicjami.
