# Multi-Account OAuth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dodawanie kont Claude przez OAuth (ręczne wklejenie kodu) i obsługa wielu własnych kont — z szyfrowaniem tokenów w bazie, przełącznikiem kont w UI i izolacją historii per konto.

**Architecture:** Tokeny i metadane kont trafiają do nowej tabeli `accounts` (tokeny szyfrowane Fernetem). `snapshots` dostaje `account_id`. Nowy moduł `oauth_flow.py` realizuje PKCE + wymianę kodu + profil; `collect_all.py` pollu­je wszystkie aktywne konta sekwencyjnie (refresh tokenu → zapis do bazy → fetch → snapshot). Flask dostaje strony zarządzania kontami i parametr `account` na endpointach API; dashboard — pasek mini-kart z przełączaniem.

**Tech Stack:** Python 3.11 stdlib (urllib, hashlib, secrets, base64), `cryptography` (Fernet), Flask, SQLite, pytest (HTTP mockowany przez `_urlopen`), vanilla JS + Chart.js.

**Zweryfikowane na prodzie (2026-06-11):**
- `GET https://api.anthropic.com/api/oauth/profile` (te same nagłówki co usage) zwraca
  `{"account":{"email":"…","has_claude_max":true,"has_claude_pro":false,…},"organization":{…}}`.
  → `account_type = 'max' if has_claude_max else 'pro' if has_claude_pro else 'unknown'`.
- Wymiana kodu (better-ccflare): `POST https://platform.claude.com/v1/oauth/token`,
  body `{grant_type:"authorization_code", code, state, code_verifier, redirect_uri, client_id}`,
  kod wklejany w formacie `kod#state` (split po `#`). Zwraca `access_token`/`refresh_token`/`expires_in`.
- Authorize URL: `https://claude.ai/oauth/authorize?code=true&client_id=<id>&response_type=code&redirect_uri=https://platform.claude.com/oauth/code/callback&scope=<spacja>&code_challenge=<S256>&code_challenge_method=S256&state=<state>`.
- client_id Claude Code: `9d1c250a-e61b-44d9-88ed-5944d1962f5e` (już w `api_usage_fetcher.CLIENT_ID`).

**Decyzja (uproszczenie względem specu):** rezygnujemy z „bootstrap z pliku w collectorze" (YAGNI).
Migracja: deploy → dodaj istniejące konto przez OAuth → backfill po emailu. Przerwa w zbieraniu to
maksymalnie jeden tick crona. Collector od początku czyta wyłącznie bazę.

## Struktura plików

- **Create** `crypto_util.py` — `encrypt(str)->str` / `decrypt(str)->str` (Fernet, klucz z `config.TOKEN_ENCRYPTION_KEY`).
- **Create** `oauth_flow.py` — PKCE, authorize URL, `exchange_code`, `fetch_profile`.
- **Create** `collect_all.py` — runner wielokontowy (zastępuje wywołanie `api_usage_fetcher.py` w cronie).
- **Create** `templates/accounts.html` — zarządzanie kontami + flow „Dodaj konto".
- **Modify** `config.py` — `TOKEN_ENCRYPTION_KEY`, bump VERSION 1.3.0.
- **Modify** `requirements.txt` — `cryptography`.
- **Modify** `database.py` — tabela `accounts`, `account_id` na `snapshots`, CRUD kont, `account_id` w `get_current`/`get_history`, `backfill_account_by_email`.
- **Modify** `api_usage_fetcher.py` — wydziel `needs_refresh_ms`/`refresh_access_token`/`build_snapshot`; usuń ścieżkę czytania pliku z `fetch_usage`/`main` (zostają tylko jako dawny tryb? Nie — usuwamy, collector_all przejmuje).
- **Modify** `app.py` — routy `/accounts*`, parametr `account` na `/api/*`.
- **Modify** `templates/dashboard.html`, `static/style.css` — pasek kont + przełącznik.
- **Create** testy: `test_crypto_util.py`, `test_oauth_flow.py`, `test_accounts_db.py`, `test_collect_all.py`, `test_multi_account_api.py`.

---

### Task 1: Szyfrowanie tokenów (crypto_util.py)

**Files:**
- Create: `crypto_util.py`
- Create: `test_crypto_util.py`
- Modify: `config.py`
- Modify: `requirements.txt`

- [ ] **Step 1: Dopisz zależność** — w `requirements.txt` dodaj wiersz:

```
cryptography>=42.0.0
```

Zainstaluj lokalnie: `python3 -m pip install 'cryptography>=42.0.0'`

- [ ] **Step 2: Dodaj klucz do config.py** — po `SESSION_LIFETIME_HOURS = 24` dopisz:

```python
# Fernet key for encrypting OAuth tokens at rest in the accounts table.
# Generate once: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Store in .env (never in repo/rsync). Losing it = re-add every account.
TOKEN_ENCRYPTION_KEY = os.environ.get('TOKEN_ENCRYPTION_KEY')
```

- [ ] **Step 3: Write the failing tests** — `test_crypto_util.py`:

```python
"""Tests for crypto_util: Fernet round-trip of token strings."""
import importlib

import pytest
from cryptography.fernet import Fernet

import config
import crypto_util


@pytest.fixture
def key(monkeypatch):
    k = Fernet.generate_key().decode()
    monkeypatch.setattr(config, 'TOKEN_ENCRYPTION_KEY', k)
    crypto_util._reset_cache()  # drop any cached Fernet built from a prior key
    return k


def test_round_trip(key):
    token = 'sk-ant-oat01-secret-value'
    enc = crypto_util.encrypt(token)
    assert enc != token
    assert crypto_util.decrypt(enc) == token


def test_ciphertext_differs_each_call(key):
    # Fernet embeds a random IV/timestamp, so two encryptions differ
    assert crypto_util.encrypt('x') != crypto_util.encrypt('x')


def test_missing_key_raises(monkeypatch):
    monkeypatch.setattr(config, 'TOKEN_ENCRYPTION_KEY', None)
    crypto_util._reset_cache()
    with pytest.raises(crypto_util.CryptoError):
        crypto_util.encrypt('x')
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `python3 -m pytest test_crypto_util.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'crypto_util'`

- [ ] **Step 5: Write the implementation** — `crypto_util.py`:

```python
"""Encrypt/decrypt OAuth token strings at rest with Fernet.

Key comes from config.TOKEN_ENCRYPTION_KEY (env TOKEN_ENCRYPTION_KEY). The
Fernet instance is cached; _reset_cache() exists so tests can swap keys.
"""
from cryptography.fernet import Fernet

import config

_fernet = None


class CryptoError(Exception):
    """Raised when encryption is requested without a configured key."""


def _reset_cache() -> None:
    global _fernet
    _fernet = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = config.TOKEN_ENCRYPTION_KEY
        if not key:
            raise CryptoError(
                'TOKEN_ENCRYPTION_KEY is not set — cannot encrypt/decrypt tokens')
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _get_fernet().decrypt(token.encode()).decode()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m pytest test_crypto_util.py -q`
Expected: 3 passed

- [ ] **Step 7: Commit**

```bash
git add crypto_util.py test_crypto_util.py config.py requirements.txt
git commit -m "feat(accounts): Fernet token encryption helper"
```

### Task 2: Tabela accounts + account_id na snapshots (schema + migracja)

**Files:**
- Modify: `database.py:73-104` (`_create_tables`)
- Create: `test_accounts_db.py`

- [ ] **Step 1: Write the failing test** — `test_accounts_db.py`:

```python
"""Tests for accounts table schema, CRUD, token encryption, account_id wiring."""
import os

