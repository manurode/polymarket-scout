"""Tests for the PaperTrader module."""

import pytest
import time

from src.tracker import Tracker
from src.paper_trader import PaperTrader


class TestInitPaperTrading:
    """Tests for Tracker.init_paper_trading."""

    def test_init_paper_trading_creates_table(self, temp_db):
        """After init_paper_trading the paper_trades table must exist."""
        t = Tracker(temp_db)
        t.init_paper_trading()

        tables = temp_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = [r[0] for r in tables]
        assert "paper_trades" in table_names


class TestSavePaperTrade:
    """Tests for Tracker.save_paper_trade."""

    def test_save_paper_trade(self, temp_db):
        """Insert a trade and verify it is persisted."""
        t = Tracker(temp_db)
        t.init_paper_trading()

        trade = {
            "condition_id": "0xabc",
            "question": "Will it rain?",
            "side": "YES",
            "amount": 50.0,
            "price": 0.65,
            "shares": 76.92,
            "signal_type": "momentum",
            "score": 75,
            "status": "open",
            "entry_timestamp": int(time.time()),
            "close_price": None,
            "close_timestamp": None,
            "pnl": None,
            "strategy": "momentum",
        }
        trade_id = t.save_paper_trade(trade)
        assert trade_id is not None
        assert trade_id > 0

        row = temp_db.execute(
            "SELECT * FROM paper_trades WHERE id = ?", (trade_id,)
        ).fetchone()
        assert row is not None
        assert row[1] == "0xabc"  # condition_id
        assert row[3] == "YES"    # side
        assert row[9] == "open"   # status (index 9: after signal_type, score)


class TestGetOpenPositions:
    """Tests for Tracker.get_open_positions."""

    def test_get_open_positions(self, temp_db):
        """Only open trades should be returned."""
        t = Tracker(temp_db)
        t.init_paper_trading()

        now = int(time.time())
        t.save_paper_trade({
            "condition_id": "0x1", "question": "Q1", "side": "YES",
            "amount": 50.0, "price": 0.60, "shares": 83.33,
            "signal_type": None, "score": None, "status": "open",
            "entry_timestamp": now,
            "close_price": None, "close_timestamp": None,
            "pnl": None, "strategy": None,
        })
        t.save_paper_trade({
            "condition_id": "0x2", "question": "Q2", "side": "NO",
            "amount": 30.0, "price": 0.40, "shares": 75.0,
            "signal_type": None, "score": None, "status": "closed",
            "entry_timestamp": now - 100,
            "close_price": 0.35, "close_timestamp": now - 50,
            "pnl": 3.75, "strategy": None,
        })

        open_positions = t.get_open_positions()
        assert len(open_positions) == 1
        assert open_positions[0]["condition_id"] == "0x1"


