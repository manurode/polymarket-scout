"""Tests for the Tracker module."""

import pytest
import time

from src.tracker import Tracker


class TestInit:
    """Tests for Tracker.__init__ and init_db."""

    def test_accepts_connection(self, temp_db):
        """Tracker should accept an existing sqlite3.Connection."""
        t = Tracker(temp_db)
        assert t.conn is temp_db
        assert t._own is False

    def test_accepts_path_string(self, tmp_path):
        """Tracker should create parent dirs and connect when given a path."""
        db_path = tmp_path / "sub" / "tracker.db"
        t = Tracker(str(db_path))
        assert t._own is True
        assert db_path.exists()
        t.conn.close()

    def test_init_db_creates_tables(self, temp_db):
        """After init_db both tables must exist."""
        t = Tracker(temp_db)
        t.init_db()

        tables = temp_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = [r[0] for r in tables]
        assert "snapshots" in table_names
        assert "signals" in table_names

    def test_init_db_creates_indexes(self, temp_db):
        """After init_db the required indexes must exist."""
        t = Tracker(temp_db)
        t.init_db()

        indexes = temp_db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name"
        ).fetchall()
        index_names = {r[0] for r in indexes}
        assert "idx_snapshots_condition" in index_names
        assert "idx_snapshots_timestamp" in index_names
        assert "idx_signals_timestamp" in index_names
        assert "idx_signals_score" in index_names

    def test_init_db_is_idempotent(self, temp_db):
        """Calling init_db multiple times should not raise."""
        t = Tracker(temp_db)
        t.init_db()
        t.init_db()  # should not fail
        t.init_db()


class TestSaveSnapshots:
    """Tests for save_snapshots."""

    def _snap(self, condition_id="0xabc", ts=None):
        """Helper to build a minimal snapshot dict."""
        if ts is None:
            ts = int(time.time())
        return {
            "condition_id": condition_id,
            "question": "Will it rain?",
            "slug": "rain-today",
            "event_title": "Weather 2025",
            "price_yes": 0.55,
            "price_no": 0.45,
            "spread": 0.02,
            "volume": 50_000.0,
            "liquidity": 10_000.0,
            "timestamp": ts,
        }

    def test_save_snapshots_returns_count(self, temp_db):
        """Inserting snapshots should return the number of new rows."""
        t = Tracker(temp_db)
        t.init_db()

        count = t.save_snapshots([self._snap("0x1"), self._snap("0x2")])
        assert count == 2

    def test_save_snapshots_deduplicates(self, temp_db):
        """Duplicates (same condition_id+timestamp) should be silently ignored."""
        t = Tracker(temp_db)
        t.init_db()
        ts = int(time.time())

        snap_a = self._snap("0xdup", ts)
        snap_b = self._snap("0xdup", ts)  # identical key

        c1 = t.save_snapshots([snap_a])
        assert c1 == 1

        c2 = t.save_snapshots([snap_b])
        assert c2 == 0

    def test_save_snapshots_empty_list(self, temp_db):
        """An empty list should return 0 without error."""
        t = Tracker(temp_db)
        t.init_db()
        assert t.save_snapshots([]) == 0

    def test_save_snapshots_updates_on_conflict_is_ignored(self, temp_db):
        """INSERT OR IGNORE keeps the first row; the second insert is a no-op."""
        t = Tracker(temp_db)
        t.init_db()
        ts = int(time.time())

        snap1 = {**self._snap("0xkeep", ts), "price_yes": 0.70}
        snap2 = {**self._snap("0xkeep", ts), "price_yes": 0.80}

        t.save_snapshots([snap1])
        t.save_snapshots([snap2])

        rows = t.get_recent_snapshots("0xkeep", lookback_seconds=10)
        assert len(rows) == 1
        assert rows[0]["price_yes"] == 0.70  # first insert kept


