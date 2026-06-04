# Dashboard UI Lift — „Cyan Terminal, Airy"

- **Data:** 2026-06-04
- **Typ:** Lifting wizualny (visual-only reskin), bez zmian struktury informacji
- **Status:** Zaakceptowany kierunek → do spisania planu implementacji

## Cel

Odświeżyć wygląd dashboardu (`templates/dashboard.html`, `templates/login.html`,
`static/style.css`) bez zmiany tego, *co* i *gdzie* jest pokazywane. Te same sekcje,
te same dane, te same endpointy. Zmienia się skóra: paleta, typografia, głębia,
hierarchia wizualna.

Kierunek wybrany w brainstormie (wizualnie, przez companion):
- styl **data-dense / observability** (vibe Grafany),
- paleta **Cyan Terminal** (ciemna, akcent cyan),
- waga **Airy** (lekka oprawa — bez ramek/pasków akcentu przy panelach),
- **dark-only** (usuwamy tryb jasny i przełącznik).

## Decyzje (zablokowane)

| Decyzja | Wybór | Uzasadnienie |
|---|---|---|
| Zakres | Lifting wizualny | Struktura zostaje; tylko wygląd |
| Styl | C · Pro / Data-dense | Panel monitoringu, liczby na pierwszym planie |
| Paleta | P3 · Cyan Terminal | Granatowa czerń + akcent cyan; tematycznie pasuje do Claude Code |
| Waga | W2 · Airy | Data-dense, ale „oddycha"; oprawa się cofa, liczby dominują |
| Tryb jasny | Dark-only | Cyan Terminal jest z natury ciemny; mniej kodu do utrzymania |
| Karta modelu | Zawsze „Sonnet" | Anthropic nie pokazuje Opusa osobno; trzeci limit to zawsze Sonnet |
| Trend na statusie | TAK (`▲ x%/h`) | Więcej info na pierwszy rzut oka; dane już liczone w predykcji |
| Wskaźnik świeżości | Uczciwa kropka | Pokazuje wiek danych; sygnalizuje awarię kolektora |
| Wysokość wykresów | ~280px | Mniej scrollowania przy 3 wykresach pod sobą |

## Design tokens (paleta docelowa)

Zastępują dotychczasowy zestaw zmiennych w `static/style.css` (`:root`).
Blok `[data-theme="light"]` znika w całości.

```
--bg:        #0a0e12   /* tło strony */
--panel:     #0f161d   /* panel/karta — subtelnie jaśniejsze tło, BEZ ramki */
--panel-2:   #0a1018   /* tor progressu (tło paska) */
--border:    #15202b   /* hairline tylko tam, gdzie naprawdę trzeba (np. linie siatki) */
--text:      #e8f0f5   /* liczby, treść główna */
--mid:       #8499ab   /* tytuły wykresów, treść drugorzędna */
--label:     #5b7083   /* mikro-etykiety mono uppercase */
--accent:    #22d3ee   /* cyan — interaktywne: aktywny zakres, Refresh, focus, linia Weekly */
--ok:        #10b981   /* zielony — stan ok */
--warn:      #f5a524   /* bursztyn — stan uwaga + linia Target */
--crit:      #f43f5e   /* czerwony — stan krytyczny */
--line-session: #10b981
--line-sonnet:  #a78bfa
--reset-marker-color: rgba(120,150,170,0.5)
--mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace
```

Progi koloru stanu (bez zmian wobec obecnej logiki `getProgressClass`):
`>=90% → crit`, `>=70% → warn`, reszta `ok`.

## Specyfikacja komponentów

### Header
- Tytuł „🤖 Claude Usage Dashboard" (system-sans).
- **Kropka świeżości** (zastępuje przełącznik motywu): mała kropka + tekst „Xm temu"
  liczony z `data.timestamp` (`/api/current`). Bez nowego endpointu.
  - wiek `< 12 min` → zielona (`--ok`), tekst np. „3m temu"
  - `12–30 min` → bursztyn (`--warn`)
  - `> 30 min` → czerwona (`--crit`) — sygnał, że kolektor stanął
  - (snapshoty są co 5 min; 12 min ≈ 2 nieudane cykle)
- `↻ Refresh` (akcent cyan, mono), `Logout` (czerwony, mono).

