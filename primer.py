"""Manual 5h-window primer: send a minimal 'Hi' to Haiku to start the session window.

Trigger-agnostic — the web button calls prime_account today; a future cron can call
the exact same function. Guards against priming an already-active window so it never
wastes quota or pings the API for nothing.
"""
import account_session
import api_usage_fetcher as auf


def _session_status(api):
    """Return {'active': bool, 'resets_at': str|None} for the 5h (session) window.

    Reuses map_usage_response: time_remaining_seconds is max(0, resets_at - now), so
    > 0 means a window is currently running.
    """
    for q in auf.map_usage_response(api):
        if q['type'] == 'session':
            secs = q.get('time_remaining_seconds')
            return {'active': bool(secs and secs > 0), 'resets_at': q.get('resets_at')}
    return {'active': False, 'resets_at': None}


def prime_account(db, account):
    """Start the 5h window for one account unless it is already active.

    account: a decrypted dict from db.get_account_for_primer (id, access_token,
    refresh_token, expires_at, email, account_type). Returns
    {'started': bool, 'resets_at': str|None}. Raises auf.UsageApiError on an API
    failure (the route maps it to HTTP 502).
    """
    api = account_session.call_with_401_retry(db, account, auf._http_get_usage)
    status = _session_status(api)
    if status['active']:
        return {'started': False, 'resets_at': status['resets_at']}

    account_session.call_with_401_retry(db, account, auf.send_haiku_primer)

    api2 = account_session.call_with_401_retry(db, account, auf._http_get_usage)
    db.insert_snapshot(auf.build_snapshot(api2, account))
    db.record_account_poll(account['id'], error=None)
    return {'started': True, 'resets_at': _session_status(api2)['resets_at']}
