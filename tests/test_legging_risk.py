"""Tests para LeggingRiskManager y FOK execution."""

import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.legging_risk import (
    LeggingRiskManager,
    LegOrder,
    LegStatus,
    FOKResult,
    PlaceOrderFn,
)
from src.book_analyzer import BookAnalyzer


# ── Fixtures ───────────────────────────────────────────────────────

@pytest.fixture
def book_analyzer():
    ba = BookAnalyzer()
    ba.initialize_book("token_A", {
        "bids": [[0.64, 500.0], [0.63, 300.0]],
        "asks": [[0.66, 200.0], [0.67, 100.0]],
    })
    ba.initialize_book("token_B", {
        "bids": [[0.54, 100.0]],
        "asks": [[0.56, 50.0]],  # menos liquidez → cuello de botella
    })
    return ba


@pytest.fixture
def fok(book_analyzer):
    return LeggingRiskManager(book_analyzer=book_analyzer, timeout_ms=100)


# ── Constructor ────────────────────────────────────────────────────

def test_constructor_defaults():
    fok = LeggingRiskManager()
    assert fok.timeout_ms == 500
    assert fok.max_retries == 3


def test_constructor_custom():
    fok = LeggingRiskManager(timeout_ms=200, max_retries=5)
    assert fok.timeout_ms == 200
    assert fok.max_retries == 5


# ── Bottleneck Identification ──────────────────────────────────────

def test_identify_bottleneck_returns_less_liquid_first(fok):
    leg_a = {"token_id": "token_A", "side": "buy", "size": 50}
    leg_b = {"token_id": "token_B", "side": "sell", "size": 50}

    illiquid, liquid = fok._identify_bottleneck(leg_a, leg_b)
    # token_B tiene menos liquidez → debe ser el primero
    assert illiquid["token_id"] == "token_B"
    assert liquid["token_id"] == "token_A"


def test_identify_bottleneck_equal_liquidity(fok):
    """Si liquidez igual, el primero se considera ilíquido."""
    leg_a = {"token_id": "token_A", "side": "buy", "size": 50}
    leg_b = {"token_id": "token_A", "side": "sell", "size": 50}

    illiquid, liquid = fok._identify_bottleneck(leg_a, leg_b)
    assert illiquid is leg_a
    assert liquid is leg_b


# ── FOK Execution (success) ────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_arbitrage_success(fok):
    """Ambas patas se llenan → éxito."""
    async def mock_place_order(cid, tid, side, size, price):
        return {"order_id": f"order_{cid}", "status": "filled", "filled_size": size}

    result = await fok.execute_arbitrage(
        leg_a={"condition_id": "0xA", "token_id": "token_A", "side": "buy", "size": 50},
        leg_b={"condition_id": "0xB", "token_id": "token_B", "side": "sell", "size": 50},
        place_order=mock_place_order,
    )

    assert result.success is True
    assert len(result.legs) == 2
    assert result.emergency_unwind is False


@pytest.mark.asyncio
async def test_execute_arbitrage_illiquid_fails(fok):
    """Pata ilíquida falla → abortar sin pérdida."""
    async def mock_place_order(cid, tid, side, size, price):
        if "token_B" in tid:
            return {"order_id": "", "status": "failed"}
        return {"order_id": f"order_{cid}", "status": "filled"}

    result = await fok.execute_arbitrage(
        leg_a={"condition_id": "0xA", "token_id": "token_A", "side": "buy", "size": 50},
        leg_b={"condition_id": "0xB", "token_id": "token_B", "side": "sell", "size": 50},
        place_order=mock_place_order,
    )

    assert result.success is False
    assert len(result.legs) <= 1  # solo la pata ilíquida
    assert result.emergency_unwind is False