### Etykiety sekcji
Mono uppercase z trackingiem, kolor `--label`: `STATUS`, `📊 PREDICTION`, `CHARTS`.
(„Status" to nowa etykieta; wcześniej sekcja była bez nagłówka — czysto wizualny dodatek.)

### Karty statusu (Weekly / Session / Sonnet)
Waga Airy: tło `--panel`, **bez ramki, bez paska akcentu u góry**, zaokrąglenie 8px,
padding ~14–16px. Zawartość:
- nazwa: `● Nazwa · <okno>` — kropka w kolorze stanu, mono uppercase `--label`
  (`Weekly · 168h`, `Session · 5h`, `Sonnet · 168h`),
- w rogu: **trend** `▲ x%/h` (mono, `--mid`); ukryj gdy brak danych/`low_confidence`,
- wielka liczba: `NN.N` mono 700, sufiks `% used` (`--label`),
- cienki pasek (6px), tło `--panel-2`, wypełnienie w kolorze stanu,
- meta (mono, `--label`): `NN.N% left` ↔ `reset DD.MM HH:MM · <za ile>`.

Tytuł karty modelu pochodzi dynamicznie z `model` (jak teraz) — w praktyce zawsze
„Sonnet"; logika bez zmian, zmienia się tylko styl.

### Karty predykcji (Weekly / Session / Sonnet)
Tło `--panel`, bez ramki. Zawartość:
- nazwa mono uppercase `--label`,
- **badge** stanu: `✓ On track` (`--ok`) / `⚠ Will exceed limit` (`--crit`);
  warianty „Collecting…" / „Reset…" jak w obecnej logice (`low_confidence`, `stale_data`),
- siatka **klucz–wartość** (mono): `Current`, `At reset`, `Trend`, `To 100%`, `To reset`.
  Klucze `--label`, wartości `--text` 600; wartości krytyczne w `--crit`.
  Te same pola co dziś — zmiana wyłącznie prezentacji.

### Sekcja wykresów
- **Pasek zakresu**: `Range` (mono `--label`) + przyciski `1h 6h 24h 3d 7d 14d 1m`;
  aktywny w akcencie cyan. Te same `data-hours` co teraz.
- **3 panele wykresów** (Weekly / Session / Sonnet), tło `--panel`, lekka oprawa,
  wysokość canvasa **~280px** (desktop), responsywnie niżej na mobile (np. 220/180px).
- Tytuł panelu mono `--mid`. Legenda: chip „Usage" w kolorze linii + „Target" przerywany bursztyn.

#### Chart.js — zmiany konfiguracji (`initCharts` + `chartConfig`)
- Kolory linii Usage: Weekly `--accent` (#22d3ee), Session `#10b981`, Sonnet `#a78bfa`;
  `fill: true` z gradientem do przezroczystości (jak dziś, nowe kolory).
- Target: bursztyn `#f5a524`, `borderDash`, bez punktów (bez zmian logiki `targetSegment`).
- `scales.x/y`: kolor siatki `--border`, kolor ticków `--label`, tytuł osi `--mid`.
- `plugins.legend.labels.color` = `--mid`; tooltip dopasowany do ciemnego tła
  (tło `--panel`, border `--border`, tekst `--text`).
- `--reset-marker-color` użyte przez istniejący `resetMarkersPlugin` (bez zmian logiki,
  nowa wartość zmiennej).
- Czcionka etykiet wykresu: `--mono` (Chart.defaults.font.family) dla spójności.
- **Bez zmian logiki danych**: `calculateIdealLine`, `extractResetMarkers`,
  `targetSegment`, `period_start_at` — nietknięte.

### Footer
Mono `--label`: „last update: … · auto-refresh 5m".

### Login (`templates/login.html`)
Reskin pod tę samą skórę: tło `--bg`, panel `--panel` bez ramki, input z obwódką
`--border` i focus w `--accent`, przycisk login w akcencie cyan. Komunikat błędu w `--crit`.

## Usuwane

- Blok `[data-theme="light"]` w `style.css` oraz style `.theme-toggle`.
- Przycisk `#theme-toggle` w `dashboard.html`.
- JS motywu: `initTheme`, `toggleTheme`, `updateThemeIcon`, listener toggla, oraz
  jasna gałąź w inline-skrypcie `<head>` (zostaje stałe ustawienie ciemnego
  `meta theme-color` = `#0a0e12`).
- `localStorage` klucz `theme` przestaje być używany (bez migracji — nieistotny).

## Pliki objęte zmianą

- `static/style.css` — przepisany zestaw tokenów + style komponentów; usunięty light + toggle.
- `templates/dashboard.html` — markup nagłówka (kropka świeżości), etykiety sekcji,
  markup kart statusu (kropka + trend), kart predykcji (badge + kv-grid), paneli wykresów;
  usunięty toggle i JS motywu; wpięcie `trend_per_hour` z `/api/prediction` do kart statusu;
  logika kropki świeżości z `data.timestamp`; nowe kolory/opcje Chart.js.
- `templates/login.html` — reskin.

Backend (`app.py`, `database.py`, fetcher) **bez zmian** — wszystkie dane już są w
odpowiedziach `/api/current` i `/api/prediction`.

## Plumbing danych (frontend)

- **Trend na statusie**: `updatePredictions()` ma `prediction.trend_per_hour`. Po pobraniu
  predykcji ustawiamy trend na odpowiedniej karcie statusu (Weekly/Session/Sonnet).
  Gdy `low_confidence`/`stale_data`/`null` → trend ukryty (nie pokazujemy „0%/h" jako szumu).
- **Kropka świeżości**: `updateCurrentStatus()` liczy `Date.now() - new Date(data.timestamp)`
  i mapuje na kolor + tekst względny.

## Poza zakresem (non-goals)

- Zmiana układu informacji (scalanie status+predykcja, nowe widoki) — to był odrzucony wariant.
- Nowe endpointy, zmiany w bazie, w kolektorze, w parserze `/usage`.
- Tryb jasny.
- Nowe zależności frontendowe (żadnych web-fontów/CDN ponad obecny Chart.js).

## Weryfikacja

- Uruchomić `python app.py`, zalogować się, porównać z makietą companion (`fullpage`/`weight`).
- Sprawdzić wszystkie zakresy czasu (1h…1m) — wykresy renderują się w nowej skórze,
  Target i markery resetu widoczne.
- Stany kart: ok/uwaga/krytyczny (kolory), oraz predykcja „Will exceed".
- Kropka świeżości: świeże dane = zielona; sztucznie postarzony `timestamp` = bursztyn/czerwony.
- Mobile (≤768px, ≤380px): układ się nie rozjeżdża, wysokości wykresów zmniejszone.
- Brak odwołań do usuniętego trybu jasnego (grep `data-theme`, `theme-toggle`, `toggleTheme`).

## Otwarte kwestie

Brak — kierunek i wszystkie detale zatwierdzone w brainstormie.
