"""Tests para WhaleTracker — tracking de ballenas + Conviction Multiplier."""

import time
import pytest
from src.whale_tracker import (
    WhaleTracker,
    WhaleFlow,
    ConvictionMultiplier,
    WHALE_FACTOR,
)


# ── Fixtures ───────────────────────────────────────────────────────

@pytest.fixture
def tracker():
    return WhaleTracker()


@pytest.fixture
def tracker_with_whale():
    """Tracker con una Alpha Whale pre-cargada."""
    wt = WhaleTracker(alpha_threshold=0.5)  # umbral bajo para tests
    wt.update_wallet_profile(
        "0xWHALE1",
        pnl=30000,
        win_rate=0.70,
        sortino=2.5,
        trades_per_week=10,
        total_trades=200,
    )
    return wt


# ── Wallet Profiling ───────────────────────────────────────────────

def test_update_wallet_profile_creates_profile(tracker):
    profile = tracker.update_wallet_profile(
        "0xABC",
        pnl=25000,
        win_rate=0.65,
        sortino=2.0,
        trades_per_week=8,
        total_trades=150,
    )
    assert profile.address == "0xABC"
    assert profile.total_pnl == 25000
    assert profile.win_rate == 0.65


def test_alpha_whale_score(tracker_with_whale):
    profile = tracker_with_whale.get_wallet_profile("0xWHALE1")
    assert profile is not None
    assert profile.alpha_score > 0.5  # umbral bajo → es Alpha Whale
    assert profile.is_alpha_whale is True


def test_not_alpha_whale_with_few_trades(tracker):
    """Wallet con pocas transacciones no es Alpha Whale aunque tenga buen score."""
    tracker.update_wallet_profile(
        "0xNOOB",
        pnl=50000,       # mucho P&L
        win_rate=0.90,   # alta win rate
        sortino=4.0,     # excelente Sortino
        trades_per_week=50,
        total_trades=10,  # pero pocas transacciones
    )
    profile = tracker.get_wallet_profile("0xNOOB")
    assert profile.alpha_score > tracker.alpha_threshold  # score alto
    assert profile.is_alpha_whale is False  # pero no es whale


def test_is_alpha_whale_unknown(tracker):
    assert tracker.is_alpha_whale("0xUNKNOWN") is False


# ── Whale Trade Recording ──────────────────────────────────────────

def test_record_whale_trade_only_alphawhale(tracker_with_whale):
    """Solo se registran trades de Alpha Whales."""
    # 0xWHALE1 es Alpha Whale
    trade = tracker_with_whale.record_whale_trade(
        "0xWHALE1", "0xmarket1", "buy", 5000.0,
    )
    assert trade is not None
    assert trade.wallet == "0xWHALE1"
    assert trade.volume == 5000.0


def test_record_whale_trade_ignores_nonwhale(tracker):
    """Wallet no-Alpha → no se registra."""
    trade = tracker.record_whale_trade(
        "0xNOOB", "0xmarket1", "buy", 1000.0,
    )
    assert trade is None


# ── Whale Flow ─────────────────────────────────────────────────────

def test_whale_flow_no_trades(tracker):
    flow = tracker.get_whale_flow("0xmarket1")
    assert flow.net_flow_1h == 0.0
    assert flow.whale_consensus == 0.0
    assert flow.active_whales == 0


def test_whale_flow_with_trades(tracker_with_whale):
    """Varias ballenas comprando → flujo positivo + consenso alto."""
    # Añadir otra ballena
    tracker_with_whale.update_wallet_profile(
        "0xWHALE2", pnl=20000, win_rate=0.60, sortino=1.8,
        trades_per_week=6, total_trades=80,
    )

    now = time.time()
    tracker_with_whale.record_whale_trade(
        "0xWHALE1", "0xmarket1", "buy", 10000.0, timestamp=now,
    )
    tracker_with_whale.record_whale_trade(
        "0xWHALE2", "0xmarket1", "buy", 5000.0, timestamp=now,
    )

    flow = tracker_with_whale.get_whale_flow("0xmarket1")
    assert flow.net_flow_1h == 15000.0
    assert flow.bullish_whales == 2
    assert flow.bearish_whales == 0
    assert flow.whale_consensus == 1.0  # todas en la misma dirección


def test_whale_flow_mixed(tracker_with_whale):
    """Ballenas divididas → consenso bajo."""
    tracker_with_whale.update_wallet_profile(
        "0xWHALE2", pnl=20000, win_rate=0.60, sortino=1.8,
        trades_per_week=6, total_trades=80,
    )

    now = time.time()
    tracker_with_whale.record_whale_trade("0xWHALE1", "0xmarket1", "buy", 10000.0, timestamp=now)
    tracker_with_whale.record_whale_trade("0xWHALE2", "0xmarket1", "sell", 8000.0, timestamp=now)

    flow = tracker_with_whale.get_whale_flow("0xmarket1")
    assert flow.net_flow_1h == 2000.0  # 10000 - 8000
    assert flow.whale_consensus == 0.0  # 1 bullish, 1 bearish → |1-1|/2 = 0


# ── Conviction Multiplier ──────────────────────────────────────────

def test_conviction_multiplier_no_whales(tracker):
    cm = tracker.get_conviction_multiplier("0xmarket1")
    assert cm.cm == 1.0  # sin ballenas → neutro
    assert cm.interpretation == "neutral"


