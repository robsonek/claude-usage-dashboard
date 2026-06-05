"""When the latest snapshot is missing an expected quota (a partial/glitched
PTY render — most often the late-rendering Sonnet bar), get_current() must carry
the previous reading forward tagged stale, instead of leaving the status card
blank. The DB itself must stay truthful (no synthetic rows), so history charts
and predictions are unaffected.

Runs under pytest or standalone: python3 test_status_fallback.py
"""
import os
import tempfile

from database import UsageDatabase

# Resets chosen to be plausible for each quota's period so the insert-time
# glitch sanitizer leaves them alone (session period is 5h).
_WEEKLY_RESET = "2026-06-08T13:00:00+00:00"
_SESSION_RESET = "2026-06-05T16:30:00+00:00"
_SONNET_RESET = "2026-06-08T13:00:00+00:00"


def _snap(captured, *, weekly=None, session=None, sonnet=None):
    quotas = []
    if weekly is not None:
        quotas.append({"type": "weekly", "model": None,
                       "percent_remaining": weekly, "resets_at": _WEEKLY_RESET})
    if session is not None:
        quotas.append({"type": "session", "model": None,
                       "percent_remaining": session, "resets_at": _SESSION_RESET})
    if sonnet is not None:
        quotas.append({"type": "model_specific", "model": "sonnet",
                       "percent_remaining": sonnet, "resets_at": _SONNET_RESET})
    return {"captured_at": captured, "account_type": "max", "quotas": quotas}


def _db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return UsageDatabase(path), path


def test_missing_sonnet_carries_previous_value_tagged_stale():
    db, path = _db()
    try:
        db.insert_snapshot(_snap("2026-06-05T12:00:00+00:00",
                                 weekly=40.0, session=77.0, sonnet=95.0))
        # Latest reading dropped the Sonnet row (the reported glitch).
        db.insert_snapshot(_snap("2026-06-05T12:05:00+00:00",
                                 weekly=40.0, session=77.0))

        limits = db.get_current()["limits"]
        assert "model_specific" in limits, "missing quota was not filled from history"
        m = limits["model_specific"]
        assert m["percent_remaining"] == 95.0, "did not carry the previous Sonnet value"
        assert m["model"] == "sonnet"
        assert m.get("stale") is True, "carried value not tagged stale"
        assert (m.get("stale_since") or "").startswith("2026-06-05T12:00:00"), \
            f"stale_since should point at the source reading, got {m.get('stale_since')}"
        # Fresh quotas are NOT tagged stale.
        assert "stale" not in limits["weekly"]
        assert "stale" not in limits["session"]
    finally:
        db.close()
        os.unlink(path)


def test_empty_latest_snapshot_falls_back_for_all_quotas():
    db, path = _db()
    try:
        db.insert_snapshot(_snap("2026-06-05T12:00:00+00:00",
                                 weekly=40.0, session=77.0, sonnet=95.0))
        # Fully glitched read: zero quotas parsed.
        db.insert_snapshot(_snap("2026-06-05T12:05:00+00:00"))

        limits = db.get_current()["limits"]
        for k in ("weekly", "session", "model_specific"):
            assert k in limits, f"{k} not carried forward on an empty snapshot"
            assert limits[k].get("stale") is True, f"{k} not tagged stale"
    finally:
        db.close()
        os.unlink(path)


def test_fallback_does_not_write_synthetic_rows():
    db, path = _db()
    try:
        db.insert_snapshot(_snap("2026-06-05T12:00:00+00:00",
                                 weekly=40.0, session=77.0, sonnet=95.0))
        db.insert_snapshot(_snap("2026-06-05T12:05:00+00:00",
                                 weekly=40.0, session=77.0))
        db.get_current()  # must be read-only w.r.t. the DB
        n = db.conn.execute(
            "SELECT COUNT(*) FROM quotas WHERE quota_type='model_specific'"
        ).fetchone()[0]
        assert n == 1, f"get_current injected a synthetic quota row (found {n}, expected 1)"
    finally:
        db.close()
        os.unlink(path)


def test_no_fallback_when_no_history():
    db, path = _db()
    try:
        # First-ever reading already missing Sonnet → nothing to carry forward.
        db.insert_snapshot(_snap("2026-06-05T12:00:00+00:00",
                                 weekly=40.0, session=77.0))
        limits = db.get_current()["limits"]
        assert "model_specific" not in limits, \
            "fabricated a value with no prior reading to carry forward"
    finally:
        db.close()
        os.unlink(path)


def test_complete_reading_has_no_stale_tags():
    db, path = _db()
    try:
        db.insert_snapshot(_snap("2026-06-05T12:00:00+00:00",
                                 weekly=40.0, session=77.0, sonnet=95.0))
        db.insert_snapshot(_snap("2026-06-05T12:05:00+00:00",
                                 weekly=41.0, session=78.0, sonnet=96.0))
        limits = db.get_current()["limits"]
        for k in ("weekly", "session", "model_specific"):
            assert "stale" not in limits[k], f"{k} wrongly tagged stale on a complete read"
        assert limits["model_specific"]["percent_remaining"] == 96.0
    finally:
        db.close()
        os.unlink(path)


