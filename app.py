"""Claude Usage Dashboard - Flask Application"""
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import wraps

import numpy as np
from flask import Flask, render_template, request, redirect, url_for, jsonify, session
from werkzeug.security import check_password_hash

import config
import oauth_flow
from database import UsageDatabase

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.permanent_session_lifetime = timedelta(hours=config.SESSION_LIFETIME_HOURS)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    # Set SESSION_COOKIE_SECURE=1 in production (HTTPS); off by default so local
    # HTTP development still receives the session cookie.
    SESSION_COOKIE_SECURE=os.environ.get('SESSION_COOKIE_SECURE', '0') == '1',
)

# Fail closed: refuse to serve with the built-in default secret/password unless
# explicitly allowed for local dev. This guard lives in the web app only — the
# collector (which imports config for paths) is unaffected.
if (config.SECRET_KEY_IS_DEFAULT or config.PASSWORD_IS_DEFAULT) and \
        os.environ.get('ALLOW_DEFAULT_CREDENTIALS') != '1':
    raise RuntimeError(
        "Refusing to start with default FLASK_SECRET_KEY / DASHBOARD_PASSWORD. "
        "Set both environment variables (or ALLOW_DEFAULT_CREDENTIALS=1 for local dev).")

# Best-effort in-process login throttle (per client IP). With multiple gunicorn
# workers this is per-worker, so the effective limit is N_workers × _LOGIN_MAX_FAILS
# and it resets on restart — a basic brake on brute force, not a replacement for a
# shared store / fail2ban at the edge.
_LOGIN_MAX_FAILS = 5
_LOGIN_WINDOW_SECONDS = 300
_login_fails = defaultdict(list)


def _client_ip() -> str:
    # The throttle keys on this value, so it must come from a header the edge
    # controls. Behind Cloudflare, CF-Connecting-IP is set by the edge and
    # overwrites any client-supplied value. X-Forwarded-For's first element is
    # attacker-controlled (the edge APPENDS the real IP rather than replacing the
    # chain), so trusting it would let a brute-forcer rotate the bucket every
    # request — and balloon _login_fails with one entry per forged IP. Fall back
    # to the direct peer, never to XFF.
    cf = request.headers.get('CF-Connecting-IP', '').strip()
    if cf:
        return cf
    return request.remote_addr or 'unknown'


def _login_blocked(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _login_fails[ip] if now - t < _LOGIN_WINDOW_SECONDS]
    _login_fails[ip] = recent
    return len(recent) >= _LOGIN_MAX_FAILS

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


@app.after_request
def _security_headers(resp):
    """Baseline hardening headers on every response. No strict CSP yet — the
    dashboard relies on inline <script>, so a real policy needs nonces (deferred,
    see audit point 5). X-Frame-Options already blocks clickjacking."""
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('Referrer-Policy', 'no-referrer')
    # TLS is terminated at Cloudflare, which sets X-Forwarded-Proto; only emit
    # HSTS over real HTTPS so it can't pin localhost during plain-HTTP dev.
    if request.headers.get('X-Forwarded-Proto') == 'https' or request.is_secure:
        resp.headers.setdefault('Strict-Transport-Security',
                                'max-age=31536000; includeSubDomains')
    return resp


# Cap chart history at ~this many points. Long ranges (2m ≈ 17k snapshots / 9 MB
# JSON) are downsampled to keep the payload/render fast; prediction is NOT capped.
HISTORY_CHART_MAX_POINTS = 2000

# /api/accounts recomputes both predictions per account on every (heartbeat-driven)
# call. calculate_prediction only looks at the last 24h, so fetching the full
# default 168h per account was wasted work — bound it to just over the window.
ACCOUNT_BAR_PREDICTION_HOURS = 25


def load_history(hours=None, max_points=None):
    """Load history from SQLite database."""
    db = get_db()
    return db.get_history(hours=hours, max_points=max_points)


def get_current_usage():
    """Get the most recent usage record from database."""
    db = get_db()
    return db.get_current()


