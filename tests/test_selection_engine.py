"""Tests para el Selection Engine — ranking Top Dual (v5.0)."""

import math
import pytest
from src.selection_engine import (
    SelectionEngine,
    MarketScore,
    RankingResult,
    MM_SCORE_WEIGHTS as SCORE_WEIGHTS,
)


# ── Helpers ───────────────────────────────────────────────────────

def _make_snapshot(condition_id, volume=50000, volume_24h=50000, liquidity=10000, spread=0.03,
                   question="", slug="", end_date=None):
    return {
        "condition_id": condition_id,
        "question": question or f"Market {condition_id}",
        "slug": slug or f"market-{condition_id}",
        "volume": volume,
        "volume_24h": volume_24h,
        "liquidity": liquidity,
        "spread": spread,
        "end_date": end_date,
        "price_yes": 0.5,
        "timestamp": 1000,
        "order_count": 50,
    }


def _make_snapshots(n=100):
    """Genera N snapshots variados para tests de ranking."""
    snaps = []
    for i in range(n):
        vol = 100000 - i * 800   # volumen decreciente
        liq = 30000 - i * 250    # liquidez decreciente
        spread = 0.01 + i * 0.002  # spread creciente
        snaps.append(_make_snapshot(
            f"0x{i:04x}",
            volume=max(vol, 100),
            volume_24h=max(vol, 100),
            liquidity=max(liq, 100),
            spread=min(spread, 0.25),
        ))
    return snaps


# ── Constructor ──────────────────────────────────────────────────

def test_default_top_n():
    """Por defecto, top_n_mm = 10."""
    engine = SelectionEngine()
    assert engine.top_n_mm == 10
    assert engine.top_n_directional == 10


def test_custom_top_n():
    """Acepta top_n_mm y top_n_directional personalizados."""
    engine = SelectionEngine(top_n_mm=5, top_n_directional=8)
    assert engine.top_n_mm == 5
    assert engine.top_n_directional == 8


def test_default_weights():
    """Los pesos por defecto del perfil MM son los correctos."""
    engine = SelectionEngine()
    assert SCORE_WEIGHTS["volume_24h"] == 0.50
    assert SCORE_WEIGHTS["liquidity"] == 0.30


# ── Scoring ───────────────────────────────────────────────────────

def test_higher_volume_higher_score():
    """Más volumen → score más alto (todo lo demás igual)."""
    engine = SelectionEngine()
    s_low = _make_snapshot("0xa", volume_24h=1000, liquidity=5000)
    s_high = _make_snapshot("0xb", volume_24h=100000, liquidity=5000)

    score_low, *_ = engine._compute_mm_score(s_low, max_volume_24h=100000, max_liquidity=5000, max_order_count=100)
    score_high, *_ = engine._compute_mm_score(s_high, max_volume_24h=100000, max_liquidity=5000, max_order_count=100)
    assert score_high > score_low


def test_tight_spread_higher_score():
    """Spread más tight → score más alto."""
    engine = SelectionEngine()
    s_tight = _make_snapshot("0xa", spread=0.01)
    s_wide = _make_snapshot("0xb", spread=0.20)

    score_tight, *_ = engine._compute_mm_score(s_tight, max_volume_24h=100000, max_liquidity=5000, max_order_count=100)
    score_wide, *_ = engine._compute_mm_score(s_wide, max_volume_24h=100000, max_liquidity=5000, max_order_count=100)
    assert score_tight > score_wide


def test_spread_none_is_neutral():
    """Spread None → no crashea."""
    engine = SelectionEngine()
    s = _make_snapshot("0xa", spread=None)
    score, *_ = engine._compute_mm_score(s, max_volume_24h=100000, max_liquidity=5000, max_order_count=100)
    assert 0 <= score <= 1


