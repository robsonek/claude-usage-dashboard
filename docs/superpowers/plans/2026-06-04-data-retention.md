# Data Retention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically delete data older than `RETENTION_DAYS` (default 90) — SQLite snapshots+quotas, `data/YYYY-MM-DD/` JSON dirs, and `data/raw_debug/` files — triggered once/day from the existing collector.

**Architecture:** A configurable cutoff drives an FK-safe DB delete (`database.py`), orchestrated by a new `cleanup_old_data.py` that also prunes the filesystem, invoked once/day (date-marker gated) at the end of `collect_history.sh`. No new systemd units; no schema/web/fetcher changes.

**Tech Stack:** Python 3.11+ stdlib (`sqlite3`, `datetime`, `os`, `shutil`, `re`), bash, pytest.

**Spec:** `docs/superpowers/specs/2026-06-04-data-retention-design.md`

---

## File Structure

- `config.py` — **modify**: add `RETENTION_DAYS`.
- `database.py` — **modify**: add `delete_older_than(cutoff)` + `vacuum()` to `UsageDatabase`.
- `cleanup_old_data.py` — **create**: orchestrator (cutoff → DB delete + conditional VACUUM → filesystem prune), CLI with `--dry-run`. Exposes `prune_data_dir(data_dir, cutoff, dry_run=False)` for testing.
- `collect_history.sh` — **modify**: once/day gated call to `cleanup_old_data.py` after a successful insert.
- `test_retention.py` — **create**: DB delete (incl. no-orphan-quotas) + filesystem prune + dry-run tests.
- `README.md` — **modify**: `RETENTION_DAYS` env row + short retention note.

Key facts (verified against the codebase):
- `database.py` registers a datetime adapter; comparisons pass a `datetime` bound param and rely on it formatting identically to stored values (same pattern as `get_history`'s `WHERE s.captured_at >= ?`). **Tests must insert `captured_at` as `datetime` objects** so formats match.
- `PRAGMA foreign_keys` is **off** → `ON DELETE CASCADE` will NOT fire; delete `quotas` explicitly (child) before `snapshots` (parent).
- `collect_history.sh` already defines `$DATA_DIR`, `$TODAY`, `$VENV_PYTHON`, `$SCRIPT_DIR` and holds a flock for the whole run.

---

## Task 1: Config + FK-safe DB delete & vacuum

**Files:**
- Modify: `config.py`
- Modify: `database.py` (add two methods to `UsageDatabase`, e.g. just before `def close`)
- Test: `test_retention.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `test_retention.py`:

```python
"""Tests for the data-retention mechanism (DB delete + filesystem prune)."""
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone

from database import UsageDatabase


def _fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return UsageDatabase(path), path


def _insert(db, captured_dt, n_quotas=1):
    """Insert a snapshot (captured_at as a datetime, so the adapter formats it
    exactly like stored rows) plus N quotas. Returns the snapshot id."""
    cur = db.conn.cursor()
    cur.execute(
        "INSERT INTO snapshots (captured_at, account_type, email) VALUES (?, ?, ?)",
        (captured_dt, "max", "x@example.com"))
    sid = cur.lastrowid
    for _ in range(n_quotas):
        cur.execute(
            "INSERT INTO quotas (snapshot_id, quota_type, percent_remaining) "
            "VALUES (?, ?, ?)", (sid, "weekly", 50.0))
    db.conn.commit()
    return sid


def test_delete_older_than_removes_old_and_their_quotas():
    db, path = _fresh_db()
    try:
        _insert(db, datetime(2026, 1, 1, tzinfo=timezone.utc), n_quotas=2)  # old
        _insert(db, datetime(2026, 1, 2, tzinfo=timezone.utc), n_quotas=1)  # old
        _insert(db, datetime(2026, 6, 1, tzinfo=timezone.utc), n_quotas=3)  # recent
        cutoff = datetime(2026, 3, 1, tzinfo=timezone.utc)

        res = db.delete_older_than(cutoff)

        assert res == {"snapshots": 2, "quotas": 3}
        cur = db.conn.cursor()
        assert cur.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1
        assert cur.execute("SELECT COUNT(*) FROM quotas").fetchone()[0] == 3
        # foreign_keys pragma is OFF, so the explicit child-first delete is what
        # prevents orphans — assert there are none.
        orphans = cur.execute(
            "SELECT COUNT(*) FROM quotas WHERE snapshot_id NOT IN "
            "(SELECT id FROM snapshots)").fetchone()[0]
        assert orphans == 0
    finally:
        db.close()
        os.unlink(path)


def test_delete_older_than_noop_when_all_recent():
    db, path = _fresh_db()
    try:
        _insert(db, datetime(2026, 6, 1, tzinfo=timezone.utc))
        cutoff = datetime(2026, 3, 1, tzinfo=timezone.utc)

        res = db.delete_older_than(cutoff)

        assert res == {"snapshots": 0, "quotas": 0}
        cur = db.conn.cursor()
        assert cur.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1
    finally:
        db.close()
        os.unlink(path)


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"PASS {_name}")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"FAIL {_name}: {type(e).__name__}: {e}")
    raise SystemExit(failures)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest test_retention.py -v`
Expected: FAIL — `AttributeError: 'UsageDatabase' object has no attribute 'delete_older_than'`.

- [ ] **Step 3: Add `RETENTION_DAYS` to `config.py`**

After the `DB_FILE = ...` line, add:

```python
# Data retention: rows/files older than this many days are pruned by
# cleanup_old_data.py (invoked once/day from collect_history.sh).
RETENTION_DAYS = int(os.environ.get('RETENTION_DAYS', '90'))
```

- [ ] **Step 4: Add `delete_older_than` and `vacuum` to `database.py`**

Insert these two methods into the `UsageDatabase` class, immediately before `def close(self):`:

```python
    def delete_older_than(self, cutoff: datetime) -> Dict[str, int]:
        """Delete snapshots captured before `cutoff` and their quotas.

        foreign_keys pragma is OFF on this connection, so ON DELETE CASCADE does
        NOT fire — delete child `quotas` first, then `snapshots`, in one
        transaction. `cutoff` is a tz-aware datetime passed as a bound param so
        the registered adapter formats it exactly like stored `captured_at`
        (same approach as get_history). Returns {'snapshots': N, 'quotas': M}.
        """
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
        """Reclaim free pages after a delete. Commit first — VACUUM cannot run
        inside a transaction."""
        self.conn.commit()
        self.conn.execute("VACUUM")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest test_retention.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Run the full suite (no regressions)**

