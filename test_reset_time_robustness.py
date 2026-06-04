"""Robustness tests for usage_fetcher.parse_reset_time / parse_quotas.

Covers two parser hardening fixes:
  (#3) The +24h date-rollover glitch SOURCE: a bare session reset time that is
       barely in the past (boundary firing, e.g. now=05:30, line="5:29am") must
       NOT be bumped a full day — a 5h session reset is never ~24h away. A genuine
       next-day reset (gap >= one period) still rolls to tomorrow.
  (#5) A garbled PTY time/date ("25:99pm", "Feb 31") must yield reset_time=None
       instead of raising and discarding the whole snapshot, and one malformed
       quota section must not nuke the others.

Run: python3 test_reset_time_robustness.py
"""
from datetime import datetime, timezone

from usage_fetcher import parse_reset_time, parse_quotas

UTC = timezone.utc


def test_session_just_passed_not_bumped_to_tomorrow():
    now = datetime(2026, 6, 4, 5, 30, tzinfo=UTC)
    lines = ['8% used', 'Resets 5:29am (UTC)']
    _, reset_time, _ = parse_reset_time(lines, 0, quota_type='session', now=now)
    assert reset_time is not None, "reset dropped entirely"
    gap_h = abs((reset_time - now).total_seconds()) / 3600.0
    assert gap_h < 1.0, f"session reset bumped ~{gap_h:.1f}h into the future: {reset_time}"
    assert (reset_time.day, reset_time.hour, reset_time.minute) == (4, 5, 29), reset_time


def test_session_genuine_tomorrow_still_bumped():
    # Late evening: a session reset at 01:30 is genuinely tomorrow (~1.5h away),
    # gap from today-01:30 is >> one period, so it must roll to the next day.
    now = datetime(2026, 6, 4, 23, 55, tzinfo=UTC)
    lines = ['8% used', 'Resets 1:30am (UTC)']
    _, reset_time, _ = parse_reset_time(lines, 0, quota_type='session', now=now)
    assert reset_time is not None
    assert (reset_time.day, reset_time.hour, reset_time.minute) == (5, 1, 30), reset_time


def test_non_session_past_time_still_bumped():
    # Quotas without a known short period keep the original "tomorrow" behavior.
    now = datetime(2026, 6, 4, 5, 30, tzinfo=UTC)
    lines = ['41% used', 'Resets 5:29am (UTC)']
    _, reset_time, _ = parse_reset_time(lines, 0, quota_type='weekly', now=now)
    assert reset_time is not None
    assert reset_time.day == 5, reset_time


def test_garbled_time_returns_none_no_crash():
    now = datetime(2026, 6, 4, 5, 30, tzinfo=UTC)
    lines = ['8% used', 'Resets 25:99pm (UTC)']
    _, reset_time, _ = parse_reset_time(lines, 0, quota_type='session', now=now)
    assert reset_time is None, f"out-of-range time should be discarded, got {reset_time}"


def test_garbled_date_returns_none_no_crash():
    now = datetime(2026, 6, 4, 5, 30, tzinfo=UTC)
    lines = ['0% used', 'Resets Feb 31 at 3pm (UTC)']
    _, reset_time, _ = parse_reset_time(lines, 0, quota_type='weekly', now=now)
    assert reset_time is None, f"impossible date should be discarded, got {reset_time}"


def test_parse_quotas_isolates_malformed_section():
    text = ("Current session\n8% used\nResets 25:99pm (UTC)\n\n"
            "Current week (all models)\n41% used\nResets Jun 8 at 1pm (UTC)\n")
    quotas = parse_quotas(text)
    by = {q['type']: q for q in quotas}
    assert 'session' in by, f"good session quota lost to a sibling's bad time: {by}"
    assert by['session']['percent_remaining'] == 92
    assert 'weekly' in by, f"weekly quota lost: {by}"
    assert by['weekly'].get('resets_at'), "weekly reset should still parse"


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
