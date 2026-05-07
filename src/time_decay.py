"""
Time-Decay Risk Manager — Gestión de riesgo por cercanía a expiración.

En mercados binarios con fecha de expiración, mantener inventario cerca
de la fecha final es riesgo direccional puro. Este módulo implementa una
función de decaimiento temporal que reduce progresivamente el inventario
máximo permitido a medida que el mercado se acerca a su resolución.

La función risk_multiplier(τ) es cuadrática con un punto de transición
en τ=0.70 (70% de la vida del mercado consumida).

Uso:
    tdm = TimeDecayManager()
    mult = tdm.get_risk_multiplier(market_created_at, market_end_date)
    max_inv = BASE_INVENTORY_CAP * mult * tdm.get_liquidity_factor(liquidity)
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────

RISK_FLOOR = 0.05             # riesgo mínimo (nunca cero absoluto)
TRANSITION_POINT = 0.70       # τ donde empieza la reducción
BASE_INVENTORY_CAP = 500.0    # $500 — inventario máximo base
MIN_LIQUIDITY = 10000.0       # $10K — liquidez mínima para factor 1.0

# Umbrales de liquidación forzosa
LIQUIDATION_TAU = 0.95        # τ > 0.95 → liquidación forzosa
LIQUIDATION_SLIPPAGE = 0.02   # 2% de slippage aceptable en liquidación


# ── Tipos ──────────────────────────────────────────────────────────

@dataclass
class TimeDecayResult:
    """Resultado del cálculo de time-decay para un mercado."""
    tau: float                      # fracción de vida consumida [0, 1]
    risk_multiplier: float          # multiplicador de riesgo [RISK_FLOOR, 1.0]
    max_inventory_usd: float        # inventario máximo permitido en USD
    time_to_expiry_hours: float     # horas hasta expiración
    is_liquidation_zone: bool       # True si τ > LIQUIDATION_TAU
    is_transition_zone: bool        # True si τ > TRANSITION_POINT


class TimeDecayManager:
    """Gestor de riesgo por decaimiento temporal.

    Parameters
    ----------
    base_inventory_cap : float
        Inventario máximo base en USD (default $500).
    transition_point : float
        Fracción de vida donde empieza la reducción (default 0.70).
    risk_floor : float
        Multiplicador mínimo de riesgo (default 0.05).
    min_liquidity : float
        Liquidez mínima para factor 1.0 (default $10,000).
    """

    def __init__(
        self,
        base_inventory_cap: float = BASE_INVENTORY_CAP,
        transition_point: float = TRANSITION_POINT,
        risk_floor: float = RISK_FLOOR,
        min_liquidity: float = MIN_LIQUIDITY,
    ):
        self.base_inventory_cap = base_inventory_cap
        self.transition_point = transition_point
        self.risk_floor = risk_floor
        self.min_liquidity = min_liquidity

    # ── Core Calculation ──────────────────────────────────────────

    def get_risk_multiplier(
        self,
        created_at: float,
        end_date: float,
        now: Optional[float] = None,
    ) -> float:
        """Calcula el multiplicador de riesgo basado en τ.

        Parameters
        ----------
        created_at : float
            Timestamp Unix de creación del mercado.
        end_date : float
            Timestamp Unix de expiración/resolución.
        now : float | None
            Timestamp actual (default: time.time()).

        Returns
        -------
        float
            Multiplicador en [risk_floor, 1.0].
        """
        if now is None:
            now = time.time()

        tau = self._compute_tau(created_at, end_date, now)

        if tau <= self.transition_point:
            return 1.0

        # Función cuadrática: 1 - (τ - transition_point)²
        # Normalizada para que en τ=1.0 valga risk_floor
        raw = 1.0 - (tau - self.transition_point) ** 2

        # Escalar para que el mínimo sea risk_floor en vez de negativo
        # En τ=1.0: raw = 1 - (1-0.7)² = 1 - 0.09 = 0.91 (muy alto)
        # Necesitamos que baje más. Usamos una interpolación exponencial.
        # Refinamiento: escalar el rango [0, 1] → [risk_floor, 1.0]
        max_reduction = 1.0 - (1.0 - self.transition_point) ** 2
        if tau >= 1.0:
            return self.risk_floor

        # Mapeo lineal del decaimiento cuadrático al rango [risk_floor, 1.0]
        decay_factor = (tau - self.transition_point) / (1.0 - self.transition_point)
        multiplier = 1.0 - decay_factor * (1.0 - self.risk_floor)

        return max(self.risk_floor, multiplier)

    def evaluate(
        self,
        created_at: float,
        end_date: float,
        liquidity: float = 0.0,
        now: Optional[float] = None,
    ) -> TimeDecayResult:
        """Evalúa el riesgo completo de time-decay para un mercado.

        Parameters
        ----------
        created_at : float
            Timestamp de creación.
        end_date : float
            Timestamp de expiración.
        liquidity : float
            Liquidez del mercado en USD.
        now : float | None
            Timestamp actual.

        Returns
        -------
        TimeDecayResult
        """
        if now is None:
            now = time.time()

        tau = self._compute_tau(created_at, end_date, now)
        risk_mult = self.get_risk_multiplier(created_at, end_date, now)
        liq_factor = self.get_liquidity_factor(liquidity)

        max_inv = self.base_inventory_cap * risk_mult * liq_factor
        hours_left = (end_date - now) / 3600.0

        return TimeDecayResult(
            tau=tau,
            risk_multiplier=risk_mult,
            max_inventory_usd=max_inv,
            time_to_expiry_hours=max(0, hours_left),
            is_liquidation_zone=tau >= LIQUIDATION_TAU,
            is_transition_zone=tau >= self.transition_point,
        )

    # ── Liquidity Factor ─────────────────────────────────────────

    def get_liquidity_factor(self, liquidity: float) -> float:
        """Factor de ajuste por liquidez: min(1.0, liquidity / MIN_LIQUIDITY).

        Mercados con poca liquidez → posiciones más pequeñas.
        """
        if liquidity <= 0:
            return 0.1  # mínimo para mercados sin datos
        return min(1.0, liquidity / self.min_liquidity)

    # ── Liquidation Protocol ──────────────────────────────────────

    def should_liquidate(self, tau: float) -> bool:
        """Determina si un mercado debe entrar en liquidación forzosa."""
        return tau >= LIQUIDATION_TAU

    def get_liquidation_params(self) -> dict:
        """Parámetros para la liquidación forzosa."""
        return {
            "mode": "close_only",          # no abrir nuevas posiciones
            "cancel_all_passive": True,    # cancelar órdenes límite
            "use_market_orders": True,     # órdenes a mercado (taker)
            "max_slippage": LIQUIDATION_SLIPPAGE,
            "notify_operator": True,
        }

    # ── Quote Width Time Scalar ───────────────────────────────────

    def get_time_decay_scalar(
        self,
        created_at: float,
        end_date: float,
        now: Optional[float] = None,
    ) -> float:
        """Scalar para el quote width del Market Maker (§2.1).

        A medida que τ crece, los spreads deben ampliarse para compensar
        el mayor riesgo direccional. Rango: [0.8, 3.0].
        """
        tau = self._compute_tau(created_at, end_date, now)

        if tau <= self.transition_point:
            return 1.0
        elif tau <= 0.85:
            # 0.70 → 0.85: 1.0 → 1.5
            return 1.0 + (tau - 0.70) / 0.15 * 0.5
        elif tau <= 0.95:
            # 0.85 → 0.95: 1.5 → 2.5
            return 1.5 + (tau - 0.85) / 0.10 * 1.0
        else:
            # 0.95 → 1.0: 2.5 → 3.0
            return 2.5 + min(0.5, (tau - 0.95) / 0.05 * 0.5)

    # ── Internal ──────────────────────────────────────────────────

    @staticmethod
    def _compute_tau(created_at: float, end_date: float, now: float) -> float:
        """Calcula τ = fracción de vida consumida.

        τ = (now - created_at) / (end_date - created_at)
        Rango: [0, ∞). Valores > 1 significan mercado expirado.
        """
        duration = end_date - created_at
        if duration <= 0:
            return 1.0  # mercado ya expirado o datos inválidos

        elapsed = now - created_at
        return max(0.0, elapsed / duration)
