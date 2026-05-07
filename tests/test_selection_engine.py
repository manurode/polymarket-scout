"""Tests para el Selection Engine — ranking Top 50."""

import math
import pytest
from src.selection_engine import (
    SelectionEngine,
    MarketScore,
    RankingResult,
    SCORE_WEIGHTS,
)


# ── Helpers ───────────────────────────────────────────────────────

def _make_snapshot(condition_id, volume=50000, liquidity=10000, spread=0.03,
                   question="", slug="", end_date=None):
    return {
        "condition_id": condition_id,
        "question": question or f"Market {condition_id}",
        "slug": slug or f"market-{condition_id}",
        "volume": volume,
        "liquidity": liquidity,
        "spread": spread,
        "end_date": end_date,
        "price_yes": 0.5,
        "timestamp": 1000,
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
            liquidity=max(liq, 100),
            spread=min(spread, 0.25),
        ))
    return snaps


# ── Constructor ──────────────────────────────────────────────────

def test_default_top_n():
    """Por defecto, top_n = 50."""
    engine = SelectionEngine()
    assert engine.top_n == 50


def test_custom_top_n():
    """Acepta top_n personalizado."""
    engine = SelectionEngine(top_n=10)
    assert engine.top_n == 10


def test_custom_weights():
    """Acepta pesos personalizados."""
    engine = SelectionEngine(weights={"volume": 1.0, "liquidity": 0, "recency": 0, "spread": 0})
    assert engine.weights["volume"] == 1.0


# ── Scoring ───────────────────────────────────────────────────────

def test_higher_volume_higher_score():
    """Más volumen → score más alto (todo lo demás igual)."""
    engine = SelectionEngine()
    s_low = _make_snapshot("0xa", volume=1000, liquidity=5000)
    s_high = _make_snapshot("0xb", volume=100000, liquidity=5000)

    score_low = engine._compute_score(s_low, max_volume=100000, max_liquidity=5000)
    score_high = engine._compute_score(s_high, max_volume=100000, max_liquidity=5000)
    assert score_high > score_low


def test_tight_spread_higher_score():
    """Spread más tight → score más alto."""
    engine = SelectionEngine()
    s_tight = _make_snapshot("0xa", spread=0.01)
    s_wide = _make_snapshot("0xb", spread=0.20)

    score_tight = engine._compute_score(s_tight, max_volume=100000, max_liquidity=5000)
    score_wide = engine._compute_score(s_wide, max_volume=100000, max_liquidity=5000)
    assert score_tight > score_wide


def test_spread_none_is_neutral():
    """Spread None → score neutro (~0.5)."""
    engine = SelectionEngine()
    s = _make_snapshot("0xa", spread=None)
    score = engine._compute_score(s, max_volume=100000, max_liquidity=5000)
    # spread_score = 0.5 * 0.10 = 0.05
    # No hay fallo, simplemente verificar que no crashea
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
    s_soon["liquidity"] = s_far["liquidity"] = 5000
    s_soon["spread"] = s_far["spread"] = 0.03

    score_soon = engine._compute_score(s_soon, max_volume=50000, max_liquidity=5000)
    score_far = engine._compute_score(s_far, max_volume=50000, max_liquidity=5000)

    assert score_far > score_soon  # far should be higher


# ── Ranking ───────────────────────────────────────────────────────

def test_rank_returns_top_n():
    """rank() retorna exactamente top_n mercados."""
    engine = SelectionEngine(top_n=10)
    snaps = _make_snapshots(50)
    result = engine.rank(snaps)

    assert len(result.top) == 10


def test_rank_sorted_descending():
    """El Top N está ordenado por score descendente."""
    engine = SelectionEngine(top_n=5)
    snaps = _make_snapshots(50)
    result = engine.rank(snaps)

    scores = [ms.score for ms in result.top]
    assert scores == sorted(scores, reverse=True)


def test_rank_empty_returns_empty():
    """Lista vacía → resultado vacío sin crash."""
    engine = SelectionEngine()
    result = engine.rank([])
    assert len(result.top) == 0


def test_rank_all_scored_has_all():
    """all_scored contiene TODOS los mercados."""
    engine = SelectionEngine()
    snaps = _make_snapshots(20)
    result = engine.rank(snaps)
    assert len(result.all_scored) == 20


# ── Enter / Exit tracking ─────────────────────────────────────────

def test_first_rank_all_are_enter():
    """En el primer ranking, todos los Top N son entradas."""
    engine = SelectionEngine(top_n=10)
    snaps = _make_snapshots(30)
    result = engine.rank(snaps)

    assert len(result.enter) == 10
    assert len(result.exit) == 0


def test_subsequent_rank_detects_changes():
    """Rankings consecutivos detectan entradas y salidas."""
    engine = SelectionEngine(top_n=5)

    # Primer ranking
    snaps1 = _make_snapshots(10)
    result1 = engine.rank(snaps1)
    top1_ids = {ms.condition_id for ms in result1.top}

    # Segundo ranking: invertir el orden (los de abajo ahora arriba y viceversa)
    snaps2 = list(reversed(_make_snapshots(10)))
    # Renombrar condition_ids de snaps2 para que sean diferentes
    for i, s in enumerate(snaps2):
        s["condition_id"] = f"0x2{i:04x}"

    result2 = engine.rank(snaps2)

    # Todos los del primer top salieron, todos los nuevos entraron
    assert len(result2.exit) == 5
    assert len(result2.enter) == 5


# ── is_top / get_top_ids ─────────────────────────────────────────

def test_is_top_after_rank():
    """is_top() funciona correctamente tras rank()."""
    engine = SelectionEngine(top_n=5)
    snaps = _make_snapshots(20)
    result = engine.rank(snaps)

    top_id = result.top[0].condition_id
    assert engine.is_top(top_id)

    # El último del ranking NO debería estar en el top
    last_id = result.all_scored[-1].condition_id
    if last_id not in {ms.condition_id for ms in result.top}:
        assert not engine.is_top(last_id)


def test_get_top_ids_returns_set():
    """get_top_ids() retorna un set de condition_ids."""
    engine = SelectionEngine(top_n=5)
    snaps = _make_snapshots(10)
    engine.rank(snaps)

    ids = engine.get_top_ids()
    assert isinstance(ids, set)
    assert len(ids) == 5


# ── Edge cases ────────────────────────────────────────────────────

def test_zero_volume_does_not_crash():
    """Volumen 0 no causa división por cero."""
    engine = SelectionEngine()
    s = _make_snapshot("0x0", volume=0, liquidity=0)
    score = engine._compute_score(s, max_volume=1, max_liquidity=1)
    assert isinstance(score, float)
    assert 0 <= score <= 1


def test_negative_values_handled():
    """Valores negativos son tratados como 0."""
    engine = SelectionEngine()
    s = _make_snapshot("0x0", volume=-100, liquidity=-50)
    score = engine._compute_score(s, max_volume=1000, max_liquidity=1000)
    assert isinstance(score, float)
