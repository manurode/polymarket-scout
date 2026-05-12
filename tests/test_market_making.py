"""Tests para MarketMaker."""

import time
import pytest
from unittest.mock import MagicMock
from src.book_analyzer import BookAnalyzer
from src.market_making import MarketMaker, Quote, MarketMakerState


# ── Fixtures ───────────────────────────────────────────────────────

@pytest.fixture
def book_analyzer():
    ba = BookAnalyzer()
    ba.initialize_book("token_001", {
        "bids": [[0.64, 500.0], [0.63, 300.0]],
        "asks": [[0.66, 200.0], [0.67, 100.0]],
    })
    return ba


@pytest.fixture
def mm(book_analyzer):
    return MarketMaker(book_analyzer=book_analyzer)


# ── Quote Calculation ──────────────────────────────────────────────

def test_calculate_quote_returns_valid_quote(mm):
    quote = mm.calculate_quote("token_001", condition_id="0xabc")
    assert quote is not None
    assert isinstance(quote, Quote)
    assert 0 < quote.bid_price < 1.0
    assert 0 < quote.ask_price < 1.0
    assert quote.bid_price < quote.ask_price


def test_calculate_quote_bid_below_mid_ask_above(mm, book_analyzer):
    """Bid < fair_price < Ask."""
    quote = mm.calculate_quote("token_001")
    mid = book_analyzer.get_mid_price("token_001")
    assert quote.bid_price < mid < quote.ask_price


def test_calculate_quote_with_custom_fair_price(mm):
    fair = 0.70
    quote = mm.calculate_quote("token_001", fair_price=fair, spread=0.04)
    assert abs(quote.fair_price - 0.70) < 0.01
    assert quote.bid_price < 0.70
    assert quote.ask_price > 0.70


def test_calculate_quote_invalid_price_returns_none(mm):
    """fair_price fuera de (0, 1) → None."""
    quote = mm.calculate_quote("token_001", fair_price=0.0)
    assert quote is None

    quote = mm.calculate_quote("token_001", fair_price=1.5)
    assert quote is None


def test_calculate_quote_bid_floor(mm):
    """Bid no baja de 0.01."""
    quote = mm.calculate_quote("token_001", fair_price=0.02, spread=0.1)
    assert quote.bid_price >= 0.01


def test_calculate_quote_ask_ceiling(mm):
    """Ask no sube de 0.99."""
    quote = mm.calculate_quote("token_001", fair_price=0.98, spread=0.1)
    assert quote.ask_price <= 0.99


# ── Quote Width Multiplier ─────────────────────────────────────────

def test_quote_width_multiplier_base(mm):
    quote = mm.calculate_quote("token_001", spread=0.04)
    assert quote.quote_width_multiplier >= 0.5


def test_quote_width_multiplier_with_volatility(mm):
    """Alta volatilidad → spreads más anchos."""
    q_low = mm.calculate_quote("token_001", realized_vol_1h=0.01, avg_vol=0.05)
    q_high = mm.calculate_quote("token_001", realized_vol_1h=0.20, avg_vol=0.05)
    assert q_high.quote_width_multiplier > q_low.quote_width_multiplier


def test_quote_width_multiplier_with_inventory(mm):
    """Inventario desbalanceado → spreads más anchos."""
    q_balanced = mm.calculate_quote("token_001", inventory_yes=0, inventory_no=0)
    q_imbalanced = mm.calculate_quote("token_001", inventory_yes=500, inventory_no=0)
    assert q_imbalanced.quote_width_multiplier > q_balanced.quote_width_multiplier


# ── Adverse Selection Protection ───────────────────────────────────

def test_should_quote_returns_true_initially(mm):
    ok, reason = mm.should_quote("token_001")
    assert ok is True
    assert reason == "ok"


def test_should_quote_book_reconciling(mm):
    """Book en RECONCILING → no cotizar."""
    ok, reason = mm.should_quote("token_001", book_reconciling=True)
    assert ok is False
    assert "reconciling" in reason.lower()


def test_should_quote_whale_detected(mm):
    """Ballena detectada → no cotizar."""
    ok, reason = mm.should_quote("token_001", whale_detected=True)
    assert ok is False
    assert "whale" in reason.lower()


def test_should_quote_flash_crash(mm):
    ok, reason = mm.should_quote("token_001", flash_crash=True)
    assert ok is False
    assert "flash" in reason.lower()


def test_should_quote_extreme_obi(mm, book_analyzer):
    """OBI extremo (>0.95) con tamaño adecuado → pausar."""
    # Crear book con OBI muy desbalanceado (999/1001 ≈ 0.998)
    book_analyzer.initialize_book("token_extreme", {
        "bids": [[0.5, 1000.0]],
        "asks": [[0.6, 1.0]],
    })
    ok, reason = mm.should_quote("token_extreme")
    assert ok is False
    assert "obi" in reason.lower()


def test_should_quote_moderate_obi_allowed(mm, book_analyzer):
    """OBI 0.80 con buen tamaño → NO pausar (antes se pausaba con 0.70)."""
    # bid=900, ask=100 → OBI = 800/1000 = 0.80. Size total = 1000 → justo en el límite
    book_analyzer.initialize_book("token_moderate", {
        "bids": [[0.5, 900.0]],
        "asks": [[0.6, 100.0]],
    })
    ok, reason = mm.should_quote("token_moderate")
    assert ok is True, f"OBI 0.80 NO debería pausar. Reason: {reason}"


