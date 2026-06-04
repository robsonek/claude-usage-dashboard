# Data Retention / Cleanup — Design

- **Data:** 2026-06-04
- **Typ:** Nowa funkcjonalność (mechanizm retencji starych danych)
- **Status:** Zatwierdzony kierunek → do spisania planu implementacji

## Cel

Automatycznie usuwać dane starsze niż okno retencji, żeby baza i katalog `data/`
nie rosły w nieskończoność. Wcześniej czyszczono ręcznie (stąd najstarszy
`snapshots.id = 15414`, najstarszy `captured_at = 2026-03-28`).

## Decyzje (zablokowane w brainstormie)

| Decyzja | Wybór |
|---|---|
| Okno retencji | **90 dni**, konfigurowalne przez env `RETENTION_DAYS` (domyślnie 90) |
| Zakres | **Wszystko**: SQLite (`snapshots`+`quotas`), `data/YYYY-MM-DD/` (JSON), `data/raw_debug/` — wspólne okno 90 dni |
| Uruchamianie | **Wpięte w `collect_history.sh`**, gated raz/dzień (znacznik daty w `data/`) — bez nowych unitów systemd |
| VACUUM | Tylko gdy faktycznie coś skasowano |
| Kasowanie DB | Jawne (quotas→snapshots) — `PRAGMA foreign_keys` jest wyłączone, nie polegamy na kaskadzie |

## Architektura (komponenty o jednej odpowiedzialności)

### 1. `config.py` — parametr
```python
RETENTION_DAYS = int(os.environ.get('RETENTION_DAYS', '90'))
```
Cutoff liczony w runtime: `datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)`.

### 2. `database.py` — bezpieczne kasowanie + vacuum
Dwie nowe metody na `UsageDatabase`:

```python
def delete_older_than(self, cutoff: datetime) -> Dict[str, int]:
    """Delete snapshots (and their quotas) captured before `cutoff`.
    Deletes child `quotas` first, then `snapshots`, in one transaction —
    foreign_keys pragma is OFF, so we must not rely on ON DELETE CASCADE.
    Returns {'snapshots': N, 'quotas': M}. `cutoff` is a tz-aware datetime;
    passed as a bound param so the registered adapter formats it exactly like
    stored `captured_at` (same pattern as get_history's `captured_at >= ?`)."""
    cur = self.conn.cursor()
    cur.execute(
        "DELETE FROM quotas WHERE snapshot_id IN "
        "(SELECT id FROM snapshots WHERE captured_at < ?)", (cutoff,))
    quotas = cur.rowcount
    cur.execute("DELETE FROM snapshots WHERE captured_at < ?", (cutoff,))
    snapshots = cur.rowcount
    self.conn.commit()
    return {'snapshots': snapshots, 'quotas': quotas}

def vacuum(self) -> None:
    """Reclaim free pages. Commit first — VACUUM cannot run in a transaction."""
    self.conn.commit()
    self.conn.execute("VACUUM")
```

Uwaga (gotcha): porównanie `captured_at < ?` z parametrem `datetime` działa, bo
adapter `_adapt_datetime_utc` formatuje cutoff identycznie jak zapisane wartości
(wszystkie UTC `+00:00`), tak samo jak istniejące `get_history`.

### 3. `cleanup_old_data.py` — orkiestrator (nowy, uruchamialny)
Odpowiedzialność: policz cutoff, posprzątaj DB + filesystem, zaloguj podsumowanie.

Przepływ:
1. `cutoff = now(UTC) - RETENTION_DAYS dni`.
2. **DB:** `db.delete_older_than(cutoff)`; jeśli `snapshots > 0` → `db.vacuum()`.
3. **JSON dni:** w `DATA_DIR` usuń katalogi o nazwie pasującej do `^\d{4}-\d{2}-\d{2}$`,
   których data < `cutoff.date()`. Pomijaj wszystko inne (`.collect.lock`,
   `raw_debug`, `.cleanup_done`, pliki ukryte).
4. **raw_debug:** w `DATA_DIR/raw_debug/` usuń pliki z `mtime` starszym niż cutoff
   (to samo okno 90 dni). Katalog zostaje.