def test_expired_period_is_not_carried_forward():
    """A value whose resets_at already passed by the current reading's time means
    a reset fired during the outage — carrying the old percentage (and its past
    resets_at) would be actively wrong, so that quota is left blank instead."""
    db, path = _db()
    try:
        db.insert_snapshot({
            "captured_at": "2026-06-01T10:00:00+00:00", "account_type": "max",
            "quotas": [
                # session resets long before the glitch — period has since reset.
                {"type": "session", "model": None, "percent_remaining": 20.0,
                 "resets_at": "2026-06-01T15:00:00+00:00"},
                # weekly/sonnet still in-period at glitch time → may carry.
                {"type": "weekly", "model": None, "percent_remaining": 40.0,
                 "resets_at": "2026-06-07T10:00:00+00:00"},
                {"type": "model_specific", "model": "sonnet", "percent_remaining": 95.0,
                 "resets_at": "2026-06-07T10:00:00+00:00"},
            ],
        })
        # Fully-glitched read ~4 days later (≈19 session resets passed).
        db.insert_snapshot({"captured_at": "2026-06-05T12:05:00+00:00",
                            "account_type": "max", "quotas": []})

        limits = db.get_current()["limits"]
        assert "session" not in limits, \
            "expired session value was carried forward as if current"
        assert limits.get("weekly", {}).get("stale") is True, \
            "in-period weekly should still carry forward"
        assert limits.get("model_specific", {}).get("stale") is True, \
            "in-period sonnet should still carry forward"
    finally:
        db.close()
        os.unlink(path)


def test_same_timestamp_glitch_still_falls_back():
    """Two snapshots sharing the exact captured_at: get_current picks the later
    (higher-id) glitched one; the fallback must still backfill from its
    same-second sibling (anchored on snapshot id, not a strict captured_at <)."""
    db, path = _db()
    try:
        db.insert_snapshot(_snap("2026-06-05T12:00:00+00:00",
                                 weekly=40.0, session=77.0, sonnet=95.0))
        # Same second, but glitched (no Sonnet) and inserted second → higher id.
        db.insert_snapshot(_snap("2026-06-05T12:00:00+00:00",
                                 weekly=40.0, session=77.0))

        m = db.get_current()["limits"].get("model_specific")
        assert m is not None, "same-second sibling was not used as fallback"
        assert m["percent_remaining"] == 95.0
        assert m.get("stale") is True
    finally:
        db.close()
        os.unlink(path)


def test_opus_row_is_not_carried_into_the_sonnet_card():
    """If a prior snapshot holds both Opus and Sonnet model_specific rows, the
    fallback must carry the Sonnet one (the status card is always Sonnet)."""
    db, path = _db()
    try:
        db.insert_snapshot({
            "captured_at": "2026-06-05T12:00:00+00:00", "account_type": "max",
            "quotas": [
                {"type": "weekly", "model": None, "percent_remaining": 40.0,
                 "resets_at": _WEEKLY_RESET},
                {"type": "model_specific", "model": "opus", "percent_remaining": 10.0,
                 "resets_at": _SONNET_RESET},
                {"type": "model_specific", "model": "sonnet", "percent_remaining": 95.0,
                 "resets_at": _SONNET_RESET},
            ],
        })
        db.insert_snapshot(_snap("2026-06-05T12:05:00+00:00",
                                 weekly=40.0, session=77.0))

        m = db.get_current()["limits"]["model_specific"]
        assert m["model"] == "sonnet", f"carried the wrong model: {m['model']}"
        assert m["percent_remaining"] == 95.0
        assert m.get("stale") is True
    finally:
        db.close()
        os.unlink(path)


def test_api_current_passes_stale_keys_through():
    """End-to-end contract: /api/current must surface stale + stale_since so the
    frontend badge has data. Pins the cross-layer pass-through (jsonify)."""
    import importlib
    os.environ["ALLOW_DEFAULT_CREDENTIALS"] = "1"
    import config
    import app as app_module
    importlib.reload(app_module)  # re-run the fail-closed guard with creds allowed

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    orig_db_file = config.DB_FILE
    config.DB_FILE = path
    app_module._db = None

    db = UsageDatabase(path)
    db.insert_snapshot(_snap("2026-06-05T12:00:00+00:00",
                             weekly=40.0, session=77.0, sonnet=95.0))
    db.insert_snapshot(_snap("2026-06-05T12:05:00+00:00", weekly=40.0, session=77.0))
    db.close()

    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    try:
        resp = client.get("/api/current")
        assert resp.status_code == 200, resp.status_code
        limits = resp.get_json()["limits"]
        assert limits["model_specific"]["stale"] is True
        assert limits["model_specific"].get("stale_since")
        assert limits["model_specific"]["percent_remaining"] == 95.0
        assert "stale" not in limits["weekly"]
    finally:
        if app_module._db:
            app_module._db.close()
        config.DB_FILE = orig_db_file
        os.unlink(path)


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
