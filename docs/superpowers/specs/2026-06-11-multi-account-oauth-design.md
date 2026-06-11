# Multi-Account OAuth — Design

**Data:** 2026-06-11
**Wersja docelowa:** 1.3.0 (minor)
**Cel:** Dodawanie kont Claude do dashboardu przez OAuth (ręczne wklejenie kodu) oraz obsługa wielu własnych kont — zbieranie, baza, API, UI.

## Kontekst i decyzje

- **Cel użycia:** monitoring kilku własnych subskrypcji (np. Max + Pro) w jednym dashboardzie z przełączaniem między kontami.
- **Obecne konto:** przechodzi w całości na OAuth; plik `~/.claude/.credentials.json` przestaje być źródłem (znika ryzyko wyścigu rotacji refresh tokenu z claude CLI). Bootstrap z pliku tylko na czas migracji.
- **Historia:** ~37k istniejących snapshotów przypisane po emailu (`misc@2fas.com`) do konta dodanego przez OAuth — ciągłość wykresów.
- **UI:** przełącznik kont + pasek podsumowania (mini-karty per konto nad obecnym dashboardem).
- **Flow OAuth:** ręczne wklejenie kodu `kod#state` (jak claude CLI / better-ccflare) — jedyny realnie wykonalny wariant z publicznym client_id Claude Code (`9d1c250a-e61b-44d9-88ed-5944d1962f5e`).

## Sekcja 1 — Model danych

Nowa tabela `accounts` jako źródło prawdy o kontach i tokenach:

```sql
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,              -- nazwa nadawana przez użytkownika
    email TEXT,                       -- z profilu OAuth
    account_type TEXT,                -- max/pro z subscriptionType
    access_token TEXT NOT NULL,       -- zaszyfrowany (Fernet)
    refresh_token TEXT NOT NULL,      -- zaszyfrowany (Fernet)
    expires_at INTEGER NOT NULL,      -- epoch ms
    is_active INTEGER DEFAULT 1,      -- 0 = nie pollujemy, historia zostaje
    created_at DATETIME,
    last_polled_at DATETIME,
    last_error TEXT                   -- ostatni błąd fetchu/refresh (do UI)
);
```

`snapshots` dostaje `account_id INTEGER REFERENCES accounts(id)` (nullable, dodawane przez `ALTER TABLE`). Istniejące wiersze: `account_id = NULL` (legacy) do czasu backfillu po emailu.

**Szyfrowanie tokenów at-rest:** `access_token`/`refresh_token` szyfrowane Fernetem (`cryptography`) kluczem z env `TOKEN_ENCRYPTION_KEY`. Broni przed wyciekiem pliku bazy (np. backup). Utrata klucza = konieczność ponownego dodania kont. Klucz w `.env` (jak `FLASK_SECRET_KEY`), nigdy w repo ani rsync.

## Sekcja 2 — Dodawanie konta przez OAuth

Strony za loginem:

1. **`/accounts`** — lista kont: label, email, typ, status (aktywny/błąd/wyłączony), wiek ostatniego odczytu; przyciski dodaj / wyłącz / włącz / usuń / zmień nazwę.
2. **`/accounts/add` (krok 1):** backend generuje PKCE (`verifier` + challenge S256) i `state`, trzyma w sesji Flask, pokazuje link autoryzacji:
   `https://claude.ai/oauth/authorize?code=true&client_id=9d1c250a-…&response_type=code&redirect_uri=https://platform.claude.com/oauth/code/callback&scope=user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload&code_challenge=…&code_challenge_method=S256&state=…`
3. **Krok 2:** użytkownik wkleja `kod#state` → backend dzieli po `#`, POST `https://platform.claude.com/v1/oauth/token`:
   ```json
   {"grant_type": "authorization_code", "code": "...", "state": "...",
    "code_verifier": "...", "redirect_uri": "https://platform.claude.com/oauth/code/callback",
    "client_id": "9d1c250a-..."}
   ```
   → `access_token`/`refresh_token`/`expires_in`.
4. Walidacja: `GET /api/oauth/usage` nowym tokenem (czy konto działa) + profil po email/typ; zapis konta (tokeny zaszyfrowane), label domyślny = email.

