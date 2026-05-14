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
from collections import defaultdict, deque
from typing import Optional
from src.async_scanner import AsyncPolymarketScanner
from src.selection_engine import SelectionEngine, MIN_VOLUME_24H_THRESHOLD, MM_NICHE_CLOB_DEPTH
from src.rate_limiter import RateLimiter
from src.book_analyzer import BookAnalyzer
from src.trade_aggregator import TradeAggregator
from src.spoof_detector import SpoofDetector
from src.websocket_manager import WebSocketManager
from src.time_decay import TimeDecayManager
from src.market_making import MarketMaker
from src.markout_analysis import MarkoutAnalyzer
from src.portfolio_manager import PortfolioManager, PROBATION_ALLOCATION
from src.whale_tracker import WhaleTracker
from src.degradation import DegradationManager, SystemMode
from src.redis_bus import MessageBus, create_message_bus
from src.paper_trading import PaperTradingEngine
from src.signal_pipeline import SignalPipeline
from src.price_history import PriceHistory
from src.adaptive_strategy_engine import AdaptiveStrategyEngine
from src.trading_logger import trading_log
from src.correlation_graph import CorrelationGraph
from src.nlp_oracle import NLPOracle
from src.nlp_oracle_logger import nlp_log
from src.config import get_nlp_oracle_config

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────────────

DEFAULT_RADAR_INTERVAL = 60       # segundos entre polls del Radar
DEFAULT_HEALTH_CHECK_INTERVAL = 10  # segundos entre health checks
DEFAULT_MARKOUT_UPDATE_INTERVAL = 5  # segundos entre actualizaciones de markout
PAPER_MTM_INTERVAL = 5            # segundos entre mark-to-market
PAPER_AUTO_CLOSE_INTERVAL = 10    # segundos entre evaluaciones de auto-close
PAPER_SIGNAL_INTERVAL = 30        # segundos entre señales de trading (demo)
MM_QUOTE_INTERVAL = 10            # segundos entre cotizaciones de market making
MM_TOP_MARKETS = 10               # cuántos mercados del Top MM trackear (legacy, usa SelectionEngine)
AUTONOMOUS_EXEC_INTERVAL = 15    # segundos entre ciclos del ejecutor autónomo
L2_SEED_INTERVAL = 90            # segundos entre seedings de L2 desde REST
EQUITY_LOG_INTERVAL = 300        # segundos entre snapshots de equity (5 min)