def test_should_quote_small_size_ignored(mm, book_analyzer):
    """OBI > 0.95 pero con calderilla (< $1000 total) → NO pausar."""
    # bid=500, ask=1 → OBI = 499/501 ≈ 0.996, pero total size = 501 < 1000
    # → get_obi retorna 0.0 por filtro anti-calderilla
    book_analyzer.initialize_book("token_small", {
        "bids": [[0.5, 500.0]],
        "asks": [[0.6, 1.0]],
    })
    ok, reason = mm.should_quote("token_small")
    assert ok is True, (
        f"OBI con calderilla (<$1000 total) NO debería pausar. "
        f"Reason: {reason}"
    )


def test_should_quote_large_imbalance_pauses(mm, book_analyzer):
    """OBI > 0.95 con tamaño grande (>$1000) → SÍ pausar."""
    # bid=5000, ask=5 → OBI = 4995/5005 ≈ 0.998, total size = 5005 > 1000
    book_analyzer.initialize_book("token_large", {
        "bids": [[0.5, 5000.0]],
        "asks": [[0.6, 5.0]],
    })
    ok, reason = mm.should_quote("token_large")
    assert ok is False, (
        f"OBI 0.998 con $5005 size SÍ debería pausar. Reason: {reason}"
    )
    assert "obi" in reason.lower()


def test_pause_has_duration(mm):
    """La pausa tiene duración finita y se reanuda."""
    # Activar pausa
    mm.should_quote("token_001", book_reconciling=True)
    assert mm.is_paused("token_001")

    # La pausa debería expirar eventualmente (en test unitario, REENTRY_DELAY=30s)
    # No podemos esperar 30s, así que verificamos que el estado se creó
    state = mm.get_state("token_001")
    assert state is not None
    assert state.pause_reason == "book_reconciling"


# ── Flash Crash Detection ──────────────────────────────────────────

def test_detect_flash_crash_no_data(mm):
    assert mm.detect_flash_crash("t1", 0.5, []) is False


def test_detect_flash_crash_normal(mm):
    """Precios estables → no flash crash."""
    prices = [0.50, 0.51, 0.50, 0.49, 0.51]
    assert mm.detect_flash_crash("t1", 0.50, prices) is False


def test_detect_flash_crash_detected(mm):
    """Caída >5% → flash crash."""
    prices = [0.50, 0.47, 0.45]  # 0.50 → 0.45 = -10%
    assert mm.detect_flash_crash("t1", 0.45, prices) is True


# ── Fair Price Resolution ──────────────────────────────────────────

def test_get_fair_price_uses_book_when_spread_tight(mm, book_analyzer):
    fair = mm.get_fair_price("token_001")
    assert fair == 0.65  # mid del book


def test_get_fair_price_falls_back_to_gamma_when_spread_wide(mm, book_analyzer):
    """Spread > 5% → usa Gamma."""
    book_analyzer.initialize_book("token_wide", {
        "bids": [[0.50, 100]],
        "asks": [[0.60, 100]],  # spread = 0.10 = 10% > 5%
    })
    fair = mm.get_fair_price("token_wide", gamma_price=0.55, max_clob_spread=0.05)
    assert fair == 0.55


def test_get_fair_price_defaults_when_no_data(mm):
    """Sin book ni Gamma → default 0.50."""
    fair = mm.get_fair_price("nonexistent")
    assert fair == 0.50


# ── Inventory Tracking ─────────────────────────────────────────────

def test_update_inventory(mm):
    mm.update_inventory("token_001", inventory_yes=200, inventory_no=100)
    state = mm.get_state("token_001")
    assert state.inventory_yes == 200
    assert state.inventory_no == 100


# ── Remove Market ──────────────────────────────────────────────────

def test_remove_market(mm):
    mm.calculate_quote("token_001")
    assert mm.get_state("token_001") is not None

    mm.remove_market("token_001")
    assert mm.get_state("token_001") is None


# ── Quote Fields ───────────────────────────────────────────────────

def test_quote_has_all_metadata(mm):
    quote = mm.calculate_quote(
        "token_001",
        realized_vol_1h=0.03,
        avg_vol=0.02,
        inventory_yes=100,
        inventory_no=50,
    )
    # v2.0: scalars reflect computed values (not defaults)
    # volatility: 1.0 + (0.03/0.02 - 1.0)*0.5 = 1.0 + 0.25 = 1.25 (clamped to 2.0)
    assert quote.volatility_scalar >= 1.0
    # inventory: 1.0 + |(100-50)/150| * 0.5 = 1.0 + 0.166... = ~1.167
    assert quote.inventory_scalar >= 1.0
    assert quote.time_decay_scalar == 1.0
    # v2.0: new fields
    assert hasattr(quote, 'obi_scalar')
    assert hasattr(quote, 'inventory_skew')
    assert hasattr(quote, 'net_inventory')
    assert hasattr(quote, 'mode')
    assert quote.mode == "Normal"


# ── MarketMakerState ───────────────────────────────────────────────

def test_state_defaults():
    state = MarketMakerState(token_id="t1", condition_id="c1")
    assert state.last_quote_time == 0.0
    assert state.pause_until == 0.0
    assert state.inventory_yes == 0.0
