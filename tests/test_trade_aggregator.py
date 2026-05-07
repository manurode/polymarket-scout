"""Tests para TradeAggregator — agregación de trades y TFI."""

import time
import pytest
from src.trade_aggregator import TradeAggregator, TradeRecord, TFIResult


# ── Fixtures ───────────────────────────────────────────────────────

@pytest.fixture
def aggregator():
    return TradeAggregator()


@pytest.fixture
def sample_buy_trade():
    return {
        "price": "0.65",
        "size": "100.0",
        "side": "BUY",
        "id": "trade_001",
        "timestamp": time.time() - 10,  # hace 10s
    }


@pytest.fixture
def sample_sell_trade():
    return {
        "price": "0.64",
        "size": "50.0",
        "side": "SELL",
        "id": "trade_002",
        "timestamp": time.time() - 20,  # hace 20s
    }


# ── Add Trade ─────────────────────────────────────────────────────

def test_add_trade_returns_trade_record(aggregator, sample_buy_trade):
    trade = aggregator.add_trade("0xabc", sample_buy_trade)
    assert isinstance(trade, TradeRecord)
    assert trade.side == "buy"
    assert trade.price == 0.65
    assert trade.size == 100.0
    assert trade.condition_id == "0xabc"


def test_add_trade_parses_sell_side(aggregator, sample_sell_trade):
    trade = aggregator.add_trade("0xabc", sample_sell_trade)
    assert trade.side == "sell"


def test_add_trade_defaults_timestamp(aggregator):
    trade = aggregator.add_trade("0xabc", {"price": "0.5", "size": "10", "side": "BUY"})
    assert trade.timestamp > 0
    assert time.time() - trade.timestamp < 2  # timestamp reciente


def test_add_trade_increments_count(aggregator, sample_buy_trade):
    assert aggregator.total_trades == 0
    aggregator.add_trade("0xabc", sample_buy_trade)
    assert aggregator.total_trades == 1
    aggregator.add_trade("0xabc", sample_buy_trade)
    assert aggregator.total_trades == 2


# ── TFI Calculation (buy-heavy) ────────────────────────────────────

def test_tfi_buy_heavy(aggregator, sample_buy_trade, sample_sell_trade):
    """2 compras de 100 + 1 venta de 50 = TFI positivo."""
    now = time.time()
    aggregator.add_trade("0xabc", {**sample_buy_trade, "timestamp": now - 10, "size": "100"})
    aggregator.add_trade("0xabc", {**sample_buy_trade, "timestamp": now - 5, "size": "100"})
    aggregator.add_trade("0xabc", {**sample_sell_trade, "timestamp": now - 15, "size": "50"})

    tfi = aggregator.get_tfi("0xabc", window=60)

    # buy_vol: 200, sell_vol: 50, total: 250
    # tfi = (200-50)/250 = 150/250 = 0.6
    assert abs(tfi.tfi - 0.6) < 0.001
    assert tfi.buy_volume == 200.0
    assert tfi.sell_volume == 50.0
    assert tfi.trade_count == 3


def test_tfi_sell_heavy(aggregator):
    now = time.time()
    aggregator.add_trade("0xabc", {"price": "0.5", "size": "10", "side": "BUY", "timestamp": now - 5})
    aggregator.add_trade("0xabc", {"price": "0.5", "size": "90", "side": "SELL", "timestamp": now - 10})

    tfi = aggregator.get_tfi("0xabc", window=60)
    # (10-90)/100 = -0.8
    assert abs(tfi.tfi - (-0.8)) < 0.001


def test_tfi_no_trades(aggregator):
    tfi = aggregator.get_tfi("0xabc", window=60)
    assert tfi.tfi == 0.0
    assert tfi.trade_count == 0
    assert tfi.buy_volume == 0.0
    assert tfi.sell_volume == 0.0


def test_tfi_all_buys(aggregator):
    now = time.time()
    aggregator.add_trade("0xabc", {"price": "0.5", "size": "100", "side": "BUY", "timestamp": now})
    aggregator.add_trade("0xabc", {"price": "0.5", "size": "50", "side": "BUY", "timestamp": now})

    tfi = aggregator.get_tfi("0xabc", window=60)
    assert tfi.tfi == 1.0


def test_tfi_all_sells(aggregator):
    now = time.time()
    aggregator.add_trade("0xabc", {"price": "0.5", "size": "100", "side": "SELL", "timestamp": now})

    tfi = aggregator.get_tfi("0xabc", window=60)
    assert tfi.tfi == -1.0


# ── TFI Window ─────────────────────────────────────────────────────

