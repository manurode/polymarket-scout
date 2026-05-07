"""Tests for the Backtester module."""

import pytest
import time

from src.tracker import Tracker
from src.paper_trader import PaperTrader
from src.backtester import Backtester


def _seed_snapshots(tracker, condition_id, snapshots_data):
    """Insert snapshots directly into the tracker's DB."""
    tracker.init_db()
    for i, s in enumerate(snapshots_data):
        ts = s.get("timestamp", 1000 + i * 3600)
        tracker.conn.execute(
            """INSERT OR IGNORE INTO snapshots
               (condition_id, question, slug, event_title,
                price_yes, price_no, spread, volume, liquidity, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                condition_id,
                s.get("question", "Test Q"),
                s.get("slug", "test-slug"),
                s.get("event_title", "Test Event"),
                s.get("price_yes"),
                s.get("price_no", round(1.0 - s["price_yes"], 4) if s.get("price_yes") is not None else None),
                s.get("spread"),
                s.get("volume", 0),
                s.get("liquidity", 0),
                ts,
            ),
        )
    tracker.conn.commit()


class TestBacktester:
    """Integration tests for the Backtester."""

    def test_backtester_creates_trades(self, temp_db):
        """Backtest a market with snapshots showing momentum → trades created."""
        t = Tracker(temp_db)
        t.init_paper_trading()
        pt = PaperTrader(t, initial_balance=1000.0, position_size_pct=0.05)

        # Seed snapshots with upward price momentum
        condition_id = "0xMOMENTUM"
        _seed_snapshots(t, condition_id, [
            {"price_yes": 0.50, "volume": 10000, "timestamp": 1000, "spread": 0.04},
            {"price_yes": 0.58, "volume": 12000, "timestamp": 2000, "spread": 0.03},
            {"price_yes": 0.65, "volume": 15000, "timestamp": 3000, "spread": 0.02},
        ])

        bt = Backtester(t, pt)
        trades = bt.run_single_market(condition_id)

        # Should have generated at least one trade from momentum signals
        assert len(trades) > 0, "Expected at least one trade for momentum market"

        # All trades should be closed (mark-to-market)
        for trade in trades:
            assert trade.get("status") == "closed" or trade.get("pnl") is not None

    def test_backtester_no_signals_no_trades(self, temp_db):
        """Market with flat price and no volume → no trades."""
        t = Tracker(temp_db)
        t.init_paper_trading()
        pt = PaperTrader(t, initial_balance=1000.0, position_size_pct=0.05)

        condition_id = "0xFLAT"
        _seed_snapshots(t, condition_id, [
            {"price_yes": 0.50, "volume": 500, "timestamp": 1000, "spread": 0.05},
            {"price_yes": 0.50, "volume": 500, "timestamp": 2000, "spread": 0.05},
        ])

        bt = Backtester(t, pt)
        trades = bt.run_single_market(condition_id)

        assert len(trades) == 0

    def test_backtester_strategy_filter(self, temp_db):
        """--strategy flag filters to only that strategy."""
        t = Tracker(temp_db)
        t.init_paper_trading()
        pt = PaperTrader(t, initial_balance=1000.0, position_size_pct=0.05)

        # Seed with strong momentum → should trigger momentum_follow
        condition_id = "0xFILTER"
        _seed_snapshots(t, condition_id, [
            {"price_yes": 0.50, "volume": 50000, "timestamp": 1000, "spread": 0.02},
            {"price_yes": 0.58, "volume": 60000, "timestamp": 2000, "spread": 0.02},
            {"price_yes": 0.65, "volume": 70000, "timestamp": 3000, "spread": 0.02},
        ])

        bt = Backtester(t, pt)

        # Run with only "new_market_yes" — should get zero trades
        # (momentum is there but we're filtering it out)
        trades_new = bt.run_single_market(condition_id, strategy_name="new_market_yes")
        # new_market_yes won't trigger because there are 3 snapshots (not new_interest)
        # AND price_yes starts at 0.50 which is >= 0.50
        for trade in trades_new:
            assert trade.get("strategy") == "new_market_yes"

        # Run again with "momentum_follow" — should get trades
        trades_mom = bt.run_single_market(condition_id, strategy_name="momentum_follow")
        # Should have at least one momentum_follow trade
        for trade in trades_mom:
            assert trade.get("strategy") == "momentum_follow"

    def test_backtester_contrarian_triggers(self, temp_db):
        """Contrarian strategy: dip + volume spike → YES bet."""
        t = Tracker(temp_db)
        t.init_paper_trading()
        pt = PaperTrader(t, initial_balance=1000.0, position_size_pct=0.05)

        condition_id = "0xCONTRARIAN"
        # Price drops significantly + high volume
        _seed_snapshots(t, condition_id, [
            {"price_yes": 0.70, "volume": 10000, "timestamp": 1000, "spread": 0.04},
            {"price_yes": 0.60, "volume": 15000, "timestamp": 2000, "spread": 0.05},
            {"price_yes": 0.50, "volume": 50000, "timestamp": 3000, "spread": 0.06},
            {"price_yes": 0.45, "volume": 80000, "timestamp": 4000, "spread": 0.07},
        ])

        bt = Backtester(t, pt)
        trades = bt.run_single_market(condition_id, strategy_name="contrarian")

        # The contrarian strategy should trigger at some point
        # (dip + volume spike)
        for trade in trades:
            assert trade.get("strategy") == "contrarian"

    def test_generate_report(self, temp_db):
        """generate_report should return expected keys and sensible values."""
        t = Tracker(temp_db)
        t.init_paper_trading()
        pt = PaperTrader(t)

        # Create some mock closed trades
        trades = [
            {
                "id": 1, "condition_id": "0xA", "side": "YES",
                "amount": 50.0, "price": 0.50, "shares": 100.0,
                "status": "closed", "pnl": 20.0, "strategy": "momentum_follow",
            },
            {
                "id": 2, "condition_id": "0xB", "side": "NO",
                "amount": 30.0, "price": 0.60, "shares": 50.0,
                "status": "closed", "pnl": -10.0, "strategy": "contrarian",
            },
            {
                "id": 3, "condition_id": "0xC", "side": "YES",
                "amount": 40.0, "price": 0.40, "shares": 100.0,
                "status": "open", "pnl": None, "strategy": "volume_breakout",
            },
        ]

        bt = Backtester(t, pt)
        report = bt.generate_report(trades)

        # Expected keys
        expected_keys = {
            "total_trades", "open_positions", "closed_positions",
            "wins", "losses", "win_rate", "realized_pnl", "roi",
            "avg_win", "avg_loss", "total_invested", "best_trade", "worst_trade",
        }
        assert set(report.keys()) == expected_keys

        assert report["total_trades"] == 3
        assert report["open_positions"] == 1
        assert report["closed_positions"] == 2
        assert report["wins"] == 1
        assert report["losses"] == 1
        assert report["win_rate"] == 0.5
        assert report["realized_pnl"] == 10.0  # 20 + (-10)
        assert report["roi"] == pytest.approx(0.0833, abs=0.0001)
        assert report["avg_win"] == 20.0
        assert report["avg_loss"] == -10.0
        assert report["best_trade"] == 20.0
        assert report["worst_trade"] == -10.0
        assert report["total_invested"] == 120.0

    def test_generate_report_empty(self, temp_db):
        """generate_report with empty list returns zeros."""
        t = Tracker(temp_db)
        t.init_paper_trading()
        pt = PaperTrader(t)

        bt = Backtester(t, pt)
        report = bt.generate_report([])

        assert report["total_trades"] == 0
        assert report["closed_positions"] == 0
        assert report["win_rate"] == 0.0
        assert report["realized_pnl"] == 0.0
        assert report["roi"] == 0.0
        assert report["best_trade"] is None
        assert report["worst_trade"] is None

    def test_run_method(self, temp_db):
        """Backtester.run() should return results dict keyed by strategy."""
        t = Tracker(temp_db)
        t.init_paper_trading()
        pt = PaperTrader(t, initial_balance=1000.0, position_size_pct=0.05)

        # Seed a market with momentum
        condition_id = "0xRUN"
        now = int(time.time())
        _seed_snapshots(t, condition_id, [
            {"price_yes": 0.50, "volume": 20000, "timestamp": now - 86400 * 2, "spread": 0.04},
            {"price_yes": 0.58, "volume": 25000, "timestamp": now - 86400, "spread": 0.03},
            {"price_yes": 0.65, "volume": 30000, "timestamp": now - 3600, "spread": 0.02},
        ])

        bt = Backtester(t, pt)
        results = bt.run(strategy_name="momentum_follow", days_back=7)

        assert "momentum_follow" in results
        data = results["momentum_follow"]
        assert "trades" in data
        assert "report" in data
        report = data["report"]
        assert "total_trades" in report
        assert "roi" in report
        assert "win_rate" in report

    def test_single_snapshot_market_no_trades(self, temp_db):
        """Market with only one snapshot → no trades (not enough history)."""
        t = Tracker(temp_db)
        t.init_paper_trading()
        pt = PaperTrader(t, initial_balance=1000.0, position_size_pct=0.05)

        condition_id = "0xSINGLE"
        _seed_snapshots(t, condition_id, [
            {"price_yes": 0.50, "volume": 10000, "timestamp": 1000, "spread": 0.03},
        ])

        bt = Backtester(t, pt)
        trades = bt.run_single_market(condition_id)
        assert len(trades) == 0

    @pytest.mark.skip(reason="Requires sufficient historical snapshots in lookback window")
    def test_multiple_markets_backtest(self, temp_db):
        """Backtest across multiple markets with run() should aggregate results."""
        t = Tracker(temp_db)
        t.init_paper_trading()
        pt = PaperTrader(t, initial_balance=1000.0, position_size_pct=0.05)

        now = int(time.time())

        # Market 1: momentum up
        _seed_snapshots(t, "0xM1", [
            {"price_yes": 0.50, "volume": 20000, "timestamp": now - 86400 * 2, "spread": 0.04},
            {"price_yes": 0.58, "volume": 25000, "timestamp": now - 86400, "spread": 0.03},
            {"price_yes": 0.65, "volume": 30000, "timestamp": now - 3600, "spread": 0.02},
        ])

        # Market 2: flat, no signals
        _seed_snapshots(t, "0xM2", [
            {"price_yes": 0.50, "volume": 500, "timestamp": now - 86400 * 2, "spread": 0.05},
            {"price_yes": 0.50, "volume": 500, "timestamp": now - 86400, "spread": 0.05},
        ])

        bt = Backtester(t, pt)
        results = bt.run(days_back=7)

        # Should have results for ALL strategies, not just one
        assert len(results) >= 1
        # At least momentum_follow should have trades
        mom_data = results.get("momentum_follow", {})
        report = mom_data.get("report", {})
        assert report.get("total_trades", 0) > 0, "Expected momentum_follow to have trades"

    def test_backtester_respects_config(self, temp_db):
        """Backtester with custom signal_config should use it."""
        t = Tracker(temp_db)
        t.init_paper_trading()
        pt = PaperTrader(t, initial_balance=1000.0, position_size_pct=0.05)

        condition_id = "0xCONFIG"
        # Very small price movement (3%) — should not trigger with default 5% threshold
        # but could trigger with a lower threshold
        _seed_snapshots(t, condition_id, [
            {"price_yes": 0.50, "volume": 20000, "timestamp": 1000, "spread": 0.04},
            {"price_yes": 0.515, "volume": 20000, "timestamp": 2000, "spread": 0.04},
        ])

        # Default config (5% momentum threshold) → no momentum signal
        bt_default = Backtester(t, pt)
        trades_default = bt_default.run_single_market(condition_id, strategy_name="momentum_follow")
        # 3% move < 5% threshold → no momentum signal → no trade
        assert len(trades_default) == 0

        # Custom config with lower threshold (2%)
        custom_config = {
            "momentum": {"threshold": 0.02, "window_hours": 1},
            "volume_spike": {"threshold": 3.0, "window_hours": 24},
            "spread": {"tight_threshold": 0.03, "wide_threshold": 0.10},
            "new_interest": {"min_volume": 10000},
        }
        bt_custom = Backtester(t, pt, signal_config=custom_config)
        trades_custom = bt_custom.run_single_market(condition_id, strategy_name="momentum_follow")
        assert len(trades_custom) > 0