import pytest
from cryptography.fernet import Fernet

import config
import crypto_util
from database import UsageDatabase


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'TOKEN_ENCRYPTION_KEY', Fernet.generate_key().decode())
    crypto_util._reset_cache()
    d = UsageDatabase(str(tmp_path / 'test.db'))
    yield d
    d.close()


def test_accounts_table_exists(db):
    cur = db.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='accounts'")
    assert cur.fetchone() is not None


def test_snapshots_has_account_id_column(db):
    cur = db.conn.execute("PRAGMA table_info(snapshots)")
    cols = {r['name'] for r in cur.fetchall()}
    assert 'account_id' in cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_accounts_db.py -q`
Expected: FAIL — accounts table missing

- [ ] **Step 3: Write the implementation** — w `database.py`, w `_create_tables` dodaj do `executescript` (po tabeli `quotas`, przed `CREATE INDEX`):

```sql
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                email TEXT,
                account_type TEXT,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at DATETIME,
                last_polled_at DATETIME,
                last_error TEXT
            );
```

a po bloku `executescript` (po istniejącym ALTER dla `period_start_at`) dodaj migrację kolumny:

```python
        # account_id on snapshots (nullable; legacy rows keep NULL until backfilled)
        cursor.execute("PRAGMA table_info(snapshots)")
        snap_cols = {row['name'] for row in cursor.fetchall()}
        if 'account_id' not in snap_cols:
            cursor.execute("ALTER TABLE snapshots ADD COLUMN account_id INTEGER")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_snapshots_account_id ON snapshots(account_id)")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_accounts_db.py -q`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add database.py test_accounts_db.py
git commit -m "feat(accounts): accounts table + account_id column on snapshots"
```

### Task 3: CRUD kont w bazie (upsert, list, pollable, tokens, flags)

**Files:**
- Modify: `database.py` (nowe metody przed `get_snapshot_count`)
- Modify: `test_accounts_db.py`

- [ ] **Step 1: Write the failing tests** — dopisz do `test_accounts_db.py`:

```python
def _add(db, **kw):
    base = dict(label='Main', email='a@x.com', account_type='max',
                access_token='AT-1', refresh_token='RT-1', expires_at=1000)
    base.update(kw)
    return db.add_or_update_account(**base)


def test_add_account_encrypts_tokens(db):
    acc_id = _add(db)
    row = db.conn.execute(
        "SELECT access_token, refresh_token FROM accounts WHERE id=?", (acc_id,)).fetchone()
    assert row['access_token'] != 'AT-1'  # stored encrypted
    assert crypto_util.decrypt(row['access_token']) == 'AT-1'


def test_list_accounts_omits_tokens(db):
    _add(db)
    accounts = db.list_accounts()
    assert len(accounts) == 1
    assert accounts[0]['email'] == 'a@x.com'
    assert 'access_token' not in accounts[0]


def test_get_pollable_accounts_decrypts_tokens(db):
    _add(db)
    pollable = db.get_pollable_accounts()
    assert pollable[0]['access_token'] == 'AT-1'
    assert pollable[0]['refresh_token'] == 'RT-1'


def test_upsert_by_email_updates_not_duplicates(db):
    first = _add(db, access_token='AT-1')
    second = _add(db, access_token='AT-2', label='Renamed')
    assert first == second  # same row
    assert len(db.list_accounts()) == 1
    assert db.get_pollable_accounts()[0]['access_token'] == 'AT-2'


def test_update_account_tokens(db):
    acc_id = _add(db)
    db.update_account_tokens(acc_id, 'AT-NEW', 'RT-NEW', 2000)
    p = db.get_pollable_accounts()[0]
    assert (p['access_token'], p['refresh_token'], p['expires_at']) == ('AT-NEW', 'RT-NEW', 2000)


def test_set_active_excludes_from_pollable(db):
    acc_id = _add(db)
    db.set_account_active(acc_id, False)
    assert db.get_pollable_accounts() == []
    assert len(db.list_accounts()) == 1  # still listed for UI


def test_rename_and_record_poll_and_delete(db):
    acc_id = _add(db)
    db.rename_account(acc_id, 'New Label')
    db.record_account_poll(acc_id, error='boom')
    acc = db.list_accounts()[0]
    assert acc['label'] == 'New Label'
    assert acc['last_error'] == 'boom'
    assert acc['last_polled_at'] is not None
    db.delete_account(acc_id)
    assert db.list_accounts() == []


def test_get_default_account_id_first_active(db):
    a = _add(db, email='a@x.com')
    b = _add(db, email='b@x.com')
    db.set_account_active(a, False)
    assert db.get_default_account_id() == b
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest test_accounts_db.py -q`
Expected: FAIL — `AttributeError: ... no attribute 'add_or_update_account'`

- [ ] **Step 3: Write the implementation** — w `database.py` dodaj metody (przed `def get_snapshot_count`):

```python
    # ---- accounts ----

    def add_or_update_account(self, label, email, account_type,
                              access_token, refresh_token, expires_at) -> int:
        """Insert a new account or, if one with the same email exists, refresh
        its tokens/label/type. Tokens are stored encrypted. Returns the id."""
        import crypto_util
        enc_at = crypto_util.encrypt(access_token)
        enc_rt = crypto_util.encrypt(refresh_token)
        cur = self.conn.cursor()
        existing = None
        if email:
            existing = cur.execute(
                "SELECT id FROM accounts WHERE email = ?", (email,)).fetchone()
        if existing:
            acc_id = existing['id']
            cur.execute("""
                UPDATE accounts SET label=?, account_type=?, access_token=?,
                    refresh_token=?, expires_at=?, is_active=1, last_error=NULL
                WHERE id=?
            """, (label, account_type, enc_at, enc_rt, expires_at, acc_id))
        else:
            cur.execute("""
                INSERT INTO accounts (label, email, account_type, access_token,
                    refresh_token, expires_at, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            """, (label, email, account_type, enc_at, enc_rt, expires_at,
                  datetime.now(timezone.utc)))
            acc_id = cur.lastrowid
        self.conn.commit()
        return acc_id

    def list_accounts(self):
        """Account metadata for UI — never includes tokens."""
        cur = self.conn.execute("""
            SELECT id, label, email, account_type, is_active,
                   created_at, last_polled_at, last_error
            FROM accounts ORDER BY id ASC
        """)
        return [dict(row) for row in cur.fetchall()]

    def get_pollable_accounts(self):
        """Active accounts with DECRYPTED tokens, for the collector."""
        import crypto_util
        cur = self.conn.execute("""
            SELECT id, label, email, account_type, access_token, refresh_token,
                   expires_at
            FROM accounts WHERE is_active = 1 ORDER BY id ASC
        """)
        out = []
        for row in cur.fetchall():
            d = dict(row)
            d['access_token'] = crypto_util.decrypt(d['access_token'])
            d['refresh_token'] = crypto_util.decrypt(d['refresh_token'])
            out.append(d)
        return out

    def update_account_tokens(self, account_id, access_token, refresh_token, expires_at):
        import crypto_util
        self.conn.execute("""
            UPDATE accounts SET access_token=?, refresh_token=?, expires_at=?
            WHERE id=?
        """, (crypto_util.encrypt(access_token), crypto_util.encrypt(refresh_token),
              expires_at, account_id))
        self.conn.commit()

    def set_account_active(self, account_id, is_active: bool):
        self.conn.execute("UPDATE accounts SET is_active=? WHERE id=?",
                          (1 if is_active else 0, account_id))
        self.conn.commit()

    def rename_account(self, account_id, label: str):
        self.conn.execute("UPDATE accounts SET label=? WHERE id=?", (label, account_id))
        self.conn.commit()

    def record_account_poll(self, account_id, error=None):
        self.conn.execute(
            "UPDATE accounts SET last_polled_at=?, last_error=? WHERE id=?",
            (datetime.now(timezone.utc), error, account_id))
        self.conn.commit()

    def delete_account(self, account_id):
        """Remove the account row. Snapshots keep their account_id (history stays)."""
        self.conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        self.conn.commit()

    def get_default_account_id(self):
        row = self.conn.execute(
            "SELECT id FROM accounts WHERE is_active=1 ORDER BY id ASC LIMIT 1").fetchone()
        return row['id'] if row else None

    def backfill_account_by_email(self, account_id, email) -> int:
        """Attach legacy NULL-account snapshots with this email to account_id.
        Returns the number of rows updated."""
        cur = self.conn.execute(
            "UPDATE snapshots SET account_id=? WHERE account_id IS NULL AND email=?",
            (account_id, email))
        self.conn.commit()
        return cur.rowcount
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest test_accounts_db.py -q`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add database.py test_accounts_db.py
git commit -m "feat(accounts): account CRUD with encrypted tokens + email backfill"
```