@pytest.mark.asyncio
async def test_execute_arbitrage_hedge_fails_emergency_unwind(fok):
    """Pata ilíquida OK pero hedge falla → emergency unwind."""
    unwind_calls = []

    async def mock_place_order(cid, tid, side, size, price):
        if "token_B" in tid:
            unwind_calls.append((cid, side, size))
            return {"order_id": "unwind", "status": "filled"}

        if side == "sell":  # la pata líquida (hedge)
            return {"order_id": "", "status": "failed"}
        else:
            return {"order_id": "illiquid_ok", "status": "filled", "filled_size": 50}

    result = await fok.execute_arbitrage(
        leg_a={"condition_id": "0xA", "token_id": "token_A", "side": "sell", "size": 50},
        leg_b={"condition_id": "0xB", "token_id": "token_B", "side": "buy", "size": 50},
        place_order=mock_place_order,
    )

    assert result.success is False
    assert result.emergency_unwind is True
    assert result.unwind_loss > 0
    assert len(unwind_calls) > 0


# ── Capital Lock-Up ────────────────────────────────────────────────

def test_calculate_capital_efficiency_meets_hurdle(fok):
    result = fok.calculate_capital_efficiency(
        gross_profit=50,
        capital_required=100,
        days_to_resolution=30,
        risk_free_rate=0.05,
        risk_premium=0.15,
        hurdle_rate=0.20,
    )
    # annualized = (50/100) * (365/30) = 0.5 * 12.167 = ~6.08 → muy por encima
    assert result["meets_hurdle"] is True
    assert result["annualized_return"] > 1.0


def test_calculate_capital_efficiency_below_hurdle(fok):
    result = fok.calculate_capital_efficiency(
        gross_profit=2,
        capital_required=1000,
        days_to_resolution=365,
        hurdle_rate=0.20,
    )
    # annualized = (2/1000) * (365/365) = 0.002 = 0.2%
    assert result["meets_hurdle"] is False


def test_calculate_capital_efficiency_zero_capital(fok):
    result = fok.calculate_capital_efficiency(
        gross_profit=10,
        capital_required=0,
        days_to_resolution=30,
    )
    assert result["meets_hurdle"] is False
    assert result["annualized_return"] == 0.0


# ── Gas Cost Estimation ────────────────────────────────────────────

def test_estimate_execution_cost(fok):
    cost = fok.estimate_execution_cost(
        gas_estimated=200000,
        base_fee_gwei=50,
        priority_fee_gwei=30,
        pol_price_usd=0.40,
    )
    # (200000 * 80) / 1e9 = 0.016 POL * 0.40 = $0.0064
    assert cost > 0
    assert cost < 0.05  # debe ser barato


def test_should_execute_with_gas_positive(fok):
    result = fok.should_execute_with_gas(
        gross_profit=100,
        capital=1000,
        days=30,
        gas_cost=0.01,
        avg_spread=0.02,
        hurdle_rate=0.10,
    )
    assert result["execute"] is True


def test_should_execute_with_gas_negative(fok):
    """Coste total > beneficio → no ejecutar."""
    result = fok.should_execute_with_gas(
        gross_profit=1,
        capital=1000,
        days=30,
        gas_cost=10,  # gas muy caro
        avg_spread=0.02,
        hurdle_rate=0.10,
    )
    assert result["execute"] is False


# ── LegOrder dataclass ─────────────────────────────────────────────

def test_leg_order_defaults():
    order = LegOrder(condition_id="0xA", token_id="tA", side="buy", size=50)
    assert order.status == LegStatus.PENDING
    assert order.fill_size == 0.0


# ── LegStatus enum ─────────────────────────────────────────────────

def test_leg_status_values():
    assert LegStatus.PENDING.value == "pending"
    assert LegStatus.FILLED.value == "filled"
    assert LegStatus.FAILED.value == "failed"


# ── FOKResult dataclass ────────────────────────────────────────────

def test_fok_result_success():
    result = FOKResult(success=True, legs=[], duration_ms=150.0)
    assert result.success is True
    assert result.duration_ms == 150.0


def test_fok_result_failure():
    result = FOKResult(
        success=False, legs=[],
        error_message="timeout", emergency_unwind=True,
        unwind_loss=5.0, duration_ms=200.0,
    )
    assert result.success is False
    assert result.emergency_unwind is True
    assert result.unwind_loss == 5.0
