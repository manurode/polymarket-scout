"""Tests for the AutoTrader module."""

import json
import time as _time
import pytest
from unittest.mock import MagicMock, patch

from src.auto_trader import AutoTrader
from src.strategies import STRATEGIES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_tracker():
    tracker = MagicMock()
    tracker.get_open_positions.return_value = []
    tracker.save_paper_trade.return_value = 1
    tracker.get_portfolio_stats.return_value = {
        "total_trades": 0, "open_positions": 0, "closed_positions": 0,
        "realized_pnl": 0.0, "win_rate": 0.0, "wins": 0, "losses": 0,
        "total_invested_open": 0.0,
    }
    return tracker


def _mock_paper_trader():
    pt = MagicMock()
    def _place_bet(**kwargs):
        return {
            "id": 1, "amount": 50.0, "price": kwargs.get("price", 0.60),
            "side": kwargs.get("side", "YES"),
            "condition_id": kwargs.get("condition_id", "0xabc"),
            "question": kwargs.get("question", "Will X happen?"),
            "strategy": kwargs.get("strategy", "momentum_follow"),
            "shares": 83.33, "status": "open",
            "entry_timestamp": kwargs.get("timestamp", 1715000000),
        }
    pt.place_bet.side_effect = _place_bet
    pt.close_position.return_value = {
        "id": 1, "amount": 50.0, "price": 0.60, "side": "YES",
        "condition_id": "0xabc", "pnl": 15.0, "status": "closed",
    }
    return pt


def _snapshot():
    return {
        "condition_id": "0xabc123",
        "question": "Will X happen?",
        "price_yes": 0.60,
        "price_no": 0.40,
        "timestamp": 1715000000,
    }


def _momentum_up_signal():
    return {"signal_type": "momentum_up", "change_pct": 0.20, "weight": 20}


# ---------------------------------------------------------------------------
# evaluate_and_trade
# ---------------------------------------------------------------------------

class TestEvaluateAndTrade:

    def test_places_trade_when_strategy_triggers(self):
        tracker = _mock_tracker()
        pt = _mock_paper_trader()
        at = AutoTrader(tracker, pt, auto_config={"enabled": True, "min_score": 30})

        result = at.evaluate_and_trade(
            [_momentum_up_signal()], _snapshot(), score=60,
        )

        assert result is not None
        assert result["side"] == "YES"
        assert result["strategy"] == "momentum_follow"
        assert at.trades_placed_this_cycle == 1

    def test_skips_when_already_open(self):
        tracker = _mock_tracker()
        tracker.get_open_positions.return_value = [
            {"condition_id": "0xabc123", "side": "YES"},
        ]
        pt = _mock_paper_trader()
        at = AutoTrader(tracker, pt)

        result = at.evaluate_and_trade(
            [_momentum_up_signal()], _snapshot(), score=60,
        )

        assert result is None
        assert at.trades_placed_this_cycle == 0

    def test_skips_when_max_open_reached(self):
        tracker = _mock_tracker()
        tracker.get_open_positions.return_value = [
            {"condition_id": f"0x{i}"} for i in range(5)
        ]
        pt = _mock_paper_trader()
        at = AutoTrader(tracker, pt, auto_config={"max_open_positions": 5, "min_score": 30})

        result = at.evaluate_and_trade(
            [_momentum_up_signal()], _snapshot(), score=60,
        )

        assert result is None

    def test_skips_below_min_score(self):
        tracker = _mock_tracker()
        pt = _mock_paper_trader()
        at = AutoTrader(tracker, pt, auto_config={"min_score": 50})

        result = at.evaluate_and_trade(
            [_momentum_up_signal()], _snapshot(), score=30,
        )

        assert result is None

    def test_no_signals_no_trade(self):
        tracker = _mock_tracker()
        pt = _mock_paper_trader()
        at = AutoTrader(tracker, pt)

        result = at.evaluate_and_trade([], _snapshot(), score=50)

        assert result is None

    def test_unknown_strategy_skipped(self):
        tracker = _mock_tracker()
        pt = _mock_paper_trader()
        at = AutoTrader(tracker, pt, auto_config={
            "enabled_strategies": ["nonexistent"],
            "min_score": 30,
        })

        result = at.evaluate_and_trade(
            [_momentum_up_signal()], _snapshot(), score=60,
        )

        assert result is None

    def test_disabled_strategy_not_used(self):
        tracker = _mock_tracker()
        pt = _mock_paper_trader()
        at = AutoTrader(tracker, pt, auto_config={
            "enabled_strategies": ["contrarian"],
            "min_score": 30,
        })

        result = at.evaluate_and_trade(
            [_momentum_up_signal()], _snapshot(), score=60,
        )

        assert result is None

    def test_contrarian_triggers(self):
        tracker = _mock_tracker()
        pt = _mock_paper_trader()
        at = AutoTrader(tracker, pt, auto_config={
            "enabled_strategies": ["contrarian"],
            "min_score": 30,
        })

        signals = [
            {"signal_type": "momentum_down", "change_pct": -0.15, "weight": 20},
            {"signal_type": "volume_spike", "change_pct": 3.0, "weight": 20},
        ]
        result = at.evaluate_and_trade(signals, _snapshot(), score=55)

        assert result is not None
        assert result["strategy"] == "contrarian"
        assert result["side"] == "YES"

    def test_consensus_breakout_triggers(self):
        tracker = _mock_tracker()
        pt = _mock_paper_trader()
        at = AutoTrader(tracker, pt, auto_config={
            "enabled_strategies": ["consensus_breakout"],
            "min_score": 30,
        })

        signals = [
            {"signal_type": "momentum_up", "change_pct": 0.10, "weight": 20},
            {"signal_type": "spread_tight", "weight": 15},
        ]
        result = at.evaluate_and_trade(signals, _snapshot(), score=50)

        assert result is not None
        assert result["strategy"] == "consensus_breakout"
        assert result["side"] == "YES"