### Task 4: account_id w insert_snapshot, get_current, get_history

**Files:**
- Modify: `database.py` (`insert_snapshot`, `get_current`, `get_history`)
- Modify: `test_accounts_db.py`

- [ ] **Step 1: Write the failing tests** — dopisz do `test_accounts_db.py`:

```python
SNAP = lambda acc_id, pct: {
    'account_id': acc_id, 'account_type': 'max', 'email': 'a@x.com',
    'captured_at': '2026-06-11T07:00:00Z',
    'quotas': [{'type': 'weekly', 'percent_remaining': pct,
                'resets_at': '2026-06-15T11:00:00Z', 'time_remaining_seconds': 100}],
}


def test_insert_snapshot_persists_account_id(db):
    acc = _add(db)
    sid = db.insert_snapshot(SNAP(acc, 50))
    row = db.conn.execute("SELECT account_id FROM snapshots WHERE id=?", (sid,)).fetchone()
    assert row['account_id'] == acc


def test_get_current_filters_by_account(db):
    a = _add(db, email='a@x.com')
    b = _add(db, email='b@x.com')
    db.insert_snapshot(dict(SNAP(a, 11), email='a@x.com'))
    db.insert_snapshot(dict(SNAP(b, 22), email='b@x.com'))
    cur_a = db.get_current(account_id=a)
    assert cur_a['limits']['weekly']['percent_remaining'] == 11
    cur_b = db.get_current(account_id=b)
    assert cur_b['limits']['weekly']['percent_remaining'] == 22


def test_get_history_filters_by_account(db):
    a = _add(db, email='a@x.com')
    b = _add(db, email='b@x.com')
    db.insert_snapshot(dict(SNAP(a, 11), email='a@x.com'))
    db.insert_snapshot(dict(SNAP(b, 22), email='b@x.com'))
    hist_a = db.get_history(account_id=a)
    assert len(hist_a) == 1
    assert hist_a[0]['limits']['weekly']['percent_remaining'] == 11
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest test_accounts_db.py -q -k "account_id or filters_by_account"`
Expected: FAIL — account_id not stored / get_current takes no account_id

- [ ] **Step 3: Write the implementation**

(a) `insert_snapshot` — zmień `INSERT INTO snapshots`:

```python
        cursor.execute("""
            INSERT INTO snapshots (captured_at, account_type, email, account_id)
            VALUES (?, ?, ?, ?)
        """, (captured_at, data.get('account_type'), data.get('email'),
              data.get('account_id')))
```

(b) `get_current` — zmień sygnaturę i zapytanie:

```python
    def get_current(self, account_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Most recent snapshot. If account_id is given, restrict to that account."""
        cursor = self.conn.cursor()
        where, params = "", ()
        if account_id is not None:
            where, params = "WHERE account_id = ?", (account_id,)
        cursor.execute(f"""
            SELECT id, captured_at, account_type, email
            FROM snapshots
            {where}
            ORDER BY captured_at DESC, id DESC
            LIMIT 1
        """, params)
        row = cursor.fetchone()
        if not row:
            return None
        result = self._snapshot_to_dict(row)
        self._fill_missing_quotas(result['limits'], row['id'], row['captured_at'])
        return result
```

(c) `get_history` — dodaj parametr `account_id` i wpleć go w `WHERE` obu gałęzi:

```python
    def get_history(self, hours: Optional[int] = None,
                    max_points: Optional[int] = None,
                    account_id: Optional[int] = None) -> List[Dict[str, Any]]:
```

W treści, po `cutoff = ...`, zbuduj filtr konta i dołącz do obu zapytań:

```python
        acct_sql = " AND account_id = ?" if account_id is not None else ""
        acct_params = (account_id,) if account_id is not None else ()
```

- w gałęzi id-scan zmień zapytanie i parametry:

```python
            cursor.execute(
                "SELECT id FROM snapshots WHERE captured_at >= ?" + acct_sql +
                " ORDER BY captured_at ASC, id ASC", (cutoff,) + acct_params)
```

- w domyślnej gałęzi zmień inicjalizację `where, params`:

```python
        where, params = "s.captured_at >= ?" + acct_sql, (cutoff,) + acct_params
```

(gałąź `s.id IN (...)` zostaje bez zmian — lista PK już ogranicza do wybranego konta).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest test_accounts_db.py -q`
Expected: 13 passed

- [ ] **Step 5: Pełny regres** (stare testy bazy/insertów):

Run: `python3 -m pytest -q`
Expected: wszystko zielone

- [ ] **Step 6: Commit**

```bash
git add database.py test_accounts_db.py
git commit -m "feat(accounts): per-account filtering in insert/get_current/get_history"
```

### Task 5: OAuth flow (PKCE, authorize URL, exchange, profile)

**Files:**
- Create: `oauth_flow.py`
- Create: `test_oauth_flow.py`

- [ ] **Step 1: Write the failing tests** — `test_oauth_flow.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest test_oauth_flow.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'oauth_flow'`

- [ ] **Step 3: Write the implementation** — `oauth_flow.py`:

```python
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

