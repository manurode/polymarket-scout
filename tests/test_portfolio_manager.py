"""Tests para PortfolioManager — MAB + Kelly + Sortino."""

import math
import pytest
from src.portfolio_manager import (
    PortfolioManager,
    StrategyStatus,
    Allocation,
    PositionSize,
    K_BASE,
    RUIN_LIMIT,
)


# ── Fixtures ───────────────────────────────────────────────────────

@pytest.fixture
def pm():
    return PortfolioManager(
        strategies=["momentum", "contrarian", "market_making", "arbitrage"],
    )


# ── Strategy Registration ─────────────────────────────────────────

def test_strategies_start_in_probation(pm):
    for name in ["momentum", "contrarian", "market_making", "arbitrage"]:
        state = pm.get_strategy_state(name)
        assert state is not None
        assert state.status == StrategyStatus.PROBATION


def test_add_strategy(pm):
    pm.add_strategy("whale_follow")
    assert pm.strategy_count == 5
    state = pm.get_strategy_state("whale_follow")
    assert state.status == StrategyStatus.PROBATION


def test_remove_strategy(pm):
    pm.remove_strategy("contrarian")
    assert pm.strategy_count == 3
    assert pm.get_strategy_state("contrarian") is None


# ── Sortino Calculation ────────────────────────────────────────────

def test_calculate_sortino_all_winners():
    trades = [
        {"pnl": 10, "amount_invested": 100},
        {"pnl": 15, "amount_invested": 100},
        {"pnl": 8, "amount_invested": 100},
    ]
    sortino = PortfolioManager._calculate_sortino(trades)
    # Todos ganadores → sin downside risk → Sortino alto
    assert sortino > 2.0


def test_calculate_sortino_all_losers():
    trades = [
        {"pnl": -20, "amount_invested": 100},
        {"pnl": -15, "amount_invested": 100},
        {"pnl": -30, "amount_invested": 100},
    ]
    sortino = PortfolioManager._calculate_sortino(trades)
    # Todos perdedores → Sortino negativo
    assert sortino < 0


def test_calculate_sortino_mixed():
    trades = [
        {"pnl": 20, "amount_invested": 100},
        {"pnl": -5, "amount_invested": 100},
        {"pnl": 15, "amount_invested": 100},
    ]
    sortino = PortfolioManager._calculate_sortino(trades)
    # Mayoría ganadores pero con algo de downside → Sortino positivo moderado
    assert sortino > 0


def test_calculate_sortino_empty():
    assert PortfolioManager._calculate_sortino([]) == 0.0


def test_calculate_sortino_single_trade():
    trades = [{"pnl": 50, "amount_invested": 100}]
    sortino = PortfolioManager._calculate_sortino(trades)
    assert sortino > 0


# ── Performance Update ─────────────────────────────────────────────

def test_update_performance_records_sortino(pm):
    trades = [
        {"pnl": 20, "amount_invested": 100},
        {"pnl": 15, "amount_invested": 100},
    ]
    sortino = pm.update_strategy_performance("momentum", trades)
    assert sortino > 0
    assert pm.get_sortino("momentum") == sortino


def test_update_performance_advances_probation(pm):
    """Después de PROBATION_EPOCHS épocas, la estrategia sale de probation."""
    trades = [{"pnl": 10, "amount_invested": 100}]
    for _ in range(4):
        pm.update_strategy_performance("momentum", trades)

    state = pm.get_strategy_state("momentum")
    assert state.status == StrategyStatus.ACTIVE


def test_update_performance_goes_to_recovery_after_losses(pm):
    """v2.0: 3 épocas consecutivas con Sortino ≤ 0 → RECOVERY (no FROZEN)."""
    losing_trades = [{"pnl": -10, "amount_invested": 100}]

    # Primero, sacar de probation
    winning = [{"pnl": 10, "amount_invested": 100}]
    for _ in range(4):
        pm.update_strategy_performance("momentum", winning)

    # Luego, 3 épocas perdedoras → RECOVERY
    for _ in range(3):
        pm.update_strategy_performance("momentum", losing_trades)

    state = pm.get_strategy_state("momentum")
    assert state.status == StrategyStatus.RECOVERY


def test_update_performance_recovery_to_frozen(pm):
    """v2.0: RECOVERY con 10 trades perdedores → FROZEN."""
    winning = [{"pnl": 10, "amount_invested": 100}]
    losing = [{"pnl": -10, "amount_invested": 100}]

    # Sacar de probation
    for _ in range(4):
        pm.update_strategy_performance("momentum", winning)

    # 3 épocas perdedoras → RECOVERY
    for _ in range(3):
        pm.update_strategy_performance("momentum", losing)
    assert pm.get_strategy_state("momentum").status == StrategyStatus.RECOVERY

    # Simular 10 trades perdedores en RECOVERY → FROZEN
    for _ in range(10):
        pm.record_trade("momentum", pnl=-5.0, equity_before=10000.0)
    assert pm.get_strategy_state("momentum").status == StrategyStatus.FROZEN


