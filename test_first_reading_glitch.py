"""#4 — a glitched resets_at on the FIRST reading of a quota (fresh DB, or a
per-model row that only appears after first use) has no prior good value to carry
forward. It must still be sanitized — clamped to a plausible bound — instead of
being stored as the raw +24h value (which would then make period_start_at wrong
by a full offset).

Runs under pytest or standalone: python3 test_first_reading_glitch.py
"""
import os
import tempfile
from datetime import datetime, timezone, timedelta

from database import UsageDatabase


def _snap(captured, used, resets):
    return {
        "captured_at": captured,
        "account_type": "max",
        "quotas": [{"type": "session", "model": None,
                    "percent_remaining": 100.0 - used, "resets_at": resets}],
    }


def test_first_reading_glitch_is_clamped():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd); os.unlink(path)
    db = UsageDatabase(path)
    try:
        # Very first session reading IS the +24h glitch (no prior row to carry forward).
        db.insert_snapshot(_snap("2026-06-04T05:29:06+00:00", 3.0, "2026-06-05T05:29:00+00:00"))

        r = db.conn.execute("""SELECT q.resets_at reset, q.period_start_at pstart
                               FROM quotas q WHERE q.quota_type='session'""").fetchone()
        reset = db._parse_dt(r["reset"])
        pstart = db._parse_dt(r["pstart"])
        captured = datetime(2026, 6, 4, 5, 29, 6, tzinfo=timezone.utc)

        assert reset is not None, "resets_at became NULL; this build expects a clamp"
        assert reset - captured <= timedelta(hours=5, minutes=1), \
            f"resets_at not clamped to one period: {reset} (captured {captured})"
        assert reset > captured, f"clamped resets_at should be in the future: {reset}"
        assert pstart == captured, f"period_start_at off: expected {captured}, got {pstart}"
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