from api_usage_fetcher import CLIENT_ID, TOKEN_URL, CLI_VERSION

AUTHORIZE_URL = 'https://claude.ai/oauth/authorize'
REDIRECT_URI = 'https://platform.claude.com/oauth/code/callback'
PROFILE_URL = 'https://api.anthropic.com/api/oauth/profile'
SCOPES = ['user:profile', 'user:inference', 'user:sessions:claude_code',
          'user:mcp_servers', 'user:file_upload']

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
    req = urllib.request.Request(TOKEN_URL, data=body, method='POST', headers={
        'Content-Type': 'application/json',
        'User-Agent': f'claude-code/{CLI_VERSION}',
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
    if account.get('has_claude_max'):
        account_type = 'max'
    elif account.get('has_claude_pro'):
        account_type = 'pro'
    else:
        account_type = 'unknown'
    return {'email': account.get('email'), 'account_type': account_type}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest test_oauth_flow.py -q`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add oauth_flow.py test_oauth_flow.py
git commit -m "feat(accounts): OAuth add-account flow (PKCE, code exchange, profile)"
```

### Task 6: Refaktor api_usage_fetcher.py do funkcji wielokontowych

**Files:**
- Modify: `api_usage_fetcher.py`
- Modify: `test_api_usage_fetcher.py`

- [ ] **Step 1: Write the failing tests** — dopisz do `test_api_usage_fetcher.py`:

```python
# ---- refactored multi-account helpers ----

def test_needs_refresh_ms_margin():
    assert auf.needs_refresh_ms(1_000_000, now_ms=1_000_000 - 60_000) is True   # 60s < 2min
    assert auf.needs_refresh_ms(1_000_000, now_ms=1_000_000 - 600_000) is False  # 10min left


def test_refresh_access_token_returns_new_tokens(monkeypatch):
    monkeypatch.setattr(auf, '_urlopen',
                        lambda req, timeout=None: FakeResponse(
                            {'access_token': 'AT2', 'refresh_token': 'RT2', 'expires_in': 28800}))
    out = auf.refresh_access_token('RT1', now_ms=1_000_000)
    assert out == {'access_token': 'AT2', 'refresh_token': 'RT2',
                   'expires_at': 1_000_000 + 28800 * 1000}


def test_build_snapshot_from_account_and_api():
    account = {'id': 7, 'email': 'a@x.com', 'account_type': 'max'}
    snap = auf.build_snapshot(PROD_SAMPLE, account, now=NOW)
    assert snap['account_id'] == 7
    assert snap['email'] == 'a@x.com'
    assert snap['account_type'] == 'max'
    assert snap['source'] == 'api'
    assert len(snap['quotas']) == 3
    datetime.strptime(snap['captured_at'], '%Y-%m-%dT%H:%M:%SZ')
```

(`FakeResponse` jest już zdefiniowane wcześniej w pliku w Tasku 3 sekcji refresh.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest test_api_usage_fetcher.py -q -k "needs_refresh_ms or refresh_access_token or build_snapshot"`
Expected: FAIL — brak nowych funkcji

- [ ] **Step 3: Write the implementation** — w `api_usage_fetcher.py` dodaj funkcje (po `map_usage_response`):

```python
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
    req = urllib.request.Request(TOKEN_URL, data=body, method='POST', headers={
        'Content-Type': 'application/json',
        'User-Agent': f'claude-code/{CLI_VERSION}',
    })
    try:
        with _urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            token = json.loads(resp.read().decode())
    except Exception as e:
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
```

- [ ] **Step 4: Usuń martwą ścieżkę pliku** — w `api_usage_fetcher.py` usuń funkcje `load_credentials`, `needs_refresh`, `_atomic_write_credentials`, `refresh_credentials`, `read_account_email`, `fetch_usage`, `main` ORAZ stałe `CREDENTIALS_FILE`, `CLAUDE_CONFIG_FILE`, `REFRESH_MARGIN_MS` (przenosząc `REFRESH_MARGIN_MS = 120_000` na górę pliku przy innych stałych, bo używa go `needs_refresh_ms`), i blok `if __name__ == '__main__'`. Zostają: stałe URL/CLIENT_ID/CLI_VERSION/HTTP_TIMEOUT/`REFRESH_MARGIN_MS`, `_urlopen`, `WINDOW_MAP`, `UsageApiError`, `map_usage_response`, `needs_refresh_ms`, `refresh_access_token`, `build_snapshot`, `_http_get_usage`.

- [ ] **Step 5: Wytnij osierocone testy** — z `test_api_usage_fetcher.py` usuń testy odwołujące się do usuniętych funkcji: `test_load_credentials_*`, `test_needs_refresh_false_*`/`test_needs_refresh_true_*` (stare, na `needs_refresh`), `test_refresh_updates_*`, `test_refresh_failure_*`, `test_fetch_usage_*`, `test_email_none_*`, `test_main_*`, oraz fixture `creds_file`, `claude_config`, helper `_fresh_creds` i stałą `CREDS`. Zostają testy `map_usage_response` + nowe z kroku 1.

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m pytest test_api_usage_fetcher.py -q`
Expected: tylko testy mapowania + 3 nowe, wszystkie zielone

- [ ] **Step 7: Commit**

```bash
git add api_usage_fetcher.py test_api_usage_fetcher.py
git commit -m "refactor(fetcher): functional multi-account helpers; drop file-credential path"
```

### Task 7: Runner wielokontowy (collect_all.py)

**Files:**
- Create: `collect_all.py`
- Create: `test_collect_all.py`

- [ ] **Step 1: Write the failing tests** — `test_collect_all.py`:

```python
"""Tests for collect_all: per-account refresh+fetch+insert, error isolation."""
import json

import pytest
from cryptography.fernet import Fernet

import config
import crypto_util
import collect_all
from database import UsageDatabase

PROD_SAMPLE = {
    "five_hour": {"utilization": 10.0, "resets_at": "2026-06-11T08:40:00+00:00"},
    "seven_day": {"utilization": 58.0, "resets_at": "2026-06-15T11:00:00+00:00"},
    "seven_day_sonnet": {"utilization": 0.0, "resets_at": "2026-06-15T11:00:00+00:00"},
}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'TOKEN_ENCRYPTION_KEY', Fernet.generate_key().decode())
    crypto_util._reset_cache()
    d = UsageDatabase(str(tmp_path / 'c.db'))
    yield d
    d.close()


def _add(db, email, expires_at):
    return db.add_or_update_account(
        label=email, email=email, account_type='max',
        access_token='AT', refresh_token='RT', expires_at=expires_at)


def test_polls_each_active_account_and_inserts(db, tmp_path, monkeypatch):
    far = 9_999_999_999_999
    a = _add(db, 'a@x.com', far)
    b = _add(db, 'b@x.com', far)
    monkeypatch.setattr(collect_all.auf, '_http_get_usage',
                        lambda token: PROD_SAMPLE)
    monkeypatch.setattr(collect_all, 'DATA_DIR', str(tmp_path / 'data'))
    rc = collect_all.run(db)
    assert rc == 0
    assert db.get_current(account_id=a) is not None
    assert db.get_current(account_id=b) is not None


def test_refreshes_expired_token_and_persists(db, tmp_path, monkeypatch):
    a = _add(db, 'a@x.com', 0)  # already expired
    monkeypatch.setattr(collect_all.auf, 'refresh_access_token',
                        lambda rt, now_ms=None: {'access_token': 'AT2',
                            'refresh_token': 'RT2', 'expires_at': 9_999_999_999_999})
    monkeypatch.setattr(collect_all.auf, '_http_get_usage', lambda token: PROD_SAMPLE)
    monkeypatch.setattr(collect_all, 'DATA_DIR', str(tmp_path / 'data'))
    assert collect_all.run(db) == 0
    p = db.get_pollable_accounts()[0]
    assert p['access_token'] == 'AT2' and p['refresh_token'] == 'RT2'


def test_one_account_failing_does_not_block_others(db, tmp_path, monkeypatch):
    far = 9_999_999_999_999
    a = _add(db, 'a@x.com', far)
    b = _add(db, 'b@x.com', far)
    def usage(token):
        # account a's token is 'AT'; make the FIRST call raise, second succeed
        raise_list.append(1)
        if len(raise_list) == 1:
            raise collect_all.auf.UsageApiError('boom')
        return PROD_SAMPLE
    raise_list = []
    monkeypatch.setattr(collect_all.auf, '_http_get_usage', usage)
    monkeypatch.setattr(collect_all, 'DATA_DIR', str(tmp_path / 'data'))
    rc = collect_all.run(db)
    assert rc == 2  # at least one account failed
    accounts = {acc['email']: acc for acc in db.list_accounts()}
    assert accounts['a@x.com']['last_error'] is not None
    # the second account still got a snapshot
    assert db.get_current(account_id=b) is not None


def test_no_accounts_returns_one(db, monkeypatch, tmp_path):
    monkeypatch.setattr(collect_all, 'DATA_DIR', str(tmp_path / 'data'))
    assert collect_all.run(db) == 1  # nothing to poll
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest test_collect_all.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'collect_all'`

- [ ] **Step 3: Write the implementation** — `collect_all.py`:

```python
#!/usr/bin/env python3
"""Poll every active account's usage and insert one snapshot each.

Replaces the single-account api_usage_fetcher.main() in collect_history.sh.
Sequential (2-3 accounts × ~1s); errors are isolated per account. Exit codes:
  0 — every active account polled OK
  1 — no active accounts to poll
  2 — at least one account failed (others still inserted)
"""
import json
import os
import sys
from datetime import datetime, timezone

import api_usage_fetcher as auf
import config
from database import UsageDatabase

DATA_DIR = config.DATA_DIR


def _log(msg):
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{stamp}] {msg}', file=sys.stderr)


def _write_backup(snapshot, account_id):
    """Mirror the JSON backup collect_history.sh used to write, one file/account."""
    try:
        now = datetime.now(timezone.utc)
        day_dir = os.path.join(DATA_DIR, now.strftime('%Y-%m-%d'))
        os.makedirs(day_dir, exist_ok=True)
        name = now.strftime('%H-%M') + f'-{account_id}.json'
        tmp = os.path.join(day_dir, f'.{name}.{os.getpid()}')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f)
        os.replace(tmp, os.path.join(day_dir, name))
    except OSError as e:
        _log(f'WARN: backup write failed for account {account_id}: {e}')


def _poll_one(db, account) -> bool:
    """Refresh-if-needed, fetch usage, insert snapshot. Returns True on success."""
    acc_id = account['id']
    try:
        access_token = account['access_token']
        if auf.needs_refresh_ms(account['expires_at']):
            new = auf.refresh_access_token(account['refresh_token'])
            db.update_account_tokens(acc_id, new['access_token'],
                                     new['refresh_token'], new['expires_at'])
            access_token = new['access_token']
        api = auf._http_get_usage(access_token)
        snapshot = auf.build_snapshot(api, account)
        db.insert_snapshot(snapshot)
        _write_backup(snapshot, acc_id)
        db.record_account_poll(acc_id, error=None)
        return True
    except Exception as e:
        _log(f'WARN: account {acc_id} ({account.get("email")}) failed: {e}')
        db.record_account_poll(acc_id, error=str(e)[:300])
        return False


def run(db) -> int:
    accounts = db.get_pollable_accounts()
    if not accounts:
        _log('no active accounts to poll')
        return 1
    failures = 0
    for account in accounts:
        if not _poll_one(db, account):
            failures += 1
    return 2 if failures else 0


def main() -> int:
    with UsageDatabase(config.DB_FILE) as db:
        return run(db)


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest test_collect_all.py -q`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add collect_all.py test_collect_all.py
git commit -m "feat(collector): multi-account poll runner with per-account error isolation"
```

### Task 8: Podłącz collect_all.py do collect_history.sh

**Files:**
- Modify: `collect_history.sh`

- [ ] **Step 1: Zamień blok fetch+insert** — w `collect_history.sh` zastąp obecny blok (od komentarza „Fetch data from the oauth/usage HTTP API" do `insert_to_db.py`/zapisu JSON) jednym wywołaniem runnera. Konkretnie zastąp:

```bash
USAGE_JSON=$("$VENV_PYTHON" "$SCRIPT_DIR/api_usage_fetcher.py")
FETCH_STATUS=$?

if [ -z "$USAGE_JSON" ]; then
    log "ERROR: api_usage_fetcher.py exited with $FETCH_STATUS and produced no output"
    exit 1
fi
```

oraz cały dalszy ciąg zapisu JSON / `insert_to_db.py` / `FETCH_ERROR` aż do retencji, na:

```bash
# Poll all active accounts (collect_all.py handles refresh, fetch, per-account
# snapshot insert + JSON backup, and per-account error isolation).
"$VENV_PYTHON" "$SCRIPT_DIR/collect_all.py"
FETCH_STATUS=$?
case $FETCH_STATUS in
    0) : ;;                                   # all accounts OK
    1) log "WARN: no active accounts to poll" ;;
    2) log "WARN: at least one account failed (see above)"; FETCH_ERROR=1 ;;
    *) log "ERROR: collect_all.py exited $FETCH_STATUS"; exit 1 ;;
