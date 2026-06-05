"""Read-loop exit decision for usage_fetcher.

The /usage fetch used to take ~17.5s because the loop only exited early on finding
the synchronized-output end (\x1b[?2026l) after the 3rd quota marker. Some Claude
CLI builds don't re-emit that escape after the late-rendering quota bars, so the
loop fell through and sat out the full IDLE_USAGE_TIMEOUT (15s) even though all
quotas were already present. _should_stop_reading() decides when to stop based on
completeness + settle, so a complete-but-no-sync-end render stops after a short
idle instead of 15s.

Run: python3 test_should_stop_reading.py
"""
from usage_fetcher import _should_stop_reading, IDLE_TIMEOUT, IDLE_USAGE_TIMEOUT


def test_complete_with_sync_end_stops_immediately():
    # Fully rendered frame (all markers + sync-end) — stop at once, no wait.
    assert _should_stop_reading(True, True, 0.0, True) is True


def test_complete_no_sync_end_stops_once_settled():
    # All quotas present but the sync-end never matched: keep going until the
    # render settles (idle >= IDLE_TIMEOUT), then stop — DON'T wait IDLE_USAGE_TIMEOUT.
    assert _should_stop_reading(True, False, 0.0, True) is False
    assert _should_stop_reading(True, False, 1.0, True) is False          # not settled yet
    assert _should_stop_reading(True, False, IDLE_TIMEOUT + 0.1, True) is True   # the fix


def test_incomplete_on_usage_screen_waits_for_lagging_bars():
    # Quota bars render last and can lag — keep waiting up to IDLE_USAGE_TIMEOUT.
    assert _should_stop_reading(False, False, 5.0, True) is False
    assert _should_stop_reading(False, False, IDLE_USAGE_TIMEOUT + 0.1, True) is True


def test_incomplete_off_usage_screen_bails_fast():
    # Auth error / setup / unknown layout: Claude HAS drawn a screen (ui_rendered)
    # that simply isn't the usage view — no quotas coming, bail after IDLE_TIMEOUT.
    assert _should_stop_reading(False, False, IDLE_TIMEOUT + 0.1, False, ui_rendered=True) is True
    assert _should_stop_reading(False, False, 1.0, False, ui_rendered=True) is False   # under IDLE_TIMEOUT


def test_no_ui_yet_does_not_bail_at_idle_timeout():
    # The empty-capture bug: Claude emitted only its terminal-init escape burst
    # (ui_rendered=False) and went quiet waiting on a terminal-capability reply we
    # never send. The old code bailed at IDLE_TIMEOUT with an empty buffer because
    # "not on_usage_screen" was true. Now "no UI drawn yet" counts as "still
    # starting" — keep waiting instead of killing Claude before it renders.
    assert _should_stop_reading(False, False, IDLE_TIMEOUT + 0.1, False, ui_rendered=False) is False
    assert _should_stop_reading(False, False, 5.0, False, ui_rendered=False) is False


def test_no_ui_eventually_bails_at_usage_timeout():
    # Genuinely hung start (Claude never draws anything): give up at
    # IDLE_USAGE_TIMEOUT so a stuck child can't pin the loop to the overall timeout.
    assert _should_stop_reading(False, False, IDLE_USAGE_TIMEOUT + 0.1, False, ui_rendered=False) is True


def test_no_data_yet_never_stops():
    # idle below IDLE_TIMEOUT and nothing complete → keep reading.
    assert _should_stop_reading(False, False, 0.0, True) is False


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
