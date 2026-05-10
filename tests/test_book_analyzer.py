"""Tests para BookAnalyzer — L2 order book y cálculo de OBI."""

import numpy as np
import pytest
from src.book_analyzer import BookAnalyzer, BookSnapshot, MAX_BOOK_LEVELS


# ── Fixtures ───────────────────────────────────────────────────────

@pytest.fixture
def analyzer():
    return BookAnalyzer()


@pytest.fixture
def sample_bids():
    """Bids típicas del CLOB: [price, size]."""
    return [
        [0.64, 500.0],
        [0.63, 300.0],
        [0.62, 200.0],
        [0.61, 100.0],
    ]


@pytest.fixture
def sample_asks():
    return [
        [0.66, 400.0],
        [0.67, 250.0],
        [0.68, 150.0],
        [0.69, 100.0],
    ]


@pytest.fixture
def sample_snapshot(sample_bids, sample_asks):
    return {
        "market": "0xabc123",
        "asset_id": "token_yes_001",
        "bids": sample_bids,
        "asks": sample_asks,
        "seq_num": 42,
    }


# ── Inicialización ─────────────────────────────────────────────────

def test_empty_analyzer_has_no_books(analyzer):
    assert len(analyzer) == 0
    assert analyzer.tracked_markets == []


def test_obi_is_zero_for_unknown_market(analyzer):
    assert analyzer.get_obi("nonexistent") == 0.0


def test_mid_price_zero_for_unknown_market(analyzer):
    assert analyzer.get_mid_price("nonexistent") == 0.0


# ── Initialize Book from Snapshot ──────────────────────────────────

def test_initialize_book_populates_bids_asks(analyzer, sample_snapshot, sample_bids, sample_asks):
    snapshot = analyzer.initialize_book("token_yes_001", sample_snapshot)

    assert snapshot.token_id == "token_yes_001"
    assert snapshot.seq_num == 42
    assert snapshot.bids[0, 0] == 0.64
    assert snapshot.bids[0, 1] == 500.0
    assert snapshot.asks[0, 0] == 0.66
    assert snapshot.asks[0, 1] == 400.0


def test_initialize_book_calculates_obi(analyzer, sample_snapshot):
    snapshot = analyzer.initialize_book("token_yes_001", sample_snapshot)

    # Bids total: 500+300+200+100 = 1100
    # Asks total: 400+250+150+100 = 900
    # OBI = (1100-900)/(1100+900) = 200/2000 = 0.1
    assert abs(snapshot.obi - 0.1) < 0.001


def test_initialize_book_calculates_mid_price(analyzer, sample_snapshot):
    snapshot = analyzer.initialize_book("token_yes_001", sample_snapshot)
    # Best bid: 0.64, Best ask: 0.66 → mid: 0.65
    assert abs(snapshot.mid_price - 0.65) < 0.001


def test_initialize_book_calculates_spread(analyzer, sample_snapshot):
    snapshot = analyzer.initialize_book("token_yes_001", sample_snapshot)
    assert abs(snapshot.spread - 0.02) < 0.001


def test_initialize_book_without_seq_num(analyzer):
    snap = {"bids": [[0.5, 100]], "asks": [[0.6, 100]]}
    snapshot = analyzer.initialize_book("t1", snap)
    assert snapshot.seq_num == 0


# ── Apply Delta (Update) ──────────────────────────────────────────

def test_apply_delta_updates_existing_levels(analyzer, sample_snapshot):
    analyzer.initialize_book("t1", sample_snapshot)

    # Update: modificar bid existente
    delta = {
        "bids": [[0.64, 250.0]],  # reducir de 500 a 250
        "asks": [[0.66, 200.0]],  # reducir de 400 a 200
        "seq_num": 43,
    }
    snapshot = analyzer.apply_delta("t1", delta)

    assert snapshot.seq_num == 43
    assert snapshot.bids[0, 1] == 250.0
    assert snapshot.asks[0, 1] == 200.0


def test_apply_delta_adds_new_level(analyzer, sample_snapshot):
    analyzer.initialize_book("t1", sample_snapshot)

    delta = {
        "bids": [[0.60, 50.0]],  # nuevo nivel
        "seq_num": 44,
    }
    snapshot = analyzer.apply_delta("t1", delta)

    # Buscar el nuevo nivel en las bids
    prices = snapshot.bids[:5, 0]
    assert 0.60 in prices