Run: `pytest -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add config.py database.py test_retention.py
git commit -m "feat(retention): RETENTION_DAYS + FK-safe delete_older_than/vacuum"
```

---

## Task 2: `cleanup_old_data.py` orchestrator + filesystem prune

**Files:**
- Create: `cleanup_old_data.py`
- Test: `test_retention.py` (append)

- [ ] **Step 1: Append failing tests to `test_retention.py`**

Add these imports at the top of `test_retention.py` (next to the existing imports):

```python
import cleanup_old_data
```

Add these tests (before the `if __name__ == "__main__":` block):

```python
def test_prune_data_dir_removes_old_keeps_recent_and_nondates():
    data_dir = tempfile.mkdtemp()
    try:
        for name in ("2026-01-01", "2026-01-02", "2026-06-01"):
            os.makedirs(os.path.join(data_dir, name))
            open(os.path.join(data_dir, name, "12-00.json"), "w").close()
        # non-date entries that must survive
        open(os.path.join(data_dir, ".collect.lock"), "w").close()
        open(os.path.join(data_dir, ".cleanup_done"), "w").close()
        raw = os.path.join(data_dir, "raw_debug")
        os.makedirs(raw)
        old_raw = os.path.join(raw, "old.bin")
        new_raw = os.path.join(raw, "new.bin")
        open(old_raw, "w").close()
        open(new_raw, "w").close()
        old_ts = time.time() - 200 * 86400  # ~200 days ago
        os.utime(old_raw, (old_ts, old_ts))

        cutoff = datetime(2026, 3, 1, tzinfo=timezone.utc)
        res = cleanup_old_data.prune_data_dir(data_dir, cutoff)

        assert res == {"dirs": 2, "raw_files": 1}
        assert not os.path.exists(os.path.join(data_dir, "2026-01-01"))
        assert not os.path.exists(os.path.join(data_dir, "2026-01-02"))
        assert os.path.isdir(os.path.join(data_dir, "2026-06-01"))
        assert os.path.exists(os.path.join(data_dir, ".collect.lock"))
        assert os.path.exists(os.path.join(data_dir, ".cleanup_done"))
        assert not os.path.exists(old_raw)
        assert os.path.exists(new_raw)
    finally:
        shutil.rmtree(data_dir)


def test_prune_data_dir_dry_run_changes_nothing():
    data_dir = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(data_dir, "2026-01-01"))
        cutoff = datetime(2026, 3, 1, tzinfo=timezone.utc)

        res = cleanup_old_data.prune_data_dir(data_dir, cutoff, dry_run=True)

        assert res == {"dirs": 1, "raw_files": 0}
        assert os.path.isdir(os.path.join(data_dir, "2026-01-01"))  # NOT removed
    finally:
        shutil.rmtree(data_dir)
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest test_retention.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cleanup_old_data'`.