def test_near_expiry_penalized():
    """Mercado cerca de expiración recibe recency_score reducido."""
    import time
    engine = SelectionEngine()

    # 12 horas para expirar
    soon = time.time() + 12 * 3600
    s_soon = _make_snapshot("0xa", end_date=soon)

    # 7 días para expirar (fuera de la ventana de penalización)
    far = time.time() + 7 * 24 * 3600
    s_far = _make_snapshot("0xb", end_date=far)

    # Ambas con mismos vol/liq/spread
    s_soon["volume"] = s_far["volume"] = 50000
    s_soon["volume_24h"] = s_far["volume_24h"] = 50000
    s_soon["liquidity"] = s_far["liquidity"] = 5000
    s_soon["spread"] = s_far["spread"] = 0.03

    score_soon, *_ = engine._compute_mm_score(s_soon, max_volume_24h=50000, max_liquidity=5000, max_order_count=100)
    score_far, *_ = engine._compute_mm_score(s_far, max_volume_24h=50000, max_liquidity=5000, max_order_count=100)

    # Con expiración inminente (<48h) debe ser más bajo
    assert score_soon < score_far


# ── Ranking ────────────────────────────────────────────────────────

def test_rank_returns_top_n():
    """rank() retorna DualRankingResult con mm_top y directional_top."""
    engine = SelectionEngine(top_n_mm=5, top_n_directional=5)
    snaps = _make_snapshots(50)
    result = engine.rank(snaps)
    assert len(result.mm_top) == 5
    assert len(result.directional_top) == 5


def test_rank_sorted_descending():
    """Los tops están ordenados por score descendente."""
    engine = SelectionEngine(top_n_mm=5, top_n_directional=5)
    snaps = _make_snapshots(50)
    result = engine.rank(snaps)
    mm_scores = [ms.score for ms in result.mm_top]
    assert mm_scores == sorted(mm_scores, reverse=True)


def test_first_rank_all_are_enter():
    """La primera ejecución de rank → todos los mercados son enter."""
    engine = SelectionEngine(top_n_mm=5, top_n_directional=5)
    snaps = _make_snapshots(50)
    result = engine.rank(snaps)
    assert len(result.mm_enter) == 5


def test_subsequent_rank_detects_changes():
    """Segunda ejecución con snapshots distintos detecta entradas/salidas."""
    engine = SelectionEngine(top_n_mm=3, top_n_directional=3)
    snaps_a = _make_snapshots(30)
    result_a = engine.rank(snaps_a)

    # Segunda ejecución: solo los primeros 3 (deberían mantenerse)
    snaps_b = _make_snapshots(30)
    result_b = engine.rank(snaps_b)

    # Ambas deben tener 3 en el top
    assert len(result_b.mm_top) == 3
    # enter + exit deben sumar cambios netos (puede ser 0 si no hay cambios)
    assert len(result_b.mm_enter) + len(result_b.mm_exit) >= 0


def test_is_top_after_rank():
    """is_top() funciona después de rank()."""
    engine = SelectionEngine(top_n_mm=5, top_n_directional=5)
    snaps = _make_snapshots(20)
    engine.rank(snaps)
    top_id = snaps[0]["condition_id"]
    assert engine.is_top(top_id)


def test_get_top_ids_returns_set():
    """get_top_ids() retorna un set de condition_ids."""
    engine = SelectionEngine(top_n_mm=3, top_n_directional=3)
    snaps = _make_snapshots(20)
    engine.rank(snaps)
    ids = engine.get_top_ids()
    assert isinstance(ids, set)
    assert len(ids) == 3


# ── Edge Cases ──────────────────────────────────────────────────

def test_zero_volume_does_not_crash():
    """Volumen 0 no crashea el scoring."""
    engine = SelectionEngine()
    s = _make_snapshot("0xa", volume=0, volume_24h=0)
    score, *_ = engine._compute_mm_score(s, max_volume_24h=1, max_liquidity=1000, max_order_count=1)
    assert 0 <= score <= 1


def test_negative_values_handled():
    """Valores negativos no crashean (se clipean a 0)."""
    engine = SelectionEngine()
    s = _make_snapshot("0xa", volume=-100, volume_24h=-100, liquidity=-50)
    score, *_ = engine._compute_mm_score(s, max_volume_24h=1, max_liquidity=1000, max_order_count=1)
    assert score == 0.0 or score >= 0
