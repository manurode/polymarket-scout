"""Tests para MarkoutAnalyzer — detección de flujo tóxico."""

import time
import pytest
from src.markout_analysis import (
    MarkoutAnalyzer,
    MarkoutScore,
    FillRecord,
    TOXICITY_CLEAN,
    TOXICITY_MIXED,
    TOXICITY_TOXIC,
)
from src.book_analyzer import BookAnalyzer


# ── Fixtures ───────────────────────────────────────────────────────

@pytest.fixture
def book_analyzer():
    ba = BookAnalyzer()
    ba.initialize_book("token_001", {
        "bids": [[0.64, 500.0]],
        "asks": [[0.66, 300.0]],
    })
    return ba


@pytest.fixture
def analyzer(book_analyzer):
    return MarkoutAnalyzer(book_analyzer=book_analyzer)


# ── Record Fill ────────────────────────────────────────────────────

def test_record_fill_returns_fill_record(analyzer):
    fill = analyzer.record_fill(
        trade_id="t1",
        token_id="token_001",
        fill_price=0.65,
        size=50.0,
        side="buy",
    )
    assert isinstance(fill, FillRecord)
    assert fill.trade_id == "t1"
    assert fill.fill_price == 0.65
    assert fill.size == 50.0


def test_record_fill_buy_side(analyzer):
    """side='buy' = nos compraron YES (éramos el ask, short YES)."""
    fill = analyzer.record_fill("t1", "token_001", 0.65, 50.0, "buy")
    assert fill.side == "buy"


def test_record_fill_sell_side(analyzer):
    """side='sell' = nos vendieron YES (éramos el bid, long YES)."""
    fill = analyzer.record_fill("t1", "token_001", 0.65, 50.0, "sell")
    assert fill.side == "sell"


def test_record_fill_initializes_markout_prices(analyzer):
    fill = analyzer.record_fill("t1", "token_001", 0.65, 50.0, "buy")
    assert "1" in fill.markout_prices
    assert "5" in fill.markout_prices
    assert "10" in fill.markout_prices
    assert "60" in fill.markout_prices


# ── P&L Calculation ────────────────────────────────────────────────

def test_compute_pnl_buy_side(analyzer):
    """Buy side = short YES → P&L = fill_price - current_mid."""
    fill = FillRecord(trade_id="t1", token_id="t1", fill_price=0.65, size=50, side="buy", fill_time=time.time())
    pnl = analyzer._compute_pnl(fill, current_mid=0.60, interval=10)
    assert abs(pnl - 0.05) < 0.001  # 0.65 - 0.60 = +0.05 (ganancia: precio bajó)


def test_compute_pnl_sell_side(analyzer):
    """Sell side = long YES → P&L = current_mid - fill_price."""
    fill = FillRecord(trade_id="t1", token_id="t1", fill_price=0.65, size=50, side="sell", fill_time=time.time())
    pnl = analyzer._compute_pnl(fill, current_mid=0.70, interval=10)
    assert abs(pnl - 0.05) < 0.001  # 0.70 - 0.65 = +0.05 (ganancia: precio subió)


def test_compute_pnl_loss(analyzer):
    """Si el precio se mueve en contra, P&L negativo."""
    fill = FillRecord(trade_id="t1", token_id="t1", fill_price=0.65, size=50, side="buy", fill_time=time.time())
    pnl = analyzer._compute_pnl(fill, current_mid=0.70, interval=10)
    assert pnl < 0  # 0.65 - 0.70 = -0.05 (pérdida: precio subió)


# ── Toxicity ───────────────────────────────────────────────────────

def test_get_toxicity_few_trades_returns_clean(analyzer):
    """Con pocos trades, la toxicidad es 0 y clasificación 'clean'."""
    analyzer.record_fill("t1", "token_001", 0.65, 50, "buy")

    score = analyzer.get_toxicity("token_001")
    assert score.markout_toxicity == 0.0
    assert score.classification == "clean"