# ---------------------------------------------------------------------------
# check_close_conditions
# ---------------------------------------------------------------------------

class TestCheckCloseConditions:

    def test_take_profit_closes_position(self):
        tracker = _mock_tracker()
        tracker.get_open_positions.return_value = [
            {"id": 1, "condition_id": "0xabc", "side": "YES",
             "amount": 50.0, "price": 0.60, "shares": 83.33,
             "question": "Will X happen?", "entry_timestamp": 1715000000},
        ]
        tracker.conn.execute.return_value.fetchall.return_value = [
            (0.75, 0.25, 1715001000),
        ]
        pt = _mock_paper_trader()
        at = AutoTrader(tracker, pt, auto_config={
            "exit_strategy": {"take_profit_pct": 0.10, "stop_loss_pct": 0.20,
                              "max_hold_hours": 48},
        })

        closed = at.check_close_conditions()

        assert len(closed) == 1
        assert at.trades_closed_this_cycle == 1
        pt.close_position.assert_called_once()

    def test_stop_loss_closes_position(self):
        tracker = _mock_tracker()
        tracker.get_open_positions.return_value = [
            {"id": 1, "condition_id": "0xabc", "side": "YES",
             "amount": 50.0, "price": 0.60, "shares": 83.33,
             "question": "Will X happen?", "entry_timestamp": 1715000000},
        ]
        tracker.conn.execute.return_value.fetchall.return_value = [
            (0.40, 0.60, 1715001000),
        ]
        pt = _mock_paper_trader()
        at = AutoTrader(tracker, pt, auto_config={
            "exit_strategy": {"take_profit_pct": 0.10, "stop_loss_pct": 0.20,
                              "max_hold_hours": 48},
        })

        closed = at.check_close_conditions()

        assert len(closed) == 1
        assert at.trades_closed_this_cycle == 1

    def test_no_close_when_in_range(self):
        tracker = _mock_tracker()
        one_hour_ago = int(_time.time()) - 3600
        tracker.get_open_positions.return_value = [
            {"id": 1, "condition_id": "0xabc", "side": "YES",
             "amount": 50.0, "price": 0.60, "shares": 83.33,
             "question": "Will X happen?", "entry_timestamp": one_hour_ago},
        ]
        tracker.conn.execute.return_value.fetchall.return_value = [
            (0.63, 0.37, 1715001000),
        ]
        pt = _mock_paper_trader()
        at = AutoTrader(tracker, pt, auto_config={
            "exit_strategy": {"take_profit_pct": 0.10, "stop_loss_pct": 0.20,
                              "max_hold_hours": 48},
        })

        closed = at.check_close_conditions()

        assert len(closed) == 0
        pt.close_position.assert_not_called()

    def test_trailing_stop_activates(self):
        tracker = _mock_tracker()
        tracker.get_open_positions.return_value = [
            {"id": 1, "condition_id": "0xabc", "side": "YES",
             "amount": 50.0, "price": 0.60, "shares": 83.33,
             "question": "Will X happen?", "entry_timestamp": 1715000000},
        ]
        tracker.conn.execute.return_value.fetchall.return_value = [
            (0.588, 0.412, 1715001000),
        ]
        pt = _mock_paper_trader()
        at = AutoTrader(tracker, pt, auto_config={
            "exit_strategy": {
                "take_profit_pct": 0.15,
                "stop_loss_pct": 0.20,
                "trailing_activate_pct": 0.08,
                "trailing_sl_pct": 0.0,
                "max_hold_hours": 48,
            },
        })

        closed = at.check_close_conditions()

        assert len(closed) == 1

    def test_time_based_close(self):
        two_days_ago = int(_time.time()) - 50 * 3600
        tracker = _mock_tracker()
        tracker.get_open_positions.return_value = [
            {"id": 1, "condition_id": "0xabc", "side": "YES",
             "amount": 50.0, "price": 0.60, "shares": 83.33,
             "question": "Will X happen?", "entry_timestamp": two_days_ago},
        ]
        tracker.conn.execute.return_value.fetchall.return_value = [
            (0.61, 0.39, 1715001000),
        ]
        pt = _mock_paper_trader()
        at = AutoTrader(tracker, pt, auto_config={
            "exit_strategy": {
                "take_profit_pct": 0.10,
                "stop_loss_pct": 0.20,
                "max_hold_hours": 48,
            },
        })

        closed = at.check_close_conditions()

        assert len(closed) == 1
        assert at.trades_closed_this_cycle == 1

    def test_closes_on_market_resolution(self):
        tracker = _mock_tracker()
        tracker.get_open_positions.return_value = [
            {"id": 1, "condition_id": "0xabc", "side": "YES",
             "amount": 50.0, "price": 0.60, "shares": 83.33,
             "question": "Will X happen?", "entry_timestamp": 1715000000},
        ]
        tracker.conn.execute.return_value.fetchall.return_value = [
            (0.999, 0.001, 1715001000),
        ]
        pt = _mock_paper_trader()
        at = AutoTrader(tracker, pt)

        closed = at.check_close_conditions()

        assert len(closed) == 1


# ---------------------------------------------------------------------------
# cycle_summary
# ---------------------------------------------------------------------------

class TestCycleSummary:

    def test_summary_with_trades(self):
        tracker = _mock_tracker()
        pt = _mock_paper_trader()
        at = AutoTrader(tracker, pt)
        at.trades_placed_this_cycle = 2
        at.trades_closed_this_cycle = 1

        summary = at.cycle_summary()

        assert "2 trade(s) abierto(s)" in summary
        assert "1 trade(s) cerrado(s)" in summary

    def test_summary_no_activity(self):
        tracker = _mock_tracker()
        pt = _mock_paper_trader()
        at = AutoTrader(tracker, pt)

        summary = at.cycle_summary()

        assert "Sin actividad" in summary

    def test_summary_open_positions_unchanged(self):
        tracker = _mock_tracker()
        tracker.get_open_positions.return_value = [
            {"id": 1}, {"id": 2},
        ]
        pt = _mock_paper_trader()
        at = AutoTrader(tracker, pt)

        summary = at.cycle_summary()

        assert "2 posiciones abiertas" in summary
