"""DB connection should use WAL + a busy_timeout so the collector's writes and the
web app's reads don't intermittently hit 'database is locked'.

Run: python3 test_db_pragmas.py
"""
import os
import tempfile

from database import UsageDatabase


def main():
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd); os.unlink(p)
    db = UsageDatabase(p)
    try:
        jm = db.conn.execute("PRAGMA journal_mode").fetchone()[0]
        bt = db.conn.execute("PRAGMA busy_timeout").fetchone()[0]
        failures = []
        if str(jm).lower() != "wal":
            failures.append(f"journal_mode expected wal, got {jm}")
        if int(bt) < 5000:
            failures.append(f"busy_timeout expected >=5000, got {bt}")
        if failures:
            print("FAIL")
            for f in failures:
                print("  -", f)
            raise SystemExit(1)
        print("PASS", {"journal_mode": jm, "busy_timeout": bt})
    finally:
        db.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(p + ext)
            except OSError:
                pass


if __name__ == "__main__":
    main()
