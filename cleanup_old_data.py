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