class TestClosePosition:
    """Tests for Tracker.close_position."""

    def test_close_position_yes_profit(self, temp_db):
        """Buy YES at 0.50, close at 0.70 → P&L positive."""
        t = Tracker(temp_db)
        t.init_paper_trading()

        trade_id = t.save_paper_trade({
            "condition_id": "0xY", "question": "Test", "side": "YES",
            "amount": 50.0, "price": 0.50, "shares": 100.0,
            "signal_type": None, "score": None, "status": "open",
            "entry_timestamp": 1000,
            "close_price": None, "close_timestamp": None,
            "pnl": None, "strategy": "test",
        })

        ok = t.close_position(trade_id, 0.70, 2000)
        assert ok is True

        row = temp_db.execute(
            "SELECT status, close_price, close_timestamp, pnl FROM paper_trades WHERE id = ?",
            (trade_id,),
        ).fetchone()
        assert row[0] == "closed"
        assert row[1] == 0.70
        assert row[2] == 2000
        assert row[3] == pytest.approx(20.0)  # 100 * (0.70 - 0.50) = 20

    def test_close_position_yes_loss(self, temp_db):
        """Buy YES at 0.50, close at 0.30 → P&L negative."""
        t = Tracker(temp_db)
        t.init_paper_trading()

        trade_id = t.save_paper_trade({
            "condition_id": "0xY2", "question": "Test", "side": "YES",
            "amount": 50.0, "price": 0.50, "shares": 100.0,
            "signal_type": None, "score": None, "status": "open",
            "entry_timestamp": 1000,
            "close_price": None, "close_timestamp": None,
            "pnl": None, "strategy": "test",
        })

        ok = t.close_position(trade_id, 0.30, 2000)
        assert ok is True

        row = temp_db.execute(
            "SELECT pnl FROM paper_trades WHERE id = ?", (trade_id,)
        ).fetchone()
        assert row[0] == pytest.approx(-20.0)  # 100 * (0.30 - 0.50) = -20

    def test_close_position_no_profit(self, temp_db):
        """Buy NO at 0.70, close at 0.50 → P&L positive (NO wins when price drops)."""
        t = Tracker(temp_db)
        t.init_paper_trading()

        trade_id = t.save_paper_trade({
            "condition_id": "0xN", "question": "Test", "side": "NO",
            "amount": 70.0, "price": 0.70, "shares": 100.0,
            "signal_type": None, "score": None, "status": "open",
            "entry_timestamp": 1000,
            "close_price": None, "close_timestamp": None,
            "pnl": None, "strategy": "test",
        })

        ok = t.close_position(trade_id, 0.50, 2000)
        assert ok is True

        row = temp_db.execute(
            "SELECT pnl FROM paper_trades WHERE id = ?", (trade_id,)
        ).fetchone()
        # NO side: pnl = shares * (entry_price - close_price) = 100 * (0.70 - 0.50) = 20
        assert row[0] == pytest.approx(20.0)

    def test_close_position_not_found(self, temp_db):
        """Closing a non-existent trade returns False."""
        t = Tracker(temp_db)
        t.init_paper_trading()
        ok = t.close_position(9999, 0.50, 1000)
        assert ok is False

    def test_close_position_already_closed(self, temp_db):
        """Closing an already-closed trade returns False."""
        t = Tracker(temp_db)
        t.init_paper_trading()

        trade_id = t.save_paper_trade({
            "condition_id": "0xC", "question": "Test", "side": "YES",
            "amount": 50.0, "price": 0.50, "shares": 100.0,
            "signal_type": None, "score": None, "status": "closed",
            "entry_timestamp": 1000,
            "close_price": 0.60, "close_timestamp": 2000,
            "pnl": 10.0, "strategy": "test",
        })

        ok = t.close_position(trade_id, 0.70, 3000)
        assert ok is False


