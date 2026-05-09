"""
Scout Lab v2.0 Orchestrator — Orquestrador central del sistema.

Coordina todos los daemons y módulos de la arquitectura v2.0:
- Radar Daemon (Gamma API polling)
- CLOB Daemon (WebSocket L2)
- Strategy Engine (Market Making, Arbitrage, Spoof Detection)
- Portfolio Manager (Bandit, Kelly, Sortino)
- Whale Tracker (on-chain monitoring)
- Risk Manager (Time-Decay, Markout, Degradation)

Comunicación vía MessageBus (Redis o InMemory).

Uso:
    orchestrator = ScoutOrchestrator(config)
    await orchestrator.start()
    # El sistema corre autónomamente
    await orchestrator.stop()
"""

import asyncio
import logging
import random
import time
from typing import Optional

from src.async_scanner import AsyncPolymarketScanner
from src.selection_engine import SelectionEngine
from src.rate_limiter import RateLimiter
from src.book_analyzer import BookAnalyzer
from src.trade_aggregator import TradeAggregator
from src.spoof_detector import SpoofDetector
from src.websocket_manager import WebSocketManager
from src.time_decay import TimeDecayManager
from src.market_making import MarketMaker
from src.markout_analysis import MarkoutAnalyzer
from src.portfolio_manager import PortfolioManager
from src.whale_tracker import WhaleTracker
from src.degradation import DegradationManager, SystemMode
from src.redis_bus import MessageBus, create_message_bus
from src.paper_trading import PaperTradingEngine
from src.signal_pipeline import SignalPipeline
from src.price_history import PriceHistory
from src.adaptive_strategy_engine import AdaptiveStrategyEngine

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────────────

DEFAULT_RADAR_INTERVAL = 60       # segundos entre polls del Radar
DEFAULT_HEALTH_CHECK_INTERVAL = 10  # segundos entre health checks
DEFAULT_MARKOUT_UPDATE_INTERVAL = 5  # segundos entre actualizaciones de markout
PAPER_MTM_INTERVAL = 5            # segundos entre mark-to-market
PAPER_AUTO_CLOSE_INTERVAL = 10    # segundos entre evaluaciones de auto-close
PAPER_SIGNAL_INTERVAL = 30        # segundos entre señales de trading (demo)
MM_QUOTE_INTERVAL = 10            # segundos entre cotizaciones de market making
MM_TOP_MARKETS = 10               # cuántos mercados del Top 50 trackear con MM