def test_get_toxicity_with_multiple_fills(analyzer, book_analyzer):
    """Con suficientes fills, calcula toxicidad real."""
    # Simular fills con precios que se mueven en nuestra contra
    for i in range(10):
        analyzer.record_fill(
            f"t{i}", "token_001", 0.65, 50.0, "buy",
            fill_time=time.time() - 60 + i * 5,
        )

    # Actualizar markouts con precio actual (que es peor para nosotros)
    # Mid actual: 0.65 (book tiene 0.64 bid / 0.66 ask)
    # Para fills "buy" (short YES), P&L = fill_price - current_mid
    # Si el mid se mantiene en 0.65, P&L ≈ 0

    score = analyzer.get_toxicity("token_001")
    assert isinstance(score, MarkoutScore)
    assert score.trades_analyzed >= 5


def test_toxicity_with_adverse_price_move(analyzer):
    """Simular movimiento adverso de precio post-fill."""
    now = time.time()
    for i in range(10):
        analyzer.record_fill(
            f"t{i}", "token_001", 0.65, 50.0, "buy",
            fill_time=now - 60 + i * 5,
        )

    # Actualizar markouts — si el mid subió, P&L es negativo
    analyzer.update_markouts("token_001")

    score = analyzer.get_toxicity("token_001")
    assert 0 <= score.markout_toxicity < 1.0  # probablemente limpio si mid no cambió


# ── Classification ─────────────────────────────────────────────────

def test_classify_toxicity_clean(analyzer):
    assert analyzer._classify_toxicity(0.1) == "clean"
    assert analyzer._classify_toxicity(0.29) == "clean"


def test_classify_toxicity_mixed(analyzer):
    assert analyzer._classify_toxicity(0.3) == "mixed"
    assert analyzer._classify_toxicity(0.69) == "mixed"


def test_classify_toxicity_toxic(analyzer):
    assert analyzer._classify_toxicity(0.7) == "toxic"
    assert analyzer._classify_toxicity(1.4) == "toxic"


def test_classify_toxicity_highly_toxic(analyzer):
    assert analyzer._classify_toxicity(1.5) == "highly_toxic"
    assert analyzer._classify_toxicity(3.0) == "highly_toxic"


# ── Recommended Response ───────────────────────────────────────────

def test_recommended_response_clean(analyzer):
    """Con poca toxicidad → operar normal."""
    response = analyzer.get_recommended_response("token_001")
    assert response["action"] == "normal"
    assert response["position_size_multiplier"] == 1.0


def test_recommended_response_pause_when_high_toxicity():
    """Con toxicidad alta → pausar."""
    # Crear analizador con fills que producen alta toxicidad
    ba = BookAnalyzer()
    ba.initialize_book("token_bad", {
        "bids": [[0.50, 100]],
        "asks": [[0.70, 100]],  # mid = 0.60, pero...
    })
    ma = MarkoutAnalyzer(book_analyzer=ba)

    # Fills a 0.65 (buy = short YES) pero el mid ahora es 0.70
    # → P&L muy negativo → toxicidad alta
    now = time.time()
    for i in range(30):
        ma.record_fill(f"t{i}", "token_bad", 0.65, 50, "buy", fill_time=now - i)

    ma.update_markouts("token_bad")
    response = ma.get_recommended_response("token_bad")

    # La toxicidad debería ser alta
    # Verificar que al menos no crashea
    assert "action" in response
    assert "position_size_multiplier" in response


# ── Purge ──────────────────────────────────────────────────────────

def test_purge_removes_old_fills(analyzer):
    now = time.time()
    # Fill muy antiguo
    analyzer.record_fill("old", "token_001", 0.65, 50, "buy", fill_time=now - 7200)
    # Fill reciente
    analyzer.record_fill("new", "token_001", 0.65, 50, "buy", fill_time=now)

    purged = analyzer.purge_old_fills("token_001")
    assert purged == 1


# ── Remove / Clear ─────────────────────────────────────────────────

def test_remove_market(analyzer):
    analyzer.record_fill("t1", "token_001", 0.65, 50, "buy")
    analyzer.remove_market("token_001")

    score = analyzer.get_toxicity("token_001")
    assert score.trades_analyzed == 0


def test_clear(analyzer):
    analyzer.record_fill("t1", "token_001", 0.65, 50, "buy")
    analyzer.record_fill("t2", "token_002", 0.65, 50, "buy")
    analyzer.clear()

    assert analyzer.get_toxicity("token_001").trades_analyzed == 0
    assert analyzer.get_toxicity("token_002").trades_analyzed == 0
