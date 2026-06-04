"""Tests for the data-retention mechanism (DB delete + filesystem prune)."""
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone

import cleanup_old_data
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
