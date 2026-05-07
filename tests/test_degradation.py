"""Tests para DegradationManager."""

import time
import pytest
from src.degradation import (
    DegradationManager,
    DegradationState,
    SystemMode,
    simulate_degradation_scenarios,
)


# ── Fixtures ───────────────────────────────────────────────────────

@pytest.fixture
def dg():
    return DegradationManager(auto_recover=False)


# ── Initial State ──────────────────────────────────────────────────

def test_starts_in_full_mode(dg):
    assert dg.get_mode() == SystemMode.FULL
    assert dg.state.can_trade is True
    assert dg.state.can_market_make is True


# ── Mode Transitions ───────────────────────────────────────────────

def test_set_mode_transitions(dg):
    dg.set_mode(SystemMode.CLOB_WS_DOWN, "test")
    assert dg.get_mode() == SystemMode.CLOB_WS_DOWN


def test_set_mode_same_no_change(dg):
    old_time = dg.state.last_mode_change
    dg.set_mode(SystemMode.FULL, "noop")
    assert dg.state.last_mode_change == old_time


def test_mode_pauses_incompatible_strategies(dg):
    dg.set_mode(SystemMode.CLOB_WS_DOWN)
    assert "market_making" in dg.state.paused_strategies


# ── Capability Properties ──────────────────────────────────────────

def test_can_trade_per_mode(dg):
    dg.set_mode(SystemMode.FULL)
    assert dg.state.can_trade is True

    dg.set_mode(SystemMode.CLOB_WS_DOWN)
    assert dg.state.can_trade is True  # direccionales siguen

    dg.set_mode(SystemMode.MINIMAL)
    assert dg.state.can_trade is False


def test_can_market_make_per_mode(dg):
    dg.set_mode(SystemMode.FULL)
    assert dg.state.can_market_make is True

    dg.set_mode(SystemMode.CLOB_WS_DOWN)
    assert dg.state.can_market_make is False


def test_can_whale_track_per_mode(dg):
    dg.set_mode(SystemMode.POLYGON_RPC_DOWN)
    assert dg.state.can_whale_track is False

    dg.set_mode(SystemMode.FULL)
    assert dg.state.can_whale_track is True


def test_has_order_book_per_mode(dg):
    dg.set_mode(SystemMode.FULL)
    assert dg.state.has_order_book is True

    dg.set_mode(SystemMode.CLOB_WS_DOWN)
    assert dg.state.has_order_book is False


def test_conviction_multiplier_active_per_mode(dg):
    dg.set_mode(SystemMode.POLYGON_RPC_DOWN)
    assert dg.state.conviction_multiplier_active is False

    dg.set_mode(SystemMode.FULL)
    assert dg.state.conviction_multiplier_active is True


# ── Strategy Compatibility ─────────────────────────────────────────

def test_is_strategy_allowed(dg):
    assert dg.is_strategy_allowed("momentum_follow") is True

    dg.set_mode(SystemMode.CLOB_WS_DOWN)
    assert dg.is_strategy_allowed("market_making") is False
    assert dg.is_strategy_allowed("momentum_follow") is True


def test_get_allowed_strategies_filters(dg):
    all_strats = ["momentum", "market_making", "correlation_arb", "whale_follow"]

    dg.set_mode(SystemMode.CLOB_WS_DOWN)
    allowed = dg.get_allowed_strategies(all_strats)
    assert "market_making" not in allowed
    assert "correlation_arb" not in allowed
    assert "momentum" in allowed


# ── Price Source ────────────────────────────────────────────────────

def test_get_price_source(dg):
    assert dg.get_price_source() == "clob_l2"

    dg.set_mode(SystemMode.CLOB_WS_DOWN)
    assert dg.get_price_source() == "gamma"

    dg.set_mode(SystemMode.MINIMAL)
    assert dg.get_price_source() == "gamma"


# ── Conviction Multiplier Override ─────────────────────────────────

def test_get_conviction_multiplier_override(dg):
    dg.set_mode(SystemMode.FULL)
    assert dg.get_conviction_multiplier_override() is None

    dg.set_mode(SystemMode.POLYGON_RPC_DOWN)
    assert dg.get_conviction_multiplier_override() == 1.0


# ── Health Checks ──────────────────────────────────────────────────

def test_register_and_check_health(dg):
    dg.register_health_check("test_component", lambda: True)
    results = dg.check_health()
    assert results["test_component"] is True


def test_health_check_failure(dg):
    dg.register_health_check("bad_component", lambda: False)
    results = dg.check_health()
    assert results["bad_component"] is False


def test_health_check_exception_returns_false(dg):
    def bad_check():
        raise RuntimeError("boom")
    dg.register_health_check("explosive", bad_check)
    results = dg.check_health()
    assert results["explosive"] is False


# ── Auto-Evaluate ──────────────────────────────────────────────────

def test_auto_evaluate_detects_clob_failure():
    dg = DegradationManager(auto_recover=False)
    dg.register_health_check("clob_ws", lambda: False)
    dg.register_health_check("polygon_rpc", lambda: True)
    dg.register_health_check("redis", lambda: True)

    dg.auto_evaluate()
    assert dg.get_mode() == SystemMode.CLOB_WS_DOWN


def test_auto_evaluate_detects_polygon_failure():
    dg = DegradationManager(auto_recover=False)
    dg.register_health_check("clob_ws", lambda: True)
    dg.register_health_check("polygon_rpc", lambda: False)
    dg.register_health_check("redis", lambda: True)

    dg.auto_evaluate()
    assert dg.get_mode() == SystemMode.POLYGON_RPC_DOWN


# ── Metrics ─────────────────────────────────────────────────────────

def test_get_degradation_metrics(dg):
    metrics = dg.get_degradation_metrics()
    assert metrics["mode"] == "full"
    assert metrics["can_trade"] is True
    assert "paused_strategies" in metrics


# ── Degradation Scenarios ──────────────────────────────────────────

def test_simulate_degradation_scenarios():
    scenarios = simulate_degradation_scenarios()
    assert len(scenarios) == 4
    for s in scenarios:
        assert "scenario" in s
        assert "mode" in s
        assert "response" in s


# ── SystemMode Enum ─────────────────────────────────────────────────

def test_system_mode_values():
    assert SystemMode.FULL.value == "full"
    assert SystemMode.CLOB_WS_DOWN.value == "clob_ws_down"
    assert SystemMode.POLYGON_RPC_DOWN.value == "polygon_rpc_down"
    assert SystemMode.REDIS_DOWN.value == "redis_down"
    assert SystemMode.MINIMAL.value == "minimal"