- [ ] **Step 3: Create `cleanup_old_data.py`**

```python
#!/usr/bin/env python3
"""Prune data older than RETENTION_DAYS.

Deletes old SQLite snapshots (+their quotas), old `data/YYYY-MM-DD/` JSON
directories, and old `data/raw_debug/` files (by mtime). VACUUMs only when DB
rows were actually removed. `--dry-run` reports what would be deleted without
touching anything. Invoked once/day from collect_history.sh.
"""
import os
import re
import shutil
import sys
from datetime import date, datetime, timedelta, timezone

import config
from database import UsageDatabase

_DATE_DIR_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def prune_data_dir(data_dir, cutoff, dry_run=False):
    """Remove `data/YYYY-MM-DD` dirs older than cutoff.date() and `raw_debug`
    files older than cutoff (by mtime). Non-date entries (.collect.lock,
    .cleanup_done, raw_debug itself, etc.) are left alone. Returns
    {'dirs': n, 'raw_files': m}."""
    dirs_removed = 0
    raw_removed = 0
    cutoff_date = cutoff.date()

    if os.path.isdir(data_dir):
        for name in sorted(os.listdir(data_dir)):
            if not _DATE_DIR_RE.match(name):
                continue
            full = os.path.join(data_dir, name)
            if not os.path.isdir(full):
                continue
            try:
                d = date.fromisoformat(name)
            except ValueError:
                continue
            if d < cutoff_date:
                if not dry_run:
                    shutil.rmtree(full)
                dirs_removed += 1

    raw_dir = os.path.join(data_dir, 'raw_debug')
    if os.path.isdir(raw_dir):
        cutoff_ts = cutoff.timestamp()
        for name in sorted(os.listdir(raw_dir)):
            full = os.path.join(raw_dir, name)
            if not os.path.isfile(full):
                continue
            if os.path.getmtime(full) < cutoff_ts:
                if not dry_run:
                    os.remove(full)
                raw_removed += 1

    return {'dirs': dirs_removed, 'raw_files': raw_removed}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    dry_run = '--dry-run' in argv
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.RETENTION_DAYS)

    db = UsageDatabase(config.DB_FILE)
    try:
        if dry_run:
            cur = db.conn.cursor()
            snaps = cur.execute(
                "SELECT COUNT(*) FROM snapshots WHERE captured_at < ?",
                (cutoff,)).fetchone()[0]
            quotas = cur.execute(
                "SELECT COUNT(*) FROM quotas WHERE snapshot_id IN "
                "(SELECT id FROM snapshots WHERE captured_at < ?)",
                (cutoff,)).fetchone()[0]
            db_res = {'snapshots': snaps, 'quotas': quotas}
        else:
            db_res = db.delete_older_than(cutoff)
            if db_res['snapshots'] > 0:
                db.vacuum()
        fs_res = prune_data_dir(config.DATA_DIR, cutoff, dry_run=dry_run)
    finally:
        db.close()

    tag = 'DRY-RUN ' if dry_run else ''
    total = (db_res['snapshots'] + db_res['quotas']
             + fs_res['dirs'] + fs_res['raw_files'])
    if total == 0:
        print(f"{tag}cleanup: nothing older than {config.RETENTION_DAYS}d "
              f"(cutoff {cutoff.date()})")
    else:
        print(f"{tag}cleanup: {db_res['snapshots']} snapshots, "
              f"{db_res['quotas']} quotas, {fs_res['dirs']} day-dirs, "
              f"{fs_res['raw_files']} raw_debug files "
              f"(cutoff {cutoff.date()})")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest test_retention.py -v`