esac
```

Zostaw nietknięty blok retencji (`cleanup_old_data.py`) i końcowy `if [ "$FETCH_ERROR" -ne 0 ]; then exit 2; fi`. Zadeklaruj `FETCH_ERROR=0` przed `case` jeśli nie jest już ustawione wcześniej.

- [ ] **Step 2: Sprawdź składnię**

Run: `bash -n collect_history.sh && echo OK`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add collect_history.sh
git commit -m "feat(collector): drive collection through collect_all.py runner"
```

### Task 9: Routy zarządzania kontami w Flask

**Files:**
- Modify: `app.py`
- Create: `templates/accounts.html`

- [ ] **Step 1: Dodaj importy i routy** — w `app.py` na górze dołącz `import oauth_flow`, a w sekcji `# ============ ROUTES ============` (po `dashboard`) dodaj:

```python
@app.route('/accounts')
@login_required
def accounts_page():
    """Account management UI."""
    return render_template('accounts.html', version=config.VERSION,
                           accounts=get_db().list_accounts())


@app.route('/api/accounts')
@login_required
def api_accounts():
    """Account list + each account's latest snapshot summary (for the bar)."""
    db = get_db()
    out = []
    for acc in db.list_accounts():
        current = db.get_current(account_id=acc['id']) if acc['is_active'] else None
        weekly = session_pct = None
        if current:
            weekly = (current['limits'].get('weekly') or {}).get('percent_remaining')
            session_pct = (current['limits'].get('session') or {}).get('percent_remaining')
        out.append({**acc, 'weekly_remaining': weekly, 'session_remaining': session_pct})
    return jsonify(out)


@app.route('/accounts/add', methods=['POST'])
@login_required
def accounts_add():
    """Step 1 (action=start): return authorize URL, stash PKCE in session.
       Step 2 (action=complete): exchange pasted code, fetch profile, save account."""
    action = request.form.get('action')
    if action == 'start':
        pkce = oauth_flow.generate_pkce()
        state = oauth_flow.generate_state()
        session['oauth_verifier'] = pkce['verifier']
        session['oauth_state'] = state
        return jsonify({'authorize_url': oauth_flow.build_authorize_url(
            pkce['challenge'], state)})
    if action == 'complete':
        verifier = session.get('oauth_verifier')
        if not verifier:
            return jsonify({'error': 'Brak rozpoczętej sesji OAuth — kliknij „Dodaj konto" ponownie.'}), 400
        code = request.form.get('code', '').strip()
        if not code:
            return jsonify({'error': 'Wklej kod autoryzacyjny.'}), 400
        try:
            tokens = oauth_flow.exchange_code(code, verifier)
            profile = oauth_flow.fetch_profile(tokens['access_token'])
        except oauth_flow.OAuthError as e:
            return jsonify({'error': str(e)}), 400
        db = get_db()
        label = request.form.get('label') or profile['email'] or 'Konto'
        acc_id = db.add_or_update_account(
            label=label, email=profile['email'], account_type=profile['account_type'],
            access_token=tokens['access_token'], refresh_token=tokens['refresh_token'],
            expires_at=tokens['expires_at'])
        backfilled = db.backfill_account_by_email(acc_id, profile['email']) \
            if profile['email'] else 0
        session.pop('oauth_verifier', None)
        session.pop('oauth_state', None)
        return jsonify({'id': acc_id, 'email': profile['email'],
                        'account_type': profile['account_type'], 'backfilled': backfilled})
    return jsonify({'error': 'unknown action'}), 400


@app.route('/accounts/<int:account_id>/<string:op>', methods=['POST'])
@login_required
def accounts_op(account_id, op):
    """enable / disable / delete / rename an account."""
    db = get_db()
    if op == 'enable':
        db.set_account_active(account_id, True)
    elif op == 'disable':
        db.set_account_active(account_id, False)
    elif op == 'delete':
        db.delete_account(account_id)
    elif op == 'rename':
        label = request.form.get('label', '').strip()
        if not label:
            return jsonify({'error': 'Pusta nazwa.'}), 400
        db.rename_account(account_id, label)
    else:
        return jsonify({'error': 'unknown op'}), 400
    return jsonify({'ok': True})
```

