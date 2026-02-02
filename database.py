"""SQLite Database Layer for Claude Usage Dashboard"""
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import os


class UsageDatabase:
    """Data Access Layer for usage snapshots stored in SQLite."""

    def __init__(self, db_path: str):
        """Initialize database connection and create tables if needed."""
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
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
                FOREIGN KEY (snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_snapshots_captured_at ON snapshots(captured_at);
            CREATE INDEX IF NOT EXISTS idx_quotas_snapshot_id ON quotas(snapshot_id);
        """)
        self.conn.commit()

    def insert_snapshot(self, data: Dict[str, Any]) -> int:
        """
        Insert a new usage snapshot into the database.

        Args:
            data: Dictionary with usage data from usage_fetcher.py
                  Expected keys: captured_at, account_type, email, quotas

        Returns:
            The ID of the inserted snapshot
        """
        cursor = self.conn.cursor()

        captured_at = data.get('captured_at', datetime.now().isoformat())
        if isinstance(captured_at, str):
            captured_at = captured_at.replace('Z', '+00:00')
            try:
                captured_at = datetime.fromisoformat(captured_at)
            except ValueError:
                captured_at = datetime.now()

        cursor.execute("""
            INSERT INTO snapshots (captured_at, account_type, email)
            VALUES (?, ?, ?)
        """, (captured_at, data.get('account_type'), data.get('email')))

        snapshot_id = cursor.lastrowid

        for quota in data.get('quotas', []):
            resets_at = quota.get('resets_at')
            if resets_at:
                resets_at = resets_at.replace('Z', '+00:00')
                try:
                    resets_at = datetime.fromisoformat(resets_at)
                except ValueError:
                    resets_at = None

            cursor.execute("""
                INSERT INTO quotas (snapshot_id, quota_type, model, percent_remaining,
                                   resets_at, time_remaining_seconds)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                snapshot_id,
                quota.get('type'),
                quota.get('model'),
                quota.get('percent_remaining', 0),
                resets_at,
                quota.get('time_remaining_seconds')
            ))

        self.conn.commit()
        return snapshot_id

    def get_current(self) -> Optional[Dict[str, Any]]:
        """
        Get the most recent usage snapshot.

        Returns:
            Dictionary with usage data in dashboard format, or None if no data
        """
        cursor = self.conn.cursor()

        cursor.execute("""
            SELECT id, captured_at, account_type, email
            FROM snapshots
            ORDER BY captured_at DESC
            LIMIT 1
        """)

        row = cursor.fetchone()
        if not row:
            return None

        return self._snapshot_to_dict(row)

    def get_history(self, hours: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Get historical usage data.

        Args:
            hours: Number of hours to look back. Default is 168 (7 days).

        Returns:
            List of usage records in dashboard format
        """
        if hours is None:
            hours = 168  # 7 days

        cutoff = datetime.now() - timedelta(hours=hours)

        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT id, captured_at, account_type, email
            FROM snapshots
            WHERE captured_at >= ?
            ORDER BY captured_at ASC
        """, (cutoff,))

        rows = cursor.fetchall()
        return [self._snapshot_to_dict(row) for row in rows]

    def _snapshot_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        """Convert a snapshot database row to dashboard format."""
        cursor = self.conn.cursor()

        cursor.execute("""
            SELECT quota_type, model, percent_remaining, resets_at, time_remaining_seconds
            FROM quotas
            WHERE snapshot_id = ?
        """, (row['id'],))

        limits = {}
        for quota in cursor.fetchall():
            quota_type = quota['quota_type']

            resets_at_str = None
            time_remaining_human = None

            if quota['resets_at']:
                try:
                    resets_at = datetime.fromisoformat(str(quota['resets_at']))
                    resets_at_str = resets_at.strftime('%Y-%m-%dT%H:%M:%SZ')
                except (ValueError, TypeError):
                    resets_at_str = str(quota['resets_at'])

            if quota['time_remaining_seconds']:
                time_remaining_human = self._format_duration(quota['time_remaining_seconds'])

            limit_data = {
                'percent_remaining': quota['percent_remaining'],
                'resets_at': resets_at_str,
                'time_remaining_human': time_remaining_human
            }

            if quota['model']:
                limit_data['model'] = quota['model']

            limits[quota_type] = limit_data

        captured_at = row['captured_at']
        if isinstance(captured_at, str):
            timestamp = captured_at
        else:
            timestamp = captured_at.strftime('%Y-%m-%dT%H:%M:%SZ')

        return {
            'timestamp': timestamp,
            'limits': limits,
            'account_type': row['account_type'],
            'email': row['email']
        }

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

    def get_snapshot_count(self) -> int:
        """Get total number of snapshots in database."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM snapshots")
        return cursor.fetchone()[0]

    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