5. Log jednolinijkowy: ile snapshotów/quotas/katalogów/plików usunięto (lub „nothing to clean").
6. **`--dry-run`:** liczy i raportuje co *zostałoby* usunięte, nic nie kasuje
   (do bezpiecznej weryfikacji na prod).

Wejście przez env (`RETENTION_DAYS`, ścieżki z `config`). Uruchamiany venv-Pythonem.

### 4. `collect_history.sh` — wyzwalacz raz/dzień
Na **końcu** skryptu (po udanym insercie do DB), w obrębie istniejącego flocka:
```bash
CLEANUP_MARKER="$DATA_DIR/.cleanup_done"
if [ "$(cat "$CLEANUP_MARKER" 2>/dev/null)" != "$TODAY" ]; then
    if "$VENV_PYTHON" "$SCRIPT_DIR/cleanup_old_data.py"; then
        echo "$TODAY" > "$CLEANUP_MARKER"
    else
        log "WARN: cleanup_old_data.py failed (non-fatal)"
    fi
fi
```
- Gate: marker `data/.cleanup_done` zawiera ostatnią datę cleanupu; cleanup odpala
  się przy pierwszym 5-min ticku po północy.
- Błąd cleanupu = WARN, **nie zmienia** statusu zbierania (snapshot już zapisany).
  Marker zapisywany tylko po sukcesie (przy błędzie spróbuje ponownie następnym tickiem).
- Umiejscowienie: po insercie, a **przed** finalnym `exit 2` dla `FETCH_ERROR`
  (żeby błąd fetchu nadal był sygnalizowany; cleanup to housekeeping niezależny).

## Testy (pytest, spójne z istniejącym zestawem)

1. `test_delete_older_than` — temp-DB: wstaw snapshoty + quotas z różnymi
   `captured_at` (część < cutoff, część ≥). Po `delete_older_than`: zostają tylko
   nowe snapshoty, zwrócone liczby się zgadzają, i **brak osieroconych `quotas`**
   (pokrywa pułapkę wyłączonego FK — quotas skasowanych snapshotów nie istnieją).
2. `test_delete_older_than_noop` — gdy nic nie jest starsze niż cutoff → zwraca zera,
   nic nie ruszone.
3. `test_cleanup_prunes_filesystem` — temp `DATA_DIR` z katalogami dat (stare+świeże)
   i plikami w `raw_debug` (stare+świeże po mtime): stare znikają, świeże zostają,
   `.collect.lock`/inne nie-datowe pozostają nietknięte.
4. (opcjonalnie) `test_cleanup_dry_run` — `--dry-run` nic nie kasuje, ale raportuje liczby.

## Wdrożenie / rollout (bezpieczeństwo)

- Najstarsze dane mają teraz **68 dni < 90** → **pierwszy run niczego nie skasuje**.
  Mechanizm wchodzi „na sucho"; realne kasowanie zacznie się, gdy dane przekroczą 90 dni.
- Na prod najpierw `cleanup_old_data.py --dry-run` (potwierdzenie „0 do skasowania"),
  potem wdrożenie skryptu + zmiany w `collect_history.sh`.
- Zgodnie z zasadą [[feedback_irreversible_prod_ops]]: przed pierwszym realnym
  kasowaniem (gdy dane przekroczą 90 dni) i tak mamy backupy JSON do daty cutoff;
  dodatkowo deploy backupuje pliki jak dotąd.

## Poza zakresem (non-goals)

- Brak downsamplingu/agregacji starych danych (twarde kasowanie; widoki ≤30 dni i tak < 90).
- Brak zmian schematu DB, web appki, fetchera, formatu zbierania.
- Brak nowych unitów systemd (świadomie — wpinamy w istniejący collector).
- `RETENTION_DAYS` udokumentować w CLAUDE.md/README (tabela env + sekcja retencji);
  na prod env wpięte przez istniejący systemd EnvironmentFile ([[project_prod_env_systemd_wiring]]).

## Otwarte kwestie

Brak — wszystkie decyzje zatwierdzone.