# Estrategias que SIEMPRE compiten en el Bandit (mínimo dos brazos)
BANDIT_PRIMARY_STRATEGIES = ["market_making", "momentum_follow"]


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
            top_n_mm=config.get("selection", {}).get("top_n_mm", 10),
            top_n_directional=config.get("selection", {}).get("top_n_directional", 10),
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
        # Garantizar que los dos brazos primarios del Bandit siempre estén presentes
        for arm in BANDIT_PRIMARY_STRATEGIES:
            if arm not in strategies:
                strategies.insert(0, arm)
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

        # ── Correlation Graph ──────────────────────────────────────────────
        self.correlation_graph = CorrelationGraph()

        # Inyectar contexto externo para correlation_arb y whale_follow
        self.adaptive_engine.set_external_context(
            correlation_graph=self.correlation_graph,
            whale_tracker=self.whale_tracker,
        )

        # ── NLP Oracle — Real-Time Sentiment Validator ─────────────────
        # Validador de confluencia para momentum_follow: requiere que
        # los spikes de volumen L2 estén respaldados por noticias reales.
        # Credenciales de Telegram (api_id/api_hash) se leen del .env,
        # NUNCA de config.yaml (que está versionado en git).
        nlp_config = get_nlp_oracle_config()
        self.nlp_oracle = NLPOracle(nlp_config)
        self.adaptive_engine.set_nlp_oracle(self.nlp_oracle)

        # ── MAINNET ALIGNMENT: whale_follow desactivada hasta RPC real ──
        # Sin datos on-chain (Alchemy/QuickNode), esta estrategia no puede
        # operar con señales reales. Se desactiva hasta integrar un provider.
        wt = self.adaptive_engine.adaptive_thresholds.get("whale_follow")
        if wt:
            wt.enabled = False
            logger.warning(
                "whale_follow DESACTIVADA — requiere provider RPC on-chain "
                "(Alchemy/QuickNode). Se reactivará al integrar datos reales."
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
        self._last_mm_markets: list = []          # MarketScore list for MM
        self._last_directional_markets: list = []  # MarketScore list for Directional
        self._mm_low_vol_watchlist: list = []      # MM niche: low vol pero potencial CLOB profundo
        self._last_l2_seed_time: float = 0.0  # timestamp del último seeding REST de L2

        # ── Market Making State ───────────────────────────────────────────────────
        self._mm_quotes_generated: int = 0
        self._mm_quotes_skipped: int = 0
        self._mm_markets_active: int = 0
        self._mm_last_quote_time: float = 0.0
        self._mm_errors: int = 0
        self._mm_active_tokens: set = set()  # token_ids actualmente suscritos
        self._mm_bandit_blocked: bool = False  # trackear cambios de estado MM en el Bandit

        # ── CLOB Micro-Price History (para estrategias direccionales) ──────
        # Alimentado en tiempo real desde _on_ws_book. Reemplaza a Gamma
        # como fuente de precios para momentum/mean_reversion/volume_breakout.
        self._clob_micro_prices: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=50)
        )  # token_id → rolling window de micro-prices
        self._token_to_cid: dict[str, str] = {}  # token_id → condition_id

    # ── Lifecycle ─────────────────────────────────────────────────

    async def start(self) -> None:
        """Arranca todos los daemons del sistema."""
        logger.info("Scout Lab v2.0 — Arrancando...")
        trading_log.system_start()
        nlp_log.system_start()

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
        mm_config = self.config.get("market_making", {})
        self.market_maker = MarketMaker(
            book_analyzer=self.book_analyzer,
            time_decay=self.time_decay,
            spoof_detector=self.spoof_detector,
            max_obi=mm_config.get("max_obi", 0.95),
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
            asyncio.create_task(self._l2_seed_loop()),           # ← NUEVO: poblar L2 desde REST
            asyncio.create_task(self._autonomous_execution_loop()),  # ← NUEVO: MM quotes → paper trades
            asyncio.create_task(self._equity_logger_loop()),     # ← NUEVO: Equity Curve
        ]

        # ── 9. Conectar WebSocket CLOB en background ──────────
        if self.ws_manager and not self.ws_manager.is_connected:
            asyncio.create_task(self.ws_manager.connect())
            logger.info("CLOB WebSocket conectando en background...")

        logger.info("Scout Lab v2.0 — Todos los daemons arrancados")

        # ── NLP Oracle: iniciar streamer de Telegram en background ──
        if self.nlp_oracle.enabled:
            await self.nlp_oracle.start_streamer()
            logger.info("NLP Oracle: streamer de noticias Telegram iniciado")

        # ── 10. 🧹 CLEAN SLATE: borrar historial contaminado + resetear Bandit ──
        # El bot ahora solo opera si el CLOB real cruza sus órdenes límite (Maker)
        # o si hay un movimiento matemático real (Taker). Sin simulaciones.
        nuke_result = self.paper_trading.reset_all_positions()
        bandit_audit = self.portfolio_manager.reset_all_strategies()
        logger.warning(
            "🧹 CLEAN SLATE: %d posiciones eliminadas, %d trades borrados, "
            "%d estrategias → PROBATION (%.0f%% alloc cada una). Estado anterior: %s",
            nuke_result["positions_cleared"],
            nuke_result["trades_cleared"],
            len(bandit_audit),
            PROBATION_ALLOCATION * 100,
            bandit_audit,
        )

    async def stop(self) -> None:
        """Detiene todos los daemons."""
        logger.info("Scout Lab v2.0 — Deteniendo...")
        trading_log.system_stop("user requested")
        nlp_log.system_stop("user requested")
        self._running = False

        # ── NLP Oracle: detener streamer de Telegram ──
        await self.nlp_oracle.stop_streamer()

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

                # Ranking Dual (MM + Direccional)
                ranked = self.selection_engine.rank(snapshots)

                elapsed_ms = (time.monotonic() - t0) * 1000
                self._last_radar_elapsed_ms = elapsed_ms

                # ── Guardar rankings para daemons ──
                self._last_radar_snapshots = snapshots
                self._last_mm_markets = ranked.mm_top
                self._last_directional_markets = ranked.directional_top
                self._mm_low_vol_watchlist = ranked.mm_low_vol_watchlist

                # ── Guardar precios reales para mark-to-market ──
                for snap in snapshots:
                    name = snap.get("question", "")
                    price = snap.get("price_yes")
                    if name and price is not None:
                        self._market_prices[name.lower()] = float(price)

                # ── Guardar historial persistente de precios ──
                self.price_history.save_snapshots(snapshots)

                # ── Mapeo token_id → condition_id (para CLOB micro-prices) ──
                for snap in snapshots:
                    cid = snap.get("condition_id", "")
                    tokens = snap.get("clobTokenIds", [])
                    if cid and isinstance(tokens, list) and tokens:
                        self._token_to_cid[tokens[0]] = cid

                # ── Reconstruir grafo de correlación con datos frescos ──
                if snapshots:
                    try:
                        self.correlation_graph.build(snapshots)
                    except Exception as e:
                        logger.debug("Correlation graph build error: %s", e)

                # ── Log radar en consola ──
                logger.info(
                    "Radar: %d mercados | MM=%d Direccional=%d (%dms)",
                    len(snapshots),
                    len(ranked.mm_top),
                    len(ranked.directional_top),
                    int(elapsed_ms),
                )
                # Top MM
                if ranked.mm_top:
                    logger.info("── TOP MM (%d) ────────────────────────────────────────────────────────────",
                                len(ranked.mm_top))
                    for i, ms in enumerate(ranked.mm_top[:10], 1):
                        snap = ms.snapshot
                        vol24 = snap.get("volume_24h") or 0
                        liq = snap.get("liquidity") or 0
                        price = snap.get("price_yes") or snap.get("price")
                        sports_flag = "⚽" if ms.is_sports else "  "
                        price_str = f"{price:.4f}" if price is not None else "N/A"
                        logger.info(
                            "  MM #%02d [score=%.4f] vol24h=$%-7.0f liq=$%-7.0f price=%-7s %s | %s",
                            i, ms.score, vol24, liq, price_str, sports_flag,
                            ms.question[:65],
                        )
                    logger.info("─" * 75)
                # Top Direccional
                if ranked.directional_top:
                    logger.info("── TOP DIRECCIONAL (%d) ─────────────────────────────────────────────────────",
                                len(ranked.directional_top))
                    for i, ms in enumerate(ranked.directional_top[:10], 1):
                        snap = ms.snapshot
                        vol24 = snap.get("volume_24h") or 0
                        price = snap.get("price_yes") or snap.get("price")
                        hrs = ms.hours_to_expiry
                        hrs_str = f"{hrs:.0f}h" if hrs is not None else "N/A"
                        price_str = f"{price:.4f}" if price is not None else "N/A"
                        logger.info(
                            "  DIR #%02d [score=%.4f] vol24h=$%-7.0f price=%-7s expiry=%-6s | %s",
                            i, ms.score, vol24, price_str, hrs_str,
                            ms.question[:65],
                        )
                    logger.info("─" * 75)

                # ── Liquidity Guard: log de descartes por volumen ──
                dir_discarded = [
                    ms for ms in ranked.directional_all_scored
                    if ms.score == 0 and ms.is_low_vol
                ]
                if dir_discarded:
                    logger.debug(
                        "[LIQUIDITY_GUARD] Direccional: %d mercados descartados por vol24h < $%d",
                        len(dir_discarded), int(MIN_VOLUME_24H_THRESHOLD),
                    )
                    for ms in dir_discarded[:5]:  # solo los primeros 5 para no spamear
                        tid = ""
                        tokens = ms.snapshot.get("clobTokenIds", [])
                        if isinstance(tokens, list) and tokens:
                            tid = str(tokens[0])[:16]
                        logger.debug(
                            "[LIQUIDITY_GUARD] Descartado token=%s | Vol24h=$%.0f | Razón: Low Liquidity (Dir)",
                            tid, ms.volume_24h,
                        )
                if ranked.mm_low_vol_watchlist:
                    logger.debug(
                        "[LIQUIDITY_GUARD] MM watchlist: %d mercados con vol24h bajo pendientes de validación CLOB",
                        len(ranked.mm_low_vol_watchlist),
                    )

                # Publicar en el bus
                if self._bus:
                    await self._bus.publish("radar:update", {
                        "timestamp": time.time(),
                        "market_count": len(snapshots),
                        "mm_top": [ms.condition_id for ms in ranked.mm_top],
                        "directional_top": [ms.condition_id for ms in ranked.directional_top],
                        "mm_enter": ranked.mm_enter,
                        "mm_exit": ranked.mm_exit,
                        "dir_enter": ranked.directional_enter,
                        "dir_exit": ranked.directional_exit,
                    })

                # Notificar entradas/salidas
                for cid in ranked.mm_enter:
                    if self._bus:
                        await self._bus.publish("market:enter_mm", {
                            "condition_id": cid, "timestamp": time.time(),
                        })
                for cid in ranked.mm_exit:
                    if self._bus:
                        await self._bus.publish("market:exit_mm", {
                            "condition_id": cid, "timestamp": time.time(),
                        })
                for cid in ranked.directional_enter:
                    if self._bus:
                        await self._bus.publish("market:enter_dir", {
                            "condition_id": cid, "timestamp": time.time(),
                        })
                for cid in ranked.directional_exit:
                    if self._bus:
                        await self._bus.publish("market:exit_dir", {
                            "condition_id": cid, "timestamp": time.time(),
                        })

                # ── Trading log: descubrimientos ──
                for cid in ranked.mm_enter:
                    ms = next((m for m in ranked.mm_top if m.condition_id == cid), None)
                    if ms and ms.snapshot:
                        tokens = ms.snapshot.get("clobTokenIds", [])
                        tid = tokens[0] if isinstance(tokens, list) and tokens else ""
                        trading_log.market_discovered(
                            token_id=tid,
                            condition_id=cid,
                            question=ms.question,
                            score=ms.score,
                            profile="MM",
                            volume_24h=ms.volume_24h,
                            price=ms.price,
                            tags=ms.snapshot.get("tags", []),
                        )
                for cid in ranked.directional_enter:
                    ms = next((m for m in ranked.directional_top if m.condition_id == cid), None)
                    if ms and ms.snapshot:
                        tokens = ms.snapshot.get("clobTokenIds", [])
                        tid = tokens[0] if isinstance(tokens, list) and tokens else ""
                        trading_log.market_discovered(
                            token_id=tid,
                            condition_id=cid,
                            question=ms.question,
                            score=ms.score,
                            profile="DIRECTIONAL",
                            volume_24h=ms.volume_24h,
                            price=ms.price,
                            tags=ms.snapshot.get("tags", []),
                        )

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
                    # ── Capital Preservation Check ──
                    # Si no hubo trades, es por el filtro de liquidez, no por fallo.
                    active_strats = self.portfolio_manager.get_active_strategies()
                    total_trades = sum(
                        len(self.paper_trading._trade_history) for _ in [1]
                    ) if self.paper_trading else 0
                    if total_trades == 0 and self.paper_trading:
                        # Contar trades reales
                        total_trades = len(self.paper_trading._trade_history)
                    logger.info(
                        "Portfolio: época %d avanzada | trades=%d | estrategias activas=%d | %s",
                        self.portfolio_manager.current_epoch,
                        total_trades,
                        len(active_strats),
                        "🛡️ CAPITAL PRESERVATION" if total_trades == 0 else "⚡ ACTIVE TRADING",
                    )
            except asyncio.CancelledError:
                break

    # ── Paper Trading Loops ─────────────────────────────────────────────────────────────────────────────

    async def _paper_mtm_loop(self) -> None:
        """Mark-to-market institucional: usa CLOB L2 con Ghost Filter y lógica NO asimétrica."""
        logger.info("PaperTrading MTM daemon iniciado (v2: Depth-Aware + Ghost Filter)")
        while self._running:
            try:
                # ── Rule 2+3: MTM institucional con BookAnalyzer (CLOB L2) ──
                if self.book_analyzer and len(self.book_analyzer) > 0:
                    stats = await self.paper_trading.mark_to_market_v2(self.book_analyzer)
                    if stats["positions_skipped_ghost"] > 0 or stats["positions_no_ask_empty"] > 0:
                        logger.warning(
                            "MTM v2 stats: updated=%d no_book=%d ghost=%d no_ask=%d",
                            stats["positions_updated"],
                            stats["positions_skipped_no_book"],
                            stats["positions_skipped_ghost"],
                            stats["positions_no_ask_empty"],
                        )
                else:
                    # Fallback: usar precios Gamma del radar si no hay CLOB L2
                    price_source: dict[str, float] = {}
                    if self._market_prices:
                        for pos in self.paper_trading._positions:
                            if pos.closed_at is not None:
                                continue
                            pos_key = pos.market.lower().strip("?")
                            for radar_name, radar_price in self._market_prices.items():
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
        """Evalúa TP/SL/tau + active exits + OBI evacuation y cierra posiciones automáticamente."""
        logger.info("PaperTrading auto-close daemon iniciado (v2: TP Maker + Active Exits + OBI Evac)")
        while self._running:
            try:
                # ── WS health check: SL solo con datos frescos ──
                ws_healthy = (
                    self.ws_manager is not None
                    and self.ws_manager.is_connected
                )

                # ── Rule 4: TP usa Maker Limit Orders; SL/tau/expired usan Market Close ──
                # ── Active Exits: time_decay + trailing_stop integrados en evaluate ──
                if self.book_analyzer and len(self.book_analyzer) > 0:
                    closed = await self.paper_trading.evaluate_auto_close_v2(
                        self.book_analyzer,
                        ws_healthy=ws_healthy,
                    )
                else:
                    closed = await self.paper_trading.evaluate_auto_close(
                        ws_healthy=ws_healthy,
                    )

                # ── OBI Toxic Flow Evacuation (v2.1): detectar tsunamis de liquidez tóxica ──
                # MAINNET: solo evacuar con WS healthy (necesita OBI fresco del L2)
                if ws_healthy and self.market_maker and self.book_analyzer and len(self.book_analyzer) > 0:
                    evac_closed = await self._evaluate_obi_evacuations()
                    if evac_closed:
                        closed.extend(evac_closed)

                # ── Momentum Reversal Exit: cerrar si el momentum se invirtió ──
                mom_map = self._build_momentum_map()
                if mom_map:
                    reversal_closed = await self.paper_trading.evaluate_momentum_reversal(mom_map)
                    if reversal_closed:
                        closed.extend(reversal_closed)

                if closed:
                    logger.info("PaperTrading: %d posiciones cerradas automáticamente", len(closed))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error en paper auto-close: %s", e)
            await asyncio.sleep(PAPER_AUTO_CLOSE_INTERVAL)

    def _build_momentum_map(self) -> dict[str, float]:
        """Construye un mapping token_id → momentum_value desde el SignalPipeline.

        Usa los últimos snapshots del radar para mapear condition_id → token_id,
        y extrae el momentum actual del MarketHistory de cada mercado.
        """
        momentum_map: dict[str, float] = {}
        snapshots = getattr(self, '_last_radar_snapshots', [])
        if not snapshots:
            return momentum_map

        pipeline = self.adaptive_engine.pipeline

        # Construir condition_id → token_id desde los snapshots
        cid_to_token: dict[str, str] = {}
        for snap in snapshots:
            cid = snap.get("condition_id", "")
            tokens = snap.get("clobTokenIds", [])
            if cid and isinstance(tokens, list) and tokens:
                cid_to_token[cid] = tokens[0]  # YES token

        # Extraer momentum de cada MarketHistory
        for cid, history in pipeline._history.items():
            mom = history.momentum
            if mom is not None:
                token_id = cid_to_token.get(cid)
                if token_id:
                    momentum_map[token_id] = mom

        return momentum_map

    async def _evaluate_obi_evacuations(self) -> list[dict]:
        """Evalúa OBI Toxic Flow Evacuation para todas las posiciones MM abiertas.

        Si el Order Book Imbalance (OBI) se inclina > 0.85 en contra de una
        posición de Market Making durante 3+ ciclos consecutivos, se ejecuta
        un EMERGENCY_DUMP a precio de mercado. Esto evita esperar al Stop Loss
        cuando el libro de órdenes ya indica que nos van a barrer.

        Returns
        -------
        list[dict]
            Trades cerrados por evacuación OBI.
        """
        closed: list[dict] = []
        open_positions = [
            p for p in self.paper_trading._positions
            if p.closed_at is None and p.strategy == "market_making"
        ]
        if not open_positions:
            return closed

        import time as _time
        now = _time.time()

        for pos in open_positions:
            if not pos.token_id:
                continue

            should_dump, reason = self.market_maker.evaluate_obi_evacuation(
                token_id=pos.token_id,
                side=pos.side,
                now=now,
            )

            if should_dump:
                logger.error(
                    "🚨 OBI EVACUATION EXECUTING #%d [%s] side=%s — %s",
                    pos.id, pos.market[:50], pos.side, reason,
                )
                trade = await self.paper_trading.close_position(
                    pos.id,
                    reason="obi_evacuation",
                )
                if trade:
                    closed.append(trade)
                    trading_log.position_closed(
                        pos_id=pos.id,
                        strategy=pos.strategy,
                        market=pos.market,
                        pnl=pos.pnl,
                        pnl_pct=pos.pnl_pct,
                        reason="obi_evacuation",
                    )

        return closed

    async def _equity_logger_loop(self) -> None:
        """Equity Logger Daemon: guarda una foto del equity total cada EQUITY_LOG_INTERVAL segundos.

        El registro incluye Capital libre + Collateral + P&L latente, para que
        la curva refleje el valor real de la cuenta en cada momento.
        """
        logger.info("Equity Logger daemon iniciado (intervalo: %ds)", EQUITY_LOG_INTERVAL)
        # Registrar snapshot inicial
        self.paper_trading.record_equity_snapshot()

        while self._running:
            try:
                await asyncio.sleep(EQUITY_LOG_INTERVAL)
                if self._running:
                    self.paper_trading.record_equity_snapshot()
                    history = self.paper_trading.get_equity_history()
                    last_eq = history[-1]["equity"] if history else 0
                    trading_log.equity_snapshot(
                        equity=self.paper_trading.wallet.usdc_total,
                        total_pnl=self.paper_trading.total_pnl,
                        unrealized_pnl=self.paper_trading.unrealized_pnl,
                        open_positions=self.paper_trading.open_position_count,
                    )
                    logger.debug(
                        "Equity snapshot: $%.2f | total_snapshots=%d",
                        last_eq, len(history),
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error en equity logger: %s", e)

    async def _paper_signal_loop(self) -> None:
        """Pipeline de señales adaptativo — complementa al autonomous_execution_loop.

        Usa el AdaptiveStrategyEngine para señales de mean_reversion y volume_breakout
        (estrategias secundarias del Bandit). El market_making es responsabilidad
        exclusiva del _market_making_loop → Cross Engine → _pending_quotes.
        """
        logger.info("PaperTrading adaptive signal pipeline iniciado")

        # Mapping de nombres de señal → brazo del Bandit
        STRATEGY_BANDIT_MAP = {
            "momentum": "momentum_follow",
            "mean_reversion": "contrarian",
            "volume_breakout": "volume_breakout",
            "consensus_breakout": "consensus_breakout",
            "correlation_arb": "correlation_arb",
            "whale_follow": "whale_follow",
        }

        while self._running:
            try:
                # Esperar a tener suficiente historial de precios
                if self.adaptive_engine.pipeline.get_history_size() < 3:
                    await asyncio.sleep(5)
                    continue

                # Solo abrir si hay liquidez y margen — el autonomous_loop ya tiene 8 slots
                max_positions = 10
                if self.paper_trading.open_position_count >= max_positions:
                    await asyncio.sleep(10)
                    continue

                if self.paper_trading.wallet.usdc_free < 150:
                    await asyncio.sleep(10)
                    continue

                # Generar señales adaptativas
                snapshots = getattr(self, '_last_radar_snapshots', [])
                if not snapshots:
                    await asyncio.sleep(5)
                    continue

                # ── Sincronizar precios CLOB al pipeline ────────────────
                self._sync_clob_prices_to_pipeline()

                # Usar cooldown más largo para no colisionar con autonomous loop
                signals = self.adaptive_engine.generate_adaptive_signals(snapshots, cooldown_s=600)

                if not signals:
                    await asyncio.sleep(15)
                    continue

                # Ejecutar las señales secundarias (máximo 1 por ciclo)
                executed = 0
                for sig in signals:
                    if executed >= 1:
                        break

                    # Mapear estrategia al brazo del Bandit correcto
                    bandit_strategy = STRATEGY_BANDIT_MAP.get(sig.strategy, sig.strategy)
                    entry = sig.entry_price
                    side = sig.side

                    if entry <= 0.01 or entry >= 0.99:
                        continue  # precio extremo

                    # ── Extraer token_id del snapshot para inventory tracking ──
                    token_id = ""
                    cid = sig.condition_id
                    for snap in snapshots:
                        if snap.get("condition_id") == cid:
                            tokens = snap.get("clobTokenIds", [])
                            if isinstance(tokens, list) and tokens:
                                token_id = tokens[0]
                            break

                    # ── KEY 2: NLP Oracle — Confluencia de noticias ────────────
                    if sig.strategy in ("momentum", "momentum_follow"):
                        nlp_approved, nlp_score, nlp_headline = await self.nlp_oracle.validate_market_premise(
                            market_question=sig.question,
                            side=sig.side,
                            token_id=token_id,
                        )
                        if not nlp_approved:
                            logger.info(
                                "[NLP_ORACLE] Token=%s | Premise=\"%s\" | Top News: \"%s\" | "
                                "NLP_Score=%.3f | Action=REJECTED",
                                token_id[:16] if token_id else cid[:16],
                                sig.question[:40],
                                nlp_headline[:50] if nlp_headline else "(sin noticias)",
                                nlp_score,
                            )
                            continue

                    # ── EXECUTION_BLOCK: verificar inventory slots ──
                    if token_id:
                        open_for_token = [
                            p for p in self.paper_trading._positions
                            if p.closed_at is None and p.token_id == token_id
                        ]
                        dir_slot_occ = any(
                            p.strategy != "market_making" for p in open_for_token
                        )
                        mm_slot_occ = any(
                            p.strategy == "market_making" for p in open_for_token
                        )
                        if dir_slot_occ:
                            logger.info(
                                "[EXECUTION_BLOCK] Token=%s Strategy=%s | "
                                "Reason: Direccional slot occupied (%d pos) | "
                                "mm_slot=%s",
                                token_id[:16], sig.strategy,
                                len([p for p in open_for_token if p.strategy != "market_making"]),
                                "occupied" if mm_slot_occ else "free",
                            )
                            continue
                        # ── M3: Override — DIRECTIONAL > MM ──
                        if mm_slot_occ:
                            cancelled = self.cancel_active_quotes(token_id)
                            logger.info(
                                "[OVERRIDE] Token=%s | Action=Halting MM | "
                                "Reason=Directional Signal Triggered (%s) | "
                                "MM quotes_cancelled=%s",
                                token_id[:16], sig.strategy,
                                "yes" if cancelled else "no quotes",
                            )

                    # Kelly position sizing — con adjusted_confidence tras slippage
                    edge = abs(sig.confidence * 0.07)
                    
                    # ── M4 REFACTOR: Slippage descuenta el edge, no bloquea ──────
                    # En vez de rechazar la señal, ajustamos la confianza
                    # proporcionalmente al coste del spread y reducimos el tamaño.
                    adjusted_confidence = sig.confidence
                    if token_id and self.book_analyzer:
                        book_snap = self.book_analyzer.get_book(token_id)
                        if book_snap and book_snap.bid_count > 0 and book_snap.ask_count > 0:
                            real_ba = float(book_snap.asks[0, 0])
                            real_bb = float(book_snap.bids[0, 0])
                            bb_sz = float(book_snap.bids[0, 1])
                            ba_sz = float(book_snap.asks[0, 1])
                            total_sz = bb_sz + ba_sz
                            if total_sz > 0 and real_ba > real_bb:
                                real_micro_price = (real_bb * ba_sz + real_ba * bb_sz) / total_sz
                                expected_spread_cost = (real_ba - real_bb) / real_micro_price if real_micro_price > 0 else 0.0
                                adjusted_confidence = sig.confidence * (1.0 - expected_spread_cost)
                                if adjusted_confidence <= 0.01:
                                    logger.debug(
                                        "[SKIP_DIR] Token=%s | Reason=Spread_Too_Wide "
                                        "(spread=%.1f%% adjusted_conf=%.4f — spread eats all edge)",
                                        token_id[:16],
                                        expected_spread_cost * 100,
                                        adjusted_confidence,
                                    )
                                    continue
                                logger.debug(
                                    "[SPREAD_DISCOUNT] Token=%s | spread=%.1f%% | "
                                    "conf %.3f→%.3f | size scaled down",
                                    token_id[:16],
                                    expected_spread_cost * 100,
                                    sig.confidence, adjusted_confidence,
                                )
                    
                    edge = abs(adjusted_confidence * 0.07)
                    kelly = self.portfolio_manager.position_size(
                        strategy_name=bandit_strategy,
                        edge=edge,
                        price=entry,
                        equity=self.paper_trading.wallet.usdc_total,
                    )
                    size = min(kelly.size_final, self.paper_trading.wallet.usdc_free * 0.08)

                    if size < 50:
                        logger.debug(
                            "[DISCARD] SIZE_TOO_SMALL | Strategy=%s Token=%s | "
                            "size=$%.2f < $50 min (adj_conf=%.4f)",
                            sig.strategy, token_id[:16] if token_id else cid[:16], size,
                            adjusted_confidence,
                        )
                        continue  # demasiado pequeño

                    pos = await self.paper_trading.open_position(
                        strategy=bandit_strategy,
                        market=f"[SIG] {sig.question[:55]}",
                        side=side,
                        size=size,
                        entry=entry,
                        token_id=token_id,
                        tau_pct=random.uniform(10, 60),
                        toxicity=random.uniform(0.05, 0.3),
                    )

                    if pos:
                        executed += 1
                        trading_log.position_opened(
                            pos_id=pos.id,
                            strategy=bandit_strategy,
                            market=f"[SIG] {sig.question[:55]}",
                            side=side,
                            size=size,
                            entry=entry,
                        )
                        logger.info(
                            "PaperTrade SIG [%s→%s] %s @ $%.3f size=$%.2f conf=%.2f | %s",
                            sig.strategy, bandit_strategy, side, entry, size,
                            sig.confidence, sig.reason[:60],
                        )
                        # ── v3.0 POST-TRADE COOLDOWN ──────────────────────
                        # Solo marcar cooldown DESPUÉS de ejecución exitosa.
                        # Si la señal fue rechazada, el token queda libre.
                        self.adaptive_engine.mark_trade_executed(
                            condition_id=cid,
                            strategy=bandit_strategy,
                            cooldown_s=600,
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error en paper signal loop: %s", e, exc_info=True)

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
            "Market Making daemon iniciado (intervalo: %ds, top: %d mercados MM, creds: %s)",
            MM_QUOTE_INTERVAL, self.selection_engine.top_n_mm,
            "✅" if self.ws_manager._clob_authed else "❌ sin API keys",
        )

        while self._running:
            try:
                # ── Esperar a que el WS esté conectado ──────────
                if not self.ws_manager or not self.ws_manager.is_connected:
                    await asyncio.sleep(2)
                    continue

                # ── Obtener Top N mercados MM del radar ──────
                top_snapshots = self._last_mm_markets  # MarketScore list from SelectionEngine
                if not top_snapshots:
                    await asyncio.sleep(5)
                    continue

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
                    # Buscar el MarketScore para obtener el score calculado
                    ms_match = next((ms for ms in top_snapshots if ms.condition_id == condition_id), None)
                    score_val = ms_match.score if ms_match else 0.0
                    vol24 = snap.get("volume_24h") or 0
                    vol_tot = snap.get("volume") or 0
                    liq = snap.get("liquidity") or 0
                    spread = snap.get("spread")
                    price = snap.get("price_yes") or snap.get("price")
                    spread_str = f"{spread*100:.1f}%" if spread is not None else "N/A"
                    price_str  = f"{price:.4f}" if price is not None else "N/A"
                    try:
                        await self.ws_manager.subscribe_book(
                            token_id, condition_id=condition_id, fetch_snapshot=True,
                        )
                        logger.info(
                            "WS ⇒ subscribe | score=%.4f vol24h=$%-9.0f vol=$%-11.0f "
                            "liq=$%-8.0f spread=%-7s price=%-7s | %s",
                            score_val, vol24, vol_tot, liq,
                            spread_str, price_str,
                            question[:80],
                        )
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

                # ── MM Niche: promover mercados de watchlist con CLOB profundo ──
                # Mercados con vol24h < $5K pero book depth > $2K merecen MM.
                for niche_ms in self._mm_low_vol_watchlist[:5]:  # máx 5 por ciclo
                    tokens = niche_ms.snapshot.get("clobTokenIds", [])
                    if not isinstance(tokens, list) or not tokens:
                        continue
                    tid = tokens[0]
                    if tid in self._mm_active_tokens:
                        continue  # ya está suscrito
                    # Verificar si el BookAnalyzer ya tiene datos (vía L2 seed o WS)
                    book_snap = self.book_analyzer.get_book(tid) if self.book_analyzer else None
                    if not book_snap or book_snap.bid_count == 0 or book_snap.ask_count == 0:
                        continue
                    bb_size = float(book_snap.bids[0, 1])
                    ba_size = float(book_snap.asks[0, 1])
                    clob_depth = bb_size + ba_size
                    if clob_depth >= MM_NICHE_CLOB_DEPTH:
                        # Promover: suscribir al WS y añadir a activos
                        try:
                            await self.ws_manager.subscribe_book(
                                tid,
                                condition_id=niche_ms.condition_id,
                                fetch_snapshot=True,
                            )
                            self._mm_active_tokens.add(tid)
                            token_to_snap[tid] = niche_ms.snapshot
                            logger.info(
                                "🔬 MM NICHE PROMOTED token=%s | vol24h=$%.0f < $%d BUT clob_depth=$%.0f ≥ $%d | %s",
                                tid[:16], niche_ms.volume_24h, int(MIN_VOLUME_24H_THRESHOLD),
                                clob_depth, int(MM_NICHE_CLOB_DEPTH),
                                niche_ms.question[:60],
                            )
                        except Exception as e:
                            logger.debug("MM niche subscribe error %s: %s", tid, e)

                # ── Generar quotes para mercados activos ────────
                quotes_this_cycle = 0
                import time as _time
                now = _time.time()

                # ── FIX: Respetar estado del Bandit ──────────────────────────
                # Si el Bandit ha congelado o retirado market_making,
                # no generamos quotes. La suscripción WS sigue activa
                # para que el L2 book esté caliente si se reactiva.
                # v2.0: RECOVERY mode permite operar con tamaño reducido.
                mm_state = self.portfolio_manager.get_strategy_state("market_making")
                mm_blocked = mm_state is not None and mm_state.status.value in ("frozen", "retired")
                mm_recovery = mm_state is not None and mm_state.status.value == "recovery"
                if mm_blocked != self._mm_bandit_blocked:
                    self._mm_bandit_blocked = mm_blocked
                    if mm_blocked:
                        logger.warning(
                            "MM BLOCKED by Bandit: market_making status=%s — quotes detenidas hasta recuperación",
                            mm_state.status.value,
                        )
                    else:
                        logger.info("MM REACTIVATED by Bandit: market_making status=%s", mm_state.status.value)
                if mm_blocked:
                    await asyncio.sleep(MM_QUOTE_INTERVAL)
                    continue

                # v2.0: RECOVERY mode — double cooldown, reduced sizes handled in calculate_quote
                if mm_recovery:
                    await asyncio.sleep(MM_QUOTE_INTERVAL * 2)  # double SIGNAL_COOLDOWN

                for token_id in list(self._mm_active_tokens):
                    if not self._running:
                        break

                    # ── Datos del book desde BookAnalyzer (alimentado por WS deltas) ──
                    book_snap = self.book_analyzer.get_book(token_id) if self.book_analyzer else None
                    best_bid_price = book_snap.bids[0, 0] if book_snap and book_snap.bid_count > 0 else 0.0
                    best_ask_price = book_snap.asks[0, 0] if book_snap and book_snap.ask_count > 0 else 0.0
                    clob_spread = best_ask_price - best_bid_price if best_bid_price > 0 and best_ask_price > 0 else 1.0

                    # Gamma price como referencia (lo que el mercado realmente cree)
                    gamma_price = None
                    for ms in top_snapshots:
                        snap = ms.snapshot
                        ct = snap.get("clobTokenIds", [])
                        if isinstance(ct, list) and len(ct) > 0 and ct[0] == token_id:
                            gamma_price = snap.get("price_yes")
                            break

                    # ── Fair price: CLOB micro-price es el ancla principal ──────
                    # El micro-price ponderado por tamaño es la referencia canónica.
                    # Gamma solo actúa como último recurso si el CLOB está vacío.
                    if book_snap and book_snap.bid_count > 0 and book_snap.ask_count > 0:
                        bb_sz_fp = float(book_snap.bids[0, 1])
                        ba_sz_fp = float(book_snap.asks[0, 1])
                        total_sz_fp = bb_sz_fp + ba_sz_fp
                        if total_sz_fp > 0:
                            micro_price_fp = (best_bid_price * ba_sz_fp + best_ask_price * bb_sz_fp) / total_sz_fp
                        else:
                            micro_price_fp = (best_bid_price + best_ask_price) / 2.0
                        fair_price = micro_price_fp
                    elif gamma_price is not None:
                        # CLOB vacío → fallback Gamma
                        fair_price = float(gamma_price)
                        logger.debug("MM %s: CLOB vacío, usando gamma_price=%.4f como fallback", token_id[:16], fair_price)
                    else:
                        # Ni CLOB ni Gamma → saltar este mercado
                        logger.debug("MM skip %s: sin fair price (CLOB vacío y gamma=None)", token_id[:16])
                        continue

                    spread = clob_spread

                    # ── Saltar mercados sin datos reales ──
                    if book_snap is None or book_snap.bid_count == 0 or book_snap.ask_count == 0:
                        logger.debug("MM skip %s: order book empty (bid_count=%d, ask_count=%d)",
                                     token_id, book_snap.bid_count if book_snap else 0,
                                     book_snap.ask_count if book_snap else 0)
                        continue

                    # ── FIX 2: Rechazar mercados con spread CLOB > 5% ──────────────
                    # Un spread superior al 5% hundirá el Mark-to-Market al momento
                    # de abrir la posición, disparando el SL instantáneamente.
                    # El MM no debe jugar en mercados ilíquidos (ej. deportes).
                    MAX_ALLOWED_CLOB_SPREAD = 0.05  # 5%
                    if clob_spread > MAX_ALLOWED_CLOB_SPREAD:
                        logger.debug(
                            "MM skip %s: spread CLOB=%.3f supera límite del %.0f%% — mercado ilíquido, rechazado",
                            token_id[:16], clob_spread, MAX_ALLOWED_CLOB_SPREAD * 100,
                        )
                        continue

                    # Si el fair price está en los extremos, saltar
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
                        recovery_mode=mm_recovery,
                    )

                    if quote and not quote.paused:
                        # ── Sanity Check: la quote NUNCA debe cruzar el spread real ──
                        # Si nuestro bid virtual >= best_ask real, actuaríamos como
                        # takers pagando el spread en lugar de capturarlo.
                        # Si nuestro ask virtual <= best_bid real, lo mismo al revés.
                        quote_crosses_spread = (
                            best_bid_price > 0 and best_ask_price > 0
                            and (quote.bid_price >= best_ask_price or quote.ask_price <= best_bid_price)
                        )
                        if quote_crosses_spread:
                            self._mm_quotes_skipped += 1
                            logger.warning(
                                "MM SANITY FAIL %s: quote cruza el spread real! "
                                "virtual bid=%.4f >= ba=%.4f  OR  virtual ask=%.4f <= bb=%.4f — descartando",
                                token_id[:12],
                                quote.bid_price, best_ask_price,
                                quote.ask_price, best_bid_price,
                            )
                            continue

                        quotes_this_cycle += 1
                        self._mm_quotes_generated += 1
                        self._mm_last_quote_time = now

                        # ── Log de quote en INFO ────────────────
                        logger.info(
                            "MM QUOTE %s | micro=%.4f spread=%.4f | "
                            "CLOB bb=%.4f ba=%.4f | "
                            "gamma=%.4f | "
                            "bid=%.4f (size=$%.0f) ask=%.4f (size=$%.0f) | "
                            "qw=%.2fx vol=%.2f inv=%.2f td=%.2f obi=%.2f | "
                            "Inv: %+.0f | Skew: %+.4f | Mode: %s",
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
                            quote.obi_scalar,
                            quote.net_inventory,
                            quote.inventory_skew,
                            quote.mode,
                        )

                        # ── FIX: Control de Inventario con Independent Slots ──────
                        # INDEPENDENT SLOTS (Paper Trading Test): 2 slots por token
                        #   - Slot MM: strategy="market_making" → exclusivo para MM
                        #   - Slot Direccional: cualquier otra estrategia → para señales
                        # Bloquear SOLO si el slot correspondiente ya está ocupado.
                        open_for_token = [
                            p for p in self.paper_trading._positions
                            if p.closed_at is None and p.token_id == token_id
                        ]
                        mm_slot_occupied = any(
                            p.strategy == "market_making" for p in open_for_token
                        )
                        dir_slot_occupied = any(
                            p.strategy != "market_making" for p in open_for_token
                        )

                        if mm_slot_occupied:
                            self._mm_quotes_skipped += 1
                            logger.debug(
                                "MM INV BLOCK [MM Slot] %s: slot MM ocupado [%s] — quote no registrada",
                                token_id[:16],
                                ", ".join(
                                    f"{p.side}({p.strategy})" for p in open_for_token
                                    if p.strategy == "market_making"
                                ),
                            )
                        else:
                            # ── Cross Engine: registrar como orden límite virtual ────
                            # La orden queda pendiente hasta que el precio real del mercado
                            # la cruce (via _on_ws_book → cross_and_fill).
                            snap = token_to_snap.get(token_id, {})
                            question = snap.get("question", token_id[:40])
                            self.paper_trading.register_mm_quote(
                                token_id=token_id,
                                market=f"[MM] {question[:55]}",
                                bid_price=quote.bid_price,
                                ask_price=quote.ask_price,
                                bid_size=quote.bid_size,
                                ask_size=quote.ask_size,
                                strategy="market_making",
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
                trading_log.error("market_making", str(e)[:120])

            await asyncio.sleep(MM_QUOTE_INTERVAL)

    # ── L2 Seeding Loop ─────────────────────────────────────────────────────────────────────
    # Destruye el bloqueo spread=N/A: fuerza un fetch REST del CLOB book para cada mercado
    # activo en el MM. Esto puebla el BookAnalyzer con datos reales ANTES de que lleguen los
    # deltas del WS, garantizando que el Micro-Price esté disponible desde el primer ciclo.

    async def _l2_seed_loop(self) -> None:
        """L2 Seed Daemon: fetch REST CLOB snapshots para los Top N mercados activos.

        Ejecuta cada L2_SEED_INTERVAL segundos. Para cada token activo en el MM:
        1. Llama a get_book_async() → snapshot REST completo
        2. Lo pasa al BookAnalyzer via initialize_book()
        3. Calcula y loggea el Micro-Price resultante

        Esto elimina el spread=N/A persistente incluso si el WS aún no ha enviado
        datos para ese token.
        """
        logger.info("L2 Seed daemon iniciado (intervalo: %ds)", L2_SEED_INTERVAL)

        # Esperar un poco para que el radar tenga mercados
        await asyncio.sleep(20)

        while self._running:
            try:
                if not self.scanner:
                    await asyncio.sleep(10)
                    continue

                await self.scanner._ensure_session()

                # Tomar los tokens activos del MM (ya suscritos) + los del radar si no hay MM aún
                tokens_to_seed: list[tuple[str, str]] = []  # (token_id, question)

                # Preferir tokens activos del MM
                for token_id in list(self._mm_active_tokens):
                    question = ""
                    for snap in self._last_radar_snapshots:
                        ct = snap.get("clobTokenIds", [])
                        if isinstance(ct, list) and len(ct) > 0 and ct[0] == token_id:
                            question = snap.get("question", "")[:60]
                            break
                    tokens_to_seed.append((token_id, question))

                # Si no hay tokens MM aún, usar los Top MM del SelectionEngine
                if not tokens_to_seed and self._last_mm_markets:
                    for ms in self._last_mm_markets[:5]:
                        ct = ms.snapshot.get("clobTokenIds", [])
                        if isinstance(ct, list) and len(ct) > 0:
                            tokens_to_seed.append((ct[0], ms.question[:60]))

                if not tokens_to_seed:
                    await asyncio.sleep(L2_SEED_INTERVAL)
                    continue

                seeded = 0
                for token_id, question in tokens_to_seed[:self.selection_engine.top_n_mm]:
                    if not self._running:
                        break
                    try:
                        # Espacio entre llamadas para no saturar el rate-limiter
                        await asyncio.sleep(0.5)
                        snapshot = await self.scanner.get_book_async(token_id)

                        if not snapshot:
                            continue

                        raw_bids = snapshot.get("bids", [])
                        raw_asks = snapshot.get("asks", [])

                        if not raw_bids and not raw_asks:
                            logger.debug("L2 Seed: book vacío para %s", token_id[:16])
                            continue

                        # Poblar el BookAnalyzer con datos reales
                        book_snap = self.book_analyzer.initialize_book(token_id, snapshot)
                        seeded += 1

                        # Calcular Micro-Price: media ponderada por tamaño de bid/ask
                        if book_snap.bid_count > 0 and book_snap.ask_count > 0:
                            bb = float(book_snap.bids[0, 0])
                            bb_sz = float(book_snap.bids[0, 1])
                            ba = float(book_snap.asks[0, 0])
                            ba_sz = float(book_snap.asks[0, 1])
                            total_sz = bb_sz + ba_sz
                            if total_sz > 0:
                                micro_price = (bb * ba_sz + ba * bb_sz) / total_sz
                            else:
                                micro_price = (bb + ba) / 2.0
                            spread_val = ba - bb

                            logger.info(
                                "L2 SEED ✅ %s | bb=%.4f ba=%.4f spread=%.4f micro=%.4f "
                                "bids=%d asks=%d | %s",
                                token_id[:16], bb, ba, spread_val, micro_price,
                                book_snap.bid_count, book_snap.ask_count,
                                question[:50],
                            )

                            # ── FALLO 2 FIX: propagar spread real al snapshot del radar ──
                            # El SelectionEngine lee spread de _last_radar_snapshots.
                            # Si no lo actualizamos aquí, el Radar seguirá viendo spread=None
                            # y mostrará spread=N/A hasta el siguiente ciclo Gamma completo.
                            order_count_clob = book_snap.bid_count + book_snap.ask_count
                            for radar_snap in self._last_radar_snapshots:
                                ct = radar_snap.get("clobTokenIds", [])
                                if isinstance(ct, list) and len(ct) > 0 and ct[0] == token_id:
                                    radar_snap["spread"] = spread_val
                                    radar_snap["order_count"] = max(
                                        radar_snap.get("order_count") or 0,
                                        order_count_clob,
                                    )
                                    break
                        else:
                            logger.debug(
                                "L2 Seed: %s solo con %d bids, %d asks",
                                token_id[:16], book_snap.bid_count, book_snap.ask_count,
                            )

                    except Exception as e:
                        logger.debug("L2 Seed error para %s: %s", token_id[:16], e)

                if seeded > 0:
                    logger.info("L2 Seed completado: %d/%d tokens poblados", seeded, len(tokens_to_seed))

                self._last_l2_seed_time = time.time()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error en L2 seed loop: %s", e)

            await asyncio.sleep(L2_SEED_INTERVAL)

    # ── Autonomous Execution Loop ────────────────────────────────────────────────────────
    # Pipeline direccional (Taker): solo opera con señales matemáticas reales.
    # El Market Making (Maker) vive exclusivamente en _market_making_loop →
    #   Cross Engine → PaperTradingEngine._pending_quotes.
    # NUNCA abre trades de MM directamente. Sin inyectores de señales aleatorias.

    async def _autonomous_execution_loop(self) -> None:
        """Ejecutor Autónomo direccional (Taker): señales de momentum y estrategias
        secundarias desde el pipeline adaptativo.

        Flujo cada AUTONOMOUS_EXEC_INTERVAL segundos:
        1. Genera señales de momentum/mean_reversion/volume_breakout
        2. Para cada señal → abre paper trade con la estrategia mapeada
        3. Al cierre de cada posición → PaperTradingEngine notifica al PortfolioManager
        4. El Bandit (Thompson Sampling) ajusta alloc_pct automáticamente

        ⚠️  El Market Making NO vive aquí. Sus quotes se registran en
        _pending_quotes y solo se ejecutan cuando el CLOB real las cruza
        (vía _on_ws_book → cross_and_fill).
        """
        logger.info("Autonomous Execution daemon (Taker-only) iniciado (intervalo: %ds)", AUTONOMOUS_EXEC_INTERVAL)

        # Esperar a que haya datos del radar
        await asyncio.sleep(30)

        while self._running:
            try:
                if not self._last_radar_snapshots:
                    await asyncio.sleep(5)
                    continue

                # ── Filtrar snapshots a solo los del Top Direccional ──
                dir_condition_ids = {
                    ms.condition_id for ms in self._last_directional_markets
                }
                dir_filtered_snapshots = [
                    s for s in self._last_radar_snapshots
                    if s.get("condition_id") in dir_condition_ids
                ]
                # Si el filtro está vacío, usar todos (fallback)
                if not dir_filtered_snapshots:
                    dir_filtered_snapshots = self._last_radar_snapshots

                equity = self.paper_trading.wallet.usdc_total
                max_positions = 8
                open_count = self.paper_trading.open_position_count

                if open_count >= max_positions:
                    await asyncio.sleep(AUTONOMOUS_EXEC_INTERVAL)
                    continue

                if self.paper_trading.wallet.usdc_free < 150:
                    logger.debug("AutExec: capital libre insuficiente ($%.2f)", self.paper_trading.wallet.usdc_free)
                    await asyncio.sleep(AUTONOMOUS_EXEC_INTERVAL)
                    continue

                import time as _time
                now = _time.time()
                executed_mom = 0

                # ── Sincronizar precios CLOB al pipeline direccional ─────
                # Los micro-precios del WebSocket reemplazan a Gamma
                # para momentum/mean_reversion/volume_breakout.
                clob_synced = self._sync_clob_prices_to_pipeline()
                if clob_synced > 0:
                    logger.debug(
                        "AutExec: %d mercados sincronizados con CLOB micro-prices",
                        clob_synced,
                    )

                # ── TAKER: señales del pipeline adaptativo ──────────────
                # Usar solo snapshots del Top Direccional
                signals = []
                if self.adaptive_engine.pipeline.get_history_size() >= 3:
                    signals = self.adaptive_engine.generate_adaptive_signals(
                        dir_filtered_snapshots,
                        cooldown_s=180,
                    )

                for sig in signals:
                    if open_count + executed_mom >= max_positions:
                        break
                    if executed_mom >= 2:  # máx 2 trades momentum por ciclo
                        break

                    entry = sig.entry_price
                    if entry <= 0.02 or entry >= 0.98:
                        continue

                    # ── Extraer token_id del snapshot para inventory tracking ──
                    token_id = ""
                    cid = sig.condition_id
                    for snap in dir_filtered_snapshots:
                        if snap.get("condition_id") == cid:
                            tokens = snap.get("clobTokenIds", [])
                            if isinstance(tokens, list) and tokens:
                                token_id = tokens[0]
                            break

                    # ── KEY 2: NLP Oracle — Confluencia de noticias ────────────
                    # Momentum signals require real-time news validation.
                    # Volume spike (Key 1) is already verified in the pipeline.
                    # NLP confidence > threshold (Key 2) confirms the spike has news backing.
                    if sig.strategy in ("momentum", "momentum_follow"):
                        nlp_approved, nlp_score, nlp_headline = await self.nlp_oracle.validate_market_premise(
                            market_question=sig.question,
                            side=sig.side,
                            token_id=token_id,
                        )
                        if not nlp_approved:
                            logger.info(
                                "[NLP_ORACLE] Token=%s | Premise=\"%s\" | Top News: \"%s\" | "
                                "NLP_Score=%.3f | Action=REJECTED",
                                token_id[:16] if token_id else cid[:16],
                                sig.question[:40],
                                nlp_headline[:50] if nlp_headline else "(sin noticias)",
                                nlp_score,
                            )
                            continue
                        logger.info(
                            "[NLP_ORACLE] Token=%s | Premise=\"%s\" | Top News: \"%s\" | "
                            "NLP_Score=%.3f | Action=APPROVED",
                            token_id[:16] if token_id else cid[:16],
                            sig.question[:40],
                            nlp_headline[:50] if nlp_headline else "(sin noticias)",
                            nlp_score,
                        )

                    # ── EXECUTION_BLOCK: verificar inventory slots ──
                    if token_id:
                        open_for_token = [
                            p for p in self.paper_trading._positions
                            if p.closed_at is None and p.token_id == token_id
                        ]
                        mm_slot_occ = any(
                            p.strategy == "market_making" for p in open_for_token
                        )
                        dir_slot_occ = any(
                            p.strategy != "market_making" for p in open_for_token
                        )
                        if dir_slot_occ:
                            logger.info(
                                "[EXECUTION_BLOCK] Token=%s Strategy=%s | "
                                "Reason: Direccional slot occupied (%d pos) | "
                                "mm_slot=%s",
                                token_id[:16], sig.strategy,
                                len([p for p in open_for_token if p.strategy != "market_making"]),
                                "occupied" if mm_slot_occ else "free",
                            )
                            continue
                        # ── M3: Override — DIRECTIONAL > MM ──
                        if mm_slot_occ:
                            # Cancelar quotes activas del MM y liberar el slot
                            cancelled = self.cancel_active_quotes(token_id)
                            logger.info(
                                "[OVERRIDE] Token=%s | Action=Halting MM | "
                                "Reason=Directional Signal Triggered (%s) | "
                                "MM quotes_cancelled=%s | dir_slot=free → proceeding",
                                token_id[:16], sig.strategy,
                                "yes" if cancelled else "no quotes",
                            )
                            # El MM slot queda liberado; el direccional puede operar

                    # ── M4 REFACTOR: Slippage descuenta el edge, no bloquea ──────
                    adjusted_confidence = sig.confidence
                    if token_id and self.book_analyzer:
                        book_snap = self.book_analyzer.get_book(token_id)
                        if book_snap and book_snap.bid_count > 0 and book_snap.ask_count > 0:
                            real_ba = float(book_snap.asks[0, 0])
                            real_bb = float(book_snap.bids[0, 0])
                            bb_sz = float(book_snap.bids[0, 1])
                            ba_sz = float(book_snap.asks[0, 1])
                            total_sz = bb_sz + ba_sz
                            if total_sz > 0 and real_ba > real_bb:
                                real_micro_price = (real_bb * ba_sz + real_ba * bb_sz) / total_sz
                                expected_spread_cost = (real_ba - real_bb) / real_micro_price if real_micro_price > 0 else 0.0
                                adjusted_confidence = sig.confidence * (1.0 - expected_spread_cost)
                                if adjusted_confidence <= 0.01:
                                    logger.debug(
                                        "[SKIP_DIR] Token=%s | Reason=Spread_Too_Wide "
                                        "(spread=%.1f%% adjusted_conf=%.4f)",
                                        token_id[:16],
                                        expected_spread_cost * 100,
                                        adjusted_confidence,
                                    )
                                    continue
                                logger.debug(
                                    "[SPREAD_DISCOUNT] Token=%s | spread=%.1f%% | "
                                    "conf %.3f→%.3f",
                                    token_id[:16],
                                    expected_spread_cost * 100,
                                    sig.confidence, adjusted_confidence,
                                )

                    kelly = self.portfolio_manager.position_size(
                        strategy_name="momentum_follow",
                        edge=abs(adjusted_confidence * 0.06),
                        price=entry,
                        equity=equity,
                    )
                    size = min(kelly.size_final, self.paper_trading.wallet.usdc_free * 0.10)

                    if size < 50:
                        logger.debug(
                            "[DISCARD] SIZE_TOO_SMALL | Strategy=%s Token=%s | "
                            "size=$%.2f < $50 min (adj_conf=%.4f)",
                            sig.strategy, token_id[:16] if token_id else cid[:16], size,
                            adjusted_confidence,
                        )
                        continue

                    market_name = f"[MOM] {sig.question[:55]}"

                    pos = await self.paper_trading.open_position(
                        strategy="momentum_follow",
                        market=market_name,
                        side=sig.side,
                        size=size,
                        entry=entry,
                        token_id=token_id,
                        tau_pct=random.uniform(10, 60),
                        toxicity=random.uniform(0.05, 0.30),
                    )

                    if pos:
                        executed_mom += 1
                        trading_log.position_opened(
                            pos_id=pos.id,
                            strategy="momentum_follow",
                            market=market_name,
                            side=sig.side,
                            size=size,
                            entry=entry,
                        )
                        logger.info(
                            "AutExec MOM TRADE #%d [momentum_follow] %s @ %.4f "
                            "conf=%.2f size=$%.2f | %s",
                            pos.id, sig.side, entry, sig.confidence, size,
                            sig.reason[:60],
                        )
                        # ── v3.0 POST-TRADE COOLDOWN ──────────────────────
                        # Solo marcar cooldown DESPUÉS de ejecución exitosa.
                        self.adaptive_engine.mark_trade_executed(
                            condition_id=cid,
                            strategy="momentum_follow",
                            cooldown_s=180,
                        )

                # ── Log del estado del Bandit cada ciclo ───────────────────────────
                if executed_mom > 0:
                    try:
                        allocs = self.portfolio_manager.allocate(equity)
                        alloc_parts = []
                        for a in sorted(allocs, key=lambda x: x.fraction, reverse=True)[:4]:
                            st = self.portfolio_manager.get_strategy_state(a.strategy)
                            status = st.status.value.upper() if st else "?"
                            sortino = self.portfolio_manager.get_sortino(a.strategy)
                            alloc_parts.append(
                                f"{a.strategy}={a.fraction*100:.1f}%[{status}|S={sortino:.2f}]"
                            )
                        logger.info(
                            "🧠 BANDIT ESTADO | open=%d equity=$%.0f | %s",
                            self.paper_trading.open_position_count,
                            equity,
                            "  ".join(alloc_parts),
                        )
                    except Exception as e:
                        logger.debug("Error logging bandit state: %s", e)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error en autonomous execution loop: %s", e, exc_info=True)

            await asyncio.sleep(AUTONOMOUS_EXEC_INTERVAL)

    # ── WebSocket Callbacks ─────────────────────────────────────────

    def _sync_clob_prices_to_pipeline(self) -> int:
        """Copia los micro-precios CLOB al pipeline del adaptive engine.

        Reemplaza los precios Gamma en cada MarketHistory con la serie
        temporal de micro-precios CLOB (mucha más resolución).

        Returns
        -------
        int
            Número de mercados sincronizados.
        """
        synced = 0
        pipeline = self.adaptive_engine.pipeline
        for token_id, micro_prices in self._clob_micro_prices.items():
            if len(micro_prices) < 4:
                continue
            cid = self._token_to_cid.get(token_id)
            if not cid:
                continue
            history = pipeline._history.get(cid)
            if not history:
                # Crear MarketHistory si no existe
                # Buscar la question en los snapshots del radar
                question = ""
                for snap in self._last_radar_snapshots:
                    tokens = snap.get("clobTokenIds", [])
                    if isinstance(tokens, list) and tokens and tokens[0] == token_id:
                        question = snap.get("question", "")
                        break
                from src.signal_pipeline import MarketHistory
                history = MarketHistory(
                    condition_id=cid,
                    question=question,
                )
                pipeline._history[cid] = history
            # Reemplazar precios con la serie CLOB (truncada a max_history
            # para que add_snapshot no pierda los volúmenes que añada)
            history.prices = list(micro_prices)[-history.max_history:]
            synced += 1
        return synced

    def _on_ws_book(self, asset_id: str, data: dict) -> None:
        """Callback: WS book event -> BookAnalyzer + Cross Engine fill simulation.

        Estrategia dual:
        - Si el book NO existe aun en BookAnalyzer -> initialize_book (populateo completo).
        - Si YA existe -> apply_delta (merge de updates, preserva niveles no mencionados).

        Tras actualizar el book, extrae el mejor bid/ask real y dispara el Cross Engine
        para comprobar si alguna orden límite virtual ha sido cruzada y ejecutarla.
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

                # ── Cross Engine: cruzar precio real contra órdenes límite virtuales ──
                # Rule 1: Depth-Aware Fill con VWAP Sweep del L2 completo
                if self.paper_trading._pending_quotes:
                    book_snap = self.book_analyzer.get_book(asset_id)
                    if book_snap and book_snap.bid_count > 0 and book_snap.ask_count > 0:
                        try:
                            loop = asyncio.get_event_loop()
                            if loop.is_running():
                                asyncio.ensure_future(
                                    self.paper_trading.cross_and_fill_depth_aware(
                                        asset_id, book_snap,
                                    )
                                )
                        except RuntimeError:
                            pass  # sin event loop activo (e.g., tests)

                # ── Rule 4: Verificar TP Limit Orders (Maker-style) ──
                if self.paper_trading._tp_limit_orders:
                    book_snap = self.book_analyzer.get_book(asset_id)
                    if book_snap and book_snap.bid_count > 0 and book_snap.ask_count > 0:
                        try:
                            loop = asyncio.get_event_loop()
                            if loop.is_running():
                                asyncio.ensure_future(
                                    self.paper_trading.check_tp_cross(book_snap, asset_id)
                                )
                        except RuntimeError:
                            pass

                # ── CLOB Micro-Price para estrategias direccionales ─────
                # Cada book event produce un micro-price que alimenta
                # momentum/mean_reversion/volume_breakout en tiempo real.
                book_snap = self.book_analyzer.get_book(asset_id)
                if book_snap and book_snap.bid_count > 0 and book_snap.ask_count > 0:
                    bb = float(book_snap.bids[0, 0])
                    ba = float(book_snap.asks[0, 0])
                    bb_sz = float(book_snap.bids[0, 1])
                    ba_sz = float(book_snap.asks[0, 1])
                    total_sz = bb_sz + ba_sz
                    if total_sz > 0 and ba > bb:
                        micro = (bb * ba_sz + ba * bb_sz) / total_sz
                    else:
                        micro = (bb + ba) / 2.0 if ba > 0 or bb > 0 else 0.5
                    if micro > 0.01 and micro < 0.99:
                        self._clob_micro_prices[asset_id].append(micro)

        except Exception as e:
            logger.error("WS book callback error: %s", e, exc_info=True)

    def _on_ws_price(self, data: dict) -> None:
        """Callback: WS price event → TradeAggregator.

        v2.0: También alimenta el tracker de volumen del MarketMaker para
        detección de flujo tóxico.
        """
        try:
            if self.trade_aggregator:
                asset_id = data.get("asset_id", "")
                price = data.get("price", data.get("last_trade_price", 0))
                size = float(data.get("size", 0))
                if asset_id and price:
                    self.trade_aggregator.add_trade(asset_id, {
                        "price": float(price),
                        "size": size,
                        "side": data.get("side", ""),
                        "timestamp": data.get("timestamp", ""),
                    })
                    # v2.0: Feed MarketMaker volume tracker for toxic flow detection
                    if self.market_maker and size > 0:
                        self.market_maker.record_volume(asset_id, size)
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

    # ── M3: Inventory Manager — Jerarquía Anti-Canibalización ───────────────────
    # DIRECTIONAL > MM: cuando una estrategia direccional emite una señal válida,
    # el MM debe ceder el slot. Este método cancela las quotes activas del MM
    # y libera el _pending_quotes para que el direccional pueda operar.

    def cancel_active_quotes(self, token_id: str) -> bool:
        """Cancela las quotes activas del Market Maker para un token específico.

        Parameters
        ----------
        token_id : str
            ID del token cuyas quotes de MM se cancelan.

        Returns
        -------
        bool
            True si se canceló al menos una quote, False si no había ninguna.
        """
        cancelled = False

        # 1. Cancelar en PaperTradingEngine (pending quotes)
        if token_id in self.paper_trading._pending_quotes:
            removed = self.paper_trading._pending_quotes.pop(token_id, None)
            if removed:
                cancelled = True
                logger.info(
                    "[OVERRIDE] Token=%s | Action=Halting MM | "
                    "Reason=Directional Signal Triggered | "
                    "MM quote #%d cancelada (bid=%.4f ask=%.4f)",
                    token_id[:16], removed.id,
                    removed.bid_price, removed.ask_price,
                )

        # 2. Cancelar en MarketMaker (pausar + limpiar estado)
        if self.market_maker:
            try:
                from src.market_making import REENTRY_DELAY as MM_REENTRY_DELAY
                state = self.market_maker.get_state(token_id)
                if state:
                    import time as _time
                    self.market_maker._pause(
                        token_id, "",
                        MM_REENTRY_DELAY * 2,  # 60s de pausa
                        "directional_override",
                        _time.time(),
                    )
                    logger.debug(
                        "[OVERRIDE] Token=%s | MM pausado %ds por directional_override",
                        token_id[:16], MM_REENTRY_DELAY * 2,
                    )
            except Exception as e:
                logger.debug("Error pausando MM en override: %s", e)

        return cancelled

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
            "selection": {
                "mm_top_count": len(self._last_mm_markets),
                "directional_top_count": len(self._last_directional_markets),
                "mm_top_ids": [ms.condition_id[:16] for ms in self._last_mm_markets[:5]],
                "directional_top_ids": [ms.condition_id[:16] for ms in self._last_directional_markets[:5]],
            },
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
