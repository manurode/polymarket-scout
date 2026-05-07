"""
Redis Message Bus — Pub/Sub backbone para la arquitectura multi-proceso.

En producción, Redis actúa como el sistema nervioso central de Scout Lab v2.0,
conectando los 6 daemons especializados (Radar, CLOB, Whale, Strategy, Portfolio,
Risk) mediante canales Pub/Sub.

Este módulo proporciona una capa de abstracción que:
- En producción: usa redis-py con Pub/Sub real.
- En desarrollo/paper trading: usa un bus in-memory (dict + asyncio.Queue).
- Failover automático: si Redis no está disponible, degrada a in-memory.

Canales definidos (§5.1 del ARCHITECTURE_V2.md):
- radar:update       — snapshots Gamma cada 30s
- market:enter_top50 — nuevo mercado onboardeado al CLOB
- market:exit_top50  — mercado removido del CLOB
- book:delta         — deltas de order book
- trade:print        — ejecuciones reales
- whale:flow         — flujo de ballenas por mercado
- signal:detected    — señales generadas
- strategy:decision  — decisiones de trading
- risk:allocation    — asignaciones de capital del bandit

Uso:
    bus = await MessageBus.create()  # auto-detecta Redis o in-memory
    await bus.publish("radar:update", {"markets": [...]})
    await bus.subscribe("radar:update", callback)
"""

import asyncio
import json
import logging
from collections import defaultdict
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────

REDIS_URL = "redis://localhost:6379"
REDIS_AVAILABLE = False

try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    logger.info("redis-py no instalado — usando bus in-memory")


# ── Abstract Interface ────────────────────────────────────────────

class MessageBus:
    """Interfaz abstracta del bus de mensajes."""

    async def publish(self, channel: str, message: dict) -> None:
        raise NotImplementedError

    async def subscribe(self, channel: str, callback: Callable) -> None:
        raise NotImplementedError

    async def unsubscribe(self, channel: str) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    async def get(self, key: str) -> Optional[str]:
        """KV store: get value."""
        raise NotImplementedError

    async def set(self, key: str, value: str, ttl: int = 0) -> None:
        """KV store: set value with optional TTL."""
        raise NotImplementedError


# ── In-Memory Bus (fallback) ──────────────────────────────────────

class InMemoryBus(MessageBus):
    """Bus de mensajes in-memory para desarrollo y paper trading.

    Usa dict + asyncio para simular Pub/Sub sin Redis.
    """

    def __init__(self):
        self._subscribers: dict[str, list[Callable]] = defaultdict(list)
        self._kv: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def publish(self, channel: str, message: dict) -> None:
        """Publica un mensaje a todos los suscriptores del canal."""
        callbacks = self._subscribers.get(channel, [])
        if not callbacks:
            return

        for cb in callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(message)
                else:
                    cb(message)
            except Exception as e:
                logger.error("Error en callback de %s: %s", channel, e)

    async def subscribe(self, channel: str, callback: Callable) -> None:
        """Suscribe un callback a un canal."""
        async with self._lock:
            self._subscribers[channel].append(callback)

    async def unsubscribe(self, channel: str) -> None:
        """Elimina todos los suscriptores de un canal."""
        async with self._lock:
            self._subscribers.pop(channel, None)

    async def get(self, key: str) -> Optional[str]:
        return self._kv.get(key)

    async def set(self, key: str, value: str, ttl: int = 0) -> None:
        self._kv[key] = value

    async def close(self) -> None:
        self._subscribers.clear()
        self._kv.clear()


# ── Redis Bus (producción) ────────────────────────────────────────

class RedisBus(MessageBus):
    """Bus de mensajes respaldado por Redis Pub/Sub."""

    def __init__(self, redis_url: str = REDIS_URL):
        self._redis_url = redis_url
        self._redis: Optional[aioredis.Redis] = None
        self._pubsub: Optional[aioredis.client.PubSub] = None
        self._listener_task: Optional[asyncio.Task] = None
        self._callbacks: dict[str, list[Callable]] = defaultdict(list)

    async def _connect(self) -> None:
        if self._redis is None:
            self._redis = aioredis.from_url(self._redis_url)
            self._pubsub = self._redis.pubsub()
            self._listener_task = asyncio.create_task(self._listen())

    async def _listen(self) -> None:
        """Bucle de escucha de mensajes Pub/Sub."""
        try:
            async for message in self._pubsub.listen():
                if message["type"] == "message":
                    channel = message["channel"].decode()
                    data = json.loads(message["data"])
                    await self._dispatch(channel, data)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Error en listener Redis: %s", e)

    async def _dispatch(self, channel: str, data: dict) -> None:
        for cb in self._callbacks.get(channel, []):
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(data)
                else:
                    cb(data)
            except Exception as e:
                logger.error("Error en callback de %s: %s", channel, e)

    async def publish(self, channel: str, message: dict) -> None:
        await self._connect()
        await self._redis.publish(channel, json.dumps(message))

    async def subscribe(self, channel: str, callback: Callable) -> None:
        await self._connect()
        self._callbacks[channel].append(callback)
        await self._pubsub.subscribe(channel)

    async def unsubscribe(self, channel: str) -> None:
        if self._pubsub:
            await self._pubsub.unsubscribe(channel)
        self._callbacks.pop(channel, None)

    async def get(self, key: str) -> Optional[str]:
        await self._connect()
        val = await self._redis.get(key)
        return val.decode() if val else None

    async def set(self, key: str, value: str, ttl: int = 0) -> None:
        await self._connect()
        if ttl > 0:
            await self._redis.setex(key, ttl, value)
        else:
            await self._redis.set(key, value)

    async def close(self) -> None:
        if self._listener_task:
            self._listener_task.cancel()
        if self._pubsub:
            await self._pubsub.close()
        if self._redis:
            await self._redis.close()


# ── Factory ───────────────────────────────────────────────────────

async def create_message_bus(redis_url: str = REDIS_URL) -> MessageBus:
    """Crea el bus de mensajes apropiado (Redis o in-memory).

    Auto-detecta si Redis está disponible. Si no, usa InMemoryBus.
    """
    if REDIS_AVAILABLE:
        try:
            redis_conn = aioredis.from_url(redis_url)
            await redis_conn.ping()
            await redis_conn.close()
            logger.info("Redis detectado — usando RedisBus")
            bus = RedisBus(redis_url)
            return bus
        except Exception as e:
            logger.warning("Redis no disponible (%s) — usando InMemoryBus", e)

    logger.info("Usando InMemoryBus (desarrollo)")
    return InMemoryBus()