def test_conviction_multiplier_bullish(tracker_with_whale):
    now = time.time()
    # Necesitamos al menos 5 trades para que el z-score no sea 0
    # Primero, crear historial de flujo
    for i in range(10):
        tracker_with_whale.record_whale_trade(
            "0xWHALE1", "0xmarket1", "sell" if i < 5 else "buy",
            1000.0, timestamp=now - (10 - i) * 120,
        )

    cm = tracker_with_whale.get_conviction_multiplier("0xmarket1")
    # Con historial de ventas iniciales y luego compras → z-score positivo
    # pero necesitamos que el flujo neto actual sea positivo
    # Forcemos flujo comprador con otro whale
    tracker_with_whale.update_wallet_profile(
        "0xWHALE2", pnl=20000, win_rate=0.60, sortino=1.8,
        trades_per_week=6, total_trades=80,
    )
    for i in range(5):
        tracker_with_whale.record_whale_trade(
            "0xWHALE2", "0xmarket1", "buy", 5000.0, timestamp=now - i * 10,
        )

    cm = tracker_with_whale.get_conviction_multiplier("0xmarket1")
    assert cm.cm >= 1.0  # ballenas comprando → al menos neutral o bullish


def test_conviction_multiplier_bearish(tracker_with_whale):
    now = time.time()
    # Crear historial de flujo para z-score
    for i in range(10):
        tracker_with_whale.record_whale_trade(
            "0xWHALE1", "0xmarket1", "buy" if i < 5 else "sell",
            1000.0, timestamp=now - (10 - i) * 120,
        )
    # Flujo vendedor fuerte ahora
    for i in range(5):
        tracker_with_whale.record_whale_trade(
            "0xWHALE1", "0xmarket1", "sell", 5000.0, timestamp=now - i * 10,
        )

    cm = tracker_with_whale.get_conviction_multiplier("0xmarket1")
    assert cm.cm <= 1.0  # ballenas vendiendo → al menos neutral o bearish


def test_conviction_multiplier_range(tracker_with_whale):
    """CM siempre en [0.6, 1.4]."""
    now = time.time()
    for i in range(20):
        tracker_with_whale.record_whale_trade(
            "0xWHALE1", "0xmarket1",
            "buy" if i % 2 == 0 else "sell",
            10000.0, timestamp=now - i * 30,
        )

    cm = tracker_with_whale.get_conviction_multiplier("0xmarket1")
    assert 0.6 <= cm.cm <= 1.4


# ── apply_conviction ────────────────────────────────────────────────

def test_apply_conviction(tracker_with_whale):
    now = time.time()
    tracker_with_whale.record_whale_trade("0xWHALE1", "0xmarket1", "buy", 10000, timestamp=now)

    signal, size = tracker_with_whale.apply_conviction(
        "0xmarket1", signal_strength=0.5, position_size=500, max_position_size=1000,
    )
    assert signal >= 0.5  # amplificado por CM bullish
    assert size <= 1000  # no excede max


# ── Wallet Clustering ──────────────────────────────────────────────

def test_add_wallet_to_cluster(tracker):
    tracker.update_wallet_profile(
        "0xW1", pnl=10000, win_rate=0.5, sortino=1.0,
        trades_per_week=5, total_trades=60,
    )
    tracker.add_wallet_to_cluster("0xW1", "cluster_A")

    profile = tracker.get_wallet_profile("0xW1")
    assert profile.cluster_id == "cluster_A"

    clusters = tracker.cluster_wallets()
    assert len(clusters) == 1
    assert "0xW1" in clusters[0].wallets


def test_cluster_multiple_wallets(tracker):
    tracker.update_wallet_profile("0xW1", pnl=5000, win_rate=0.5, sortino=1.0,
                                  trades_per_week=3, total_trades=50)
    tracker.update_wallet_profile("0xW2", pnl=8000, win_rate=0.6, sortino=1.5,
                                  trades_per_week=4, total_trades=60)

    tracker.add_wallet_to_cluster("0xW1", "cluster_A")
    tracker.add_wallet_to_cluster("0xW2", "cluster_A")

    clusters = tracker.cluster_wallets()
    assert len(clusters[0].wallets) == 2
    assert clusters[0].total_pnl == 13000


# ── Query Methods ──────────────────────────────────────────────────

def test_get_alpha_whales(tracker_with_whale):
    whales = tracker_with_whale.get_alpha_whales()
    assert "0xWHALE1" in whales


def test_get_market_whales(tracker_with_whale):
    now = time.time()
    tracker_with_whale.record_whale_trade("0xWHALE1", "0xmarket1", "buy", 5000, timestamp=now)

    whales = tracker_with_whale.get_market_whales("0xmarket1")
    assert "0xWHALE1" in whales


def test_get_market_whales_empty(tracker):
    assert tracker.get_market_whales("0xunknown") == []


# ── Clear ──────────────────────────────────────────────────────────

def test_clear(tracker_with_whale):
    tracker_with_whale.record_whale_trade("0xWHALE1", "0xmarket1", "buy", 5000,
                                          timestamp=time.time())
    tracker_with_whale.clear()

    assert len(tracker_with_whale.get_alpha_whales()) == 0
    flow = tracker_with_whale.get_whale_flow("0xmarket1")
    assert flow.net_flow_1h == 0.0


# ── WhaleFlow dataclass ────────────────────────────────────────────

def test_whale_flow_defaults():
    flow = WhaleFlow(condition_id="test")
    assert flow.net_flow_1h == 0.0
    assert flow.active_whales == 0


# ── ConvictionMultiplier dataclass ──────────────────────────────────

def test_conviction_multiplier_defaults():
    cm = ConvictionMultiplier(cm=1.0, net_flow=0.0, consensus=0.0, zscore=0.0, interpretation="neutral")
    assert cm.cm == 1.0
    assert cm.interpretation == "neutral"