- [ ] **Step 2: Dodaj parametr `account` do endpointów usage** — zmień trzy istniejące funkcje:

```python
def _resolve_account_id():
    """account query param, or the default (first active) account, or None (legacy)."""
    acc = request.args.get('account', type=int)
    if acc is not None:
        return acc
    return get_db().get_default_account_id()
```

(wstaw helper przed routami). Następnie:

- `api_current`: `data = get_db().get_current(account_id=_resolve_account_id())`
- `api_history`: `history = get_db().get_history(hours=hours, max_points=HISTORY_CHART_MAX_POINTS, account_id=_resolve_account_id())`
- `api_prediction`: `history = get_db().get_history(account_id=_resolve_account_id())`

(usuń użycie modułowych `load_history`/`get_current_usage` w tych trzech routach lub przekaż im `account_id` — najprościej wołać `get_db()` bezpośrednio jak wyżej).

- [ ] **Step 3: Utwórz `templates/accounts.html`** — strona zarządzania (dark Cyan Terminal; minimalny, samowystarczalny JS):

```html
<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Konta · Claude Usage</title>
    <link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}">
</head>
<body>
<div class="container">
    <header>
        <h1>Konta <span class="version-badge">v{{ version }}</span></h1>
        <a href="{{ url_for('dashboard') }}" class="btn-refresh">← Dashboard</a>
    </header>

    <section class="accounts-list" id="accounts-list">
        {% for a in accounts %}
        <div class="account-row" data-id="{{ a.id }}">
            <span class="acc-dot {{ 'on' if a.is_active else 'off' }}"></span>
            <span class="acc-label">{{ a.label }}</span>
            <span class="acc-email">{{ a.email or '—' }}</span>
            <span class="acc-type">{{ a.account_type or '?' }}</span>
            <span class="acc-error">{{ a.last_error or '' }}</span>
            <span class="acc-actions">
                {% if a.is_active %}
                <button onclick="op({{ a.id }}, 'disable')">Wyłącz</button>
                {% else %}
                <button onclick="op({{ a.id }}, 'enable')">Włącz</button>
                {% endif %}
                <button onclick="renameAcc({{ a.id }})">Zmień nazwę</button>
                <button onclick="delAcc({{ a.id }})">Usuń</button>
            </span>
        </div>
        {% else %}
        <p class="muted">Brak kont. Dodaj pierwsze konto poniżej.</p>
        {% endfor %}
    </section>

    <section class="add-account">
        <h2>Dodaj konto</h2>
        <button id="start-btn" onclick="startOAuth()">1. Rozpocznij autoryzację</button>
        <div id="oauth-step2" hidden>
            <p>Otwórz link, zaloguj się na wybrane konto Claude i wklej kod:</p>
            <p><a id="auth-link" href="#" target="_blank" rel="noopener">Otwórz stronę autoryzacji ↗</a></p>
            <input id="code-input" type="text" placeholder="wklej kod#state" size="60">
            <input id="label-input" type="text" placeholder="nazwa (opcjonalnie)">
            <button onclick="completeOAuth()">2. Dodaj konto</button>
        </div>
        <p id="add-msg" class="muted"></p>
    </section>
</div>
<script>
async function startOAuth() {
    const r = await fetch('/accounts/add', {method:'POST',
        headers:{'Content-Type':'application/x-www-form-urlencoded'},
        body:'action=start'});
    const d = await r.json();
    document.getElementById('auth-link').href = d.authorize_url;
    document.getElementById('oauth-step2').hidden = false;
}
async function completeOAuth() {
    const code = document.getElementById('code-input').value;
    const label = document.getElementById('label-input').value;
    const body = new URLSearchParams({action:'complete', code, label});
    const r = await fetch('/accounts/add', {method:'POST',
        headers:{'Content-Type':'application/x-www-form-urlencoded'}, body});
    const d = await r.json();
    const msg = document.getElementById('add-msg');
    if (r.ok) { msg.textContent = `Dodano ${d.email} (${d.account_type}), podpięto ${d.backfilled} starych snapshotów.`;
        setTimeout(()=>location.reload(), 1200); }
    else { msg.textContent = 'Błąd: ' + (d.error || r.status); }
}
async function op(id, name) {
    await fetch(`/accounts/${id}/${name}`, {method:'POST'});
    location.reload();
}
async function delAcc(id) {
    if (!confirm('Usunąć konto? Historia snapshotów zostanie zachowana.')) return;
    await fetch(`/accounts/${id}/delete`, {method:'POST'});
    location.reload();
}
async function renameAcc(id) {
    const label = prompt('Nowa nazwa konta:');
    if (!label) return;
    await fetch(`/accounts/${id}/rename`, {method:'POST',
        headers:{'Content-Type':'application/x-www-form-urlencoded'},
        body:new URLSearchParams({label})});
    location.reload();
}
</script>
</body>
</html>
```