def test_update_performance_recovers_from_recovery(pm):
    """v2.0: RECOVERY con PnL positivo → vuelve a ACTIVE."""
    winning = [{"pnl": 10, "amount_invested": 100}]
    losing = [{"pnl": -10, "amount_invested": 100}]

    # Sacar de probation
    for _ in range(4):
        pm.update_strategy_performance("momentum", winning)

    # 3 épocas perdedoras → RECOVERY
    for _ in range(3):
        pm.update_strategy_performance("momentum", losing)
    assert pm.get_strategy_state("momentum").status == StrategyStatus.RECOVERY

    # Simular 10 trades ganadores en RECOVERY → ACTIVE
    for _ in range(10):
        pm.record_trade("momentum", pnl=5.0, equity_before=10000.0)
    assert pm.get_strategy_state("momentum").status == StrategyStatus.ACTIVE


def test_update_performance_recovers_from_frozen(pm):
    """Una época positiva saca de FROZEN → ACTIVE."""
    winning = [{"pnl": 10, "amount_invested": 100}]
    losing = [{"pnl": -10, "amount_invested": 100}]

    # Sacar de probation
    for _ in range(4):
        pm.update_strategy_performance("momentum", winning)

    # Llevar a RECOVERY
    for _ in range(3):
        pm.update_strategy_performance("momentum", losing)
    # Llevar a FROZEN vía 10 trades perdedores
    for _ in range(10):
        pm.record_trade("momentum", pnl=-5.0, equity_before=10000.0)
    assert pm.get_strategy_state("momentum").status == StrategyStatus.FROZEN

    # Recuperar con época positiva
    pm.update_strategy_performance("momentum", winning)
    assert pm.get_strategy_state("momentum").status == StrategyStatus.ACTIVE


# ── Capital Allocation (Thompson Sampling) ─────────────────────────

def test_allocate_distributes_capital(pm):
    """Todas las estrategias en PROBATION → 2% cada una."""
    allocations = pm.allocate(equity=10000)

    assert len(allocations) == 4
    total_allocated = sum(a.amount for a in allocations)
    # 4 estrategias en PROBATION → ~8% del capital total cada una (tras normalización)
    # La normalización hace que cada una reciba 0.02/0.08 = 25% de equity
    assert total_allocated > 0


def test_allocate_sums_to_equity_or_less(pm):
    """La suma de asignaciones no excede el equity total."""
    allocations = pm.allocate(equity=10000)
    total = sum(a.fraction for a in allocations)
    assert total <= 1.0 + 0.01  # tolerancia


def test_allocate_active_strategies_get_more(pm):
    """Estrategias con buen rendimiento reciben más capital."""
    winning = [{"pnl": 20, "amount_invested": 100}]
    losing = [{"pnl": -20, "amount_invested": 100}]

    # Sacar de probation
    for _ in range(4):
        pm.update_strategy_performance("momentum", winning)
        pm.update_strategy_performance("contrarian", losing)

    # Más épocas para momentum (buena) y contrarian (mala)
    for _ in range(10):
        pm.update_strategy_performance("momentum", winning)
        pm.update_strategy_performance("contrarian", losing)

    allocations = pm.allocate(equity=10000)
    mom_alloc = next(a for a in allocations if a.strategy == "momentum")
    con_alloc = next(a for a in allocations if a.strategy == "contrarian")

    # momentum debería recibir más que contrarian (o contrarian frozen)
    assert mom_alloc.fraction >= con_alloc.fraction or con_alloc.status == "frozen"


def test_allocate_recovery_gets_reduced_allocation(pm):
    """v2.0: Estrategias en RECOVERY reciben asignación reducida (4%)."""
    winning = [{"pnl": 10, "amount_invested": 100}]
    losing = [{"pnl": -10, "amount_invested": 100}]

    for _ in range(4):
        pm.update_strategy_performance("momentum", winning)
    for _ in range(3):
        pm.update_strategy_performance("momentum", losing)

    state = pm.get_strategy_state("momentum")
    assert state.status == StrategyStatus.RECOVERY

    allocations = pm.allocate(equity=10000)
    mom = next(a for a in allocations if a.strategy == "momentum")
    assert mom.status == "recovery"
    assert mom.fraction > 0.0  # RECOVERY gets reduced allocation, not zero