class ScoutOrchestrator:
    """Orquestrador central de Scout Lab v2.0.

    Parameters
    ----------
    config : dict
        Configuración YAML completa.
    bus : MessageBus | None
        Bus de mensajes. Si es None, se crea automáticamente.
    """

    def __init__(
        self,
        config: dict,
        bus: Optional[MessageBus] = None,
    ):
        self.config = config
        self._bus = bus

        # ── Componentes Core ──────────────────────────────────
        self.scanner: Optional[AsyncPolymarketScanner] = None
        self.rate_limiter = RateLimiter()
        self.selection_engine = SelectionEngine(
            top_n=config.get("selection", {}).get("top_n", 50),
        )

        # ── Phase 2: Real-Time Data ───────────────────────────
        self.book_analyzer = BookAnalyzer()
        self.trade_aggregator = TradeAggregator()
        self.spoof_detector: Optional[SpoofDetector] = None
        self.ws_manager: Optional[WebSocketManager] = None

        # ── Phase 3: Strategies ───────────────────────────────
        self.time_decay = TimeDecayManager()
        self.market_maker: Optional[MarketMaker] = None
        self.markout_analyzer: Optional[MarkoutAnalyzer] = None

        # ── Phase 4: Portfolio ───────────────────────────────────────────────
        strategies = config.get("auto_trader", {}).get("enabled_strategies", [
            "momentum_follow", "contrarian", "consensus_breakout",
            "volume_breakout", "market_making", "correlation_arb",
        ])
        self.portfolio_manager = PortfolioManager(strategies=strategies)
        self.whale_tracker = WhaleTracker()

        # ── Phase 6: Paper Trading ─────────────────────────────────────────────────
        self.paper_trading = PaperTradingEngine(
            portfolio_manager=self.portfolio_manager,
            initial_usdc=config.get("paper_trading", {}).get("initial_usdc", 10000.0),
            initial_pol=config.get("paper_trading", {}).get("initial_pol", 100.0),
            on_trade_close=self._on_trade_close,  # Callback para adaptive engine
        )

        # ── Signal Pipeline ────────────────────────────────────────────────
        self.signal_pipeline = SignalPipeline()

        # ── Adaptive Strategy Engine ───────────────────────────────────────
        self.adaptive_engine = AdaptiveStrategyEngine(
            state_file="data/adaptive_state.json"
        )

        # ── Price History ───────────────────────────────────────────────────
        self.price_history = PriceHistory()

        # ── Phase 5: Resilience ───────────────────────────────────────────────
        self.degradation = DegradationManager()

        # ── State ────────────────────────────────────────────────────────────────────────────────
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._last_radar_elapsed_ms = 0.0
        self._timings: dict[str, float] = {}
        self._market_prices: dict[str, float] = {}  # market_name → price
        self._last_radar_snapshots: list[dict] = []

        # ── Market Making State ───────────────────────────────────────────────────
        self._mm_quotes_generated: int = 0
        self._mm_quotes_skipped: int = 0
        self._mm_markets_active: int = 0
        self._mm_last_quote_time: float = 0.0
        self._mm_errors: int = 0
        self._mm_active_tokens: set = set()  # token_ids actualmente suscritos

    # ── Lifecycle ─────────────────────────────────────────────────

    async def start(self) -> None:
        """Arranca todos los daemons del sistema."""
        logger.info("Scout Lab v2.0 — Arrancando...")

        # ── 1. Message Bus ────────────────────────────────────
        if self._bus is None:
            self._bus = await create_message_bus()

        # ── 2. Scanner ────────────────────────────────────────
        self.scanner = AsyncPolymarketScanner(rate_limiter=self.rate_limiter)

        # ── 3. Spoof Detector ─────────────────────────────────
        self.spoof_detector = SpoofDetector(
            self.book_analyzer, self.trade_aggregator,
        )

        # ── 4. Market Maker ───────────────────────────────────
        self.market_maker = MarketMaker(
            book_analyzer=self.book_analyzer,
            time_decay=self.time_decay,
            spoof_detector=self.spoof_detector,
        )

        # ── 5. Markout Analyzer ───────────────────────────────
        self.markout_analyzer = MarkoutAnalyzer(
            book_analyzer=self.book_analyzer,
        )

        # ── 6. WebSocket Manager ──────────────────────────────
        self.ws_manager = WebSocketManager(scanner=self.scanner)

        # Wire WS callbacks → BookAnalyzer & TradeAggregator
        self.ws_manager.on_book_delta = self._on_ws_book
        self.ws_manager.on_price = self._on_ws_price

        # ── 7. Registrar health checks ────────────────────────
        self.degradation.register_health_check(
            "clob_ws", lambda: self.ws_manager.is_connected if self.ws_manager else False,
        )
        self.degradation.register_health_check(
            "redis", lambda: self._bus is not None,
        )

        # ── 8. Arrancar daemons ─────────────────────────────────────────────────────────────────────────────────
        self._running = True
        self._tasks = [
            asyncio.create_task(self._radar_loop()),
            asyncio.create_task(self._health_check_loop()),
            asyncio.create_task(self._markout_update_loop()),
            asyncio.create_task(self._portfolio_epoch_loop()),
            asyncio.create_task(self._paper_mtm_loop()),
            asyncio.create_task(self._paper_auto_close_loop()),
            asyncio.create_task(self._paper_signal_loop()),
            asyncio.create_task(self._market_making_loop()),
        ]

        # ── 9. Conectar WebSocket CLOB en background ──────────
        if self.ws_manager and not self.ws_manager.is_connected:
            asyncio.create_task(self.ws_manager.connect())
            logger.info("CLOB WebSocket conectando en background...")

        logger.info("Scout Lab v2.0 — Todos los daemons arrancados")

    async def stop(self) -> None:
        """Detiene todos los daemons."""
        logger.info("Scout Lab v2.0 — Deteniendo...")
        self._running = False

        for task in self._tasks:
            if not task.done():
                task.cancel()

        # Esperar a que terminen
        await asyncio.gather(*self._tasks, return_exceptions=True)

        if self.scanner:
            self.scanner._own_session = False  # no cerrar la session si es externa

        if self._bus:
            await self._bus.close()

        logger.info("Scout Lab v2.0 — Detenido")

    # ── Daemon Loops ──────────────────────────────────────────────

    async def _radar_loop(self) -> None:
        """Radar Daemon: polling periódico de Gamma API + Selection Engine."""
        interval = self.config.get("radar", {}).get("interval_seconds", DEFAULT_RADAR_INTERVAL)
        logger.info("Radar daemon iniciado (intervalo: %ds)", interval)

        while self._running:
            try:
                if not self.scanner:
                    await asyncio.sleep(1)
                    continue

                # Solo escanear si no estamos en modo MINIMAL
                if self.degradation.get_mode() == SystemMode.MINIMAL:
                    await asyncio.sleep(interval)
                    continue

                # Ejecutar radar scan
                t0 = time.monotonic()
                snapshots = await self.scanner.radar_scan(
                    events_limit=100,
                    markets_per_event=20,
                    min_volume=1000,
                )

                # Ranking Top 50
                ranked = self.selection_engine.rank(snapshots)

                elapsed_ms = (time.monotonic() - t0) * 1000
                self._last_radar_elapsed_ms = elapsed_ms

                # ── Guardar precios reales para mark-to-market ──
                for snap in snapshots:
                    name = snap.get("question", "")
                    price = snap.get("price_yes")
                    if name and price is not None:
                        self._market_prices[name.lower()] = float(price)

                # ── Guardar snapshots para el signal pipeline ──
                self._last_radar_snapshots = snapshots

                # ── Guardar historial persistente de precios ──
                self.price_history.save_snapshots(snapshots)

                logger.info(
                    "Radar: %d mercados escaneados, Top %d ranked (%dms)",
                    len(snapshots), len(ranked.top), int(elapsed_ms),
                )

                # Publicar en el bus
                if self._bus:
                    await self._bus.publish("radar:update", {
                        "timestamp": time.time(),
                        "market_count": len(snapshots),
                        "top50": [ms.condition_id for ms in ranked.top],
                        "enter": ranked.enter,
                        "exit": ranked.exit,
                    })

                # Notificar entradas/salidas del Top 50
                for cid in ranked.enter:
                    if self._bus:
                        await self._bus.publish("market:enter_top50", {
                            "condition_id": cid,
                            "timestamp": time.time(),
                        })
                for cid in ranked.exit:
                    if self._bus:
                        await self._bus.publish("market:exit_top50", {
                            "condition_id": cid,
                            "timestamp": time.time(),
                        })

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error en radar loop: %s", e)

            await asyncio.sleep(interval)

    async def _health_check_loop(self) -> None:
        """Health check periódico + auto-evaluación de degradación."""
        interval = DEFAULT_HEALTH_CHECK_INTERVAL
        logger.info("Health check daemon iniciado")

        while self._running:
            try:
                if self.degradation.auto_recover:
                    self.degradation.auto_evaluate()

                metrics = self.degradation.get_degradation_metrics()
                if metrics["mode"] != SystemMode.FULL.value:
                    logger.warning("Modo degradado: %s", metrics)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error en health check: %s", e)

            await asyncio.sleep(interval)

    async def _markout_update_loop(self) -> None:
        """Actualización periódica de markout P&L."""
        interval = DEFAULT_MARKOUT_UPDATE_INTERVAL
        logger.info("Markout updater iniciado")

        while self._running:
            try:
                if self.markout_analyzer:
                    updated = self.markout_analyzer.update_markouts()
                    if updated > 0:
                        logger.debug("Markout: %d fills actualizados", updated)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error en markout update: %s", e)

            await asyncio.sleep(interval)

    async def _portfolio_epoch_loop(self) -> None:
        """Avanza épocas del Portfolio Manager cada 6 horas."""
        epoch_seconds = self.portfolio_manager.epoch_hours * 3600
        logger.info("Portfolio epoch daemon iniciado (cada %.1fh)", self.portfolio_manager.epoch_hours)

        while self._running:
            try:
                await asyncio.sleep(epoch_seconds)
                if self._running:
                    self.portfolio_manager.epoch_tick()
                    logger.info("Portfolio: época %d avanzada", self.portfolio_manager.current_epoch)
            except asyncio.CancelledError:
                break

    # ── Paper Trading Loops ─────────────────────────────────────────────────────────────────────────────

    async def _paper_mtm_loop(self) -> None:
        """Mark-to-market: actualiza precios y P&L de posiciones abiertas."""
        logger.info("PaperTrading MTM daemon iniciado")
        while self._running:
            try:
                # Construir price_source haciendo fuzzy match con los precios del radar
                price_source: dict[str, float] = {}
                if self._market_prices:
                    for pos in self.paper_trading._positions:
                        if pos.closed_at is not None:
                            continue
                        # Buscar el mercado en los precios del radar (fuzzy match)
                        pos_key = pos.market.lower().strip("?")
                        for radar_name, radar_price in self._market_prices.items():
                            # Fuzzy match: si comparten keywords significativas
                            if _fuzzy_market_match(pos_key, radar_name):
                                price_source[pos.market] = radar_price
                                break

                await self.paper_trading.mark_to_market(price_source)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error en paper MTM: %s", e)
            await asyncio.sleep(PAPER_MTM_INTERVAL)

    async def _paper_auto_close_loop(self) -> None:
        """Evalúa TP/SL/tau y cierra posiciones automáticamente."""
        logger.info("PaperTrading auto-close daemon iniciado")
        while self._running:
            try:
                closed = await self.paper_trading.evaluate_auto_close()
                if closed:
                    logger.info("PaperTrading: %d posiciones cerradas automáticamente", len(closed))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error en paper auto-close: %s", e)
            await asyncio.sleep(PAPER_AUTO_CLOSE_INTERVAL)

    async def _paper_signal_loop(self) -> None:
        """Pipeline de señales adaptativo: usa AdaptiveStrategyEngine para generar entradas
        con filtrado por régimen, aprendizaje de parámetros y ensemble weighting.
        """
        logger.info("PaperTrading adaptive signal pipeline iniciado")

        while self._running:
            try:
                # Esperar a tener suficiente historial de precios
                if self.adaptive_engine.pipeline.get_history_size() < 5:
                    await asyncio.sleep(5)
                    continue

                # Solo abrir si hay liquidez y margen
                max_positions = 6
                if self.paper_trading.open_position_count >= max_positions:
                    await asyncio.sleep(5)
                    continue

                if self.paper_trading.wallet.usdc_free < 200:
                    await asyncio.sleep(10)
                    continue

                # Generar señales adaptativas
                snapshots = getattr(self, '_last_radar_snapshots', [])
                if not snapshots:
                    await asyncio.sleep(5)
                    continue

                # Usar el motor adaptativo
                signals = self.adaptive_engine.generate_adaptive_signals(snapshots, cooldown_s=300)

                if not signals:
                    await asyncio.sleep(10)
                    continue

                # Ejecutar las señales (máximo 2 por ciclo)
                executed = 0
                for sig in signals:
                    if executed >= 2:
                        break

                    strategy = sig.strategy
                    market = sig.question[:60]
                    entry = sig.entry_price
                    side = sig.side

                    if entry <= 0.01 or entry >= 0.99:
                        continue  # precio extremo

                    # Kelly position sizing
                    edge = abs(sig.confidence * 0.08)  # confianza → edge estimado
                    kelly = self.portfolio_manager.position_size(
                        strategy_name=strategy,
                        edge=edge,
                        price=entry,
                        equity=self.paper_trading.wallet.usdc_total,
                    )
                    size = min(kelly.size_final, self.paper_trading.wallet.usdc_free * 0.10)

                    if size < 50:
                        continue  # demasiado pequeño

                    tau = random.uniform(10, 60)
                    toxicity = random.uniform(0.05, 0.3)

                    pos = await self.paper_trading.open_position(
                        strategy=strategy,
                        market=market,
                        side=side,
                        size=size,
                        entry=entry,
                        tau_pct=tau,
                        toxicity=toxicity,
                    )

                    if pos:
                        executed += 1
                        logger.info(
                            "PaperTrade SIGNAL [%s] %s %s %s @ $%.3f size=$%.2f conf=%.2f (%s)",
                            strategy, side, market[:50], entry, size, sig.confidence, sig.reason,
                        )

                if executed > 0:
                    logger.info("PaperTrade: %d señales adaptativas ejecutadas de %d generadas", executed, len(signals))

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error en paper signal loop: %s", e)

            await asyncio.sleep(PAPER_SIGNAL_INTERVAL)

    # ── Market Making Loop ───────────────────────────────────────────

    async def _market_making_loop(self) -> None:
        """Market Making Daemon: conecta CLOB WS, genera quotes, aplica protecciones.

        Flujo cada MM_QUOTE_INTERVAL segundos:
        1. Espera a que el WS esté conectado
        2. Toma los Top N mercados del selection engine
        3. Subscribe/Unsubscribe dinámicamente del WS
        4. Para cada mercado activo → calcula quote vía MarketMaker
        5. Evalúa protecciones (OBI, whale, flash crash, reconciling)
        6. Imprime quote en INFO si es seguro cotizar
        """
        logger.info(
            "Market Making daemon iniciado (intervalo: %ds, top: %d mercados, creds: %s)",
            MM_QUOTE_INTERVAL, MM_TOP_MARKETS,
            "✅" if self.ws_manager._clob_authed else "❌ sin API keys",
        )

        while self._running:
            try:
                # ── Esperar a que el WS esté conectado ──────────
                if not self.ws_manager or not self.ws_manager.is_connected:
                    await asyncio.sleep(2)
                    continue

                # ── Obtener Top N mercados del radar ────────────
                snapshots = getattr(self, '_last_radar_snapshots', [])
                if not snapshots:
                    await asyncio.sleep(5)
                    continue

                ranked = self.selection_engine.rank(snapshots)
                top_snapshots = ranked.top[:MM_TOP_MARKETS]

                wanted_tokens: set[str] = set()
                token_to_snap: dict[str, dict] = {}  # token_id → snapshot
                for ms in top_snapshots:
                    tokens = ms.snapshot.get("clobTokenIds", [])
                    if isinstance(tokens, list) and len(tokens) > 0:
                        tid = tokens[0]
                        wanted_tokens.add(tid)
                        token_to_snap[tid] = ms.snapshot

                # ── Subscribe nuevos / Unsubscribe salientes ────
                current_tokens = self._mm_active_tokens
                to_add = wanted_tokens - current_tokens
                to_remove = current_tokens - wanted_tokens

                for token_id in to_remove:
                    try:
                        await self.ws_manager.unsubscribe_book(token_id)
                        logger.debug("MM unsubscribe: %s", token_id)
                    except Exception as e:
                        logger.debug("MM unsubscribe error %s: %s", token_id, e)

                for token_id in to_add:
                    snap = token_to_snap.get(token_id, {})
                    condition_id = snap.get("condition_id", "")
                    question = snap.get("question", "")
                    try:
                        await self.ws_manager.subscribe_book(
                            token_id, condition_id=condition_id, fetch_snapshot=True,
                        )
                        logger.info("MM subscribed: %s (%s...)", token_id, question[:60])
                    except Exception as e:
                        logger.warning("MM subscribe error %s: %s", token_id, e)

                self._mm_active_tokens = wanted_tokens
                self._mm_markets_active = len(wanted_tokens)

                # ── Flush MARKET message after batch ──────────
                if to_add and self.ws_manager:
                    try:
                        await self.ws_manager._flush_market_subscription()
                    except Exception as e:
                        logger.debug("MM flush market error: %s", e)

                # ── Generar quotes para mercados activos ────────
                quotes_this_cycle = 0
                import time as _time
                now = _time.time()

                for token_id in list(self._mm_active_tokens):
                    if not self._running:
                        break

                    # Datos del book desde BookAnalyzer (alimentado por WS deltas)
                    fair_price = self.book_analyzer.get_mid_price(token_id) if self.book_analyzer else 0.5
                    spread = self.book_analyzer.get_spread(token_id) if self.book_analyzer else 0.02
                    best_bid = self.book_analyzer.get_book(token_id)
                    best_bid_price = best_bid.bids[0, 0] if best_bid and best_bid.bids.shape[0] > 0 else 0.0
                    best_ask_price = best_bid.asks[0, 0] if best_bid and best_bid.asks.shape[0] > 0 else 0.0

                    # Gamma price como referencia (lo que el mercado realmente cree)
                    gamma_price = None
                    for ms in top_snapshots:
                        snap = ms.snapshot
                        ct = snap.get("clobTokenIds", [])
                        if isinstance(ct, list) and len(ct) > 0 and ct[0] == token_id:
                            gamma_price = snap.get("price_yes")
                            break

                    # Si no hay mid price real, saltar
                    if fair_price <= 0.01 or fair_price >= 0.99:
                        continue

                    # Estado del WS para este token
                    ws_state = self.ws_manager.get_state(token_id)
                    book_reconciling = ws_state is not None and str(ws_state) != "BookState.CLEAN"

                    # ¿Es seguro cotizar?
                    should_q, reason = self.market_maker.should_quote(
                        token_id=token_id,
                        whale_detected=False,
                        book_reconciling=book_reconciling,
                        now=now,
                    )

                    if not should_q:
                        self._mm_quotes_skipped += 1
                        if "paused" not in reason:  # no spamear pauses
                            logger.debug("MM skip %s: %s", token_id, reason)
                        continue

                    # Calcular quote
                    quote = self.market_maker.calculate_quote(
                        token_id=token_id,
                        fair_price=fair_price,
                        spread=spread,
                        now=now,
                    )

                    if quote and not quote.paused:
                        quotes_this_cycle += 1
                        self._mm_quotes_generated += 1
                        self._mm_last_quote_time = now

                        # ── Log de quote en INFO ────────────────
                        logger.info(
                            "MM QUOTE %s | mid=%.4f spread=%.4f | "
                            "CLOB bb=%.4f ba=%.4f | "
                            "gamma=%.4f | "
                            "bid=%.4f (size=$%.0f) ask=%.4f (size=$%.0f) | "
                            "qw=%.2fx vol=%.2f inv=%.2f td=%.2f",
                            token_id[:12],
                            fair_price, spread,
                            best_bid_price, best_ask_price,
                            gamma_price if gamma_price else -1,
                            quote.bid_price, quote.bid_size,
                            quote.ask_price, quote.ask_size,
                            quote.quote_width_multiplier,
                            quote.volatility_scalar,
                            quote.inventory_scalar,
                            quote.time_decay_scalar,
                        )

                if quotes_this_cycle > 0:
                    logger.info(
                        "MM cycle: %d quotes | %d active markets | "
                        "generated=%d skipped=%d errors=%d",
                        quotes_this_cycle, len(self._mm_active_tokens),
                        self._mm_quotes_generated, self._mm_quotes_skipped,
                        self._mm_errors,
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._mm_errors += 1
                logger.error("Error en market making loop: %s", e)

            await asyncio.sleep(MM_QUOTE_INTERVAL)

    # ── WebSocket Callbacks ─────────────────────────────────────────

    def _on_ws_book(self, asset_id: str, data: dict) -> None:
        """Callback: WS book event -> BookAnalyzer.

        Estrategia dual:
        - Si el book NO existe aun en BookAnalyzer -> initialize_book (populateo completo).
        - Si YA existe -> apply_delta (merge de updates, preserva niveles no mencionados).

        Esto evita que un delta parcial del WS machaque un snapshot REST completo.
        """
        try:
            if self.book_analyzer:
                existing = self.book_analyzer.get_book(asset_id)
                is_new_type = data.get("type", "") == "new"

                if existing is None or existing.bid_count == 0 or is_new_type:
                    # Book vacio o es un snapshot completo -> populateo total
                    self.book_analyzer.initialize_book(asset_id, data)
                else:
                    # Book ya existe -> merge delta (preserva niveles no afectados)
                    self.book_analyzer.apply_delta(asset_id, data)
        except Exception as e:
            logger.error("WS book callback error: %s", e, exc_info=True)

    def _on_ws_price(self, data: dict) -> None:
        """Callback: WS price event → TradeAggregator."""
        try:
            if self.trade_aggregator:
                asset_id = data.get("asset_id", "")
                price = data.get("price", data.get("last_trade_price", 0))
                if asset_id and price:
                    self.trade_aggregator.add_trade(asset_id, {
                        "price": float(price),
                        "size": float(data.get("size", 0)),
                        "side": data.get("side", ""),
                        "timestamp": data.get("timestamp", ""),
                    })
        except Exception as e:
            logger.debug("WS price callback error: %s", e)

    # ── Callback para Adaptive Engine ──────────────────────────────────────────────

    def _on_trade_close(self, strategy: str, pnl: float) -> None:
        """Callback que recibe el PaperTradingEngine cuando cierra un trade.
        
        Actualiza el AdaptiveStrategyEngine para que aprenda del resultado.
        """
        try:
            self.adaptive_engine.update_from_trade(strategy, pnl)
        except Exception as e:
            logger.error("Error actualizando adaptive engine desde trade: %s", e)

    # ── Trading Pipeline ──────────────────────────────────────────────────────────────────────────────

    async def execute_trade_pipeline(
        self,
        condition_id: str,
        token_id: str,
        signal_strength: float,
        base_position_size: float,
        fair_price: float,
        side: str = "YES",
        strategy: str = "momentum_follow",
        market_name: str | None = None,
    ) -> dict:
        """Pipeline completo de decisión de trading (paper trading).

        Orden de evaluación:
        1. Degradation check
        2. Spoof detection
        3. Whale conviction
        4. Time-decay risk
        5. Kelly position sizing
        6. Markout toxicity check
        7. Ejecución en PaperTradingEngine

        Returns
        -------
        dict con decision y parámetros.
        """
        # ── 1. Degradation ───────────────────────────────────────────────
        if not self.degradation.state.can_trade:
            return {"action": "skip", "reason": "degradation_mode"}

        # ── 2. Spoof Detection ───────────────────────────────────────────────
        if self.spoof_detector:
            spoof_score = self.spoof_detector.compute_spoofing_score(
                condition_id, token_id,
            )
            if spoof_score.requires_pause:
                return {"action": "skip", "reason": f"spoofing_{spoof_score.classification}"}

            # Usar dirección autoritativa
            auth_direction = self.spoof_detector.get_authoritative_direction(
                condition_id, token_id,
            )
            position_mult = self.spoof_detector.get_recommended_action(spoof_score)[
                "position_size_multiplier"
            ]
        else:
            position_mult = 1.0

        # ── 3. Whale Conviction ───────────────────────────────────────────────
        cm_override = self.degradation.get_conviction_multiplier_override()
        if cm_override is not None:
            cm = cm_override
        else:
            cm_result = self.whale_tracker.get_conviction_multiplier(condition_id)
            cm = cm_result.cm

        signal_final = signal_strength * cm
        size_after_conviction = base_position_size * cm

        # ── 4. Time-Decay ───────────────────────────────────────────────
        # (simplificado: asumimos que el caller ya pasó time_multiplier)

        # ── 5. Kelly Sizing ───────────────────────────────────────────────
        equity = self.paper_trading.wallet.usdc_total
        size = self.portfolio_manager.position_size(
            strategy_name=strategy,
            edge=abs(signal_final),
            price=fair_price,
            equity=equity,
        )

        # ── 6. Markout Toxicity ───────────────────────────────────────────────
        if self.markout_analyzer:
            toxicity = self.markout_analyzer.get_toxicity(token_id)
            if toxicity.markout_toxicity >= 1.5:
                return {"action": "skip", "reason": "toxic_flow"}

            tox_response = self.markout_analyzer.get_recommended_response(token_id)
            if tox_response["action"] == "pause":
                return {"action": "skip", "reason": "toxic_flow_pause"}

        # ── Decisión Final ────────────────────────────────────────────────────────────
        if size.size_final <= 0:
            return {"action": "skip", "reason": "zero_size"}

        # ── 7. Ejecutar Paper Trade ───────────────────────────────────────────────
        market = market_name or condition_id
        pos = await self.paper_trading.open_position(
            strategy=strategy,
            market=market,
            side=side,
            size=size.size_final,
            entry=fair_price,
            tau_pct=random.uniform(10, 70),
            toxicity=random.uniform(0.05, 0.3),
        )
        if pos is None:
            return {"action": "skip", "reason": "insufficient_funds"}

        return {
            "action": "trade",
            "signal_strength": signal_final,
            "position_size": size.size_final,
            "position_multiplier": position_mult,
            "conviction_multiplier": cm,
            "restricted_by": size.restricted_by,
            "paper_position_id": pos.id,
        }

    # ── Query ─────────────────────────────────────────────────────

    def get_system_status(self) -> dict:
        """Retorna el estado completo del sistema para monitorización."""
        pt = self.paper_trading
        import time as _time
        mm_last_age = _time.time() - self._mm_last_quote_time if self._mm_last_quote_time > 0 else -1
        return {
            "mode": self.degradation.get_mode().value if self.degradation else "unknown",
            "degradation_metrics": self.degradation.get_degradation_metrics() if self.degradation else {},
            "tracked_markets_book": len(self.book_analyzer) if self.book_analyzer else 0,
            "tracked_markets_trades": len(self.trade_aggregator) if self.trade_aggregator else 0,
            "portfolio_epoch": self.portfolio_manager.current_epoch if self.portfolio_manager else 0,
            "active_strategies": self.portfolio_manager.get_active_strategies() if self.portfolio_manager else [],
            "alpha_whales": len(self.whale_tracker.get_alpha_whales()) if self.whale_tracker else 0,
            "websocket_connected": self.ws_manager.is_connected if self.ws_manager else False,
            "clob_auth": self.ws_manager._clob_authed if self.ws_manager else False,
            "market_making": {
                "markets_active": self._mm_markets_active,
                "quotes_generated": self._mm_quotes_generated,
                "quotes_skipped": self._mm_quotes_skipped,
                "errors": self._mm_errors,
                "last_quote_age_s": round(mm_last_age, 1),
                "active_tokens": list(self._mm_active_tokens)[:10],  # primeros 10
            },
            "paper_trading": {
                "open_positions": pt.open_position_count if pt else 0,
                "total_pnl": round(pt.total_pnl, 2) if pt else 0,
                "unrealized_pnl": round(pt.unrealized_pnl, 2) if pt else 0,
                "wallet_total": round(pt.wallet.usdc_total, 2) if pt else 0,
            } if pt else {},
        }


def _fuzzy_market_match(pos_name: str, radar_name: str) -> bool:
    """Check if a position market name matches a radar market name using keyword overlap.
    Returns True if they share enough significant keywords.
    """
    stopwords = {"the", "will", "and", "for", "has", "was", "can", "are", "yes", "no", "or", "in", "at", "on"}
    
    def keywords(text: str) -> set:
        return {w.strip("?!.,:;\"'$") for w in text.lower().split() 
                if len(w.strip("?!.,:;\"'$")) >= 3 and w not in stopwords}
    
    kw1 = keywords(pos_name)
    kw2 = keywords(radar_name)
    
    if not kw1 or not kw2:
        return False
    
    overlap = kw1 & kw2
    return len(overlap) / len(kw1) > 0.4
