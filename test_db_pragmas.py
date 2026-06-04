"""DB connection should use WAL + a busy_timeout so the collector's writes and the
web app's reads don't intermittently hit 'database is locked'.

Runs under pytest (collected test_* functions) or standalone: python3 test_db_pragmas.py
"""
import os
import tempfile

from database import UsageDatabase


def test_wal_and_busy_timeout():
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd); os.unlink(p)
    db = UsageDatabase(p)
    try:
        jm = db.conn.execute("PRAGMA journal_mode").fetchone()[0]
        bt = db.conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert str(jm).lower() == "wal", f"journal_mode expected wal, got {jm}"
        assert int(bt) >= 5000, f"busy_timeout expected >=5000, got {bt}"
    finally:
        db.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(p + ext)
            except OSError:
                pass


if __name__ == '__main__':
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:
                failures += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    raise SystemExit(failures)
