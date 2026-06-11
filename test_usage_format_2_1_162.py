"""Regression test for the 2026-06-04 parser break after Claude CLI 2.1.162.

The new /usage layout positions text with CSI `G` (Cursor Horizontal Absolute)
escapes instead of padding with spaces, e.g.:
    Current\x1b[11Gweek\x1b[16G(all\x1b[21Gmodels)
    Claude\x1b[43GMax
    8%\x1b[57Gused
emulate_terminal() did not implement `G`, so fragments collapsed
("Currentweek(allmodels)", "ClaudeMax"), which:
  - dropped the weekly + sonnet quota rows (only "Current session" survived
    because it is printed as one contiguous literal), and
  - broke account-type detection (account_type=unknown).

This test feeds the real captured PTY bytes through the parser and asserts all
three quotas + Max account type are recovered.

Run: python3 test_usage_format_2_1_162.py
"""
import os
from datetime import datetime, timezone

from usage_fetcher import (
    emulate_terminal,
    trim_to_complete_frame,
    parse_quotas,
    detect_account_type,
)

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       'tests_fixture_usage_2.1.162.bin')


def _load_clean() -> str:
    with open(FIXTURE, 'rb') as f:
        raw = f.read().decode('utf-8', errors='replace')
    return emulate_terminal(trim_to_complete_frame(raw))


def test_three_quotas_parsed():
    clean = _load_clean()
    quotas = parse_quotas(clean)
    by_key = {(q['type'], q.get('model', '')): q for q in quotas}

    assert ('session', '') in by_key, f"session quota missing; got {list(by_key)}"
    assert ('weekly', '') in by_key, f"weekly quota missing; got {list(by_key)}"
    assert ('model_specific', 'sonnet') in by_key, \
        f"sonnet quota missing; got {list(by_key)}"

    # 8% used -> 92% remaining, 41% used -> 59% remaining, 0% used -> 100%
    assert by_key[('session', '')]['percent_remaining'] == 92
    assert by_key[('weekly', '')]['percent_remaining'] == 59
    assert by_key[('model_specific', 'sonnet')]['percent_remaining'] == 100


def test_account_type_max():
    clean = _load_clean()
    assert detect_account_type(clean) == 'max', \
        f"expected 'max', got {detect_account_type(clean)!r}"


def test_weekly_reset_parsed():
    clean = _load_clean()
    # Freeze the clock at the fixture's capture day: "Resets Jun 8" parsed after
    # Jun 8 with the real clock rolls to next year (date has no year in /usage).
    frozen_now = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
    quotas = parse_quotas(clean, now=frozen_now)
    weekly = next(q for q in quotas if q['type'] == 'weekly')
    # "Resets Jun 8 at 1pm (Europe/Warsaw)" -> 1pm Warsaw (UTC+2) == 11:00 UTC
    assert weekly.get('resets_at', '').startswith('2026-06-08T11:00'), \
        f"weekly resets_at wrong: {weekly.get('resets_at')!r}"


if __name__ == '__main__':
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    raise SystemExit(failures)
