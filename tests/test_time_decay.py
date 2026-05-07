"""Tests para TimeDecayManager."""

import time
import pytest
from src.time_decay import (
    TimeDecayManager,
    TimeDecayResult,
    RISK_FLOOR,
    TRANSITION_POINT,
    LIQUIDATION_TAU,
    BASE_INVENTORY_CAP,
)


@pytest.fixture
def tdm():
    return TimeDecayManager()


# ── Tau Calculation ────────────────────────────────────────────────

def test_tau_zero_at_creation(tdm):
    """τ = 0 justo al crearse el mercado."""
    now = 1000.0
    tau = tdm._compute_tau(created_at=1000.0, end_date=2000.0, now=now)
    assert tau == 0.0


def test_tau_halfway(tdm):
    """τ = 0.5 a la mitad de la vida."""
    now = 1500.0
    tau = tdm._compute_tau(created_at=1000.0, end_date=2000.0, now=now)
    assert tau == 0.5


def test_tau_at_expiry(tdm):
    """τ = 1.0 en la fecha de expiración."""
    now = 2000.0
    tau = tdm._compute_tau(created_at=1000.0, end_date=2000.0, now=now)
    assert tau == 1.0


def test_tau_expired(tdm):
    """τ > 1.0 después de expiración."""
    now = 2500.0
    tau = tdm._compute_tau(created_at=1000.0, end_date=2000.0, now=now)
    assert tau > 1.0


def test_tau_invalid_duration(tdm):
    """Duración cero → τ = 1.0."""
    tau = tdm._compute_tau(created_at=1000.0, end_date=1000.0, now=1000.0)
    assert tau == 1.0


def test_tau_before_creation(tdm):
    """τ = 0 si now < created_at."""
    tau = tdm._compute_tau(created_at=2000.0, end_date=3000.0, now=1000.0)
    assert tau == 0.0


# ── Risk Multiplier ────────────────────────────────────────────────

def test_risk_multiplier_before_transition(tdm):
    """Antes del transition point, el multiplicador es 1.0."""
    # τ = 0.5 < 0.70
    mult = tdm.get_risk_multiplier(created_at=0, end_date=1000, now=500)
    assert mult == 1.0


def test_risk_multiplier_at_transition(tdm):
    """Justo en el transition point sigue siendo ~1.0."""
    mult = tdm.get_risk_multiplier(created_at=0, end_date=1000, now=700)
    assert abs(mult - 1.0) < 0.01


def test_risk_multiplier_decreases_after_transition(tdm):
    """Después del transition point, el multiplicador baja."""
    before = tdm.get_risk_multiplier(created_at=0, end_date=1000, now=500)
    after = tdm.get_risk_multiplier(created_at=0, end_date=1000, now=800)
    assert after < before


def test_risk_multiplier_near_expiry(tdm):
    """Cerca de la expiración, el multiplicador se acerca a RISK_FLOOR."""
    mult = tdm.get_risk_multiplier(created_at=0, end_date=1000, now=990)
    assert mult < 0.5
    assert mult >= RISK_FLOOR


def test_risk_multiplier_at_expiry(tdm):
    """En expiración, el multiplicador es RISK_FLOOR."""
    mult = tdm.get_risk_multiplier(created_at=0, end_date=1000, now=1000)
    assert abs(mult - RISK_FLOOR) < 0.01


# ── Evaluate ───────────────────────────────────────────────────────

def test_evaluate_returns_complete_result(tdm):
    result = tdm.evaluate(created_at=0, end_date=1000, now=800, liquidity=20000)

    assert isinstance(result, TimeDecayResult)
    assert 0 <= result.tau <= 1
    assert result.max_inventory_usd > 0
    assert result.time_to_expiry_hours >= 0
    assert isinstance(result.is_liquidation_zone, bool)
    assert isinstance(result.is_transition_zone, bool)