def calculate_prediction(history, limit_type='weekly'):
    """
    Calculate usage prediction based on history.

    Args:
        history: List of history records
        limit_type: 'weekly', 'session', or 'model_specific'

    Returns:
        dict with prediction or None
    """
    if len(history) < 2:
        return None

    # Filter data from last 24h or since last reset
    now = datetime.now(timezone.utc)
    recent_data = []

    for record in history:
        try:
            ts = datetime.fromisoformat(record.get('timestamp', '').replace('Z', '+00:00'))
            if not ts.tzinfo:
                ts = ts.replace(tzinfo=timezone.utc)
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
                if not current_reset_ts.tzinfo:
                    current_reset_ts = current_reset_ts.replace(tzinfo=timezone.utc)
            except:
                pass
            break

    def is_same_period(reset_str):
        """Check if resets_at is within 10 minutes of current period"""
        if not reset_str or not current_reset_ts:
            return True  # No filter if we can't compare
        try:
            reset_ts = datetime.fromisoformat(reset_str.replace('Z', '+00:00'))
            if not reset_ts.tzinfo:
                reset_ts = reset_ts.replace(tzinfo=timezone.utc)
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
            if not ts.tzinfo:
                ts = ts.replace(tzinfo=timezone.utc)

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
                session_limit = limits.get('session', {})
                remaining = session_limit.get('percent_remaining')
                record_resets_at = session_limit.get('resets_at')
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
            if not reset_dt.tzinfo:
                reset_dt = reset_dt.replace(tzinfo=timezone.utc)
            time_to_reset = (reset_dt - now).total_seconds()
        except ValueError:
            time_to_reset = 7 * 24 * 3600  # Default one week
    else:
        time_to_reset = 7 * 24 * 3600

    # Check data time span (need at least 5 min for prediction)
    time_span_hours = float(times[-1] - times[0]) / 3600
    low_confidence = bool(time_span_hours < 0.083)  # ~5 minutes

    # Trend per hour
    trend_per_hour = a * 3600

    # If time_to_reset <= 0, data is stale (reset happened)
    stale_data = bool(time_to_reset <= 0)  # Only when the reset has passed
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


def _resolve_account_id():
    """account query param, or the default (first active) account, or None (legacy)."""
    acc = request.args.get('account', type=int)
    if acc is not None:
        return acc
    return get_db().get_default_account_id()


# ============ ROUTES ============

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page"""
    error = None
    if request.method == 'POST':
        ip = _client_ip()
        if _login_blocked(ip):
            return render_template('login.html',
                                   error='Too many attempts. Try again later.'), 429
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        if username == config.USERNAME and check_password_hash(config.PASSWORD_HASH, password):
            _login_fails.pop(ip, None)
            session.permanent = True
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
        _login_fails[ip].append(time.time())
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
    return render_template('dashboard.html', version=config.VERSION)


@app.route('/accounts')
@login_required
def accounts_page():
    """Account management UI."""
    return render_template('accounts.html', version=config.VERSION,
                           accounts=get_db().list_accounts())


@app.route('/api/accounts')
@login_required
def api_accounts():
    """Account list + each account's latest snapshot summary (for the bar)."""
    db = get_db()
    out = []
    for acc in db.list_accounts():
        weekly = session_pct = None
        weekly_exceed = session_exceed = False
        if acc['is_active']:
            current = db.get_current(account_id=acc['id'])
            if current:
                weekly = (current['limits'].get('weekly') or {}).get('percent_remaining')
                session_pct = (current['limits'].get('session') or {}).get('percent_remaining')
            # Same prediction the dashboard uses, so the mini-card color can match
            # the big STATUS card (a forecast overage escalates to amber even at low %).
            history = db.get_history(account_id=acc['id'],
                                     hours=ACCOUNT_BAR_PREDICTION_HOURS)
            weekly_exceed = bool((calculate_prediction(history, 'weekly') or {}).get('will_exceed'))
            session_exceed = bool((calculate_prediction(history, 'session') or {}).get('will_exceed'))
        out.append({**acc, 'weekly_remaining': weekly, 'session_remaining': session_pct,
                    'weekly_will_exceed': weekly_exceed, 'session_will_exceed': session_exceed})
    return jsonify(out)


