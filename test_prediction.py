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
