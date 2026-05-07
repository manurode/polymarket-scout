"""Tests para MessageBus (InMemory + Redis factory)."""

import asyncio
import pytest
from src.redis_bus import (
    MessageBus,
    InMemoryBus,
    create_message_bus,
    REDIS_AVAILABLE,
)


# ── InMemoryBus ────────────────────────────────────────────────────

@pytest.fixture
def bus():
    b = InMemoryBus()
    yield b
    # Cleanup sincrono
    b._subscribers.clear()
    b._kv.clear()


@pytest.mark.asyncio
async def test_publish_subscribe(bus):
    received = []

    async def callback(msg):
        received.append(msg)

    await bus.subscribe("test_channel", callback)
    await bus.publish("test_channel", {"data": "hello"})

    # Dar tiempo al event loop
    await asyncio.sleep(0.01)

    assert len(received) == 1
    assert received[0]["data"] == "hello"


@pytest.mark.asyncio
async def test_publish_no_subscribers(bus):
    """Publicar sin suscriptores no causa error."""
    await bus.publish("empty_channel", {"data": "nobody"})


@pytest.mark.asyncio
async def test_unsubscribe(bus):
    received = []

    async def callback(msg):
        received.append(msg)

    await bus.subscribe("ch", callback)
    await bus.publish("ch", {"x": 1})
    await asyncio.sleep(0.01)
    assert len(received) == 1

    await bus.unsubscribe("ch")
    await bus.publish("ch", {"x": 2})
    await asyncio.sleep(0.01)
    assert len(received) == 1  # no más mensajes


@pytest.mark.asyncio
async def test_multiple_subscribers(bus):
    results = []

    async def cb1(msg):
        results.append(("cb1", msg["id"]))

    async def cb2(msg):
        results.append(("cb2", msg["id"]))

    await bus.subscribe("ch", cb1)
    await bus.subscribe("ch", cb2)
    await bus.publish("ch", {"id": 42})
    await asyncio.sleep(0.01)

    assert ("cb1", 42) in results
    assert ("cb2", 42) in results


@pytest.mark.asyncio
async def test_kv_store(bus):
    await bus.set("key1", "value1")
    val = await bus.get("key1")
    assert val == "value1"


@pytest.mark.asyncio
async def test_kv_missing_key(bus):
    val = await bus.get("nonexistent")
    assert val is None


@pytest.mark.asyncio
async def test_sync_callback(bus):
    """Callbacks síncronos también funcionan."""
    received = []

    def callback(msg):
        received.append(msg)

    await bus.subscribe("ch", callback)
    await bus.publish("ch", {"x": 1})
    await asyncio.sleep(0.01)
    assert len(received) == 1


@pytest.mark.asyncio
async def test_callback_error_does_not_crash(bus):
    """Error en un callback no impide que otros reciban mensajes."""
    results = []

    async def bad_cb(msg):
        raise ValueError("test error")

    async def good_cb(msg):
        results.append(msg)

    await bus.subscribe("ch", bad_cb)
    await bus.subscribe("ch", good_cb)
    await bus.publish("ch", {"x": 1})
    await asyncio.sleep(0.01)

    assert len(results) == 1


# ── Factory ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_message_bus_inmemory():
    """Sin Redis, debería crear InMemoryBus."""
    bus = await create_message_bus(redis_url="redis://nonexistent:9999")
    assert isinstance(bus, InMemoryBus)
    await bus.close()


@pytest.mark.asyncio
async def test_message_bus_interface():
    """InMemoryBus implementa la interfaz MessageBus."""
    bus = InMemoryBus()
    assert isinstance(bus, MessageBus)
    await bus.close()
