"""
WebSocket Manager — Conexión persistente al CLOB WebSocket de Polymarket.

Implementa la CAPA DEEP-DIVE (L2) de la arquitectura v2.0:
- Conexión multiplexada con suscripciones dinámicas a canales book/trades/price.
- State machine anti-desync: INIT → CLEAN → RECONCILING (seq_num-based).
- Reconexión con exponential backoff.
- Buffer de deltas durante reconciliación.

NO usamos REST para el Top 50. Toda la data de trading fluye por WebSocket.
El CLOB REST solo se usa para snapshots iniciales y reconciliación.

Usage:
    manager = WebSocketManager(scanner=async_scanner)
    manager.on_book_delta = lambda token_id, delta: ...
    manager.on_trade = lambda condition_id, trade: ...
    await manager.connect()
    await manager.subscribe_book("token_yes_001")
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

import aiohttp

from src.config import get_clob_credentials, has_clob_credentials

logger = logging.getLogger(__name__)

# ── Constantes ──────────────────────────────────────────────────────

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
HEARTBEAT_INTERVAL = 30      # segundos entre pings
RECONNECT_DELAY_BASE = 1.0   # delay inicial para exponential backoff
RECONNECT_DELAY_MAX = 30.0   # delay máximo de reconexión
GAP_TIMEOUT = 300            # segundos sin delta → forzar RECONCILING (5 min)

# Capacidad del buffer circular de deltas durante reconciliación
DELTA_BUFFER_CAPACITY = 1000


# ── Tipos ──────────────────────────────────────────────────────────

class BookState(Enum):
    """Máquina de estados para el order book de un mercado."""
    INIT = "INIT"              # sin datos, necesita snapshot
    CLEAN = "CLEAN"            # book íntegro, trading habilitado
    RECONCILING = "RECONCILING"  # gap detectado, trading pausado


@dataclass
class _BookTracker:
    """Estado interno de un mercado trackeado por WebSocket."""
    token_id: str
    condition_id: str = ""
    state: BookState = BookState.INIT
    seq_num: int = -1          # último seq_num procesado (-1 = ninguno)
    gap_count: int = 0         # gaps en última hora
    last_delta_time: float = 0.0  # timestamp del último delta recibido
    snapshot: dict | None = None  # último snapshot REST
    delta_buffer: list = field(default_factory=list)  # deltas pendientes durante RECONCILING


@dataclass
class WebSocketConfig:
    """Configuración del WebSocket Manager."""
    url: str = MARKET_WS_URL
    heartbeat_interval: float = HEARTBEAT_INTERVAL
    reconnect_delay_base: float = RECONNECT_DELAY_BASE
    reconnect_delay_max: float = RECONNECT_DELAY_MAX
    gap_timeout: float = GAP_TIMEOUT


# ── WebSocket Manager ──────────────────────────────────────────────

class WebSocketManager:
    """Gestor de conexión WebSocket multiplexada al CLOB de Polymarket.

    Mantiene una única conexión persistente con suscripciones dinámicas
    a canales `book`, `trades` y `price`. Implementa reconciliación de
    estado y detección de gaps por pérdida de paquetes.

    Parameters
    ----------
    scanner : AsyncPolymarketScanner | None
        Scanner asíncrono para fetchear snapshots REST durante reconciliación.
    config : WebSocketConfig | None
        Configuración opcional.
    """

    def __init__(
        self,
        scanner=None,
        config: WebSocketConfig | None = None,
    ):
        self._scanner = scanner
        self._config = config or WebSocketConfig()

        # Estado de conexión
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._connected = False
        self._running = False

        # Tracking de mercados
        self._books: dict[str, _BookTracker] = {}  # token_id → tracker

        # Tareas
        self._heartbeat_task: asyncio.Task | None = None
        self._reader_task: asyncio.Task | None = None
        self._health_check_task: asyncio.Task | None = None

        # Backoff
        self._reconnect_attempt = 0

        # Callbacks — se asignan externamente
        self.on_book_delta: Callable | None = None
        self.on_trade: Callable | None = None
        self.on_price: Callable | None = None
        self.on_state_change: Callable | None = None  # (token_id, old_state, new_state)

        # Market channel is PUBLIC — no auth needed for orderbook data
        # User channel (trading) uses API keys loaded from .env
        self._clob_creds = get_clob_credentials()
        self._clob_authed = has_clob_credentials()
        logger.info("CLOB WS: market channel (public) — %s",
                     "user channel available ✅" if self._clob_authed else "user channel unavailable (no .env)")

    # ── Connection Lifecycle ──────────────────────────────────────

    async def connect(self) -> None:
        """Conecta al WebSocket y arranca el bucle de lectura."""
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "polymarket-scout/2.0"},
            )

        self._running = True
        await self._connect_ws()

        # Arrancar tareas de background
        self._reader_task = asyncio.create_task(self._read_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._health_check_task = asyncio.create_task(self._health_check_loop())

    async def disconnect(self) -> None:
        """Cierra la conexión y cancela todas las tareas."""
        self._running = False

        for task in [self._reader_task, self._heartbeat_task, self._health_check_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self._ws and not self._ws.closed:
            await self._ws.close()

        self._connected = False

    async def _connect_ws(self) -> None:
        """Establece (o reestablece) la conexión WebSocket."""
        delay = min(
            self._config.reconnect_delay_max,
            self._config.reconnect_delay_base * (2 ** self._reconnect_attempt),
        )

        while self._running:
            try:
                self._ws = await self._session.ws_connect(
                    self._config.url,
                    heartbeat=self._config.heartbeat_interval,
                )
                self._connected = True
                self._reconnect_attempt = 0
                logger.info("WebSocket conectado a %s", self._config.url)

                # Re-suscribir todos los mercados trackeados
                # (esto envía el mensaje MARKET con todos los assets)
                await self._resubscribe_all()
                return

            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                self._reconnect_attempt += 1
                logger.warning(
                    "WebSocket conexión fallida (intento %d): %s. "
                    "Reintentando en %.1fs...",
                    self._reconnect_attempt, e, delay,
                )
                await asyncio.sleep(delay)
                delay = min(self._config.reconnect_delay_max, delay * 2)

    async def _resubscribe_all(self) -> None:
        """Re-suscribe todos los mercados activos tras reconexión."""
        # Reset all trackers to INIT (will transition to CLEAN on first delta)
        for token_id, tracker in list(self._books.items()):
            await self._transition_state(
                tracker, BookState.INIT,
                reason="reconnect",
            )

        # Send full MARKET subscription with ALL tracked assets
        await self._send_market_subscription()

    async def _send_market_subscription(self) -> None:
        """Send MARKET subscription with all tracked asset IDs.

        This message ACTIVATES the data flow. The server requires it to be
        sent before individual subscribe/unsubscribe messages take effect.
        Always send it — even with 0 assets — to establish the channel.
        """
        if not self._ws:
            return
        asset_ids = list(self._books.keys())
        try:
            msg = {"type": "MARKET", "assets_ids": asset_ids}
            await self._ws.send_json(msg)
            if asset_ids:
                logger.info("CLOB WS: MARKET sent with %d assets", len(asset_ids))
            else:
                logger.info("CLOB WS: MARKET sent (empty — channel initialized)")
        except Exception as e:
            logger.warning("CLOB WS: MARKET failed — %s", e)

    async def _flush_market_subscription(self) -> None:
        """Re-send MARKET message with current assets. Called after adding books."""
        if self._connected and self._ws:
            await self._send_market_subscription()

    # ── Subscribe / Unsubscribe ─────────────────────────────────────

    async def subscribe_book(
        self,
        token_id: str,
        condition_id: str = "",
        fetch_snapshot: bool = True,
    ) -> None:
        """Suscribe a actualizaciones del order book para un token.

        Si fetch_snapshot=True, obtiene el snapshot REST inicial antes de
        procesar deltas (bootstrap).
        """
        if token_id in self._books:
            logger.debug("Token %s ya suscrito", token_id)
            return

        tracker = _BookTracker(
            token_id=token_id,
            condition_id=condition_id,
            state=BookState.INIT,
        )
        self._books[token_id] = tracker

        # Bootstrap: obtener snapshot REST y aplicarlo al BookAnalyzer
        if fetch_snapshot and self._scanner:
            try:
                snapshot = await self._scanner.get_book_async(token_id)
                tracker.snapshot = snapshot
                # Asumimos que el snapshot incluye seq_num o lo derivamos
                if "seq_num" in snapshot:
                    tracker.seq_num = int(snapshot["seq_num"])
                tracker.last_delta_time = time.monotonic()
                await self._transition_state(tracker, BookState.CLEAN, reason="bootstrap")
                # ── Aplicar snapshot al BookAnalyzer vía callback ──
                # Esto asegura que el order book local se inicialice con
                # datos reales ANTES de procesar deltas del WS.
                if self.on_book_delta:
                    self.on_book_delta(token_id, snapshot)
                    logger.debug("BookAnalyzer initialized from REST snapshot for %s", token_id[:16])
            except Exception as e:
                logger.warning("No se pudo obtener snapshot para %s: %s", token_id, e)
                # Seguimos en INIT, los deltas nos sacarán de ahí

        await self._subscribe_internal(token_id)

    async def _subscribe_internal(self, token_id: str) -> None:
        """Envía mensaje de suscripción al WebSocket (protocolo CLOB Market)."""
        if not self._connected or self._ws is None:
            logger.warning("WS subscribe deferred for %s (not connected)", token_id[:12])
            return

        try:
            await self._ws.send_json({
                "assets_ids": [token_id],
                "operation": "subscribe",
            })
            logger.info("WS ⇒ subscribe: %s", token_id[:16])
        except Exception as e:
            logger.error("Error suscribiendo %s: %s", token_id, e)

    async def unsubscribe_book(self, token_id: str) -> None:
        """Cancela la suscripción y elimina el tracker."""
        if token_id not in self._books:
            return

        if self._connected and self._ws:
            try:
                await self._ws.send_json({
                    "assets_ids": [token_id],
                    "operation": "unsubscribe",
                })
            except Exception:
                pass

        del self._books[token_id]

    # ── Read Loop ─────────────────────────────────────────────────

    async def _read_loop(self) -> None:
        """Bucle principal de lectura de mensajes del WebSocket."""
        while self._running:
            try:
                if not self._ws or self._ws.closed:
                    await self._connect_ws()
                    continue

                msg = await self._ws.receive()

                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(msg.data)

                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    logger.warning("WebSocket cerrado por el servidor")
                    self._connected = False
                    await self._connect_ws()

                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error("WebSocket error: %s", self._ws.exception())
                    self._connected = False
                    await self._connect_ws()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error en read_loop: %s", e)
                self._connected = False
                await asyncio.sleep(0.5)

    async def _handle_message(self, raw: str) -> None:
        """Procesa un mensaje recibido del WebSocket (protocolo CLOB Market).

        El servidor manda arrays de objetos, donde cada objeto es un evento
        con campos: market, asset_id, timestamp, y posiblemente bids/asks.
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("Mensaje no-JSON: %.100s", raw)
            return

        # Handle array messages — each element is an event
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    await self._handle_single_message(item)
                else:
                    logger.debug("WS ⇐ array element non-dict: %s", type(item).__name__)
            return

        # Handle single dict message
        if isinstance(data, dict):
            await self._handle_single_message(data)
            return

        logger.debug("WS ⇐ unexpected: %s", type(data).__name__)

    async def _handle_single_message(self, data: dict) -> None:
        """Process a single market event dict from the WS."""
        event_type = data.get("event_type", data.get("type", ""))

        if event_type in ("book",):
            await self._handle_book_delta(data)
        elif event_type in ("price_change", "last_trade_price"):
            await self._handle_price(data)
        elif event_type == "tick_size_change":
            logger.info("WS ⇐ tick_size_change: %s", json.dumps(data)[:200])
        elif event_type == "error":
            logger.error("WS ⇐ server error: %s", data)
        elif "asset_id" in data:
            # Market event without explicit event_type — log first occurrence
            logger.info("WS ⇐ market event: asset=%s market=%s",
                        data.get("asset_id", "?")[:16],
                        data.get("market", "?")[:16])
        elif event_type:
            logger.info("WS ⇐ unknown [%s]: %.200s", event_type, json.dumps(data)[:200])
        else:
            logger.info("WS ⇐ msg: %.300s", json.dumps(data)[:300])

    # ── Message Handlers ──────────────────────────────────────────

    async def _handle_book_delta(self, data: dict) -> None:
        """Procesa un evento 'book' del WebSocket de Polymarket.

        Formato real: {event_type:'book', asset_id, market, bids, asks,
                        tick_size, last_trade_price, hash, timestamp}
        """
        asset_id = data.get("asset_id", "")
        if not asset_id:
            return

        tracker = self._books.get(asset_id)
        if tracker is None:
            return  # not tracked by us

        now = time.monotonic()
        tracker.last_delta_time = now

        # Polymarket sends full snapshots, not deltas. Mark as CLEAN on first book.
        if tracker.state == BookState.INIT:
            await self._transition_state(tracker, BookState.CLEAN, reason="first_book")
        elif tracker.state == BookState.RECONCILING:
            await self._transition_state(tracker, BookState.CLEAN, reason="book_received")

        # Notify callback
        if self.on_book_delta:
            try:
                self.on_book_delta(asset_id, data)
            except Exception as e:
                logger.error("Error en on_book_delta: %s", e)

    async def _handle_trade(self, data: dict) -> None:
        """Procesa un trade print (ejecución real)."""
        if self.on_trade:
            try:
                self.on_trade(data)
            except Exception as e:
                logger.error("Error en on_trade: %s", e)

    async def _handle_price(self, data: dict) -> None:
        """Procesa una actualización de precio."""
        if self.on_price:
            try:
                self.on_price(data)
            except Exception as e:
                logger.error("Error en on_price: %s", e)

    # ── Heartbeat & Health Check ──────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        """Envía pings periódicos para mantener viva la conexión."""
        while self._running:
            await asyncio.sleep(self._config.heartbeat_interval)
            if self._ws and not self._ws.closed:
                try:
                    await self._ws.ping()
                except Exception:
                    self._connected = False

    async def _health_check_loop(self) -> None:
        """Monitoriza gaps y fuerza reconciliación si es necesario."""
        while self._running:
            await asyncio.sleep(5)  # check cada 5s

            now = time.monotonic()
            for token_id, tracker in list(self._books.items()):
                if tracker.state != BookState.CLEAN:
                    continue

                # Sin deltas por mucho tiempo → posible desconexión silenciosa
                if tracker.last_delta_time > 0:
                    age = now - tracker.last_delta_time
                    if age > self._config.gap_timeout:
                        logger.warning(
                            "Timeout de deltas para %s: %.0fs sin actividad",
                            token_id, age,
                        )
                        await self._transition_state(
                            tracker, BookState.RECONCILING,
                            reason=f"timeout: {age:.0f}s sin delta",
                        )

    # ── Reconciliation ────────────────────────────────────────────

    async def reconcile(self, token_id: str) -> bool:
        """Fuerza la reconciliación de un mercado: fetch snapshot + replay deltas.

        Returns True si la reconciliación fue exitosa.
        """
        tracker = self._books.get(token_id)
        if tracker is None:
            return False

        if tracker.state not in (BookState.INIT, BookState.RECONCILING):
            await self._transition_state(tracker, BookState.RECONCILING, reason="manual")

        if not self._scanner:
            logger.warning("No hay scanner para reconciliar %s", token_id)
            return False

        try:
            snapshot = await self._scanner.get_book_async(token_id)
            tracker.snapshot = snapshot

            # Extraer seq_num del snapshot
            snap_seq = -1
            if "seq_num" in snapshot:
                snap_seq = int(snapshot["seq_num"])

            # Replay de deltas bufferizados
            replayed = 0
            for delta in tracker.delta_buffer:
                delta_seq = delta.get("seq_num", delta.get("sequence", -1))
                if delta_seq > snap_seq:
                    # Aplicar delta (callback externo)
                    if self.on_book_delta:
                        self.on_book_delta(token_id, delta)
                    replayed += 1
                    snap_seq = delta_seq

            tracker.seq_num = snap_seq if snap_seq >= 0 else 0
            tracker.delta_buffer.clear()
            tracker.last_delta_time = time.monotonic()

            await self._transition_state(tracker, BookState.CLEAN, reason="reconciled")
            logger.info(
                "Reconciliación exitosa para %s: %d deltas replayed",
                token_id, replayed,
            )
            return True

        except Exception as e:
            logger.warning("Reconciliación fallida para %s: %s", token_id, e)
            # Mantener en RECONCILING, reintentar en el próximo ciclo
            return False

    async def _transition_state(
        self, tracker: _BookTracker, new_state: BookState, reason: str = "",
    ) -> None:
        """Transiciona un tracker a un nuevo estado."""
        if tracker.state == new_state:
            return

        old_state = tracker.state
        tracker.state = new_state
        logger.debug(
            "%s: %s → %s (%s)",
            tracker.token_id, old_state.value, new_state.value, reason,
        )

        if self.on_state_change:
            try:
                self.on_state_change(tracker.token_id, old_state, new_state)
            except Exception as e:
                logger.error("Error en on_state_change: %s", e)

    # ── Query API ─────────────────────────────────────────────────

    def get_state(self, token_id: str) -> BookState | None:
        """Retorna el estado actual de un mercado trackeado."""
        tracker = self._books.get(token_id)
        return tracker.state if tracker else None

    def is_clean(self, token_id: str) -> bool:
        """Verifica si un mercado está en estado CLEAN (trading seguro)."""
        return self.get_state(token_id) == BookState.CLEAN

    def get_tracked_tokens(self) -> list[str]:
        """Retorna todos los token_ids actualmente suscritos."""
        return list(self._books.keys())

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── Metrics ───────────────────────────────────────────────────

    def get_health_metrics(self) -> dict:
        """Métricas de salud para monitorización."""
        now = time.monotonic()
        metrics = {}
        for token_id, tracker in self._books.items():
            metrics[token_id] = {
                "state": tracker.state.value,
                "seq_num": tracker.seq_num,
                "gap_count": tracker.gap_count,
                "last_delta_age_ms": (
                    (now - tracker.last_delta_time) * 1000
                    if tracker.last_delta_time > 0
                    else -1
                ),
                "buffer_size": len(tracker.delta_buffer),
            }
        return metrics
