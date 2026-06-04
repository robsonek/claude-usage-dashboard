"""Regression test for the 2026-06-04 session-chart glitch.

A transient `claude /usage` reading reported `resets_at` ~24h in the future
(date-rollover artifact at the reset boundary). The next genuine reset was then
misclassified as a SHIFT, freezing `period_start_at` at the previous period's
start for the whole next period. Verifies the glitch is sanitized at insert time
so the genuine reset sets period_start_at to the real boundary (05:30).

Runs under pytest or standalone: python3 test_glitch_period_start.py
"""
import os
import tempfile
from datetime import datetime, timezone

from database import UsageDatabase


def _session_snapshot(captured: str, used: float, resets: str) -> dict:
    return {
        "captured_at": captured,
        "account_type": "max",
        "email": "x@example.com",
        "quotas": [{"type": "session", "model": None,
                    "percent_remaining": 100.0 - used,
                    "resets_at": resets, "time_remaining_seconds": None}],
    }


def _session_rows(db: UsageDatabase):
    cur = db.conn.cursor()
    cur.execute("""
        SELECT s.captured_at AS cap, q.resets_at AS reset, q.period_start_at AS pstart
        FROM snapshots s JOIN quotas q ON q.snapshot_id = s.id
        WHERE q.quota_type = 'session' ORDER BY s.captured_at ASC
    """)
    return {db._parse_dt(r["cap"]): r for r in cur.fetchall()}


def test_glitch_does_not_freeze_period_start():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd); os.unlink(path)
    db = UsageDatabase(path)
    try:
        # Real-shaped sequence around the 2026-06-04 05:30 reset (UTC).
        seq = [
            _session_snapshot("2026-06-04T00:29:06+00:00", 32.0, "2026-06-04T00:30:00+00:00"),
            _session_snapshot("2026-06-04T00:34:05+00:00", 0.0,  "2026-06-04T05:30:00+00:00"),
            _session_snapshot("2026-06-04T05:24:06+00:00", 3.0,  "2026-06-04T05:30:00+00:00"),
            # GLITCH: resets_at reported ~24h ahead (06-05 instead of 06-04):
            _session_snapshot("2026-06-04T05:29:06+00:00", 3.0,  "2026-06-05T05:29:00+00:00"),
            # GENUINE RESET: usage drops to 0, new period [05:30, 10:30]:
            _session_snapshot("2026-06-04T05:34:05+00:00", 0.0,  "2026-06-04T10:30:00+00:00"),
            _session_snapshot("2026-06-04T05:39:05+00:00", 0.0,  "2026-06-04T10:30:00+00:00"),
        ]
        for snap in seq:
            db.insert_snapshot(snap)

        rows = _session_rows(db)

        def at(t):
            return rows[datetime.fromisoformat(t)]

        boundary = datetime(2026, 6, 4, 5, 30, tzinfo=timezone.utc)
        assert db._parse_dt(at("2026-06-04T05:29:06+00:00")["reset"]) == boundary, \
            "glitch resets_at not sanitized to the carried-forward value"
        assert db._parse_dt(at("2026-06-04T05:34:05+00:00")["pstart"]) == boundary, \
            "genuine reset period_start_at not set to the real boundary"
        assert db._parse_dt(at("2026-06-04T05:39:05+00:00")["pstart"]) == boundary, \
            "post-reset period_start_at drifted"
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