def test_evaluate_transition_zone(tdm):
    """τ > 0.70 → is_transition_zone = True."""
    result = tdm.evaluate(created_at=0, end_date=1000, now=800)
    assert result.is_transition_zone is True
    assert result.is_liquidation_zone is False


def test_evaluate_liquidation_zone(tdm):
    """τ > 0.95 → is_liquidation_zone = True."""
    result = tdm.evaluate(created_at=0, end_date=1000, now=970)
    assert result.is_liquidation_zone is True


def test_evaluate_with_liquidity(tdm):
    """Mercado con poca liquidez → max_inventory_usd reducido."""
    result_high = tdm.evaluate(created_at=0, end_date=1000, now=500, liquidity=50000)
    result_low = tdm.evaluate(created_at=0, end_date=1000, now=500, liquidity=1000)

    assert result_low.max_inventory_usd < result_high.max_inventory_usd


# ── Liquidity Factor ───────────────────────────────────────────────

def test_liquidity_factor_high(tdm):
    """Liquidez >= MIN_LIQUIDITY → factor 1.0."""
    assert tdm.get_liquidity_factor(50000) == 1.0
    assert tdm.get_liquidity_factor(10000) == 1.0


def test_liquidity_factor_low(tdm):
    """Liquidez < MIN_LIQUIDITY → factor proporcional."""
    factor = tdm.get_liquidity_factor(5000)
    assert factor == 0.5


def test_liquidity_factor_zero(tdm):
    """Sin liquidez → factor mínimo 0.1."""
    assert tdm.get_liquidity_factor(0) == 0.1


# ── Liquidation Protocol ───────────────────────────────────────────

def test_should_liquidate(tdm):
    assert tdm.should_liquidate(0.96) is True
    assert tdm.should_liquidate(0.94) is False
    assert tdm.should_liquidate(LIQUIDATION_TAU - 0.01) is False


def test_liquidation_params(tdm):
    params = tdm.get_liquidation_params()
    assert params["mode"] == "close_only"
    assert params["cancel_all_passive"] is True
    assert params["use_market_orders"] is True


# ── Time Decay Scalar ──────────────────────────────────────────────

def test_time_decay_scalar_before_transition(tdm):
    scalar = tdm.get_time_decay_scalar(created_at=0, end_date=1000, now=500)
    assert scalar == 1.0


def test_time_decay_scalar_increases(tdm):
    s1 = tdm.get_time_decay_scalar(created_at=0, end_date=1000, now=800)
    s2 = tdm.get_time_decay_scalar(created_at=0, end_date=1000, now=900)
    assert s2 > s1  # más cerca de expiración → spreads más anchos


def test_time_decay_scalar_range(tdm):
    """El scalar está en [0.8, 3.0]."""
    for progress in [0, 0.5, 0.75, 0.9, 0.99]:
        now = int(progress * 1000)
        scalar = tdm.get_time_decay_scalar(created_at=0, end_date=1000, now=now)
        assert 0.8 <= scalar <= 3.0


# ── Custom Parameters ──────────────────────────────────────────────

def test_custom_base_inventory_cap():
    tdm = TimeDecayManager(base_inventory_cap=1000.0)
    result = tdm.evaluate(created_at=0, end_date=1000, now=500, liquidity=20000)
    assert result.max_inventory_usd > BASE_INVENTORY_CAP  # mayor porque base es mayor


def test_custom_transition_point():
    tdm = TimeDecayManager(transition_point=0.5)
    # A 60% de vida → ya debería haber empezado a decaer
    mult = tdm.get_risk_multiplier(created_at=0, end_date=1000, now=600)
    assert mult < 1.0


# ── Constructor defaults ───────────────────────────────────────────

def test_default_values(tdm):
    assert tdm.base_inventory_cap == BASE_INVENTORY_CAP
    assert tdm.transition_point == TRANSITION_POINT
    assert tdm.risk_floor == RISK_FLOOR
