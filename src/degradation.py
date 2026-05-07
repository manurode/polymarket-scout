"""
Degradation Manager — Modos de degradación y resiliencia del sistema.

Implementa la estrategia de degradación (§6.4 del ARCHITECTURE_V2.md):
- Si un componente falla, el sistema se degrada gradualmente en vez de colapsar.
- Cada modo define qué funcionalidades se mantienen y cuáles se pausan.

Modos de degradación:
- FULL:       Todos los subsistemas operativos.
- CLOB_WS_DOWN:  CLOB WebSocket caído → Gamma-only, Market Making pausado.
- POLYGON_RPC_DOWN: Polygon RPC caído → Whale tracking pausado, CM=1.0.
- REDIS_DOWN:     Redis caído → standalone, sin comunicación entre procesos.
- MINIMAL:        Solo Radar Layer funcionando (descubrimiento básico).

Uso:
    dg = DegradationManager()
    dg.set_mode("CLOB_WS_DOWN")
    if dg.can_trade:
        ...
    if dg.can_market_make:
        ...
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# ── Types ──────────────────────────────────────────────────────────

class SystemMode(Enum):
    """Modos operativos del sistema."""
    FULL = "full"                     # todos los subsistemas OK
    CLOB_WS_DOWN = "clob_ws_down"     # CLOB WebSocket caído
    POLYGON_RPC_DOWN = "polygon_rpc_down"  # Polygon RPC caído
    REDIS_DOWN = "redis_down"         # Redis caído
    MINIMAL = "minimal"               # solo Radar Layer


@dataclass
class DegradationState:
    """Estado actual de degradación."""
    mode: SystemMode = SystemMode.FULL
    mode_duration: float = 0.0        # segundos en este modo
    last_mode_change: float = 0.0
    component_status: dict = field(default_factory=dict)
    paused_strategies: list[str] = field(default_factory=list)

    @property
    def can_trade(self) -> bool:
        """¿Se puede operar (cualquier tipo)?
        
        FULL, CLOB_WS_DOWN, REDIS_DOWN, POLYGON_RPC_DOWN → se puede operar
        (estrategias direccionales). Solo MINIMAL lo impide.
        """
        return self.mode != SystemMode.MINIMAL

    @property
    def can_market_make(self) -> bool:
        """¿Se puede hacer Market Making? (requiere CLOB L2)."""
        return self.mode == SystemMode.FULL

    @property
    def can_arbitrage(self) -> bool:
        """¿Se puede hacer arbitraje? (requiere CLOB)."""
        return self.mode == SystemMode.FULL

    @property
    def can_whale_track(self) -> bool:
        """¿Whale tracking activo?"""
        return self.mode not in (SystemMode.POLYGON_RPC_DOWN, SystemMode.MINIMAL)

    @property
    def has_order_book(self) -> bool:
        """¿Order book L2 disponible?"""
        return self.mode == SystemMode.FULL

    @property
    def has_interprocess_comms(self) -> bool:
        """¿Comunicación entre procesos disponible?"""
        return self.mode != SystemMode.REDIS_DOWN

    @property
    def conviction_multiplier_active(self) -> bool:
        """¿Conviction Multiplier activo? (sin Polygon → CM=1.0 neutro)."""
        return self.mode not in (SystemMode.POLYGON_RPC_DOWN, SystemMode.MINIMAL)


# ── DegradationManager ────────────────────────────────────────────

class DegradationManager:
    """Gestor de degradación del sistema.

    Parameters
    ----------
    initial_mode : SystemMode
        Modo inicial (default FULL).
    auto_recover : bool
        Si True, intenta recuperar automáticamente tras timeouts.
    recovery_timeout : float
        Segundos antes de intentar recuperación automática.
    """

    def __init__(
        self,
        initial_mode: SystemMode = SystemMode.FULL,
        auto_recover: bool = True,
        recovery_timeout: float = 300,  # 5 minutos
    ):
        import time as _time
        self._state = DegradationState(
            mode=initial_mode,
            last_mode_change=_time.time(),
        )
        self.auto_recover = auto_recover
        self.recovery_timeout = recovery_timeout

        # Health checks
        self._health_checks: dict[str, callable] = {}

    # ── Mode Management ───────────────────────────────────────────

    def set_mode(self, mode: SystemMode, reason: str = "") -> None:
        """Transiciona a un modo de degradación."""
        import time as _time

        if mode == self._state.mode:
            return

        old_mode = self._state.mode
        self._state.mode = mode
        self._state.last_mode_change = _time.time()
        self._state.mode_duration = 0.0

        logger.warning(
            "DEGRADACIÓN: %s → %s (%s)",
            old_mode.value, mode.value, reason,
        )

        # Auto-pausar estrategias incompatibles
        self._update_paused_strategies()

    def get_mode(self) -> SystemMode:
        """Retorna el modo actual."""
        return self._state.mode

    def _update_paused_strategies(self) -> None:
        """Actualiza las estrategias pausadas según el modo actual."""
        paused = []

        if not self._state.can_market_make:
            paused.append("market_making")

        if not self._state.can_arbitrage:
            paused.append("correlation_arb")

        if not self._state.can_whale_track:
            paused.append("whale_follow")

        self._state.paused_strategies = paused

    # ── Health Checks ─────────────────────────────────────────────

    def register_health_check(self, name: str, check_fn: callable) -> None:
        """Registra una función de health check.

        check_fn debe retornar True si el componente está sano.
        """
        self._health_checks[name] = check_fn

    def check_health(self) -> dict:
        """Ejecuta todos los health checks y retorna resultados."""
        results = {}
        for name, check_fn in self._health_checks.items():
            try:
                healthy = check_fn()
                results[name] = healthy
                self._state.component_status[name] = healthy
            except Exception as e:
                results[name] = False
                self._state.component_status[name] = False
                logger.error("Health check '%s' falló: %s", name, e)
        return results

    def auto_evaluate(self) -> None:
        """Evalúa automáticamente si se debe cambiar de modo basado en health checks."""
        import time as _time

        health = self.check_health()

        # Evaluar CLOB WebSocket
        clob_healthy = health.get("clob_ws", True)
        polygon_healthy = health.get("polygon_rpc", True)
        redis_healthy = health.get("redis", True)

        now = _time.time()

        if not clob_healthy and self._state.mode == SystemMode.FULL:
            self.set_mode(SystemMode.CLOB_WS_DOWN, "CLOB WS health check failed")
        elif not polygon_healthy and self._state.mode in (SystemMode.FULL, SystemMode.CLOB_WS_DOWN):
            self.set_mode(SystemMode.POLYGON_RPC_DOWN, "Polygon RPC health check failed")
        elif not redis_healthy and self._state.mode not in (SystemMode.REDIS_DOWN, SystemMode.MINIMAL):
            self.set_mode(SystemMode.REDIS_DOWN, "Redis health check failed")

        # Auto-recovery: intentar subir de modo tras timeout
        if self.auto_recover:
            time_in_mode = now - self._state.last_mode_change
            if time_in_mode > self.recovery_timeout:
                if self._state.mode == SystemMode.CLOB_WS_DOWN and clob_healthy:
                    self.set_mode(SystemMode.FULL, "auto-recovery: CLOB WS restored")
                elif self._state.mode == SystemMode.POLYGON_RPC_DOWN and polygon_healthy:
                    self.set_mode(SystemMode.FULL, "auto-recovery: Polygon RPC restored")
                elif self._state.mode == SystemMode.REDIS_DOWN and redis_healthy:
                    self.set_mode(SystemMode.FULL, "auto-recovery: Redis restored")

    # ── Strategy Compatibility ────────────────────────────────────

    def is_strategy_allowed(self, strategy_name: str) -> bool:
        """Verifica si una estrategia puede operar en el modo actual."""
        return strategy_name not in self._state.paused_strategies

    def get_allowed_strategies(self, all_strategies: list[str]) -> list[str]:
        """Filtra estrategias permitidas en el modo actual."""
        return [s for s in all_strategies if self.is_strategy_allowed(s)]

    # ── Trading Mode ──────────────────────────────────────────────

    def get_price_source(self) -> str:
        """Determina la fuente de precios según el modo.

        Returns: "clob_l2", "gamma", o "cached".
        """
        if self._state.mode == SystemMode.FULL:
            return "clob_l2"
        elif self._state.mode in (SystemMode.REDIS_DOWN, SystemMode.CLOB_WS_DOWN):
            return "gamma"
        else:
            return "gamma"

    def get_conviction_multiplier_override(self) -> Optional[float]:
        """Retorna el CM override según el modo.

        None = usar el CM calculado normalmente.
        1.0 = neutro (cuando Polygon RPC está caído).
        """
        if self._state.mode == SystemMode.POLYGON_RPC_DOWN:
            return 1.0
        return None

    # ── Metrics ───────────────────────────────────────────────────

    def get_degradation_metrics(self) -> dict:
        """Métricas para monitorización."""
        import time as _time
        now = _time.time()

        return {
            "mode": self._state.mode.value,
            "duration_seconds": now - self._state.last_mode_change,
            "can_trade": self._state.can_trade,
            "can_market_make": self._state.can_market_make,
            "paused_strategies": self._state.paused_strategies,
            "component_status": dict(self._state.component_status),
        }

    @property
    def state(self) -> DegradationState:
        return self._state


# ── Degradation Scenarios (helpers) ────────────────────────────────

def simulate_degradation_scenarios() -> list[dict]:
    """Retorna todos los escenarios de degradación documentados.

    Útil para testing y documentación.
    """
    return [
        {
            "scenario": "CLOB WS caído > 30s",
            "mode": SystemMode.CLOB_WS_DOWN,
            "response": (
                "Modo Gamma-only: precios Gamma, sin order book. "
                "Market Making pausado. Estrategias direccionales siguen operando."
            ),
        },
        {
            "scenario": "Polygon RPC caído",
            "mode": SystemMode.POLYGON_RPC_DOWN,
            "response": (
                "Whale tracking pausado. Conviction Multiplier = 1.0 (neutro) "
                "para todos los mercados."
            ),
        },
        {
            "scenario": "Redis caído",
            "mode": SystemMode.REDIS_DOWN,
            "response": (
                "Modo standalone: cada proceso usa su propio estado local. "
                "Sin comunicación entre módulos. Portfolio Manager usa última "
                "asignación cached."
            ),
        },
        {
            "scenario": "Degradación total",
            "mode": SystemMode.MINIMAL,
            "response": (
                "Solo Radar Layer (descubrimiento básico). Sin trading."
            ),
        },
    ]
