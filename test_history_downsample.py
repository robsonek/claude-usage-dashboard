"""get_history downsampling for long ranges.

A 60-day ("2m") range is ~17k snapshots / ~49k quota rows / ~9 MB JSON — slow to
build, transfer and render. get_history(max_points=N) caps the returned snapshots
by an even stride so charts stay snappy; the charts plot on a time axis so strided
samples look the same at that zoom. max_points=None (default) keeps the OLD
behavior untouched — prediction and other callers must get full resolution.

Runs under pytest or standalone: python3 test_history_downsample.py
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

from database import UsageDatabase


def _db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return UsageDatabase(path), path


def _seed(db, n, *, end=None, step_min=1):
    """Insert n snapshots, step_min apart, ending at `end` (default now).

    `end` must stay relative to the wall clock: get_history(hours=N) filters
    against now(), so a pinned calendar date turns these into time-bomb tests
    that silently return 0 rows once the date ages out of the window.
    """
    end = end or datetime.now(timezone.utc)
    far_reset = (end + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(n):
        ts = (end - timedelta(minutes=(n - 1 - i) * step_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
        db.insert_snapshot({
            "captured_at": ts, "account_type": "max",
            "quotas": [{"type": "weekly", "model": None,
                        "percent_remaining": i % 100, "resets_at": far_reset}],
        })


def test_under_cap_returns_all():
    db, path = _db()
    try:
        _seed(db, 50)
        assert len(db.get_history(hours=1000, max_points=2000)) == 50
    finally:
        db.close()
        os.unlink(path)


def test_default_is_full_resolution():
    # max_points omitted -> None -> NO downsampling (prediction relies on this).
    db, path = _db()
    try:
        _seed(db, 500)
        assert len(db.get_history(hours=1000)) == 500
    finally:
        db.close()
        os.unlink(path)


def test_over_cap_is_capped():
    db, path = _db()
    try:
        _seed(db, 500)
        ds = db.get_history(hours=1000, max_points=100)
        assert len(ds) <= 100
        assert len(ds) >= 90          # near the cap, not collapsed to a handful
    finally:
        db.close()
        os.unlink(path)


def test_keeps_most_recent_point():
    db, path = _db()
    try:
        _seed(db, 500)
        full = db.get_history(hours=1000)
        ds = db.get_history(hours=1000, max_points=100)
        assert ds[-1]["timestamp"] == full[-1]["timestamp"]   # latest never dropped
        assert ds[0]["timestamp"] == full[0]["timestamp"]     # oldest kept (stride starts at 0)
    finally:
        db.close()
        os.unlink(path)


def test_downsampled_rows_keep_order_and_quotas():
    db, path = _db()
    try:
        _seed(db, 400)
        ds = db.get_history(hours=1000, max_points=50)
        assert 40 <= len(ds) <= 50    # non-empty floor: an empty list passed vacuously
        ts = [r["timestamp"] for r in ds]
        assert ts == sorted(ts)                                # chronological
        assert all("weekly" in r["limits"] for r in ds)        # quota data intact
    finally:
        db.close()
        os.unlink(path)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:
                failures += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    raise SystemExit(failures)
