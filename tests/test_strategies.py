"""Tests for strategy functions."""

import pytest

from src.strategies import (
    STRATEGIES,
    momentum_follow,
    contrarian,
    consensus_breakout,
    volume_breakout,
    new_market_yes,
)


# ---------------------------------------------------------------------------
# Momentum Follow
# ---------------------------------------------------------------------------

class TestMomentumFollow:
    def test_momentum_follow_yes(self):
        """Signals with momentum_up → action YES."""
        signals = [{"signal_type": "momentum_up", "change_pct": 0.15}]
        snapshot = {"price_yes": 0.65}
        result = momentum_follow(signals, snapshot)
        assert result is not None
        assert result["action"] == "YES"
        assert "+15.0%" in result["reason"]

    def test_momentum_follow_no(self):
        """Signals with momentum_down → action NO."""
        signals = [{"signal_type": "momentum_down", "change_pct": -0.10}]
        snapshot = {"price_yes": 0.40}
        result = momentum_follow(signals, snapshot)
        assert result is not None
        assert result["action"] == "NO"
        assert "-10.0%" in result["reason"]

    def test_momentum_follow_none(self):
        """No momentum signals → None."""
        signals = [{"signal_type": "volume_spike", "ratio": 3.5}]
        snapshot = {"price_yes": 0.50}
        result = momentum_follow(signals, snapshot)
        assert result is None

    def test_momentum_follow_empty_signals(self):
        """Empty signals list → None."""
        result = momentum_follow([], {"price_yes": 0.50})
        assert result is None


# ---------------------------------------------------------------------------
# Contrarian
# ---------------------------------------------------------------------------

class TestContrarian:
    def test_contrarian_triggers(self):
        """momentum_down + volume_spike → action YES (buy the dip)."""
        signals = [
            {"signal_type": "momentum_down", "change_pct": -0.08},
            {"signal_type": "volume_spike", "ratio": 4.0},
        ]
        snapshot = {"price_yes": 0.42}
        result = contrarian(signals, snapshot)
        assert result is not None
        assert result["action"] == "YES"
        assert "rebound" in result["reason"]

    def test_contrarian_no_triggers(self):
        """Only momentum_down, no volume → no trigger."""
        signals = [{"signal_type": "momentum_down", "change_pct": -0.08}]
        snapshot = {"price_yes": 0.42}
        result = contrarian(signals, snapshot)
        assert result is None

    def test_contrarian_volume_only_no_triggers(self):
        """Only volume_spike, no momentum → no trigger."""
        signals = [{"signal_type": "volume_spike", "ratio": 4.0}]
        snapshot = {"price_yes": 0.50}
        result = contrarian(signals, snapshot)
        assert result is None


# ---------------------------------------------------------------------------
# Consensus Breakout
# ---------------------------------------------------------------------------

class TestConsensusBreakout:
    def test_consensus_breakout_triggers(self):
        """momentum_up + spread_tight → action YES."""
        signals = [
            {"signal_type": "momentum_up", "change_pct": 0.10},
            {"signal_type": "spread_tight", "spread": 0.01},
        ]
        snapshot = {"price_yes": 0.60}
        result = consensus_breakout(signals, snapshot)
        assert result is not None
        assert result["action"] == "YES"
        assert "consensus" in result["reason"]

    def test_consensus_breakout_no_momentum(self):
        """Only spread_tight → no trigger."""
        signals = [{"signal_type": "spread_tight", "spread": 0.01}]
        snapshot = {"price_yes": 0.60}
        result = consensus_breakout(signals, snapshot)
        assert result is None

    def test_consensus_breakout_no_spread(self):
        """Only momentum_up → no trigger."""
        signals = [{"signal_type": "momentum_up", "change_pct": 0.10}]
        snapshot = {"price_yes": 0.60}
        result = consensus_breakout(signals, snapshot)
        assert result is None


# ---------------------------------------------------------------------------
# Volume Breakout
# ---------------------------------------------------------------------------

class TestVolumeBreakout:
    def test_volume_breakout_triggers(self):
        """volume_spike + spread_wide → action YES."""
        signals = [
            {"signal_type": "volume_spike", "ratio": 5.0},
            {"signal_type": "spread_wide", "spread": 0.12},
        ]
        snapshot = {"price_yes": 0.55}
        result = volume_breakout(signals, snapshot)
        assert result is not None
        assert result["action"] == "YES"
        assert "price discovery" in result["reason"]

    def test_volume_breakout_no_volume(self):
        """Only spread_wide → no trigger."""
        signals = [{"signal_type": "spread_wide", "spread": 0.12}]
        snapshot = {"price_yes": 0.55}
        result = volume_breakout(signals, snapshot)
        assert result is None

    def test_volume_breakout_no_spread(self):
        """Only volume_spike → no trigger."""
        signals = [{"signal_type": "volume_spike", "ratio": 5.0}]
        snapshot = {"price_yes": 0.55}
        result = volume_breakout(signals, snapshot)
        assert result is None


# ---------------------------------------------------------------------------
# New Market YES
# ---------------------------------------------------------------------------

class TestNewMarketYes:
    def test_new_market_yes_triggers(self):
        """new_interest + price < 0.50 → action YES."""
        signals = [{"signal_type": "new_interest", "volume": 15000}]
        snapshot = {"price_yes": 0.35}
        result = new_market_yes(signals, snapshot)
        assert result is not None
        assert result["action"] == "YES"
        assert "0.35" in result["reason"]

    def test_new_market_yes_price_too_high(self):
        """new_interest but price >= 0.50 → no trigger."""
        signals = [{"signal_type": "new_interest", "volume": 15000}]
        snapshot = {"price_yes": 0.60}
        result = new_market_yes(signals, snapshot)
        assert result is None

    def test_new_market_yes_no_signal(self):
        """No new_interest signal → no trigger."""
        signals = [{"signal_type": "momentum_up", "change_pct": 0.10}]
        snapshot = {"price_yes": 0.35}
        result = new_market_yes(signals, snapshot)
        assert result is None

    def test_new_market_yes_price_none(self):
        """Handle None price gracefully."""
        signals = [{"signal_type": "new_interest", "volume": 15000}]
        snapshot = {"price_yes": None}
        result = new_market_yes(signals, snapshot)
        assert result is None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_all_strategies_registered(self):
        """STRATEGIES dict should have all 5 strategies."""
        assert len(STRATEGIES) == 5
        expected_names = {
            "momentum_follow",
            "contrarian",
            "consensus_breakout",
            "volume_breakout",
            "new_market_yes",
        }
        assert set(STRATEGIES.keys()) == expected_names

    def test_strategies_are_callable(self):
        """Every entry in STRATEGIES should be callable."""
        for name, fn in STRATEGIES.items():
            assert callable(fn), f"{name} is not callable"

    def test_all_strategies_return_none_on_empty(self):
        """All strategies should return None when given empty signals."""
        empty_snapshot = {"price_yes": 0.50}
        for name, fn in STRATEGIES.items():
            result = fn([], empty_snapshot)
            assert result is None, f"{name} did not return None for empty signals"