class TestPaperTrader:
    """Tests for the PaperTrader engine."""

    def test_place_bet_amount_calculation(self, temp_db):
        """Place bet should use balance * position_size_pct."""
        t = Tracker(temp_db)
        t.init_paper_trading()
        pt = PaperTrader(t, initial_balance=1000.0, position_size_pct=0.05)

        trade = pt.place_bet(
            condition_id="0xT1",
            question="Test Q",
            side="YES",
            price=0.60,
            signal_type="momentum",
            score=75,
            strategy="momentum",
        )

        assert trade["amount"] == pytest.approx(50.0)  # 1000 * 0.05
        assert trade["side"] == "YES"
        assert trade["status"] == "open"
        assert trade["id"] > 0

    def test_place_bet_shares_calculation(self, temp_db):
        """Shares = amount / price."""
        t = Tracker(temp_db)
        t.init_paper_trading()
        pt = PaperTrader(t, initial_balance=1000.0, position_size_pct=0.05)

        trade = pt.place_bet(
            condition_id="0xT2",
            question="Test Q",
            side="NO",
            price=0.40,
        )

        assert trade["amount"] == 50.0
        assert trade["shares"] == pytest.approx(125.0)  # 50 / 0.40 = 125

    def test_balance_decreases_after_bet(self, temp_db):
        """Available balance should decrease after placing a bet."""
        t = Tracker(temp_db)
        t.init_paper_trading()
        pt = PaperTrader(t, initial_balance=1000.0, position_size_pct=0.05)

        assert pt.get_balance() == 1000.0

        pt.place_bet(condition_id="0xB1", question="Q", side="YES", price=0.50)

        # After one bet, balance should be 1000 - 50 = 950
        assert pt.get_balance() == pytest.approx(950.0)

        pt.place_bet(condition_id="0xB2", question="Q2", side="NO", price=0.50)

        # After two bets: 1000 - 50 - 47.5 = 902.5
        # (second bet: 950 * 0.05 = 47.5)
        assert pt.get_balance() == pytest.approx(902.5)

    def test_close_position_returns_updated_trade(self, temp_db):
        """PaperTrader.close_position should return the trade with P&L."""
        t = Tracker(temp_db)
        t.init_paper_trading()
        pt = PaperTrader(t, initial_balance=1000.0, position_size_pct=0.05)

        trade = pt.place_bet(
            condition_id="0xCP",
            question="Close test",
            side="YES",
            price=0.50,
            timestamp=1000,
        )

        closed = pt.close_position(trade["id"], 0.80, timestamp=2000)
        assert closed is not None
        assert closed["status"] == "closed"
        assert closed["close_price"] == 0.80
        # YES: pnl = shares * (0.80 - 0.50) = 100 * 0.30 = 30
        assert closed["pnl"] == pytest.approx(30.0)

    def test_close_position_nonexistent(self, temp_db):
        """Closing a non-existent trade returns None."""
        t = Tracker(temp_db)
        t.init_paper_trading()
        pt = PaperTrader(t)
        result = pt.close_position(99999, 0.50)
        assert result is None

    def test_portfolio_stats(self, temp_db):
        """get_portfolio should return correct stats."""
        t = Tracker(temp_db)
        t.init_paper_trading()
        pt = PaperTrader(t, initial_balance=1000.0, position_size_pct=0.05)

        # Place two trades
        t1 = pt.place_bet(condition_id="0xS1", question="Q1", side="YES", price=0.50, timestamp=1000)
        t2 = pt.place_bet(condition_id="0xS2", question="Q2", side="NO", price=0.60, timestamp=1000)

        portfolio = pt.get_portfolio()
        # position_size_pct applies to current balance, not initial:
        # Trade 1: 1000 * 0.05 = 50.00 → balance 950
        # Trade 2: 950 * 0.05 = 47.50 → balance 902.50
        assert portfolio["balance"] == pytest.approx(902.5)
        assert portfolio["initial_balance"] == 1000.0
        assert len(portfolio["open_positions"]) == 2
        assert portfolio["stats"]["total_trades"] == 2
        assert portfolio["stats"]["open_positions"] == 2
        assert portfolio["stats"]["closed_positions"] == 0
        assert portfolio["stats"]["realized_pnl"] == 0.0

        # Close t1 with profit
        pt.close_position(t1["id"], 0.70, timestamp=2000)

        portfolio2 = pt.get_portfolio()
        assert portfolio2["stats"]["closed_positions"] == 1
        assert portfolio2["stats"]["realized_pnl"] == pytest.approx(20.0)  # 100 * 0.20
        assert portfolio2["stats"]["wins"] == 1

    def test_get_paper_trades_filtered_by_strategy(self, temp_db):
        """get_paper_trades should filter by strategy."""
        t = Tracker(temp_db)
        t.init_paper_trading()
        pt = PaperTrader(t)

        pt.place_bet(condition_id="0xF1", question="Q1", side="YES", price=0.50, strategy="momentum")
        pt.place_bet(condition_id="0xF2", question="Q2", side="NO", price=0.60, strategy="volume")

        momentum_trades = t.get_paper_trades(strategy="momentum")
        assert len(momentum_trades) == 1
        assert momentum_trades[0]["strategy"] == "momentum"

        all_trades = t.get_paper_trades()
        assert len(all_trades) == 2

    def test_place_bet_insufficient_balance(self, temp_db):
        """Place bet with zero balance should raise ValueError."""
        t = Tracker(temp_db)
        t.init_paper_trading()
        pt = PaperTrader(t, initial_balance=0.0, position_size_pct=0.05)

        with pytest.raises(ValueError, match="Insufficient balance"):
            pt.place_bet(condition_id="0xZ", question="Q", side="YES", price=0.50)

    def test_place_bet_zero_price(self, temp_db):
        """Place bet with zero price should raise ValueError."""
        t = Tracker(temp_db)
        t.init_paper_trading()
        pt = PaperTrader(t)

        with pytest.raises(ValueError, match="price must be greater than 0"):
            pt.place_bet(condition_id="0xZ", question="Q", side="YES", price=0.0)

    def test_close_position_uses_current_timestamp(self, temp_db):
        """When no timestamp given, close_position uses current time."""
        t = Tracker(temp_db)
        t.init_paper_trading()
        pt = PaperTrader(t)

        before = int(time.time())
        trade = pt.place_bet(condition_id="0xTS", question="Q", side="YES", price=0.50)
        result = pt.close_position(trade["id"], 0.60)
        after = int(time.time())

        assert result is not None
        assert before <= result["close_timestamp"] <= after
