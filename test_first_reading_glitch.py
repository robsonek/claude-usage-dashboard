"""#4 — a glitched resets_at on the FIRST reading of a quota (fresh DB, or a
per-model row that only appears after first use) has no prior good value to carry
forward. It must still be sanitized — clamped to a plausible bound — instead of
being stored as the raw +24h value (which would then make period_start_at wrong
by a full offset).

Run: python3 test_first_reading_glitch.py
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


def main():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd); os.unlink(path)
    db = UsageDatabase(path)
    try:
        # Very first session reading IS the +24h glitch (no prior row to carry forward).
        db.insert_snapshot(_snap("2026-06-04T05:29:06+00:00", 3.0, "2026-06-05T05:29:00+00:00"))

        cur = db.conn.cursor()
        r = cur.execute("""SELECT q.resets_at reset, q.period_start_at pstart
                           FROM quotas q WHERE q.quota_type='session'""").fetchone()
        reset = db._parse_dt(r["reset"])
        pstart = db._parse_dt(r["pstart"])
        captured = datetime(2026, 6, 4, 5, 29, 6, tzinfo=timezone.utc)

        failures = []
        if reset is None:
            failures.append("resets_at became NULL — acceptable alt, but this build expects a clamp")
        else:
            if (reset - captured) > timedelta(hours=5, minutes=1):
                failures.append(f"resets_at not clamped to one period: {reset} (captured {captured})")
            if reset <= captured:
                failures.append(f"clamped resets_at should be in the future: {reset}")
            # period_start derived from the clamped value, not from the +24h glitch
            if pstart != captured:
                failures.append(f"period_start_at off: expected {captured}, got {pstart}")

        if failures:
            print("FAIL")
            for f in failures:
                print("  -", f)
            raise SystemExit(1)
        print("PASS", {"resets_at": str(reset), "period_start_at": str(pstart)})
    finally:
        db.close(); os.unlink(path)


if __name__ == "__main__":
    main()