class TestGetRecentSnapshots:
    """Tests for get_recent_snapshots."""

    def _snap(self, condition_id="0xabc", ts=None):
        """Helper to build a minimal snapshot dict."""
        if ts is None:
            ts = int(time.time())
        return {
            "condition_id": condition_id,
            "question": "Will it rain?",
            "slug": "rain-today",
            "event_title": "Weather 2025",
            "price_yes": 0.55,
            "price_no": 0.45,
            "spread": 0.02,
            "volume": 50_000.0,
            "liquidity": 10_000.0,
            "timestamp": ts,
        }

    def test_returns_only_matching_condition(self, temp_db):
        """Should filter by condition_id."""
        t = Tracker(temp_db)
        t.init_db()
        now = int(time.time())

        t.save_snapshots([
            self._snap("0xA", now - 30),
            self._snap("0xB", now - 30),
            self._snap("0xA", now - 10),
        ])

        rows = t.get_recent_snapshots("0xA", lookback_seconds=3600)
        assert len(rows) == 2
        for r in rows:
            assert r["condition_id"] == "0xA"

    def test_respects_lookback_window(self, temp_db):
        """Snapshots older than lookback_seconds should be excluded."""
        t = Tracker(temp_db)
        t.init_db()
        now = int(time.time())

        t.save_snapshots([
            self._snap("0xC", now - 7200),  # 2 h ago → outside window
            self._snap("0xC", now - 1800),  # 30 min ago → inside
            self._snap("0xC", now - 60),
        ])

        rows = t.get_recent_snapshots("0xC", lookback_seconds=3600)
        assert len(rows) == 2

    def test_result_ordered_by_timestamp_asc(self, temp_db):
        """Returned rows must be sorted oldest-first."""
        t = Tracker(temp_db)
        t.init_db()
        now = int(time.time())

        t.save_snapshots([
            self._snap("0xD", now - 100),
            self._snap("0xD", now - 300),
            self._snap("0xD", now - 200),
        ])

        rows = t.get_recent_snapshots("0xD", lookback_seconds=3600)
        timestamps = [r["timestamp"] for r in rows]
        assert timestamps == sorted(timestamps)

    def test_reference_ts_override(self, temp_db):
        """Custom reference_ts shifts the lookback window."""
        t = Tracker(temp_db)
        t.init_db()

        # All timestamps are far in the past
        old = 1_000_000
        t.save_snapshots([self._snap("0xE", old)])
        t.save_snapshots([self._snap("0xE", old + 100)])

        # With a matching reference_ts we can see them
        rows = t.get_recent_snapshots(
            "0xE", lookback_seconds=3600, reference_ts=old + 200
        )
        assert len(rows) == 2

    def test_no_matching_rows(self, temp_db):
        """Empty result set when no snapshots exist."""
        t = Tracker(temp_db)
        t.init_db()
        rows = t.get_recent_snapshots("nonexistent")
        assert rows == []


