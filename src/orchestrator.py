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

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────

DEFAULT_RADAR_INTERVAL = 60       # segundos entre polls del Radar
DEFAULT_HEALTH_CHECK_INTERVAL = 10  # segundos entre health checks
DEFAULT_MARKOUT_UPDATE_INTERVAL = 5  # segundos entre actualizaciones de markout


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

        # ── Phase 4: Portfolio ────────────────────────────────
        strategies = config.get("auto_trader", {}).get("enabled_strategies", [
            "momentum_follow", "contrarian", "consensus_breakout",
            "volume_breakout", "market_making", "correlation_arb",
        ])
        self.portfolio_manager = PortfolioManager(strategies=strategies)
        self.whale_tracker = WhaleTracker()

        # ── Phase 5: Resilience ───────────────────────────────
        self.degradation = DegradationManager()

        # ── State ─────────────────────────────────────────────
        self._running = False
        self._tasks: list[asyncio.Task] = []

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

        # ── 8. Arrancar daemons ───────────────────────────────
        self._running = True
        self._tasks = [
            asyncio.create_task(self._radar_loop()),
            asyncio.create_task(self._health_check_loop()),
            asyncio.create_task(self._markout_update_loop()),
            asyncio.create_task(self._portfolio_epoch_loop()),
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

    # ── Trading Pipeline ──────────────────────────────────────────

    async def execute_trade_pipeline(
        self,
        condition_id: str,
        token_id: str,
        signal_strength: float,
        base_position_size: float,
        fair_price: float,
    ) -> dict:
        """Pipeline completo de decisión de trading.

        Orden de evaluación:
        1. Degradation check
        2. Spoof detection
        3. Whale conviction
        4. Time-decay risk
        5. Kelly position sizing
        6. Markout toxicity check

        Returns
        -------
        dict con decision y parámetros.
        """
        # ── 1. Degradation ─────────────────────────────────
        if not self.degradation.state.can_trade:
            return {"action": "skip", "reason": "degradation_mode"}

        # ── 2. Spoof Detection ─────────────────────────────
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

        # ── 3. Whale Conviction ────────────────────────────
        cm_override = self.degradation.get_conviction_multiplier_override()
        if cm_override is not None:
            cm = cm_override
        else:
            cm_result = self.whale_tracker.get_conviction_multiplier(condition_id)
            cm = cm_result.cm

        signal_final = signal_strength * cm
        size_after_conviction = base_position_size * cm

        # ── 4. Time-Decay ──────────────────────────────────
        # (simplificado: asumimos que el caller ya pasó time_multiplier)

        # ── 5. Kelly Sizing ────────────────────────────────
        size = self.portfolio_manager.position_size(
            strategy_name="momentum_follow",
            edge=abs(signal_final),
            price=fair_price,
            equity=10000,  # TODO: trackear equity real
        )

        # ── 6. Markout Toxicity ────────────────────────────
        if self.markout_analyzer:
            toxicity = self.markout_analyzer.get_toxicity(token_id)
            if toxicity.markout_toxicity >= 1.5:
                return {"action": "skip", "reason": "toxic_flow"}

            tox_response = self.markout_analyzer.get_recommended_response(token_id)
            if tox_response["action"] == "pause":
                return {"action": "skip", "reason": "toxic_flow_pause"}

        # ── Decisión Final ─────────────────────────────────
        if size.size_final <= 0:
            return {"action": "skip", "reason": "zero_size"}

        return {
            "action": "trade",
            "signal_strength": signal_final,
            "position_size": size.size_final,
            "position_multiplier": position_mult,
            "conviction_multiplier": cm,
            "restricted_by": size.restricted_by,
        }

    # ── Query ─────────────────────────────────────────────────────

    def get_system_status(self) -> dict:
        """Retorna el estado completo del sistema para monitorización."""
        return {
            "mode": self.degradation.get_mode().value,
            "degradation_metrics": self.degradation.get_degradation_metrics(),
            "tracked_markets_book": len(self.book_analyzer),
            "tracked_markets_trades": len(self.trade_aggregator),
            "portfolio_epoch": self.portfolio_manager.current_epoch,
            "active_strategies": self.portfolio_manager.get_active_strategies(),
            "alpha_whales": len(self.whale_tracker.get_alpha_whales()),
            "websocket_connected": self.ws_manager.is_connected if self.ws_manager else False,
        }