- [ ] **Step 4: Smoke test routów** — uruchom appkę z atrapą sekretów i sprawdź, że strony się renderują:

Run:
```bash
ALLOW_DEFAULT_CREDENTIALS=1 TOKEN_ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())") \
  python3 -c "import app; c=app.app.test_client();
import flask
with c.session_transaction() as s: s['logged_in']=True
print('accounts', c.get('/accounts').status_code);
print('api/accounts', c.get('/api/accounts').status_code);
print('api/current', c.get('/api/current').status_code)"
```
Expected: `accounts 200`, `api/accounts 200`, `api/current 200` (lub 500 tylko gdy pusta baza bez snapshotów — wtedy `/api/current` zwraca 500 zgodnie z dotychczasowym zachowaniem; akceptowalne)

- [ ] **Step 5: Commit**

```bash
git add app.py templates/accounts.html
git commit -m "feat(accounts): account management routes + add-account OAuth UI"
```

### Task 10: Pasek kont i przełącznik w dashboardzie

**Files:**
- Modify: `templates/dashboard.html`
- Modify: `static/style.css`

- [ ] **Step 1: Dodaj link „Konta" i pasek kont** — w `templates/dashboard.html`, w `<header>` (obok przycisku refresh) dodaj link:

```html
                <a href="{{ url_for('accounts_page') }}" class="btn-refresh" title="Zarządzaj kontami">Konta</a>
```

a bezpośrednio po zamknięciu `</header>` (przed sekcją kart STATUS) wstaw kontener paska:

```html
        <div class="account-bar" id="account-bar" hidden></div>
```

- [ ] **Step 2: Dodaj logikę przełączania w JS** — w bloku `<script>` dashboardu, na początku, dodaj stan i ładowanie kont:

```javascript
        let currentAccount = localStorage.getItem('account_id') || null;

        async function loadAccountBar() {
            const res = await fetch('/api/accounts');
            const accounts = await res.json();
            const active = accounts.filter(a => a.is_active);
            const bar = document.getElementById('account-bar');
            if (active.length <= 1) { bar.hidden = true; if (active[0]) currentAccount = String(active[0].id); return; }
            if (!currentAccount || !active.some(a => String(a.id) === String(currentAccount)))
                currentAccount = String(active[0].id);
            bar.hidden = false;
            bar.innerHTML = active.map(a => `
                <button class="acc-mini ${String(a.id)===String(currentAccount)?'active':''}"
                        onclick="switchAccount('${a.id}')">
                    <span class="acc-mini-label">${a.label}</span>
                    <span class="acc-mini-stats">7d ${a.weekly_remaining ?? '--'}% · 5h ${a.session_remaining ?? '--'}%</span>
                </button>`).join('');
        }

        function switchAccount(id) {
            currentAccount = String(id);
            localStorage.setItem('account_id', currentAccount);
            loadAccountBar();
            refreshData();          // re-fetch current/history/prediction for new account
        }

        function accountQuery(prefix) {
            return currentAccount ? `${prefix}account=${encodeURIComponent(currentAccount)}` : '';
        }
```

- [ ] **Step 3: Dołącz parametr konta do fetchy** — w dashboardzie zmień trzy wywołania:
  - `fetch('/api/current')` → `fetch('/api/current?' + accountQuery(''))`
  - `fetch(\`/api/history?hours=${currentTimeRange}\`)` → `fetch(\`/api/history?hours=${currentTimeRange}&\` + accountQuery(''))`
  - `fetch('/api/prediction')` → `fetch('/api/prediction?' + accountQuery(''))`

  (`accountQuery('')` zwraca `account=<id>` lub pusty string; przy pustym zostaje czysty querystring jak dziś.)

- [ ] **Step 4: Wywołaj `loadAccountBar()` na starcie** — w miejscu, gdzie dashboard inicjuje dane (przy istniejącym pierwszym `refreshData()`/`DOMContentLoaded`), dodaj `loadAccountBar();` przed pierwszym pobraniem danych. Jeśli istnieje `setInterval` odświeżania, dodaj tam też `loadAccountBar()`.

- [ ] **Step 5: Style paska** — w `static/style.css` dopisz:

```css
.account-bar { display: flex; gap: 8px; flex-wrap: wrap; margin: 0 0 14px; }
.acc-mini { background: #0c1518; border: 1px solid #16323a; border-radius: 8px;
    padding: 6px 12px; color: #6fe3d2; cursor: pointer; text-align: left;
    display: flex; flex-direction: column; min-width: 120px; }
.acc-mini.active { border-color: #2de0c6; box-shadow: 0 0 0 1px #2de0c6 inset; }
.acc-mini-label { font-weight: 600; font-size: 0.9rem; }
.acc-mini-stats { font-size: 0.72rem; color: #4f8c84; }
.account-row { display: flex; gap: 12px; align-items: center; padding: 8px 0;
    border-bottom: 1px solid #16323a; font-size: 0.9rem; }
.acc-dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
.acc-dot.on { background: #2de0c6; } .acc-dot.off { background: #555; }
.acc-email { color: #4f8c84; } .acc-error { color: #e36f6f; font-size: 0.75rem; }
.acc-actions { margin-left: auto; display: flex; gap: 6px; }
.acc-actions button, .add-account button { background: #0c1518; color: #6fe3d2;
    border: 1px solid #16323a; border-radius: 6px; padding: 4px 10px; cursor: pointer; }
.add-account { margin-top: 24px; } .add-account input { margin: 4px 6px 4px 0;
    background: #0c1518; border: 1px solid #16323a; color: #cfeee8; padding: 6px; border-radius: 6px; }
```

- [ ] **Step 6: Wizualna weryfikacja przy szerokości telefonu** — wg pamięci `reference_visual_preview_dashboard`: wyrenderuj `dashboard.html` standalone + serwuj po HTTP, podejrzyj pasek kont przez browser MCP (`host.docker.internal`). Potwierdź: pojedyncze konto → pasek schowany; dwa konta → mini-karty, aktywna podświetlona.

- [ ] **Step 7: Commit**

```bash
git add templates/dashboard.html static/style.css
git commit -m "feat(ui): account bar + switcher on dashboard"
```

### Task 11: Dokumentacja, wersja, pełny regres

**Files:**
- Modify: `config.py` (VERSION 1.3.0)
- Modify: `CLAUDE.md`, `README.md`
- Modify: `.env` na prodzie (TOKEN_ENCRYPTION_KEY) — w kroku deploya