class TestSaveSignal:
    """Tests for save_signal."""

    def test_save_signal_returns_true(self, temp_db):
        """A new signal should be saved successfully."""
        t = Tracker(temp_db)
        t.init_db()

        ok = t.save_signal("0xA", "momentum", 75.0)
        assert ok is True

        # Verify it's in the DB
        row = temp_db.execute(
            "SELECT * FROM signals WHERE condition_id='0xA'"
        ).fetchone()
        assert row is not None
        assert row[2] == "momentum"  # signal_type
        assert row[3] == 75.0         # score

    def test_save_signal_skips_duplicate_in_cooldown(self, temp_db):
        """Same signal_type+condition_id within cooldown should be rejected."""
        t = Tracker(temp_db)
        t.init_db()
        ts = int(time.time())

        ok1 = t.save_signal("0xB", "volume_spike", 80.0, timestamp=ts)
        assert ok1 is True

        ok2 = t.save_signal(
            "0xB", "volume_spike", 85.0,
            timestamp=ts + 60, cooldown_minutes=30,
        )
        assert ok2 is False

    def test_save_signal_allows_after_cooldown(self, temp_db):
        """After the cooldown expires a new signal is accepted."""
        t = Tracker(temp_db)
        t.init_db()
        ts = int(time.time())

        t.save_signal("0xC", "spread", 50.0, timestamp=ts, cooldown_minutes=1)
        ok = t.save_signal(
            "0xC", "spread", 60.0,
            timestamp=ts + 61, cooldown_minutes=1,
        )
        assert ok is True

    def test_save_signal_different_types_no_conflict(self, temp_db):
        """Different signal_types for the same condition_id do not conflict."""
        t = Tracker(temp_db)
        t.init_db()
        ts = int(time.time())

        assert t.save_signal("0xD", "momentum", 70.0, timestamp=ts)
        assert t.save_signal("0xD", "volume_spike", 80.0, timestamp=ts)
        # Both should exist
        count = temp_db.execute(
            "SELECT COUNT(*) FROM signals WHERE condition_id='0xD'"
        ).fetchone()[0]
        assert count == 2

    def test_save_signal_uses_current_time_when_timestamp_none(self, temp_db):
        """When timestamp is omitted the current time is used."""
        t = Tracker(temp_db)
        t.init_db()
        before = int(time.time())
        ok = t.save_signal("0xE", "new_interest", 90.0)
        after = int(time.time())
        assert ok is True

        ts = temp_db.execute(
            "SELECT timestamp FROM signals WHERE condition_id='0xE'"
        ).fetchone()[0]
        assert before <= ts <= after

    def test_default_detail_is_empty_json(self, temp_db):
        """When detail is omitted it defaults to '{}'."""
        t = Tracker(temp_db)
        t.init_db()
        t.save_signal("0xF", "momentum", 55.0)
        detail = temp_db.execute(
            "SELECT detail FROM signals WHERE condition_id='0xF'"
        ).fetchone()[0]
        assert detail == "{}"


class TestPaperTradingTracker:
    """Tests for paper trading methods on Tracker."""

    def test_init_paper_trading(self, temp_db):
        """init_paper_trading creates the paper_trades table."""
        t = Tracker(temp_db)
        t.init_paper_trading()

        tables = temp_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = [r[0] for r in tables]
        assert "paper_trades" in table_names

    def test_save_and_get_paper_trade(self, temp_db):
        """Save a trade and retrieve it via get_paper_trades."""
        t = Tracker(temp_db)
        t.init_paper_trading()

        now = int(time.time())
        trade = {
            "condition_id": "0xTX",
            "question": "Will it?",
            "side": "YES",
            "amount": 100.0,
            "price": 0.55,
            "shares": 181.82,
            "signal_type": "momentum",
            "score": 80,
            "status": "open",
            "entry_timestamp": now,
            "close_price": None,
            "close_timestamp": None,
            "pnl": None,
            "strategy": "default",
        }
        trade_id = t.save_paper_trade(trade)
        assert trade_id > 0

        trades = t.get_paper_trades(strategy="default")
        assert len(trades) == 1
        assert trades[0]["condition_id"] == "0xTX"
        assert trades[0]["side"] == "YES"
        assert trades[0]["amount"] == 100.0

    def test_close_position(self, temp_db):
        """close_position updates status and computes P&L."""
        t = Tracker(temp_db)
        t.init_paper_trading()

        trade_id = t.save_paper_trade({
            "condition_id": "0xCP2",
            "question": "Close me",
            "side": "YES",
            "amount": 50.0,
            "price": 0.50,
            "shares": 100.0,
            "signal_type": None,
            "score": None,
            "status": "open",
            "entry_timestamp": 1000,
            "close_price": None,
            "close_timestamp": None,
            "pnl": None,
            "strategy": "test",
        })

        ok = t.close_position(trade_id, 0.75, 2000)
        assert ok is True

        row = temp_db.execute(
            "SELECT status, close_price, close_timestamp, pnl FROM paper_trades WHERE id = ?",
            (trade_id,),
        ).fetchone()
        assert row[0] == "closed"
        assert row[1] == 0.75
        assert row[2] == 2000
        assert row[3] == pytest.approx(25.0)  # 100 * (0.75 - 0.50)