Expected: PASS (4 tests total).

- [ ] **Step 5: Smoke-test the CLI dry-run against the real local DB**

Run: `python3 cleanup_old_data.py --dry-run`
Expected: a line like `DRY-RUN cleanup: nothing older than 90d (cutoff YYYY-MM-DD)` (or non-zero counts), exit 0, and NO changes to `usage.db`/`data/`.

- [ ] **Step 6: Run the full suite**

Run: `pytest -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add cleanup_old_data.py test_retention.py
git commit -m "feat(retention): cleanup_old_data.py orchestrator + filesystem prune"
```

---

## Task 3: Trigger cleanup once/day from `collect_history.sh`

**Files:**
- Modify: `collect_history.sh`

- [ ] **Step 1: Insert the gated cleanup call**

In `collect_history.sh`, locate this block near the end:

```bash
# Save to SQLite database (insert_to_db.py skips pure-error records itself)
if ! echo "$USAGE_JSON" | "$VENV_PYTHON" "$SCRIPT_DIR/insert_to_db.py"; then
    log "ERROR: insert_to_db.py failed"
    exit 1
fi

# Surface a persistent fetch error to the timer/monitoring (artifacts already saved).
if [ "$FETCH_ERROR" -ne 0 ]; then
    exit 2
fi
```

Insert the cleanup block BETWEEN the insert block and the `FETCH_ERROR` block, so it reads:

```bash
# Save to SQLite database (insert_to_db.py skips pure-error records itself)
if ! echo "$USAGE_JSON" | "$VENV_PYTHON" "$SCRIPT_DIR/insert_to_db.py"; then
    log "ERROR: insert_to_db.py failed"
    exit 1
fi

# Retention cleanup, at most once per day (date-marker gated). Runs inside the
# existing flock. Housekeeping only: a failure is logged but never fails the
# collection run, and the marker is written only on success so a failed run
# retries on the next tick.
CLEANUP_MARKER="$DATA_DIR/.cleanup_done"
if [ "$(cat "$CLEANUP_MARKER" 2>/dev/null)" != "$TODAY" ]; then
    if "$VENV_PYTHON" "$SCRIPT_DIR/cleanup_old_data.py"; then
        echo "$TODAY" > "$CLEANUP_MARKER"
    else
        log "WARN: cleanup_old_data.py failed (non-fatal)"
    fi
fi

# Surface a persistent fetch error to the timer/monitoring (artifacts already saved).
if [ "$FETCH_ERROR" -ne 0 ]; then
    exit 2
fi
```

- [ ] **Step 2: Lint the shell script**

Run: `bash -n collect_history.sh`
Expected: no output (syntax OK). If `shellcheck` is installed: `shellcheck collect_history.sh` (pre-existing warnings only).

- [ ] **Step 3: Manually verify the gating + cleanup wiring locally**

Run:
```bash
rm -f data/.cleanup_done
DATA_DIR="$(pwd)/data" TODAY="$(date +%Y-%m-%d)" VENV_PYTHON="python3" SCRIPT_DIR="$(pwd)" bash -c '
CLEANUP_MARKER="$DATA_DIR/.cleanup_done"
if [ "$(cat "$CLEANUP_MARKER" 2>/dev/null)" != "$TODAY" ]; then
  if "$VENV_PYTHON" "$SCRIPT_DIR/cleanup_old_data.py" --dry-run; then echo "$TODAY" > "$CLEANUP_MARKER"; fi
fi
echo "marker now: $(cat "$CLEANUP_MARKER")"
'
```
Expected: prints the dry-run cleanup line, then `marker now: <today>`. Re-running the same block prints NO cleanup line (gate holds). Then `rm -f data/.cleanup_done` to reset.
(Note: this uses `--dry-run` only for the manual check; the real script calls without `--dry-run`.)

- [ ] **Step 4: Commit**

```bash
git add collect_history.sh
git commit -m "feat(retention): run cleanup once/day from collect_history.sh"
```

---

## Task 4: Document `RETENTION_DAYS` in README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add the env-var row**

In `README.md`, in the Configuration table, replace:

