"""Claude Usage Dashboard - Flask Application"""
import os
from datetime import datetime, timedelta
from functools import wraps

import numpy as np
from flask import Flask, render_template, request, redirect, url_for, jsonify, session
from werkzeug.security import check_password_hash

import config
from database import UsageDatabase

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.permanent_session_lifetime = timedelta(hours=config.SESSION_LIFETIME_HOURS)

# Global database instance
_db = None


def get_db() -> UsageDatabase:
    """Get database instance (lazy initialization)."""
    global _db
    if _db is None:
        _db = UsageDatabase(config.DB_FILE)
    return _db


def login_required(f):
    """Decorator requiring login"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def load_history(hours=None):
    """Load history from SQLite database."""
    db = get_db()
    return db.get_history(hours=hours)


def get_current_usage():
    """Get the most recent usage record from database."""
    db = get_db()
    return db.get_current()


def calculate_prediction(history, limit_type='weekly'):
    """
    Calculate usage prediction based on history.

    Args:
        history: List of history records
        limit_type: 'weekly', 'daily', or 'model_specific'

    Returns:
        dict with prediction or None
    """
    if len(history) < 2:
        return None

    # Filter data from last 24h or since last reset
    now = datetime.now()
    recent_data = []

    for record in history:
        try:
            ts = datetime.fromisoformat(record.get('timestamp', '').replace('Z', '+00:00'))
            # Convert to naive datetime for comparison
            if ts.tzinfo:
                ts = ts.replace(tzinfo=None)
            if (now - ts).total_seconds() < 24 * 3600:
                recent_data.append(record)
        except (ValueError, TypeError):
            continue

    if len(recent_data) < 2:
        recent_data = history[-10:]  # Use last 10 records

    if len(recent_data) < 2:
        return None

    # Find the current resets_at from the most recent record
    current_resets_at = None
    current_reset_ts = None
    for record in reversed(recent_data):
        limits = record.get('limits', {})
        if limit_type == 'weekly':
            current_resets_at = limits.get('weekly', {}).get('resets_at')
        elif limit_type == 'session':
            current_resets_at = limits.get('session', {}).get('resets_at')
        elif limit_type == 'model_specific':
            current_resets_at = limits.get('model_specific', {}).get('resets_at')
        if current_resets_at:
            try:
                current_reset_ts = datetime.fromisoformat(current_resets_at.replace('Z', '+00:00'))
                if current_reset_ts.tzinfo:
                    current_reset_ts = current_reset_ts.replace(tzinfo=None)
            except:
                pass
            break

    def is_same_period(reset_str):
        """Check if resets_at is within 10 minutes of current period"""
        if not reset_str or not current_reset_ts:
            return True  # No filter if we can't compare
        try:
            reset_ts = datetime.fromisoformat(reset_str.replace('Z', '+00:00'))
            if reset_ts.tzinfo:
                reset_ts = reset_ts.replace(tzinfo=None)
            return abs((reset_ts - current_reset_ts).total_seconds()) < 600  # 10 min tolerance
        except:
            return True

    # Prepare data for regression (only use data from current period)
    times = []
    usages = []
    resets_at = current_resets_at

    for record in recent_data:
        try:
            ts = datetime.fromisoformat(record.get('timestamp', '').replace('Z', '+00:00'))
            if ts.tzinfo:
                ts = ts.replace(tzinfo=None)

            # Select appropriate limit
            limits = record.get('limits', {})

            if limit_type == 'weekly':
                weekly = limits.get('weekly', {})
                remaining = weekly.get('percent_remaining')
                record_resets_at = weekly.get('resets_at')
                if remaining is None:
                    continue  # Skip records without data
                if not is_same_period(record_resets_at):
                    continue  # Skip records from previous period
                usages.append(100 - remaining)
                times.append(ts.timestamp())
            elif limit_type == 'session':
                session = limits.get('session', {})
                remaining = session.get('percent_remaining')
                record_resets_at = session.get('resets_at')
                if remaining is None:
                    continue  # Skip records without data
                if not is_same_period(record_resets_at):
                    continue  # Skip records from previous period
                usages.append(100 - remaining)
                times.append(ts.timestamp())
            elif limit_type == 'model_specific':
                model = limits.get('model_specific', {})
                remaining = model.get('percent_remaining')
                record_resets_at = model.get('resets_at')
                if remaining is None:
                    continue  # Skip records without data
                if not is_same_period(record_resets_at):
                    continue  # Skip records from previous period
                usages.append(100 - remaining)
                times.append(ts.timestamp())
        except (ValueError, TypeError, KeyError):
            continue

    if len(times) < 2:
        return None

    # Linear regression
    times = np.array(times)
    usages = np.array(usages)

    # Normalize times (from 0)
    t0 = times[0]
    times_norm = times - t0

    # Calculate regression coefficients: usage = a * time + b
    n = len(times_norm)
    sum_t = np.sum(times_norm)
    sum_u = np.sum(usages)
    sum_tu = np.sum(times_norm * usages)
    sum_t2 = np.sum(times_norm ** 2)

    denominator = n * sum_t2 - sum_t ** 2
    if abs(denominator) < 1e-10:
        return None

    a = (n * sum_tu - sum_t * sum_u) / denominator  # trend (% per second)
    b = (sum_u - a * sum_t) / n

    # Current usage
    current_usage = usages[-1]
    current_time = times[-1]

    # Time to reset
    if resets_at:
        try:
            reset_dt = datetime.fromisoformat(resets_at.replace('Z', '+00:00'))
            if reset_dt.tzinfo:
                reset_dt = reset_dt.replace(tzinfo=None)
            time_to_reset = (reset_dt - now).total_seconds()
        except ValueError:
            time_to_reset = 7 * 24 * 3600  # Default one week
    else:
        time_to_reset = 7 * 24 * 3600

    # Check data time span (need at least 5 min for prediction)
    time_span_hours = float(times[-1] - times[0]) / 3600
    low_confidence = bool(time_span_hours < 0.083)  # ~5 minut

    # Trend per hour
    trend_per_hour = a * 3600

    # If time_to_reset <= 0, data is stale (reset happened)
    stale_data = bool(time_to_reset <= 0)  # Tylko gdy reset minął
    if stale_data:
        low_confidence = True

    # Predicted usage at reset time
    predicted_usage = current_usage + a * max(time_to_reset, 0)

    # Time to 100% (if trend > 0)
    if a > 0:
        time_to_100 = (100 - current_usage) / a
        hours_to_100 = time_to_100 / 3600
    else:
        hours_to_100 = None

    # If low confidence, don't claim it will exceed
    will_exceed = bool(predicted_usage > 100) and not low_confidence

    return {
        'current_usage': float(round(current_usage, 2)),
        'predicted_at_reset': float(round(min(predicted_usage, 100), 2)) if not low_confidence else None,
        'will_exceed': will_exceed,
        'trend_per_hour': float(round(trend_per_hour, 2)) if not low_confidence else None,
        'hours_to_100': float(round(hours_to_100, 3)) if hours_to_100 and not low_confidence else None,
        'resets_at': resets_at,
        'time_to_reset_hours': float(round(max(time_to_reset, 0) / 3600, 3)),
        'data_points': int(len(times)),
        'time_span_hours': float(round(time_span_hours, 2)),
        'low_confidence': low_confidence,
        'stale_data': stale_data
    }


# ============ ROUTES ============

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page"""
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        if username == config.USERNAME and check_password_hash(config.PASSWORD_HASH, password):
            session.permanent = True
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
        error = 'Invalid username or password'
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    """Logout"""
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@login_required
def dashboard():
    """Main dashboard"""
    return render_template('dashboard.html')


@app.route('/api/current')
@login_required
def api_current():
    """Returns current usage data"""
    data = get_current_usage()
    if data:
        return jsonify(data)
    return jsonify({'error': 'Unable to fetch data'}), 500


@app.route('/api/history')
@login_required
def api_history():
    """Returns historical data"""
    hours = request.args.get('hours', type=int)
    history = load_history(hours=hours)
    return jsonify(history)


@app.route('/api/prediction')
@login_required
def api_prediction():
    """Returns predictions for all limits"""
    history = load_history()

    predictions = {
        'weekly': calculate_prediction(history, 'weekly'),
        'session': calculate_prediction(history, 'session'),
        'model_specific': calculate_prediction(history, 'model_specific')
    }

    return jsonify(predictions)


if __name__ == '__main__':
    # Ensure data directory exists
    os.makedirs(config.DATA_DIR, exist_ok=True)

    # Run development server
    app.run(debug=True, host='127.0.0.1', port=5000)
