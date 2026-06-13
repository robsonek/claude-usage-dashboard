"""Shared per-account token policy: proactive refresh, persist, retry-once-on-401.

Used by collect_all (polling) and primer (manual 5h-window start) so the
refresh-on-401 dance lives in one place. HTTP stays in api_usage_fetcher; this
layer wraps it with DB persistence + the retry policy.
"""
import api_usage_fetcher as auf


def refresh_and_persist(db, account):
    """Refresh this account's token, persist it, and update the in-memory dict.

    Updates account['access_token'] too (not just refresh_token/expires_at) so a
    SECOND call in the same request uses the fresh token, not the stale original.
    Returns the new access token.
    """
    new = auf.refresh_access_token(account['refresh_token'])
    db.update_account_tokens(account['id'], new['access_token'],
                             new['refresh_token'], new['expires_at'])
    account['access_token'] = new['access_token']
    account['refresh_token'] = new['refresh_token']
    account['expires_at'] = new['expires_at']
    return new['access_token']


def call_with_401_retry(db, account, call):
    """Run call(access_token); refresh+retry once on near-expiry or a 401.

    `call` takes an access token and returns the parsed response, or raises
    auf.UsageApiError(status=401) when the token is dead. Proactive refresh fires
    first when <2 min of token life remain (auf.needs_refresh_ms).
    """
    if auf.needs_refresh_ms(account['expires_at']):
        access_token = refresh_and_persist(db, account)
    else:
        access_token = account['access_token']
    try:
        return call(access_token)
    except auf.UsageApiError as e:
        if e.status != 401:
            raise
        access_token = refresh_and_persist(db, account)
        return call(access_token)