- [ ] **Step 1: Bump wersji** — `config.py`: `VERSION = '1.3.0'`

- [ ] **Step 2: CLAUDE.md** — dopisz sekcję „Konta i OAuth" (po „Ścieżka API"):

```
## Konta i OAuth (multi-account, od v1.3.0)

Konta trzymane w tabeli `accounts` (tokeny szyfrowane Fernetem kluczem
`TOKEN_ENCRYPTION_KEY`). Dodawanie przez `/accounts` (flow PKCE z ręcznym
wklejeniem kodu `kod#state` — `oauth_flow.py`); profil (email/typ) z
`GET /api/oauth/profile`. `snapshots.account_id` wiąże snapshot z kontem;
stare wiersze (NULL) przypina `backfill_account_by_email` po dodaniu konta o
tym samym emailu. Zbieranie: `collect_all.py` pollu­je aktywne konta
sekwencyjnie (refresh→fetch→insert per konto, błąd jednego nie blokuje reszty),
woła go `collect_history.sh`. API: `/api/current|history|prediction?account=<id>`
(bez parametru → pierwsze aktywne). UI: pasek mini-kart + przełącznik (chowa się
przy jednym koncie). `TOKEN_ENCRYPTION_KEY` MUSI być w prodowym `.env`
(utrata = ponowne dodanie kont).
```

W „Struktura projektu" dodaj `oauth_flow.py`, `collect_all.py`, `crypto_util.py`, `templates/accounts.html`.

- [ ] **Step 3: README.md** — w tabeli zmiennych dodaj wiersz:

```
| TOKEN_ENCRYPTION_KEY | Klucz Fernet do szyfrowania tokenów kont (wymagany od v1.3.0) | (brak — dodawanie kont nie zadziała) |
```

i krótką notkę o generowaniu klucza:
`python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.

- [ ] **Step 4: Pełny regres**

Run: `python3 -m pytest -q`
Expected: wszystko zielone (mapowanie + crypto + oauth + accounts_db + collect_all + reszta)

- [ ] **Step 5: Commit**

```bash
git add config.py CLAUDE.md README.md
git commit -m "docs: document multi-account OAuth; bump version to 1.3.0"
```

### Task 12: Deploy na prod + migracja istniejącego konta

**Files:** brak zmian kodu — deploy + jednorazowa migracja

- [ ] **Step 1: Backup prod bazy** (zgodnie z pamięcią o destrukcyjnych operacjach):

```bash
ssh robson@ai.onee.pl 'cd /home/robson/claude-dashboard && cp usage.db ~/usage.db.bak_$(date +%Y%m%d_%H%M%S) && ls -la ~/usage.db.bak_*'
```

- [ ] **Step 2: Wygeneruj i dodaj klucz szyfrowania do prodowego `.env`** (NIE w repo, NIE w rsync):

```bash
ssh robson@ai.onee.pl 'cd /home/robson/claude-dashboard && \
  KEY=$(venv/bin/python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())") && \
  grep -q TOKEN_ENCRYPTION_KEY .env || echo "TOKEN_ENCRYPTION_KEY=$KEY" >> .env && \
  echo "key set"'
```

- [ ] **Step 3: Zainstaluj cryptography na prodzie**:

```bash
ssh robson@ai.onee.pl 'cd /home/robson/claude-dashboard && venv/bin/pip install -q "cryptography>=42.0.0" && venv/bin/python -c "import cryptography;print(cryptography.__version__)"'
```

- [ ] **Step 4: Surgical rsync zmienionych/nowych plików**:

```bash
rsync -avz crypto_util.py oauth_flow.py collect_all.py api_usage_fetcher.py \
  database.py app.py config.py collect_history.sh requirements.txt CLAUDE.md README.md \
  templates/accounts.html templates/dashboard.html static/style.css \
  test_crypto_util.py test_oauth_flow.py test_accounts_db.py test_collect_all.py test_api_usage_fetcher.py \
  robson@ai.onee.pl:/home/robson/claude-dashboard/
```

- [ ] **Step 5: Restart gunicorna** (nowy szablon + kod):

```bash
ssh robson@ai.onee.pl 'sudo systemctl restart claude-dashboard && systemctl is-active claude-dashboard'
```

- [ ] **Step 6: Migracja istniejącego konta** — wejdź na `https://<dashboard>/accounts`, kliknij „Rozpocznij autoryzację", zaloguj się na konto `misc@2fas.com`, wklej kod. Oczekiwane: komunikat „Dodano misc@2fas.com (max), podpięto N starych snapshotów" (N ≈ liczba historycznych wierszy z tym emailem).

- [ ] **Step 7: Weryfikacja zbierania (jeden tick crona)**:

```bash
ssh robson@ai.onee.pl 'cd /home/robson/claude-dashboard && ./collect_history.sh; echo "exit=$?"; \
  sqlite3 -readonly usage.db "SELECT a.label, s.captured_at, q.quota_type, q.percent_remaining FROM accounts a JOIN snapshots s ON s.account_id=a.id JOIN quotas q ON q.snapshot_id=s.id ORDER BY s.id DESC LIMIT 6;"'
```

Expected: `exit=0`, świeże wiersze z `account_id` przypisanym do konta.

- [ ] **Step 8: Commit + push** (kod już w repo z Taska 11; tu tylko push jeśli zostały lokalne commity):

```bash
git push
```

---

## Self-Review

- **Spec coverage:** tabela `accounts` + szyfrowanie ✓ (Task 1-2), `account_id` na snapshots + backfill po emailu ✓ (Task 2-4, 9), flow OAuth z wklejeniem kodu + profil ✓ (Task 5, 9), refresh→zapis do bazy ✓ (Task 6-7), runner wielokontowy z izolacją błędów ✓ (Task 7-8), API z `account=` ✓ (Task 9), UI przełącznik + pasek ✓ (Task 10), migracja/backup/klucz/deploy ✓ (Task 12), wersja 1.3.0 ✓ (Task 11). Uproszczenie (brak bootstrapu z pliku) odnotowane w nagłówku — przerwa max jeden tick.
- **Placeholder scan:** brak TBD/„obsłuż błędy" — każdy krok ma kod lub dokładną komendę.
- **Type consistency:** `add_or_update_account(label,email,account_type,access_token,refresh_token,expires_at)`, `list_accounts`, `get_pollable_accounts`, `update_account_tokens(id,at,rt,exp)`, `set_account_active`, `rename_account`, `record_account_poll(id,error)`, `delete_account`, `get_default_account_id`, `backfill_account_by_email(id,email)`, `get_current(account_id)`, `get_history(...,account_id)` — spójne między Task 3/4/7/9. `oauth_flow.exchange_code(code_input,verifier,now_ms)`/`fetch_profile`/`build_authorize_url(challenge,state)` zgodne między Task 5 i 9. `auf.needs_refresh_ms`/`refresh_access_token`/`build_snapshot`/`_http_get_usage` zgodne między Task 6 i 7. Snapshot dict zawiera `account_id` (Task 6 build_snapshot ↔ Task 4 insert_snapshot).
