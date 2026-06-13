"""Session prediction regression must ignore idle records (resets_at=NULL).

When no session window is active the usage API reports the session quota as
0% used with resets_at=null. Those idle records do not belong to the current
5h period, yet is_same_period() used to let them through ("no filter if we
can't compare"), flattening the regression slope ~10x and silencing the
"Will exceed limit" warning exactly when it should fire.
"""
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault('ALLOW_DEFAULT_CREDENTIALS', '1')

import app  # noqa: E402


def _iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%S+00:00')


def _record(ts, used_pct, resets_at):
    return {
        'timestamp': _iso(ts),
        'limits': {
            'session': {
                'percent_remaining': 100 - used_pct,
                'resets_at': _iso(resets_at) if resets_at else None,
            },
        },
    }


def _history_with_idle_gap(now):
    """~17h of idle zeros (resets_at=None) + a 2.5h active climb 0->60%."""
    history = []
    # Idle stretch: 0% used, no active session window.
    t = now - timedelta(hours=20)
    while t < now - timedelta(hours=3):
        history.append(_record(t, 0.0, None))
        t += timedelta(minutes=10)
    # Current period: started 2.5h ago, resets in +2.5h, climbing 24%/h.
    reset_at = now + timedelta(hours=2.5)
    t = now - timedelta(hours=2.5)
    while t <= now:
        hours_in = (t - (now - timedelta(hours=2.5))).total_seconds() / 3600
        history.append(_record(t, 24.0 * hours_in, reset_at))
        t += timedelta(minutes=5)
    return history


def test_session_trend_ignores_idle_null_reset_records():
    now = datetime.now(timezone.utc)
    pred = app.calculate_prediction(_history_with_idle_gap(now), 'session')
    assert pred is not None
    # Real slope of the current period is 24%/h; with idle zeros polluting
    # the regression it collapses to ~2%/h.
    assert pred['trend_per_hour'] > 15, pred['trend_per_hour']


def test_session_will_exceed_fires_at_current_pace():
    now = datetime.now(timezone.utc)
    pred = app.calculate_prediction(_history_with_idle_gap(now), 'session')
    # 60% used + 24%/h * 2.5h to reset = 120% -> must warn.
    assert pred['will_exceed'] is True


def test_null_resets_still_used_when_no_current_period_known():
    """Without any resets_at to anchor on, records can't be filtered out."""
    now = datetime.now(timezone.utc)
    history = [
        _record(now - timedelta(minutes=30), 10.0, None),
        _record(now - timedelta(minutes=20), 20.0, None),
        _record(now - timedelta(minutes=10), 30.0, None),
    ]
    pred = app.calculate_prediction(history, 'session')
    assert pred is not None
    assert pred['data_points'] == 3


def _weekly_record(ts, used_pct, resets_at):
    return {
        'timestamp': _iso(ts),
        'limits': {
            'weekly': {
                'percent_remaining': 100 - used_pct,
                'resets_at': _iso(resets_at) if resets_at else None,
            },
        },
    }


def _history_with_midperiod_gift(now):
    """Real prod shape (2026-06-13): a mid-period "gift" zeroes weekly usage but
    keeps the SAME resets_at. Pre-gift points sit at ~86%, a NULL-reset gap, then
    post-gift points flat at ~7% — all sharing one resets_at ~37h out. The 86%->7%
    step across the same period is what makes the naive regression slope steeply
    negative and extrapolate to a nonsense negative "at reset"."""
    reset_at = now + timedelta(hours=37)
    history = []
    # Pre-gift tail still inside the 24h window: flat 86%.
    t = now - timedelta(hours=23)
    while t < now - timedelta(hours=22):
        history.append(_weekly_record(t, 86.0, reset_at))
        t += timedelta(minutes=5)
    # The gift itself: usage zeroed, no active window reported.
    while t < now - timedelta(hours=20):
        history.append(_weekly_record(t, 0.0, None))
        t += timedelta(minutes=5)
    # Post-gift: same resets_at, usage resumed and flat at 7%.
    while t <= now:
        history.append(_weekly_record(t, 7.0, reset_at))
        t += timedelta(minutes=5)
    return history


def test_midperiod_gift_does_not_predict_negative_usage():
    """The 86%->7% drop must not extrapolate below 0 ("At reset: -142%")."""
    now = datetime.now(timezone.utc)
    pred = app.calculate_prediction(_history_with_midperiod_gift(now), 'weekly')
    assert pred is not None
    assert pred['predicted_at_reset'] >= 0, pred['predicted_at_reset']


def test_midperiod_gift_trend_reflects_post_gift_period():
    """Regression must drop everything up to the last reset/refund step, so the
    trend reflects the flat post-gift period (~0%/h), not the bogus -3.9%/h."""
    now = datetime.now(timezone.utc)
    pred = app.calculate_prediction(_history_with_midperiod_gift(now), 'weekly')
    assert pred is not None
    assert abs(pred['trend_per_hour']) < 1.0, pred['trend_per_hour']
    assert pred['will_exceed'] is False
    # Current usage is read from the live (post-gift) tail, not the 86% pre-gift.
    assert pred['current_usage'] == 7.0, pred['current_usage']