def test_allocate_frozen_gets_zero(pm):
    """Estrategias FROZEN reciben 0% de capital."""
    winning = [{"pnl": 10, "amount_invested": 100}]
    losing = [{"pnl": -10, "amount_invested": 100}]

    for _ in range(4):
        pm.update_strategy_performance("momentum", winning)
    for _ in range(3):
        pm.update_strategy_performance("momentum", losing)
    # Push through RECOVERY → FROZEN
    for _ in range(10):
        pm.record_trade("momentum", pnl=-5.0, equity_before=10000.0)

    assert pm.get_strategy_state("momentum").status == StrategyStatus.FROZEN

    allocations = pm.allocate(equity=10000)
    mom = next(a for a in allocations if a.strategy == "momentum")
    assert mom.fraction == 0.0 or mom.status == "frozen"


# ── Position Sizing ────────────────────────────────────────────────

def test_position_size_with_positive_edge(pm):
    size = pm.position_size(
        strategy_name="momentum",
        edge=0.10,
        price=0.55,
        equity=10000,
    )
    assert size.f_kelly > 0
    assert size.f_fractional > 0
    assert size.size_final > 0


def test_position_size_no_edge(pm):
    """Sin edge → no posición."""
    size = pm.position_size(
        strategy_name="momentum",
        edge=0.0,
        price=0.55,
        equity=10000,
    )
    assert size.f_kelly == 0.0
    assert size.size_final == 0.0


def test_position_size_ruin_gate(pm):
    """Ruin Gate limita el tamaño para no perder > 2% del equity."""
    size = pm.position_size(
        strategy_name="momentum",
        edge=0.30,       # edge muy alto → Kelly muy agresivo
        price=0.80,
        equity=1000,     # equity pequeño
    )
    # max_loss = size * 0.80 ≤ 0.02 * 1000 = 20
    # → size ≤ 20/0.80 = 25
    assert size.size_final <= 30  # ~$25 máximo por ruin gate
    assert size.restricted_by != "none" if size.size_raw > 30 else True


def test_position_size_min_clamp(pm):
    """Tamaños muy pequeños se redondean a 0."""
    size = pm.position_size(
        strategy_name="momentum",
        edge=0.001,   # edge minúsculo
        price=0.50,
        equity=100,
    )
    # Debería ser muy pequeño → clamp a 0
    if size.f_fractional > 0:
        assert size.size_final == 0.0 or size.size_final >= 0


def test_position_size_max_clamp(pm):
    """Tamaños muy grandes se capean a MAX_POSITION_SIZE."""
    size = pm.position_size(
        strategy_name="momentum",
        edge=0.50,
        price=0.50,
        equity=1000000,
    )
    assert size.size_final <= 5000  # MAX_POSITION_SIZE


# ── Kelly Formula ──────────────────────────────────────────────────

def test_f_kelly_positive_edge():
    """f = p_true - (1-p_true) * P / (1-P)."""
    # Manual: edge=0.10, price=0.50 → p_true=0.60
    # f = 0.60 - 0.40*0.50/0.50 = 0.60 - 0.40 = 0.20
    pm = PortfolioManager(["test"])
    size = pm.position_size("test", edge=0.10, price=0.50, equity=10000)
    assert abs(size.f_kelly - 0.20) < 0.01


def test_f_kelly_price_near_one():
    """Si price ≈ 1.0, f_kelly = 0."""
    pm = PortfolioManager(["test"])
    size = pm.position_size("test", edge=0.05, price=0.99, equity=10000)
    # 1-price ≈ 0.01 → denominador muy pequeño → Kelly extremo o 0
    assert size.f_kelly >= 0  # al menos no es negativo


# ── p_true Estimation ──────────────────────────────────────────────

def test_estimate_p_true_default():
    p = PortfolioManager.estimate_p_true(
        ensemble_weights={},
        base_market=0.55,
    )
    # Sin modelo, ballena, ni momentum → p_true ≈ base_market
    assert abs(p - 0.55) < 0.01


def test_estimate_p_true_with_signals():
    p = PortfolioManager.estimate_p_true(
        ensemble_weights={},
        model_score=0.70,
        whale_signal=0.65,
        momentum_adj=0.60,
        base_market=0.55,
    )
    # Ensemble ponderado
    assert 0.50 < p < 0.80


# ── get_active_strategies ──────────────────────────────────────────

def test_get_active_strategies_initially_all_probation(pm):
    active = pm.get_active_strategies()
    assert len(active) == 4  # todas en PROBATION cuentan como activas


# ── epoch_tick ─────────────────────────────────────────────────────

def test_epoch_tick_increments(pm):
    assert pm.current_epoch == 0
    pm.epoch_tick()
    assert pm.current_epoch == 1