@app.route('/accounts/add', methods=['POST'])
@login_required
def accounts_add():
    """Step 1 (action=start): return authorize URL, stash PKCE+state in session.
       Step 2 (action=complete): validate state, exchange code, fetch profile, save."""
    action = request.form.get('action')
    if action == 'start':
        # Fail before the user authorizes: without the key we couldn't store the
        # tokens, and the one-time OAuth code would already be burned by then.
        if not config.TOKEN_ENCRYPTION_KEY:
            return jsonify({'error': 'TOKEN_ENCRYPTION_KEY is not set — tokens cannot be stored securely.'}), 400
        pkce = oauth_flow.generate_pkce()
        state = oauth_flow.generate_state()
        session['oauth_verifier'] = pkce['verifier']
        session['oauth_state'] = state
        return jsonify({'authorize_url': oauth_flow.build_authorize_url(
            pkce['challenge'], state)})
    if action == 'complete':
        verifier = session.get('oauth_verifier')
        expected_state = session.get('oauth_state')
        if not verifier or not expected_state:
            return jsonify({'error': 'No OAuth session in progress — click "Add account" again.'}), 400
        code = request.form.get('code', '').strip()
        if not code:
            return jsonify({'error': 'Paste the authorization code.'}), 400
        # The pasted value must be code#state and the state MUST match what we
        # issued — always, not only when a '#' happens to be present. A bare code
        # (no state) is rejected so a code from a different/forged authorization
        # can't be completed against this session.
        got_state = code.split('#', 1)[1] if '#' in code else ''
        if got_state != expected_state:
            return jsonify({'error': 'Mismatched or missing state — start the authorization again.'}), 400
        try:
            tokens = oauth_flow.exchange_code(code, verifier)
            profile = oauth_flow.fetch_profile(tokens['access_token'])
        except oauth_flow.OAuthError as e:
            return jsonify({'error': str(e)}), 400
        # Without an email we can't upsert-by-email, so repeated re-auth would mint
        # a fresh duplicate account every time (and the account couldn't be
        # backfilled). Refuse rather than silently proliferate rows.
        if not profile.get('email'):
            return jsonify({'error': "Could not read the account's e-mail address — try authorizing again."}), 400
        db = get_db()
        label = request.form.get('label') or profile['email'] or 'Account'
        acc_id = db.add_or_update_account(
            label=label, email=profile['email'], account_type=profile['account_type'],
            access_token=tokens['access_token'], refresh_token=tokens['refresh_token'],
            expires_at=tokens['expires_at'])
        backfilled = db.backfill_account_by_email(acc_id, profile['email']) \
            if profile['email'] else 0
        session.pop('oauth_verifier', None)
        session.pop('oauth_state', None)
        return jsonify({'id': acc_id, 'email': profile['email'],
                        'account_type': profile['account_type'], 'backfilled': backfilled})
    return jsonify({'error': 'unknown action'}), 400


@app.route('/accounts/<int:account_id>/<string:op>', methods=['POST'])
@login_required
def accounts_op(account_id, op):
    """enable / disable / delete / rename an account."""
    db = get_db()
    if op == 'enable':
        db.set_account_active(account_id, True)
    elif op == 'disable':
        db.set_account_active(account_id, False)
    elif op == 'delete':
        db.delete_account(account_id)
    elif op == 'rename':
        label = request.form.get('label', '').strip()
        if not label:
            return jsonify({'error': 'Empty label.'}), 400
        db.rename_account(account_id, label)
    else:
        return jsonify({'error': 'unknown op'}), 400
    return jsonify({'ok': True})


@app.route('/api/current')
@login_required
def api_current():
    """Returns current usage data"""
    data = get_db().get_current(account_id=_resolve_account_id())
    if data:
        return jsonify(data)
    return jsonify({'error': 'Unable to fetch data'}), 500


@app.route('/api/history')
@login_required
def api_history():
    """Returns historical data"""
    hours = request.args.get('hours', type=int)
    if hours is not None:
        hours = max(1, min(hours, 24 * 90))  # clamp to [1h, 90d]
    history = get_db().get_history(hours=hours, max_points=HISTORY_CHART_MAX_POINTS,
                                   account_id=_resolve_account_id())
    return jsonify(history)


@app.route('/api/prediction')
@login_required
def api_prediction():
    """Returns predictions for all limits"""
    history = get_db().get_history(account_id=_resolve_account_id())

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
    app.run(debug=os.environ.get('FLASK_DEBUG') == '1', host='127.0.0.1', port=5000)