**Błędy:** zły/wygasły kod → komunikat + ponowne wklejenie (PKCE w sesji żyje do skutku lub nowego „dodaj"); email już istnieje → aktualizacja tokenów istniejącego konta (re-autoryzacja = „napraw konto").

## Sekcja 3 — Zbieranie danych dla wielu kont

- **`collect_all.py`** (nowy cienki runner): pobiera aktywne konta z bazy, dla każdego sekwencyjnie: refresh tokenu w razie potrzeby → `GET /api/oauth/usage` → `insert_snapshot(account_id=…)`. Cron co 5 min i flock bez zmian.
- **Refresh:** margines 2 min; rotowany refresh token zapisywany **do bazy** (zaszyfrowany), nie do pliku.
- **Email:** z profilu OAuth zapisanego przy dodawaniu, nie z `~/.claude.json`.
- **Izolacja błędów:** błąd jednego konta → zapis `last_error` + `last_polled_at`, log WARN, dalej. Backup JSON: `data/YYYY-MM-DD/HH-MM-<account_id>.json`.
- **Refaktor `api_usage_fetcher.py`:** funkcje czyste (`map_usage_response` — bez zmian; `refresh_token_for(account)`; `fetch_usage_for(account)`) zamiast czytania pliku. `insert_to_db.py` zostaje dla zgodności, ale collector woła bazę bezpośrednio.

## Sekcja 4 — API i UI

**API (za loginem):**
- `/api/accounts` — lista kont + bieżący stan (ostatni snapshot: weekly %, session %, staleness, last_error).
- `/api/current?account=<id>`, `/api/history?hours=N&account=<id>`, `/api/prediction?account=<id>`. Bez parametru → pierwsze aktywne konto (zachowanie wsteczne).
- CRUD: `POST /accounts/add`, `POST /accounts/<id>/disable|enable|delete|rename`. Usunięcie pyta o potwierdzenie i **zostawia historię** (snapshoty trzymają `account_id`; konto znika z pollingu i UI).

**UI (przełącznik + pasek podsumowania):**
- Pasek kont nad kartami STATUS: mini-karta per aktywne konto (label, email, weekly %, session %, kropka statusu). Klik = przełączenie całego dashboardu (karty + 3 wykresy + predykcja). Aktywna podświetlona. Jedno konto → pasek schowany (wygląd jak dziś).
- Wybór konta w `localStorage`.
- Nagłówek: link „Konta" → `/accounts`. Stylistyka: istniejący dark Cyan Terminal, bez nowych zależności frontowych.

## Sekcja 5 — Migracja, wdrożenie, testy, ryzyka

**Kolejność migracji (odwracalna):**
1. Backup prod bazy przed czymkolwiek.
2. `ALTER TABLE` dodaje `account_id` (nullable) + tworzy `accounts`. Stare wiersze działają jak konto legacy.
3. Deploy; collector **przejściowo** umie zbootstrapować token z pliku (jednorazowo), żeby nie było dziury w danych przed dodaniem konta przez OAuth.
4. Dodanie konta `misc@2fas.com` przez OAuth → backfill przypina stare snapshoty po emailu.
5. Po potwierdzeniu pollingu z bazy: usunięcie bootstrapu z pliku (osobny mały commit, czysty rollback).

**Testy:** mapowanie (istnieje), szyfrowanie round-trip, wymiana kodu OAuth (mock HTTP), refresh→zapis do bazy, runner wielokontowy (jedno konto pada — reszta leci), backfill po emailu, izolacja `account_id` w `get_history`/`get_current`/predykcji.

**Ryzyka:**
- Token w bazie → szyfrowanie + `TOKEN_ENCRYPTION_KEY` w `.env` (utrata klucza = ponowne dodanie kont). Klucz nigdy w repo/rsync.
- claude CLI na serwerze rotuje swój token w pliku niezależnie — po kroku 5 bez znaczenia.
- Endpoint `oauth/usage` nieoficjalny — bez zmian względem stanu obecnego.
- Wersja → 1.3.0.