```markdown
| CLAUDE_BIN | Path to Claude CLI | claude |
```

with:

```markdown
| CLAUDE_BIN | Path to Claude CLI | claude |
| RETENTION_DAYS | Days of history to keep; older snapshots/quotas, `data/YYYY-MM-DD/` dirs and `data/raw_debug/` files are pruned daily | 90 |
```

- [ ] **Step 2: Add a short retention note**

In `README.md`, immediately after the line:

```markdown
After changing any variable, restart the service:
`sudo systemctl restart claude-dashboard`.
```

insert:

```markdown

### Data retention

`collect_history.sh` runs `cleanup_old_data.py` at most once per day (gated by a
`data/.cleanup_done` date marker). It deletes snapshots+quotas older than
`RETENTION_DAYS`, removes `data/YYYY-MM-DD/` JSON dirs and `data/raw_debug/`
files past the same window, and `VACUUM`s the database only when rows were
removed. Run it manually anytime, and use `--dry-run` to preview:

```bash
venv/bin/python cleanup_old_data.py --dry-run
```
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document RETENTION_DAYS and the cleanup mechanism"
```

---

## Task 5: Deploy to production (controller-only — NOT a subagent task)

> Touches the live server `ai.onee.pl`. Execute interactively, not via a subagent. The first real run is a no-op (oldest data ≈68d < 90d).

**Files:** none (deploy of existing committed files)

- [ ] **Step 1: Backup prod files that will be overwritten**

```bash
ssh robson@ai.onee.pl 'cd /home/robson/claude-dashboard && tar czf /home/robson/claude-dashboard-retention-backup-$(date +%Y%m%d-%H%M%S).tar.gz config.py database.py collect_history.sh && echo backed-up'
```

- [ ] **Step 2: Rsync the changed files**

```bash
rsync -avz ./config.py ./database.py ./cleanup_old_data.py ./collect_history.sh robson@ai.onee.pl:/home/robson/claude-dashboard/
```
(`collect_history.sh` keeps its executable bit; `cleanup_old_data.py` is invoked via the venv python, so no +x needed. No service restart needed: the collector runs a fresh process each tick and picks up the new shell/config; the web service uses none of the new code.)

- [ ] **Step 3: Dry-run on prod and confirm it's a safe no-op**

```bash
ssh robson@ai.onee.pl 'cd /home/robson/claude-dashboard && venv/bin/python cleanup_old_data.py --dry-run'
```
Expected: `DRY-RUN cleanup: nothing older than 90d (cutoff ...)` — confirms nothing would be deleted yet.

- [ ] **Step 4: Confirm the gate marker will let it run tonight**

```bash
ssh robson@ai.onee.pl 'cat /home/robson/claude-dashboard/data/.cleanup_done 2>/dev/null || echo "(no marker yet — cleanup runs on next collector tick)"'
```
Expected: no marker (or a date) — informational. The next collector tick after the change runs the real (no-op) cleanup and writes today's date.

---

## Self-Review (completed during planning)

**Spec coverage:** RETENTION_DAYS config → T1; FK-safe `delete_older_than` + `vacuum` → T1; orchestrator + filesystem prune (date-dirs + raw_debug) + `--dry-run` → T2; once/day gated trigger in collect_history.sh (non-fatal, marker) → T3; docs → T4; safe rollout (dry-run, no-op first run, backup) → T5. Tests cover the no-orphan-quotas FK gotcha (T1) and filesystem prune incl. non-date survivors + dry-run (T2). VACUUM-only-when-deleted → T2 main(). Non-goals (no schema/web/fetcher/systemd-unit changes) respected.

**Placeholder scan:** every code/step is concrete; no TBD/TODO/"handle errors" hand-waving.

**Type/name consistency:** `delete_older_than(cutoff) -> {'snapshots','quotas'}` and `vacuum()` (T1) are used exactly so in `cleanup_old_data.main` (T2); `prune_data_dir(data_dir, cutoff, dry_run=False) -> {'dirs','raw_files'}` defined in T2 and asserted with those keys in the T2 tests; `RETENTION_DAYS`/`DATA_DIR`/`DB_FILE` from `config` used consistently; shell vars (`$DATA_DIR`,`$TODAY`,`$VENV_PYTHON`,`$SCRIPT_DIR`) all pre-exist in collect_history.sh.
