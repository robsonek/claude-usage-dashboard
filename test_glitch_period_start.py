"""Regression test for the 2026-06-04 session-chart glitch.

A transient `claude /usage` reading reported `resets_at` ~24h in the future
(date-rollover artifact at the reset boundary). The next genuine reset was then
misclassified as a SHIFT, freezing `period_start_at` at the previous period's
start for the whole next period — which drew the Target line as a connected
drop to ~50% instead of a clean gap+marker restart from 0%.

Behavior under test (observable via stored rows):
  1. A glitched resets_at (> period + margin ahead of captured_at) must not be
     trusted; the carried-forward previous resets_at is stored instead.
  2. The genuine reset right after the glitch must set period_start_at to the
     real boundary (05:30), not inherit the stale previous start (00:30).

Run: python3 test_glitch_period_start.py
"""
import os
import tempfile
from datetime import datetime, timezone

from database import UsageDatabase


def _iso(s: str) -> str:
    return s  # snapshots/quotas accept ISO strings directly


def _session_snapshot(captured: str, used: float, resets: str) -> dict:
    return {
        "captured_at": captured,
        "account_type": "max",
        "email": "x@example.com",
        "quotas": [
            {
                "type": "session",
                "model": None,
                "percent_remaining": 100.0 - used,
                "resets_at": resets,
                "time_remaining_seconds": None,
            }
        ],
    }


def _session_rows(db: UsageDatabase):
    cur = db.conn.cursor()
    cur.execute(
        """
        SELECT s.captured_at AS cap, q.percent_remaining AS rem,
               q.resets_at AS reset, q.period_start_at AS pstart
        FROM snapshots s JOIN quotas q ON q.snapshot_id = s.id
        WHERE q.quota_type = 'session'
        ORDER BY s.captured_at ASC
        """
    )
    return {db._parse_dt(r["cap"]): r for r in cur.fetchall()}


def main():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
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

        glitch = at("2026-06-04T05:29:06+00:00")
        reset = at("2026-06-04T05:34:05+00:00")
        after = at("2026-06-04T05:39:05+00:00")

        expected_reset_boundary = datetime(2026, 6, 4, 5, 30, tzinfo=timezone.utc)
        expected_sane_resets = datetime(2026, 6, 4, 5, 30, tzinfo=timezone.utc)

        failures = []

        # (1) glitched resets_at must be sanitized to the carried-forward value
        g_reset = db._parse_dt(glitch["reset"])
        if g_reset != expected_sane_resets:
            failures.append(
                f"glitch resets_at: expected {expected_sane_resets}, got {g_reset}"
            )

        # (2) the genuine reset must set period_start to the real boundary
        r_pstart = db._parse_dt(reset["pstart"])
        if r_pstart != expected_reset_boundary:
            failures.append(
                f"reset period_start_at: expected {expected_reset_boundary}, got {r_pstart}"
            )

        # (3) the period continues with the same (correct) start
        a_pstart = db._parse_dt(after["pstart"])
        if a_pstart != expected_reset_boundary:
            failures.append(
                f"post-reset period_start_at: expected {expected_reset_boundary}, got {a_pstart}"
            )

        if failures:
            print("FAIL")
            for f in failures:
                print("  -", f)
            raise SystemExit(1)
        print("PASS")
    finally:
        db.close()
        os.unlink(path)


if __name__ == "__main__":
    main()
