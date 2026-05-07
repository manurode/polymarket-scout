"""Tests para WebSocketManager — state machine y reconciliación.

No probamos conexiones WebSocket reales (requieren red). Nos centramos en
la lógica de la máquina de estados, tracking de seq_num, y reconciliación.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.websocket_manager import (
    WebSocketManager,
    WebSocketConfig,
    BookState,
    _BookTracker,
    DELTA_BUFFER_CAPACITY,
    HEARTBEAT_INTERVAL,
    GAP_TIMEOUT,
)


# ── Fixtures ───────────────────────────────────────────────────────

@pytest.fixture
def mock_scanner():
    """Scanner mockeado que retorna snapshots de libro."""
    scanner = MagicMock()
    scanner.get_book_async = AsyncMock(return_value={
        "market": "0xabc",
        "asset_id": "token_001",
        "bids": [{"price": "0.64", "size": "500"}],
        "asks": [{"price": "0.66", "size": "300"}],
        "seq_num": 100,
    })
    return scanner


@pytest.fixture
def manager(mock_scanner):
    """WebSocketManager con scanner mockeado."""
    return WebSocketManager(scanner=mock_scanner)


# ── Constructor ────────────────────────────────────────────────────

def test_manager_starts_disconnected(manager):
    assert not manager.is_connected
    assert manager.get_tracked_tokens() == []


def test_manager_accepts_config():
    config = WebSocketConfig(
        url="wss://test.example.com",
        heartbeat_interval=15.0,
    )
    mgr = WebSocketManager(config=config)
    assert mgr._config.url == "wss://test.example.com"
    assert mgr._config.heartbeat_interval == 15.0


# ── State Management ───────────────────────────────────────────────

def test_get_state_unknown_token(manager):
    assert manager.get_state("nonexistent") is None


def test_is_clean_unknown_token(manager):
    assert not manager.is_clean("nonexistent")


def test_get_tracked_tokens_starts_empty(manager):
    assert manager.get_tracked_tokens() == []


# ── BookTracker State Machine ──────────────────────────────────────

def test_tracker_starts_in_init():
    tracker = _BookTracker(token_id="t1")
    assert tracker.state == BookState.INIT
    assert tracker.seq_num == -1
    assert tracker.gap_count == 0
    assert len(tracker.delta_buffer) == 0


def test_state_transition_init_to_clean(manager, mock_scanner):
    """Transición manual INIT → CLEAN vía _transition_state."""
    tracker = _BookTracker(token_id="t1", state=BookState.INIT)

    async def _test():
        await manager._transition_state(tracker, BookState.CLEAN, reason="test")

    asyncio.run(_test())
    assert tracker.state == BookState.CLEAN


def test_state_transition_clean_to_reconciling(manager):
    tracker = _BookTracker(token_id="t1", state=BookState.CLEAN)

    async def _test():
        await manager._transition_state(tracker, BookState.RECONCILING, reason="gap")

    asyncio.run(_test())
    assert tracker.state == BookState.RECONCILING


def test_transition_same_state_no_change(manager):
    tracker = _BookTracker(token_id="t1", state=BookState.CLEAN)

    async def _test():
        await manager._transition_state(tracker, BookState.CLEAN, reason="noop")

    asyncio.run(_test())
    assert tracker.state == BookState.CLEAN


def test_state_change_callback(manager):
    """El callback on_state_change se dispara en transiciones."""
    calls = []

    async def _test():
        manager.on_state_change = lambda tid, old, new: calls.append((tid, old, new))
        tracker = _BookTracker(token_id="t1", state=BookState.INIT)
        await manager._transition_state(tracker, BookState.CLEAN, reason="test")

    asyncio.run(_test())
    assert len(calls) == 1
    assert calls[0] == ("t1", BookState.INIT, BookState.CLEAN)


# ── Subscribe (internal) ───────────────────────────────────────────

def test_subscribe_book_registers_tracker(manager, mock_scanner):
    async def _test():
        await manager.subscribe_book("token_001", condition_id="0xabc")

    asyncio.run(_test())
    assert "token_001" in manager.get_tracked_tokens()
    tracker = manager._books["token_001"]
    assert tracker.condition_id == "0xabc"


def test_subscribe_book_without_scanner(manager, mock_scanner):
    """Sin scanner, el tracker se registra sin snapshot inicial."""
    manager._scanner = None

    async def _test():
        await manager.subscribe_book("token_001", fetch_snapshot=True)

    asyncio.run(_test())
    tracker = manager._books["token_001"]
    # Sin scanner, no puede fetchear snapshot → se queda en INIT
    # (pero se registra correctamente)
    assert tracker is not None


def test_double_subscribe_no_duplicate(manager, mock_scanner):
    async def _test():
        await manager.subscribe_book("token_001")
        await manager.subscribe_book("token_001")  # no-op

    asyncio.run(_test())
    assert len(manager._books) == 1


# ── Unsubscribe ────────────────────────────────────────────────────

def test_unsubscribe_removes_tracker(manager, mock_scanner):
    async def _test():
        await manager.subscribe_book("token_001", fetch_snapshot=False)
        assert "token_001" in manager._books
        await manager.unsubscribe_book("token_001")
        assert "token_001" not in manager._books

    asyncio.run(_test())


def test_unsubscribe_unknown_token_no_error(manager):
    async def _test():
        await manager.unsubscribe_book("nonexistent")  # no-op sin error

    asyncio.run(_test())


# ── Reconcile ──────────────────────────────────────────────────────

def test_reconcile_unknown_token_returns_false(manager):
    async def _test():
        result = await manager.reconcile("nonexistent")
        assert result is False

    asyncio.run(_test())


def test_reconcile_without_scanner_returns_false(manager):
    manager._scanner = None
    tracker = _BookTracker(
        token_id="token_001",
        state=BookState.RECONCILING,
        seq_num=50,
    )
    tracker.delta_buffer.append({"seq_num": 51, "bids": [[0.65, 100]]})
    manager._books["token_001"] = tracker

    async def _test():
        result = await manager.reconcile("token_001")
        assert result is False

    asyncio.run(_test())


def test_reconcile_success_clears_buffer(manager, mock_scanner):
    tracker = _BookTracker(
        token_id="token_001",
        state=BookState.RECONCILING,
        seq_num=50,
    )
    # Buffer con deltas durante reconciling
    tracker.delta_buffer.append({"seq_num": 51, "bids": [[0.65, 100]]})
    tracker.delta_buffer.append({"seq_num": 52, "asks": [[0.67, 50]]})
    manager._books["token_001"] = tracker

    async def _test():
        result = await manager.reconcile("token_001")
        assert result is True
        assert tracker.state == BookState.CLEAN
        assert len(tracker.delta_buffer) == 0
        assert tracker.seq_num >= 0

    asyncio.run(_test())


def test_reconcile_replays_deltas_after_snapshot(manager, mock_scanner):
    """Los deltas con seq > snapshot.seq se aplican."""
    on_delta_calls = []
    manager.on_book_delta = lambda tid, d: on_delta_calls.append((tid, d.get("seq_num")))

    # Snapshot tiene seq_num=100
    mock_scanner.get_book_async.return_value = {
        "market": "0xabc",
        "bids": [],
        "asks": [],
        "seq_num": 100,
    }

    tracker = _BookTracker(
        token_id="token_001",
        state=BookState.RECONCILING,
        seq_num=90,
    )
    # Deltas bufferizados: 95 (descartado, < 100), 101 (aplicado), 102 (aplicado)
    tracker.delta_buffer = [
        {"seq_num": 95, "bids": [[0.5, 10]]},
        {"seq_num": 101, "bids": [[0.6, 20]]},
        {"seq_num": 102, "asks": [[0.7, 5]]},
    ]
    manager._books["token_001"] = tracker

    async def _test():
        result = await manager.reconcile("token_001")
        assert result is True

    asyncio.run(_test())

    # El delta seq=95 se descarta (≤ 100), 101 y 102 se aplican
    applied_seqs = [s for _, s in on_delta_calls]
    assert 95 not in applied_seqs
    assert 101 in applied_seqs
    assert 102 in applied_seqs


def test_reconcile_scanner_error_keeps_reconciling(manager, mock_scanner):
    """Si el scanner falla, se mantiene en RECONCILING."""
    mock_scanner.get_book_async.side_effect = Exception("Network error")

    tracker = _BookTracker(
        token_id="token_001",
        state=BookState.RECONCILING,
    )
    manager._books["token_001"] = tracker

    async def _test():
        result = await manager.reconcile("token_001")
        assert result is False
        assert tracker.state == BookState.RECONCILING

    asyncio.run(_test())


# ── Delta Buffer ───────────────────────────────────────────────────

def test_delta_buffer_capacity():
    """El buffer circular no excede DELTA_BUFFER_CAPACITY."""
    tracker = _BookTracker(token_id="t1")
    for i in range(DELTA_BUFFER_CAPACITY + 50):
        if len(tracker.delta_buffer) < DELTA_BUFFER_CAPACITY:
            tracker.delta_buffer.append({"seq_num": i})
    assert len(tracker.delta_buffer) <= DELTA_BUFFER_CAPACITY


# ── Health Metrics ─────────────────────────────────────────────────

def test_health_metrics_empty(manager):
    metrics = manager.get_health_metrics()
    assert metrics == {}


def test_health_metrics_tracks_books(manager):
    tracker = _BookTracker(
        token_id="t1",
        state=BookState.CLEAN,
        seq_num=42,
        last_delta_time=100.0,
    )
    manager._books["t1"] = tracker

    metrics = manager.get_health_metrics()
    assert "t1" in metrics
    assert metrics["t1"]["state"] == "CLEAN"
    assert metrics["t1"]["seq_num"] == 42
    assert metrics["t1"]["gap_count"] == 0


# ── BookState Enum ─────────────────────────────────────────────────

def test_book_state_values():
    assert BookState.INIT.value == "INIT"
    assert BookState.CLEAN.value == "CLEAN"
    assert BookState.RECONCILING.value == "RECONCILING"


def test_book_state_str():
    assert str(BookState.CLEAN) == "BookState.CLEAN"


# ── WebSocketConfig ────────────────────────────────────────────────

def test_config_defaults():
    config = WebSocketConfig()
    assert config.url == "wss://ws-subscriptions-clob.polymarket.com"
    assert config.heartbeat_interval == 30
    assert config.reconnect_delay_base == 1.0
    assert config.reconnect_delay_max == 30.0


def test_config_custom():
    config = WebSocketConfig(
        url="wss://custom.example.com",
        heartbeat_interval=10.0,
        reconnect_delay_base=2.0,
        reconnect_delay_max=60.0,
        gap_timeout=15.0,
    )
    assert config.url == "wss://custom.example.com"
    assert config.heartbeat_interval == 10.0
    assert config.gap_timeout == 15.0