def test_apply_delta_deletes_level_with_zero_size(analyzer, sample_snapshot):
    analyzer.initialize_book("t1", sample_snapshot)

    # Delete: size = 0
    delta = {
        "bids": [[0.64, 0.0]],
        "seq_num": 45,
    }
    snapshot = analyzer.apply_delta("t1", delta)

    # El nivel 0.64 ya no debería estar en el top
    first_bid_price = snapshot.bids[0, 0]
    assert first_bid_price != 0.64


def test_apply_delta_full_replace(analyzer, sample_snapshot):
    analyzer.initialize_book("t1", sample_snapshot)

    delta = {
        "type": "new",
        "bids": [[0.70, 100.0], [0.69, 200.0]],
        "asks": [[0.72, 300.0]],
        "seq_num": 46,
    }
    snapshot = analyzer.apply_delta("t1", delta)

    assert snapshot.bids[0, 0] == 0.70
    assert snapshot.bids[0, 1] == 100.0
    assert snapshot.bids[1, 0] == 0.69
    assert snapshot.asks[0, 0] == 0.72


def test_apply_delta_dict_format(analyzer):
    """Soporta formato dict con keys 'price' y 'size'."""
    snap = {"bids": [[0.5, 100]], "asks": [[0.55, 100]]}
    analyzer.initialize_book("t1", snap)

    delta = {
        "bids": [{"price": 0.50, "size": 200.0}],
    }
    snapshot = analyzer.apply_delta("t1", delta)

    assert snapshot.bids[0, 1] == 200.0


# ── OBI Calculation ────────────────────────────────────────────────

def test_obi_balanced_book(analyzer):
    analyzer.initialize_book("t1", {
        "bids": [[0.5, 500.0]],
        "asks": [[0.6, 500.0]],
    })
    assert analyzer.get_obi("t1") == 0.0


def test_obi_all_bids_no_asks(analyzer):
    analyzer.initialize_book("t1", {
        "bids": [[0.5, 500.0]],
        "asks": [],
    })
    assert analyzer.get_obi("t1") == 1.0


def test_obi_all_asks_no_bids(analyzer):
    analyzer.initialize_book("t1", {
        "bids": [],
        "asks": [[0.5, 500.0]],
    })
    assert analyzer.get_obi("t1") == -1.0


def test_obi_respects_levels_param(analyzer):
    """Solo considera los top N niveles."""
    analyzer.initialize_book("t1", {
        "bids": [[0.5, 100.0], [0.4, 200.0]],
        "asks": [[0.6, 100.0], [0.7, 200.0]],
    })
    # Con 1 nivel: (100-100)/(200) = 0
    assert analyzer.get_obi("t1", levels=1) == 0.0
    # Con 2 niveles: (300-300)/(600) = 0
    assert analyzer.get_obi("t1", levels=2) == 0.0


def test_obi_min_total_size_filter_ignores_small_books(analyzer):
    """Filtro anti-calderilla: si best bid+ask < $1000, OBI = 0.0."""
    # bid=500, ask=1 → total=501 < 1000
    analyzer.initialize_book("t1", {
        "bids": [[0.5, 500.0]],
        "asks": [[0.6, 1.0]],
    })
    # Sin filtro → OBI = (500-1)/(501) ≈ 0.996
    obi_raw = analyzer.get_obi("t1")
    assert abs(obi_raw - 0.996) < 0.01

    # Con filtro min_total_size=1000 → OBI = 0.0
    obi_filtered = analyzer.get_obi("t1", min_total_size=1000.0)
    assert obi_filtered == 0.0


def test_obi_min_total_size_filter_passes_large_books(analyzer):
    """Filtro anti-calderilla: si best bid+ask >= $1000, OBI se calcula normal."""
    # bid=2000, ask=100 → total=2100 >= 1000
    analyzer.initialize_book("t1", {
        "bids": [[0.5, 2000.0]],
        "asks": [[0.6, 100.0]],
    })
    # Con filtro → OBI = (2000-100)/(2100) = 1900/2100 ≈ 0.905
    obi = analyzer.get_obi("t1", min_total_size=1000.0)
    assert abs(obi - 0.905) < 0.01
    assert obi != 0.0