def test_tfi_respects_window(aggregator):
    """Trades fuera de la ventana no se incluyen."""
    now = time.time()
    # Trade de hace 120s
    aggregator.add_trade("0xabc", {"price": "0.5", "size": "100", "side": "BUY", "timestamp": now - 120})
    # Trade de hace 10s
    aggregator.add_trade("0xabc", {"price": "0.5", "size": "50", "side": "SELL", "timestamp": now - 10})

    tfi_30s = aggregator.get_tfi("0xabc", window=30)
    # Solo el sell de 50: tfi = -1.0
    assert abs(tfi_30s.tfi - (-1.0)) < 0.001
    assert tfi_30s.trade_count == 1

    tfi_300s = aggregator.get_tfi("0xabc", window=300)
    # Ambos: buy=100, sell=50 → (100-50)/150 = 0.333
    assert abs(tfi_300s.tfi - 0.333) < 0.01
    assert tfi_300s.trade_count == 2


# ── get_all_tfis ───────────────────────────────────────────────────

def test_get_all_tfis_returns_all_windows(aggregator):
    now = time.time()
    aggregator.add_trade("0xabc", {"price": "0.5", "size": "100", "side": "BUY", "timestamp": now})

    tfis = aggregator.get_all_tfis("0xabc")
    # Las keys usan formato legible: "30s", "1m", "5m"
    assert any("30" in k for k in tfis)
    assert any("1m" in k or "60" in k for k in tfis)
    assert any("5m" in k or "300" in k for k in tfis)
    assert all(isinstance(t, TFIResult) for t in tfis.values())


# ── Volume Ratio ───────────────────────────────────────────────────

def test_volume_ratio(aggregator):
    now = time.time()
    aggregator.add_trade("0xabc", {"price": "0.5", "size": "100", "side": "BUY", "timestamp": now})
    aggregator.add_trade("0xabc", {"price": "0.5", "size": "50", "side": "SELL", "timestamp": now})

    ratio = aggregator.get_volume_ratio("0xabc", window=60)
    assert ratio == 2.0  # 100/50


def test_volume_ratio_no_sells(aggregator):
    now = time.time()
    aggregator.add_trade("0xabc", {"price": "0.5", "size": "100", "side": "BUY", "timestamp": now})

    ratio = aggregator.get_volume_ratio("0xabc", window=60)
    assert ratio == float("inf")


def test_volume_ratio_no_buys(aggregator):
    now = time.time()
    aggregator.add_trade("0xabc", {"price": "0.5", "size": "100", "side": "SELL", "timestamp": now})

    ratio = aggregator.get_volume_ratio("0xabc", window=60)
    assert ratio == 0.0


# ── Purge ──────────────────────────────────────────────────────────

def test_purge_removes_old_trades(aggregator):
    now = time.time()
    aggregator.add_trade("0xabc", {"price": "0.5", "size": "100", "side": "BUY", "timestamp": now - 7200})  # 2h ago
    aggregator.add_trade("0xabc", {"price": "0.5", "size": "50", "side": "SELL", "timestamp": now})  # now

    purged = aggregator.purge(max_age=3600)  # 1h
    assert purged == 1  # un mercado afectado

    tfi = aggregator.get_tfi("0xabc", window=3600)
    assert tfi.trade_count == 1  # solo queda el trade reciente


# ── Remove / Clear ─────────────────────────────────────────────────

def test_remove_market(aggregator, sample_buy_trade):
    aggregator.add_trade("0xabc", sample_buy_trade)
    assert len(aggregator) == 1

    aggregator.remove_market("0xabc")
    assert len(aggregator) == 0
    assert aggregator.total_trades == 0


def test_clear(aggregator, sample_buy_trade):
    aggregator.add_trade("0xabc", sample_buy_trade)
    aggregator.add_trade("0xdef", sample_buy_trade)
    assert aggregator.total_trades == 2

    aggregator.clear()
    assert aggregator.total_trades == 0


# ── Multiple Markets ───────────────────────────────────────────────

def test_multiple_markets_independent(aggregator):
    now = time.time()
    aggregator.add_trade("0xabc", {"price": "0.5", "size": "100", "side": "BUY", "timestamp": now})
    aggregator.add_trade("0xdef", {"price": "0.5", "size": "100", "side": "SELL", "timestamp": now})

    assert aggregator.get_tfi("0xabc", window=60).tfi == 1.0
    assert aggregator.get_tfi("0xdef", window=60).tfi == -1.0


# ── Buffer Circular ────────────────────────────────────────────────

def test_buffer_circular_caps_trades(aggregator):
    """No crece indefinidamente — buffer circular."""
    now = time.time()
    for i in range(200):  # max default is 10000
        aggregator.add_trade("0xabc", {
            "price": "0.5", "size": "1", "side": "BUY",
            "timestamp": now, "id": str(i),
        })

    # No debería crecer más allá del buffer
    assert aggregator.total_trades <= 10000
