"""SQLite Database Layer for Claude Usage Dashboard"""
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List
import os


def _adapt_datetime_utc(dt: datetime) -> str:
    """Serialize datetimes to a canonical UTC string for SQLite.

    The implicit default datetime adapter is deprecated since Python 3.12 (slated
    for removal); register an explicit one. Output matches the existing on-disk
    format ('YYYY-MM-DD HH:MM:SS[.ffffff]+00:00'), and naive values are assumed
    UTC and normalized — so mixed naive/aware rows can no longer make get_history's
    lexicographic range filter compare inconsistently. No data migration needed.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(sep=' ')


sqlite3.register_adapter(datetime, _adapt_datetime_utc)


# Default period length per quota type when bootstrapping period_start_at
# without prior history. After one full period the heuristic uses observed boundaries.
_DEFAULT_PERIOD_HOURS = {
    'session': 5,
    'weekly': 168,
    'model_specific': 168,
}

# Resets within this many seconds of each other are considered "the same"
# (handles claude /usage's :59 vs :00 fluctuation for the same reset instant).
_RESET_EQUIVALENCE_SECONDS = 1800

# A reading whose resets_at lands further than the quota's period (plus this
# margin) ahead of captured_at is a transient glitch — observed as claude
# /usage briefly reporting the reset +24h at the boundary (a date-rollover
# artifact). A fresh reset never pushes resets_at more than one period ahead,
# so anything beyond that is untrustworthy. If trusted, a single such reading
# poisons the reset/shift classification of the *next* snapshot and freezes
# period_start_at for the whole following period.
_RESETS_AHEAD_MARGIN_HOURS = 1

# Quota types a complete reading always contains. When the latest snapshot is
# missing one (a partial/glitched PTY render dropped the row — most often the
# late-rendering Sonnet per-model quota), get_current() carries the most recent
# earlier value forward and tags it stale=True, so the status card shows the
# last known number with a marker instead of going blank. Only the /api/current
# view falls back; get_history / predictions stay strictly truthful (no
# synthetic rows are ever written to the DB).
_FALLBACK_QUOTA_TYPES = ('weekly', 'session', 'model_specific')


class UsageDatabase:
    """Data Access Layer for usage snapshots stored in SQLite."""

    def __init__(self, db_path: str):
        """Initialize database connection and create tables if needed."""
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # WAL + a busy_timeout so the 5-min collector writes and the web app's
        # reads don't intermittently fail with 'database is locked'. WAL lets a
        # reader and a writer proceed concurrently; synchronous=NORMAL is safe
        # under WAL and avoids an fsync per write.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._create_tables()

    def _create_tables(self):
        """Create database schema if not exists."""
        cursor = self.conn.cursor()
        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at DATETIME NOT NULL,
                account_type TEXT,
                email TEXT
            );

            CREATE TABLE IF NOT EXISTS quotas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER NOT NULL,
                quota_type TEXT NOT NULL,
                model TEXT,
                percent_remaining REAL NOT NULL,
                resets_at DATETIME,
                time_remaining_seconds INTEGER,
                period_start_at DATETIME,
                FOREIGN KEY (snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                email TEXT,
                account_type TEXT,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at DATETIME,
                last_polled_at DATETIME,
                last_error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_snapshots_captured_at ON snapshots(captured_at);
            CREATE INDEX IF NOT EXISTS idx_quotas_snapshot_id ON quotas(snapshot_id);
        """)
        # Add column for older databases that pre-date period_start_at
        cursor.execute("PRAGMA table_info(quotas)")
        cols = {row['name'] for row in cursor.fetchall()}
        if 'period_start_at' not in cols:
            cursor.execute("ALTER TABLE quotas ADD COLUMN period_start_at DATETIME")
        # account_id on snapshots (nullable; legacy rows keep NULL until backfilled)
        cursor.execute("PRAGMA table_info(snapshots)")
        snap_cols = {row['name'] for row in cursor.fetchall()}
        if 'account_id' not in snap_cols:
            cursor.execute("ALTER TABLE snapshots ADD COLUMN account_id INTEGER")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_snapshots_account_id ON snapshots(account_id)")
        self.conn.commit()

    def insert_snapshot(self, data: Dict[str, Any]) -> int:
        """
        Insert a new usage snapshot into the database.

        Args:
            data: Dictionary with usage data from api_usage_fetcher.py
                  Expected keys: captured_at, account_type, email, quotas

        Returns:
            The ID of the inserted snapshot
        """
        cursor = self.conn.cursor()

        captured_at = data.get('captured_at', datetime.now(timezone.utc).isoformat())
        if isinstance(captured_at, str):
            captured_at = captured_at.replace('Z', '+00:00')
            try:
                captured_at = datetime.fromisoformat(captured_at)
            except ValueError:
                captured_at = datetime.now(timezone.utc)

        cursor.execute("""
            INSERT INTO snapshots (captured_at, account_type, email, account_id)
            VALUES (?, ?, ?, ?)
        """, (captured_at, data.get('account_type'), data.get('email'),
              data.get('account_id')))

        snapshot_id = cursor.lastrowid

        for quota in data.get('quotas', []):
            resets_at = quota.get('resets_at')
            if resets_at:
                resets_at = resets_at.replace('Z', '+00:00')
                try:
                    resets_at = datetime.fromisoformat(resets_at)
                except ValueError:
                    resets_at = None

            # Reject a glitched reading (resets_at implausibly far ahead) by
            # carrying forward the previous good reset instant, so it can't
            # poison this row's period_start nor the next snapshot's classification.
            if resets_at is not None and not self._resets_at_is_plausible(
                    quota.get('type'), captured_at, resets_at):
                carried = self._prev_resets_at(
                    quota.get('type'), quota.get('model'), captured_at,
                    data.get('account_id'))
                if carried is not None:
                    resets_at = carried
                else:
                    # No prior good value (fresh DB / first per-model row): clamp the
                    # glitch to one period ahead instead of storing the raw +24h.
                    period_hours = _DEFAULT_PERIOD_HOURS.get(quota.get('type'), 168)
                    resets_at = captured_at + timedelta(hours=period_hours)

            period_start_at = self._compute_period_start_at(
                quota.get('type'), quota.get('model'), captured_at, resets_at,
                data.get('account_id')
            )

            cursor.execute("""
                INSERT INTO quotas (snapshot_id, quota_type, model, percent_remaining,
                                   resets_at, time_remaining_seconds, period_start_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                snapshot_id,
                quota.get('type'),
                quota.get('model'),
                quota.get('percent_remaining', 0),
                resets_at,
                quota.get('time_remaining_seconds'),
                period_start_at,
            ))

        self.conn.commit()
        return snapshot_id

    def _compute_period_start_at(self, quota_type: Optional[str], model: Optional[str],
                                  captured_at: datetime,
                                  resets_at: Optional[datetime],
                                  account_id: Optional[int] = None) -> Optional[datetime]:
        """Determine when the current period started, distinguishing reset from shift.

        - same resets_at as previous snapshot → inherit prev.period_start_at
        - resets_at changed AND prev.resets_at is still in the future at *current*.captured_at
          → SHIFT (Anthropic moved the goalpost mid-period), inherit prev.period_start_at
        - resets_at changed AND prev.resets_at <= current.captured_at
          → RESET (the old deadline fired between prev and now), period_start_at = prev.resets_at
        - no prior data → fallback to resets_at − default-period

        The reset/shift distinction must compare prev.resets_at with *current*.captured_at,
        not prev.captured_at — at prev.captured_at the deadline was always in the future
        (otherwise prev wouldn't have observed it as the active resets_at).

        `account_id` scopes "previous" to the SAME account (SQLite `IS ?` matches
        a real id to that id and a None param to legacy NULL-account rows), so
        polling account B never inherits account A's reset anchor.
        """
        if not resets_at:
            return None

        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT q.resets_at, q.period_start_at, s.captured_at
            FROM quotas q
            JOIN snapshots s ON s.id = q.snapshot_id
            WHERE q.quota_type = ?
              AND ((q.model IS NULL AND ? IS NULL) OR q.model = ?)
              AND s.account_id IS ?
              AND s.captured_at < ?
              AND q.resets_at IS NOT NULL
            ORDER BY s.captured_at DESC
            LIMIT 1
        """, (quota_type, model, model, account_id, captured_at))
        prev = cursor.fetchone()

        fallback_hours = _DEFAULT_PERIOD_HOURS.get(quota_type, 168)
        fallback = resets_at - timedelta(hours=fallback_hours)

        if not prev:
            return fallback

        prev_resets_at = self._parse_dt(prev['resets_at'])
        prev_period_start_at = self._parse_dt(prev['period_start_at'])

        if prev_resets_at is None:
            return fallback

        # Same reset (within tolerance) → inherit
        delta = abs((prev_resets_at - resets_at).total_seconds())
        if delta <= _RESET_EQUIVALENCE_SECONDS:
            return prev_period_start_at if prev_period_start_at else fallback

        # Different reset: has the previous deadline already passed by now?
        if prev_resets_at <= captured_at:
            # RESET: prev deadline fired. But if there's a long gap (multiple
            # missed resets), prev.resets_at is older than one full period before
            # the current reset — clamp to fallback so period_start_at represents
            # the *most recent* period boundary rather than an ancient one.
            return max(prev_resets_at, fallback)

        # SHIFT: prev deadline still in the future, period continues with new end
        return prev_period_start_at if prev_period_start_at else fallback

    @staticmethod
    def _parse_dt(value) -> Optional[datetime]:
        """Parse a SQLite DATETIME value (str or datetime) into a tz-aware datetime."""
        if value is None:
            return None
        if isinstance(value, datetime):
            dt = value
        else:
            try:
                dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            except (ValueError, TypeError):
                return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    @staticmethod
    def _resets_at_is_plausible(quota_type: Optional[str], captured_at: datetime,
                                resets_at: datetime) -> bool:
        """True unless resets_at lands implausibly far in the future for this
        quota — i.e. more than one period (+margin) ahead of captured_at. Such a
        value is a transient glitch (see _RESETS_AHEAD_MARGIN_HOURS)."""
        if resets_at is None or captured_at is None:
            return True
        period_hours = _DEFAULT_PERIOD_HOURS.get(quota_type, 168)
        horizon = timedelta(hours=period_hours + _RESETS_AHEAD_MARGIN_HOURS)
        return (resets_at - captured_at) <= horizon

    def _prev_resets_at(self, quota_type: Optional[str], model: Optional[str],
                        captured_at: datetime,
                        account_id: Optional[int] = None) -> Optional[datetime]:
        """The most recent stored resets_at for this quota before captured_at.
        Values stored going forward are already sanitized, so this is a known
        good reset instant to carry forward when the current reading glitches.

        `account_id` scopes the lookup to the same account (`IS ?`: real id → that
        id, None → legacy NULL rows), so one account never carries another's reset.
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT q.resets_at
            FROM quotas q
            JOIN snapshots s ON s.id = q.snapshot_id
            WHERE q.quota_type = ?
              AND ((q.model IS NULL AND ? IS NULL) OR q.model = ?)
              AND s.account_id IS ?
              AND s.captured_at < ?
              AND q.resets_at IS NOT NULL
            ORDER BY s.captured_at DESC
            LIMIT 1
        """, (quota_type, model, model, account_id, captured_at))
        row = cursor.fetchone()
        return self._parse_dt(row['resets_at']) if row else None

    def get_current(self, account_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """
        Get the most recent usage snapshot.

        Args:
            account_id: If set, only consider snapshots belonging to this account.

        Returns:
            Dictionary with usage data in dashboard format, or None if no data
        """
        cursor = self.conn.cursor()
        where, params = "", ()
        if account_id is not None:
            where, params = "WHERE account_id = ?", (account_id,)
        cursor.execute(f"""
            SELECT id, captured_at, account_type, email
            FROM snapshots
            {where}
            ORDER BY captured_at DESC, id DESC
            LIMIT 1
        """, params)

        row = cursor.fetchone()
        if not row:
            return None

        result = self._snapshot_to_dict(row)
        self._fill_missing_quotas(result['limits'], row['id'], row['captured_at'],
                                  account_id)
        return result

    def get_history(self, hours: Optional[int] = None,
                    max_points: Optional[int] = None,
                    account_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Get historical usage data.

        Args:
            hours: Number of hours to look back. Default is 168 (7 days).
            max_points: If set and the range holds more snapshots than this, return
                an evenly-spaced subset capped at ~max_points (first and last point
                always kept). Charts plot on a time axis, so a strided sample looks
                identical at that zoom while cutting build/payload/render by ~10x for
                long ranges (60d ≈ 17k snapshots / 9 MB JSON otherwise). None (default)
                returns EVERY snapshot — callers needing full resolution (prediction)
                must leave it None.

        Returns:
            List of usage records in dashboard format
        """
        if hours is None:
            hours = 168  # 7 days

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        # Default branch joins snapshots s ⋈ quotas q, so qualify the column as
        # s.account_id; the id-only scan below is single-table (unqualified).
        acct_params = (account_id,) if account_id is not None else ()
        acct_sql_joined = " AND s.account_id = ?" if account_id is not None else ""
        acct_sql_scan = " AND account_id = ?" if account_id is not None else ""

        cursor = self.conn.cursor()

        # Default: filter the JOIN by captured_at (covering index) — unchanged path.
        where, params = "s.captured_at >= ?" + acct_sql_joined, (cutoff,) + acct_params
        if max_points and max_points >= 2:
            # Cheap id-only scan (covering index) tells us the count up front so we
            # can cap the expensive quota JOIN + dict build for long ranges.
            cursor.execute(
                "SELECT id FROM snapshots WHERE captured_at >= ?" + acct_sql_scan +
                " ORDER BY captured_at ASC, id ASC", (cutoff,) + acct_params)
            ids = [row['id'] for row in cursor.fetchall()]
            if len(ids) > max_points:
                # Pick max_points indices evenly across [0, n-1], inclusive of both
                # ends; dedup consecutive when n is only just over the cap.
                n = len(ids)
                picked: List[int] = []
                prev = -1
                for k in range(max_points):
                    idx = (k * (n - 1)) // (max_points - 1)
                    if idx != prev:
                        picked.append(ids[idx])
                        prev = idx
                # ids are our own integer PKs — inline them so we never hit the bound
                # -parameter ceiling on very large ranges.
                id_list = ','.join(str(int(i)) for i in picked)
                where, params = f"s.id IN ({id_list})", ()

        cursor.execute(f"""
            SELECT s.id, s.captured_at, s.account_type, s.email,
                   q.quota_type, q.model, q.percent_remaining,
                   q.resets_at, q.time_remaining_seconds, q.period_start_at
            FROM snapshots s
            LEFT JOIN quotas q ON q.snapshot_id = s.id
            WHERE {where}
            ORDER BY s.captured_at ASC, q.id ASC
        """, params)

        by_id: Dict[int, Dict[str, Any]] = {}
        order: List[int] = []
        for row in cursor.fetchall():
            sid = row['id']
            snap = by_id.get(sid)
            if snap is None:
                snap = {
                    'timestamp': self._format_timestamp(row['captured_at']),
                    'limits': {},
                    'account_type': row['account_type'],
                    'email': row['email'],
                }
                by_id[sid] = snap
                order.append(sid)
            if row['quota_type']:
                snap['limits'][row['quota_type']] = self._quota_row_to_dict(row)

        return [by_id[sid] for sid in order]

    def _snapshot_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        """Convert a snapshot database row to dashboard format."""
        cursor = self.conn.cursor()

        cursor.execute("""
            SELECT quota_type, model, percent_remaining, resets_at, time_remaining_seconds, period_start_at
            FROM quotas
            WHERE snapshot_id = ?
        """, (row['id'],))

        limits = {}
        for quota in cursor.fetchall():
            limits[quota['quota_type']] = self._quota_row_to_dict(quota)

        return {
            'timestamp': self._format_timestamp(row['captured_at']),
            'limits': limits,
            'account_type': row['account_type'],
            'email': row['email']
        }

    def _fill_missing_quotas(self, limits: Dict[str, Any],
                             snapshot_id: int, captured_at,
                             account_id: Optional[int] = None) -> None:
        """Carry the last known value forward for any expected quota missing from
        the latest snapshot, tagging it stale. Mutates `limits` in place.

        A partial/glitched PTY render occasionally drops a quota row (most often
        the late-rendering Sonnet per-model bar), so the latest snapshot has e.g.
        weekly+session but no model_specific and the status card goes blank. For
        the current-status view only, fill each missing expected quota from the
        most recent earlier snapshot that had it, marking it `stale=True` with the
        source reading's timestamp (`stale_since`). Nothing is written back, so
        history charts and predictions keep using only real readings.

        Three correctness guards:
        - Anchor on `snapshot_id` (`s.id < ?`), not captured_at, so a same-second
          earlier snapshot can still backfill the chosen (higher-id) glitch row.
        - For model_specific prefer the Sonnet row (the status card is always
          Sonnet; never carry a stray Opus value), with `q.id DESC` as a
          deterministic tiebreak when a snapshot holds multiple model rows.
        - Skip a value whose period already ended by the current reading's time
          (`resets_at <= captured_at`): a reset fired during the outage, so the
          old percentage and its now-past resets_at would be actively wrong.
          "Previous value" only means something within the same period.

        `account_id` scopes the carry-forward to the same account (`s.account_id
        IS ?`: real id → that id, None → legacy NULL rows / cross-account legacy
        behavior), so account A never backfills from account B's snapshot.
        """
        captured_dt = self._parse_dt(captured_at)
        acct_sql = " AND s.account_id IS ?" if account_id is not None else ""
        for quota_type in _FALLBACK_QUOTA_TYPES:
            if quota_type in limits:
                continue
            cursor = self.conn.cursor()
            params = (quota_type, snapshot_id)
            if account_id is not None:
                params = (quota_type, snapshot_id, account_id)
            cursor.execute(f"""
                SELECT q.quota_type, q.model, q.percent_remaining, q.resets_at,
                       q.time_remaining_seconds, q.period_start_at, s.captured_at
                FROM quotas q
                JOIN snapshots s ON s.id = q.snapshot_id
                WHERE q.quota_type = ?
                  AND s.id < ?{acct_sql}
                  AND (q.model IS NULL OR q.model = 'sonnet')
                ORDER BY s.captured_at DESC, q.id DESC
                LIMIT 1
            """, params)
            prev = cursor.fetchone()
            if not prev:
                continue
            prev_resets = self._parse_dt(prev['resets_at'])
            if (prev_resets is not None and captured_dt is not None
                    and prev_resets <= captured_dt):
                continue  # period already reset during the outage — don't mislead
            quota = self._quota_row_to_dict(prev)
            quota['stale'] = True
            quota['stale_since'] = self._format_iso_z(prev['captured_at'])
            limits[quota_type] = quota

    def _quota_row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        """Format a quotas row (from direct SELECT or JOIN) for dashboard output."""
        resets_at_str = self._format_iso_z(row['resets_at'])
        period_start_at_str = None
        try:
            period_start_at_str = self._format_iso_z(row['period_start_at'])
        except (IndexError, KeyError):
            # Older row tuples may not include the column — leave as None
            pass

        time_remaining_human = None
        if row['time_remaining_seconds']:
            time_remaining_human = self._format_duration(row['time_remaining_seconds'])

        data = {
            'percent_remaining': row['percent_remaining'],
            'resets_at': resets_at_str,
            'period_start_at': period_start_at_str,
            'time_remaining_human': time_remaining_human,
        }
        if row['model']:
            data['model'] = row['model']
        return data

    @staticmethod
    def _format_iso_z(value) -> Optional[str]:
        """Format a SQLite DATETIME value as ISO-8601 with trailing Z."""
        if value is None:
            return None
        try:
            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except (ValueError, TypeError):
            return str(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    @staticmethod
    def _format_timestamp(captured_at) -> str:
        """Normalize a snapshot's captured_at to ISO-8601 with a 'T' separator and
        trailing 'Z'. The SQLite adapter stores datetimes as
        'YYYY-MM-DD HH:MM:SS+00:00' (space separator); handing that back verbatim
        broke `new Date(...)` on Safari/iOS (Invalid Date → blank charts). Falls
        back to the raw value only if it can't be parsed."""
        iso = UsageDatabase._format_iso_z(captured_at)
        return iso if iso is not None else str(captured_at)

    def _format_duration(self, seconds: int) -> str:
        """Format seconds as human-readable text."""
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        minutes = (seconds % 3600) // 60

        parts = []
        if days > 0:
            parts.append(f"{days}d")
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0:
            parts.append(f"{minutes}m")

        return ' '.join(parts) if parts else '0m'

    # ---- accounts ----

    def add_or_update_account(self, label, email, account_type,
                              access_token, refresh_token, expires_at) -> int:
        """Insert a new account or, if one with the same email exists, refresh
        its tokens/label/type. Tokens are stored encrypted. Returns the id."""
        # Lazy import: database.py must import on first deploy before `cryptography`
        # is pip-installed; only the account paths actually need it.
        import crypto_util
        enc_at = crypto_util.encrypt(access_token)
        enc_rt = crypto_util.encrypt(refresh_token)
        cur = self.conn.cursor()
        existing = None
        if email:
            existing = cur.execute(
                "SELECT id FROM accounts WHERE email = ?", (email,)).fetchone()
        if existing:
            acc_id = existing['id']
            cur.execute("""
                UPDATE accounts SET label=?, account_type=?, access_token=?,
                    refresh_token=?, expires_at=?, is_active=1, last_error=NULL
                WHERE id=?
            """, (label, account_type, enc_at, enc_rt, expires_at, acc_id))
        else:
            cur.execute("""
                INSERT INTO accounts (label, email, account_type, access_token,
                    refresh_token, expires_at, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            """, (label, email, account_type, enc_at, enc_rt, expires_at,
                  datetime.now(timezone.utc)))
            acc_id = cur.lastrowid
        self.conn.commit()
        return acc_id

    def list_accounts(self):
        """Account metadata for UI — never includes tokens."""
        cur = self.conn.execute("""
            SELECT id, label, email, account_type, is_active,
                   created_at, last_polled_at, last_error
            FROM accounts ORDER BY id ASC
        """)
        return [dict(row) for row in cur.fetchall()]

    def get_pollable_accounts(self):
        """Active accounts with DECRYPTED tokens, for the collector.

        Decryption is per-row and isolated: if ONE account's stored token can't
        be decrypted (corrupt blob, or encrypted under a different key), it's
        skipped and flagged via last_error so the UI surfaces it — the rest of
        the accounts still get polled. A globally missing/wrong key raises
        CryptoError on the first row instead, and that propagates so collect_all
        exits 3 (hard ERROR) rather than masquerading as 'no accounts to poll'.
        """
        # Lazy import (see add_or_update_account): keep database.py importable
        # before cryptography is installed on first deploy.
        import crypto_util
        cur = self.conn.execute("""
            SELECT id, label, email, account_type, access_token, refresh_token,
                   expires_at
            FROM accounts WHERE is_active = 1 ORDER BY id ASC
        """)
        out = []
        for row in cur.fetchall():
            d = dict(row)
            try:
                d['access_token'] = crypto_util.decrypt(d['access_token'])
                d['refresh_token'] = crypto_util.decrypt(d['refresh_token'])
            except crypto_util.CryptoError:
                raise  # key missing entirely → let collect_all fail hard (exit 3)
            except Exception as e:
                # This single row is undecryptable — isolate it, don't sink the run.
                self.record_account_poll(
                    d['id'], error=f'token decrypt failed: {type(e).__name__}')
                continue
            out.append(d)
        return out

    def update_account_tokens(self, account_id, access_token, refresh_token, expires_at):
        # Lazy import (see add_or_update_account): keep database.py importable
        # before cryptography is installed on first deploy.
        import crypto_util
        self.conn.execute("""
            UPDATE accounts SET access_token=?, refresh_token=?, expires_at=?
            WHERE id=?
        """, (crypto_util.encrypt(access_token), crypto_util.encrypt(refresh_token),
              expires_at, account_id))
        self.conn.commit()

    def set_account_active(self, account_id, is_active: bool):
        self.conn.execute("UPDATE accounts SET is_active=? WHERE id=?",
                          (1 if is_active else 0, account_id))
        self.conn.commit()

    def rename_account(self, account_id, label: str):
        self.conn.execute("UPDATE accounts SET label=? WHERE id=?", (label, account_id))
        self.conn.commit()

    def record_account_poll(self, account_id, error=None):
        self.conn.execute(
            "UPDATE accounts SET last_polled_at=?, last_error=? WHERE id=?",
            (datetime.now(timezone.utc), error, account_id))
        self.conn.commit()

    def delete_account(self, account_id):
        """Remove the account row. Snapshots keep their account_id (history stays).

        Known tradeoff: re-adding the same email mints a NEW id (the old row is
        gone), and backfill_account_by_email only adopts account_id IS NULL rows,
        so the deleted account's old snapshots stay orphaned under the old id.
        """
        self.conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        self.conn.commit()

    def get_default_account_id(self):
        row = self.conn.execute(
            "SELECT id FROM accounts WHERE is_active=1 ORDER BY id ASC LIMIT 1").fetchone()
        return row['id'] if row else None

    def backfill_account_by_email(self, account_id, email) -> int:
        """Attach legacy NULL-account snapshots with this email to account_id.
        Returns the number of rows updated."""
        cur = self.conn.execute(
            "UPDATE snapshots SET account_id=? WHERE account_id IS NULL AND email=?",
            (account_id, email))
        self.conn.commit()
        return cur.rowcount

    def get_snapshot_count(self) -> int:
        """Get total number of snapshots in database."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM snapshots")
        return cursor.fetchone()[0]

    def backfill_period_start_at(self) -> Dict[str, int]:
        """One-shot pass over all quotas chronologically per (quota_type, model),
        applying the same glitch-sanitization and shift-vs-reset heuristic used at
        insert time. Repairs two columns where the recomputed value differs from
        what is stored:
          - `resets_at`: a glitched reading (implausibly far ahead) is replaced
            with the carried-forward previous good reset instant.
          - `period_start_at`: recomputed from the (sanitized) resets_at.

        Returns {'period_start_updates': N, 'resets_at_sanitized': M}.
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT q.id, q.quota_type, q.model, q.resets_at, q.period_start_at,
                   s.captured_at, s.account_id
            FROM quotas q
            JOIN snapshots s ON s.id = q.snapshot_id
            ORDER BY q.quota_type ASC, COALESCE(q.model, ''),
                     COALESCE(s.account_id, -1), s.captured_at ASC, q.id ASC
        """)

        prev_by_key: Dict[tuple, Dict[str, Any]] = {}
        psa_updates: List[tuple] = []
        resets_updates: List[tuple] = []

        for row in cursor.fetchall():
            # Partition by account too, so one account's history never seeds
            # another's reset/shift classification (matches insert-time scoping).
            key = (row['quota_type'], row['model'], row['account_id'])
            stored_resets = self._parse_dt(row['resets_at'])
            captured_at = self._parse_dt(row['captured_at'])
            stored_psa = self._parse_dt(row['period_start_at'])

            # Sanitize a glitched resets_at by carrying forward the previous good
            # value, so it neither corrupts this row's period_start nor poisons
            # the next row's reset/shift classification.
            resets_at = stored_resets
            prev = prev_by_key.get(key)
            if (resets_at is not None and captured_at is not None
                    and not self._resets_at_is_plausible(row['quota_type'], captured_at, resets_at)):
                if prev and prev['resets_at'] is not None:
                    resets_at = prev['resets_at']
                else:
                    # No prior good value to carry forward: clamp to one period ahead.
                    period_hours = _DEFAULT_PERIOD_HOURS.get(row['quota_type'], 168)
                    resets_at = captured_at + timedelta(hours=period_hours)

            if resets_at is None:
                computed = None
            else:
                fallback = resets_at - timedelta(
                    hours=_DEFAULT_PERIOD_HOURS.get(row['quota_type'], 168)
                )
                if not prev or prev['resets_at'] is None:
                    computed = fallback
                else:
                    delta = abs((prev['resets_at'] - resets_at).total_seconds())
                    if delta <= _RESET_EQUIVALENCE_SECONDS:
                        computed = prev['period_start_at'] or fallback
                    elif captured_at and prev['resets_at'] <= captured_at:
                        # RESET: clamp to fallback to handle long capture gaps
                        computed = max(prev['resets_at'], fallback)
                    else:
                        # SHIFT: previous deadline still in future at this capture
                        computed = prev['period_start_at'] or fallback

            if computed != stored_psa:
                psa_updates.append((computed, row['id']))
            if resets_at != stored_resets:
                resets_updates.append((resets_at, row['id']))

            # Match insert-time SELECT which filters `q.resets_at IS NOT NULL`:
            # rows without a known reset must not displace the last valid prev,
            # otherwise a (valid → NULL → shifted-valid) sequence would lose the
            # earlier shift information and recompute period_start from fallback.
            if resets_at is not None:
                prev_by_key[key] = {
                    'resets_at': resets_at,
                    'captured_at': captured_at,
                    'period_start_at': computed,
                }

        if resets_updates:
            cursor.executemany(
                "UPDATE quotas SET resets_at = ? WHERE id = ?",
                resets_updates,
            )
        if psa_updates:
            cursor.executemany(
                "UPDATE quotas SET period_start_at = ? WHERE id = ?",
                psa_updates,
            )
        if resets_updates or psa_updates:
            self.conn.commit()
        return {
            'period_start_updates': len(psa_updates),
            'resets_at_sanitized': len(resets_updates),
        }

    def delete_older_than(self, cutoff: datetime) -> Dict[str, int]:
        """Delete snapshots captured before `cutoff` and their quotas.

        foreign_keys pragma is OFF on this connection, so ON DELETE CASCADE does
        NOT fire — delete child `quotas` first, then `snapshots`, in one
        transaction. `cutoff` is a tz-aware datetime passed as a bound param so
        the registered adapter formats it exactly like stored `captured_at`
        (same approach as get_history). Returns {'snapshots': N, 'quotas': M}.
        """
        cur = self.conn.cursor()
        cur.execute(
            "DELETE FROM quotas WHERE snapshot_id IN "
            "(SELECT id FROM snapshots WHERE captured_at < ?)", (cutoff,))
        quotas = cur.rowcount
        cur.execute("DELETE FROM snapshots WHERE captured_at < ?", (cutoff,))
        snapshots = cur.rowcount
        self.conn.commit()
        return {'snapshots': snapshots, 'quotas': quotas}

    def vacuum(self) -> None:
        """Reclaim free pages after a delete. Commit first — VACUUM cannot run
        inside a transaction."""
        self.conn.commit()
        self.conn.execute("VACUUM")

    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
