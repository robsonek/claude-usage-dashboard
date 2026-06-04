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
