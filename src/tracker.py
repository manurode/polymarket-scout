"""Tracker — SQLite persistence for snapshots and signals."""

import json
import os
import sqlite3
import time
import logging

logger = logging.getLogger(__name__)


class Tracker:
    """Persist market snapshots and trading signals to SQLite."""

    def __init__(self, db):
        """Accept either a sqlite3.Connection or a path string.

        If a string is given, parent directories are created and a new
        connection is opened (check_same_thread=False for reuse across
        threads if needed).
        """
        if isinstance(db, sqlite3.Connection):
            self.conn = db
            self._own = False
        elif isinstance(db, str):
            os.makedirs(os.path.dirname(db) or ".", exist_ok=True)
            self.conn = sqlite3.connect(db, check_same_thread=False)
            self._own = True
        else:
            raise TypeError("db must be sqlite3.Connection or path string")

    def init_db(self):
        """Create tables and indexes if they do not exist."""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                condition_id TEXT NOT NULL,
                question TEXT,
                slug TEXT,
                event_title TEXT,
                price_yes REAL,
                price_no REAL,
                spread REAL,
                volume REAL,
                liquidity REAL,
                timestamp INTEGER NOT NULL,
                UNIQUE(condition_id, timestamp)
            );

            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                condition_id TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                score REAL,
                detail TEXT DEFAULT '{}',
                timestamp INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_snapshots_condition
                ON snapshots(condition_id);

            CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp
                ON snapshots(timestamp);

            CREATE INDEX IF NOT EXISTS idx_signals_timestamp
                ON signals(timestamp);

            CREATE INDEX IF NOT EXISTS idx_signals_score
                ON signals(score);
            """
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def save_snapshots(self, snapshots: list[dict]) -> int:
        """Persist a batch of market snapshots.

        Returns the number of *new* rows actually inserted (INSERT OR
        IGNORE skips duplicates; only fully new rows count).
        """
        if not snapshots:
            return 0

        columns = [
            "condition_id", "question", "slug", "event_title",
            "price_yes", "price_no", "spread", "volume", "liquidity",
            "timestamp",
        ]

        placeholders = ", ".join(
            "(" + ", ".join("?" for _ in columns) + ")"
            for _ in snapshots
        )

        values = []
        for s in snapshots:
            values.extend(s.get(col) for col in columns)

        sql = (
            "INSERT OR IGNORE INTO snapshots ("
            + ", ".join(columns)
            + ") VALUES "
            + placeholders
        )

        self.conn.execute(sql, values)
        self.conn.commit()
        return self.conn.execute("SELECT changes()").fetchone()[0]

    def get_recent_snapshots(
        self, condition_id: str, lookback_seconds: int = 3600,
        reference_ts: int | None = None
    ) -> list[dict]:
        """Return snapshots for *condition_id* within the lookback window.

        Results are ordered by timestamp ascending and returned as a list
        of dicts (one key per column).
        """
        if reference_ts is None:
            reference_ts = int(time.time())
        cutoff = reference_ts - lookback_seconds

        rows = self.conn.execute(
            """
            SELECT condition_id, question, slug, event_title,
                   price_yes, price_no, spread, volume, liquidity,
                   timestamp
            FROM snapshots
            WHERE condition_id = ?
              AND timestamp >= ?
            ORDER BY timestamp ASC
            """,
            (condition_id, cutoff),
        ).fetchall()

        columns = [
            "condition_id", "question", "slug", "event_title",
            "price_yes", "price_no", "spread", "volume", "liquidity",
            "timestamp",
        ]
        return [dict(zip(columns, row)) for row in rows]

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    def save_signal(
        self, condition_id: str, signal_type: str, score: float,
        detail: str = "{}", timestamp: int | None = None,
        cooldown_minutes: int = 30,
    ) -> bool:
        """Insert a new signal, respecting a per-type cooldown.

        If a signal with the same *condition_id* and *signal_type* has
        been recorded within the cooldown window the insertion is skipped
        and the method returns ``False``.  Otherwise the signal is
        inserted and ``True`` is returned.
        """
        if timestamp is None:
            timestamp = int(time.time())

        cooldown_seconds = cooldown_minutes * 60
        cutoff = timestamp - cooldown_seconds

        existing = self.conn.execute(
            """
            SELECT 1 FROM signals
            WHERE condition_id = ?
              AND signal_type = ?
              AND timestamp >= ?
            LIMIT 1
            """,
            (condition_id, signal_type, cutoff),
        ).fetchone()

        if existing:
            return False

        self.conn.execute(
            """
            INSERT INTO signals (condition_id, signal_type, score, detail, timestamp)
            VALUES (?, ?, ?, ?, ?)
            """,
            (condition_id, signal_type, score, detail, timestamp),
        )
        self.conn.commit()
        return True
