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

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────────────

DEFAULT_RADAR_INTERVAL = 60       # segundos entre polls del Radar
DEFAULT_HEALTH_CHECK_INTERVAL = 10  # segundos entre health checks
DEFAULT_MARKOUT_UPDATE_INTERVAL = 5  # segundos entre actualizaciones de markout
PAPER_MTM_INTERVAL = 5            # segundos entre mark-to-market
PAPER_AUTO_CLOSE_INTERVAL = 10    # segundos entre evaluaciones de auto-close
PAPER_SIGNAL_INTERVAL = 30        # segundos entre señales de trading (demo)


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

        # ── Phase 6: Paper Trading ───────────────────────────────────────────────
        self.paper_trading = PaperTradingEngine(
            portfolio_manager=self.portfolio_manager,
            initial_usdc=config.get("paper_trading", {}).get("initial_usdc", 10000.0),
            initial_pol=config.get("paper_trading", {}).get("initial_pol", 100.0),
        )

        # ── Phase 5: Resilience ───────────────────────────────────────────────
        self.degradation = DegradationManager()

        # ── State ────────────────────────────────────────────────────────────────────────────────
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._last_radar_elapsed_ms = 0.0
        self._timings: dict[str, float] = {}

        # ── System Status ──────────────────────────────────────────────────

    def get_system_status(self) -> dict:
        """Retorna estado completo del sistema para el dashboard."""
        return {
            "mode": self.degradation.get_mode().value,
            "degradation_metrics": self.degradation.get_degradation_metrics(),
            "tracked_markets_book": len(self.ws_manager.get_tracked_tokens()) if self.ws_manager else 0,
            "tracked_markets_trades": len(self.ws_manager.get_tracked_tokens()) if self.ws_manager else 0,
            "portfolio_epoch": self.portfolio_manager.current_epoch,
            "active_strategies": [
                name for name, s in self.portfolio_manager._strategies.items()
                if s.status.value == "active"
            ] if self.portfolio_manager else [],
            "alpha_whales": len(self.whale_tracker.get_alpha_whales()) if self.whale_tracker else 0,
            "websocket_connected": self.ws_manager.is_connected if self.ws_manager else False,
        }

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
        ]

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
                # Si tenemos precios reales del WS, los usamos; si no, el engine simula
                price_source: dict[str, float] = {}
                if self.ws_manager:
                    # TODO: extraer mid-price de cada libro trackeado
                    pass
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
        """Genera señales de trading periódicas para demostración del paper trading.

        En producción real, las señales vienen del pipeline de strategies.
        Este loop simula un flujo realista de oportunidades.
        """
        logger.info("PaperTrading signal daemon iniciado")

        # Mercados de demostración
        demo_markets = [
            ("Trump wins 2028?", "momentum_follow", 0.62, 0.75, "YES"),
            ("BTC > $100K Dec?", "correlation_arb", 0.45, 0.60, "NO"),
            ("Fed cuts rates?", "whale_follow", 0.55, 0.50, "YES"),
            ("Crypto bull market?", "market_making", 0.70, 0.65, "NO"),
            ("S&P 500 ATH Q3?", "momentum_follow", 0.35, 0.40, "YES"),
            ("Oil price > $80?", "contrarian", 0.80, 0.75, "NO"),
        ]

        while self._running:
            try:
                # Abrir una posición aleatoria si hay liquidez y pocas posiciones abiertas
                if self.paper_trading.open_position_count < 6 and self.paper_trading.wallet.usdc_free > 200:
                    market, strategy, entry, _, side = random.choice(demo_markets)
                    # Tamaño Kelly simulado
                    edge = random.uniform(0.02, 0.08)
                    kelly = self.portfolio_manager.position_size(
                        strategy_name=strategy,
                        edge=edge,
                        price=entry,
                        equity=self.paper_trading.wallet.usdc_total,
                    )
                    size = min(kelly.size_final, self.paper_trading.wallet.usdc_free * 0.15)
                    if size > 50:
                        tau = random.uniform(10, 80)
                        toxicity = random.uniform(0.05, 0.4)
                        await self.paper_trading.open_position(
                            strategy=strategy,
                            market=market,
                            side=side,
                            size=size,
                            entry=entry,
                            tau_pct=tau,
                            toxicity=toxicity,
                        )
                        logger.info("PaperTrade SIGNAL: %s %s %s size=$%.2f", strategy, side, market, size)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error en paper signal loop: %s", e)
            await asyncio.sleep(PAPER_SIGNAL_INTERVAL)

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
        return {
            "mode": self.degradation.get_mode().value,
            "degradation_metrics": self.degradation.get_degradation_metrics(),
            "tracked_markets_book": len(self.book_analyzer),
            "tracked_markets_trades": len(self.trade_aggregator),
            "portfolio_epoch": self.portfolio_manager.current_epoch,
            "active_strategies": self.portfolio_manager.get_active_strategies(),
            "alpha_whales": len(self.whale_tracker.get_alpha_whales()),
            "websocket_connected": self.ws_manager.is_connected if self.ws_manager else False,
            "paper_trading": {
                "open_positions": pt.open_position_count,
                "total_pnl": round(pt.total_pnl, 2),
                "unrealized_pnl": round(pt.unrealized_pnl, 2),
                "wallet_total": round(pt.wallet.usdc_total, 2),
            },
        }