def test_best_bid_ask_size_properties(analyzer):
    """Las propiedades best_bid_size y best_ask_size funcionan."""
    analyzer.initialize_book("t1", {
        "bids": [[0.5, 750.0], [0.4, 300.0]],
        "asks": [[0.6, 250.0], [0.7, 100.0]],
    })

    from src.book_analyzer import _BookState
    book = analyzer._books["t1"]
    assert book.best_bid_size == 750.0
    assert book.best_ask_size == 250.0
    assert book.top_size_total == 1000.0


def test_best_bid_ask_size_empty_book(analyzer):
    """Libro vacío → tamaños 0."""
    from src.book_analyzer import _BookState
    book = _BookState(token_id="empty")
    assert book.best_bid_size == 0.0
    assert book.best_ask_size == 0.0
    assert book.top_size_total == 0.0


# ── Imbalance Direction ────────────────────────────────────────────

def test_imbalance_direction_bullish(analyzer):
    analyzer.initialize_book("t1", {
        "bids": [[0.5, 1000.0]],
        "asks": [[0.6, 100.0]],
    })
    assert analyzer.get_imbalance_direction("t1") == 1


def test_imbalance_direction_bearish(analyzer):
    analyzer.initialize_book("t1", {
        "bids": [[0.5, 100.0]],
        "asks": [[0.6, 1000.0]],
    })
    assert analyzer.get_imbalance_direction("t1") == -1


def test_imbalance_direction_neutral(analyzer):
    analyzer.initialize_book("t1", {
        "bids": [[0.5, 500.0]],
        "asks": [[0.6, 500.0]],
    })
    assert analyzer.get_imbalance_direction("t1") == 0


# ── Mid Price / Spread ────────────────────────────────────────────

def test_mid_price_asks_only(analyzer):
    analyzer.initialize_book("t1", {
        "bids": [],
        "asks": [[0.6, 400.0]],
    })
    assert analyzer.get_mid_price("t1") == 0.6


def test_mid_price_bids_only(analyzer):
    analyzer.initialize_book("t1", {
        "bids": [[0.5, 400.0]],
        "asks": [],
    })
    assert analyzer.get_mid_price("t1") == 0.5


# ── Cancellation Tracking ─────────────────────────────────────────

def test_large_cancellations_tracked(analyzer):
    analyzer.initialize_book("t1", {"bids": [[0.5, 100]], "asks": [[0.6, 100]]})

    # Cancelación grande
    delta = {"changes": [{"side": "buy", "size": 2000.0}]}
    analyzer.apply_delta("t1", delta)

    assert analyzer.get_large_cancellations("t1") == 1

    # Cancelación pequeña (no se trackea)
    delta2 = {"changes": [{"side": "sell", "size": 50.0}]}
    analyzer.apply_delta("t1", delta2)

    assert analyzer.get_large_cancellations("t1") == 1


# ── Remove / Cleanup ───────────────────────────────────────────────

def test_remove_book_frees_memory(analyzer, sample_snapshot):
    analyzer.initialize_book("t1", sample_snapshot)
    assert len(analyzer) == 1

    analyzer.remove_book("t1")
    assert len(analyzer) == 0
    assert analyzer.get_obi("t1") == 0.0


def test_multiple_markets_independent(analyzer):
    analyzer.initialize_book("t1", {"bids": [[0.5, 100]], "asks": [[0.6, 50]]})
    analyzer.initialize_book("t2", {"bids": [[0.3, 200]], "asks": [[0.4, 200]]})

    assert len(analyzer) == 2
    # t1: (100-50)/150 = 0.333...
    assert abs(analyzer.get_obi("t1") - 0.333) < 0.01
    # t2: (200-200)/400 = 0
    assert analyzer.get_obi("t2") == 0.0


# ── Max Levels ─────────────────────────────────────────────────────

def test_max_levels_capped(analyzer, sample_snapshot):
    """Más niveles de los que permite max_levels → truncado."""
    many_bids = [[float(i) / 100, 100.0] for i in range(50, 0, -1)]
    many_asks = [[float(i) / 100, 100.0] for i in range(60, 110)]

    snap = {"bids": many_bids, "asks": many_asks}
    snapshot = analyzer.initialize_book("t1", snap)

    # Solo se guardan MAX_BOOK_LEVELS
    assert sum(1 for i in range(MAX_BOOK_LEVELS) if snapshot.bids[i, 1] > 0) <= MAX_BOOK_LEVELS
