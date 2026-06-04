"""Repair test: backfill_period_start_at must fix already-corrupted rows.

Simulates the exact pre-fix prod state from 2026-06-04 (a glitched resets_at +24h,
and the following genuine reset frozen at the previous period's start), injected
via raw SQL to bypass insert-time sanitization. Verifies the injected state is
indeed corrupted, then that backfill repairs both columns.

Runs under pytest or standalone: python3 test_backfill_repairs_glitch.py
"""
import os
import tempfile
from datetime import datetime, timezone

from database import UsageDatabase

# (captured_at, used, resets_at, period_start_at) — strings as stored on prod.
CORRUPT_ROWS = [
    ("2026-06-04 00:29:06+00:00", 32.0, "2026-06-04 00:30:00+00:00", "2026-06-03 19:30:00+00:00"),
    ("2026-06-04 00:34:05+00:00", 0.0,  "2026-06-04 05:30:00+00:00", "2026-06-04 00:30:00+00:00"),
    ("2026-06-04 05:24:06+00:00", 3.0,  "2026-06-04 05:30:00+00:00", "2026-06-04 00:30:00+00:00"),
    # glitch: resets_at +24h, pstart frozen
    ("2026-06-04 05:29:06+00:00", 3.0,  "2026-06-05 05:29:00+00:00", "2026-06-04 00:30:00+00:00"),
    # genuine reset, but pstart stale (the bug)
    ("2026-06-04 05:34:05+00:00", 0.0,  "2026-06-04 10:30:00+00:00", "2026-06-04 00:30:00+00:00"),
    ("2026-06-04 05:39:05+00:00", 0.0,  "2026-06-04 10:30:00+00:00", "2026-06-04 00:30:00+00:00"),
]


def _inject(db: UsageDatabase):
    cur = db.conn.cursor()
    for captured, used, resets, pstart in CORRUPT_ROWS:
        cur.execute("INSERT INTO snapshots (captured_at, account_type, email) VALUES (?,?,?)",
                    (captured, "max", "x@example.com"))
        sid = cur.lastrowid
        cur.execute(
            "INSERT INTO quotas (snapshot_id, quota_type, model, percent_remaining, resets_at, period_start_at)"
            " VALUES (?,?,?,?,?,?)",
            (sid, "session", None, 100.0 - used, resets, pstart),
        )
    db.conn.commit()


def _rows(db: UsageDatabase):
    cur = db.conn.cursor()
    cur.execute("""
        SELECT s.captured_at AS cap, q.resets_at AS reset, q.period_start_at AS pstart
        FROM snapshots s JOIN quotas q ON q.snapshot_id = s.id
        WHERE q.quota_type='session' ORDER BY s.captured_at ASC
    """)
    return {db._parse_dt(r["cap"]): (db._parse_dt(r["reset"]), db._parse_dt(r["pstart"]))
            for r in cur.fetchall()}


def test_backfill_repairs_glitch_episode():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd); os.unlink(path)
    db = UsageDatabase(path)
    try:
        _inject(db)

        boundary = datetime(2026, 6, 4, 5, 30, tzinfo=timezone.utc)
        reset_t = datetime(2026, 6, 4, 5, 34, 5, tzinfo=timezone.utc)
        glitch_t = datetime(2026, 6, 4, 5, 29, 6, tzinfo=timezone.utc)
        post_t = datetime(2026, 6, 4, 5, 39, 5, tzinfo=timezone.utc)
        early_t = datetime(2026, 6, 4, 0, 29, 6, tzinfo=timezone.utc)

        before = _rows(db)
        # sanity: the injected state really is the bug
        assert before[reset_t][1] != boundary, "precondition: reset pstart already correct"
        assert before[glitch_t][0] != boundary, "precondition: glitch resets_at already sane"

        result = db.backfill_period_start_at()

        after = _rows(db)
        assert after[glitch_t][0] == boundary, f"glitch resets_at not repaired: {after[glitch_t][0]}"
        assert after[reset_t][1] == boundary, f"reset period_start_at not repaired: {after[reset_t][1]}"
        assert after[post_t][1] == boundary, "post-reset period_start_at not repaired"
        assert after[early_t][1] == datetime(2026, 6, 3, 19, 30, tzinfo=timezone.utc), \
            "earlier period_start_at wrongly changed"
        assert result.get('resets_at_sanitized', 0) >= 1 and result.get('period_start_updates', 0) >= 2, \
            f"unexpected repair counts: {result}"
    finally:
        db.close(); os.unlink(path)


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
