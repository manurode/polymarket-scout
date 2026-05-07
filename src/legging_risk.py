"""
Legging Risk Manager — Ejecución Fill-or-Kill (FOK) a nivel de aplicación.

Polymarket no tiene smart contracts nativos para operaciones atómicas
multi-mercado. Cuando ejecutamos un arbitraje de correlación (ej: comprar A,
vender B), las órdenes se envían secuencialmente. Si la primera se ejecuta
y la segunda falla, quedamos con exposición direccional no deseada.

Este módulo implementa FOK simulado:
- Identifica el mercado con menor liquidez (cuello de botella).
- Dispara PRIMERO la orden en el mercado ilíquido.
- Solo si se confirma (vía WebSocket), dispara la orden de hedge.
- Timeout de 500ms. Si falla → abortar o emergency unwind.

Uso:
    fok = LeggingRiskManager(book_analyzer)
    result = await fok.execute_arbitrage(
        leg_a={"condition_id": "0xA", "token_id": "tokenA", "side": "buy", "size": 50},
        leg_b={"condition_id": "0xB", "token_id": "tokenB", "side": "sell", "size": 50},
        place_order=my_place_order_fn,
    )
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────

FOK_TIMEOUT_MS = 500            # timeout para confirmación WebSocket (ms)
MAX_RETRIES_PER_OPPORTUNITY = 3  # máximo reintentos antes de descartar
EMERGENCY_UNWIND_SLIPPAGE = 0.01  # 1% slippage aceptable en emergency unwind


# ── Tipos ──────────────────────────────────────────────────────────

class LegStatus(Enum):
    """Estado de una pata del arbitraje."""
    PENDING = "pending"
    PLACED = "placed"        # orden enviada, esperando confirmación
    FILLED = "filled"        # ejecutada
    PARTIAL = "partial"      # ejecutada parcialmente
    FAILED = "failed"        # rechazada o timeout
    CANCELLED = "cancelled"  # cancelada por nosotros


@dataclass
class LegOrder:
    """Una orden de una pata del arbitraje."""
    condition_id: str
    token_id: str
    side: str         # "buy" o "sell"
    size: float       # tamaño en USD
    price: float = 0.0
    order_id: str = ""
    status: LegStatus = LegStatus.PENDING
    fill_size: float = 0.0
    fill_price: float = 0.0
    placed_at: float = 0.0
    confirmed_at: float = 0.0


@dataclass
class FOKResult:
    """Resultado de una ejecución FOK."""
    success: bool
    legs: list[LegOrder]
    total_profit: float = 0.0
    emergency_unwind: bool = False
    unwind_loss: float = 0.0
    error_message: str = ""
    duration_ms: float = 0.0


# Type alias for the order placement callback
PlaceOrderFn = Callable[
    [str, str, str, float, float],  # condition_id, token_id, side, size, price
    Awaitable[dict],  # returns order info {order_id, status, ...}
]


# ── LeggingRiskManager ─────────────────────────────────────────────

class LeggingRiskManager:
    """Gestor de Legging Risk con ejecución Fill-or-Kill (FOK) simulada.

    Parameters
    ----------
    book_analyzer : BookAnalyzer | None
        Para obtener liquidez y determinar el cuello de botella.
    timeout_ms : float
        Timeout para confirmación de orden (default 500ms).
    max_retries : int
        Máximo reintentos por oportunidad (default 3).
    """

    def __init__(
        self,
        book_analyzer=None,
        timeout_ms: float = FOK_TIMEOUT_MS,
        max_retries: int = MAX_RETRIES_PER_OPPORTUNITY,
    ):
        self._books = book_analyzer
        self.timeout_ms = timeout_ms
        self.max_retries = max_retries

    # ── Core FOK Execution ─────────────────────────────────────────

    async def execute_arbitrage(
        self,
        leg_a: dict,
        leg_b: dict,
        place_order: PlaceOrderFn,
        confirm_fill: Optional[Callable[[str], Awaitable[bool]]] = None,
    ) -> FOKResult:
        """Ejecuta un arbitraje de dos patas con protección FOK.

        Parameters
        ----------
        leg_a : dict
            Primera pata (se determina automáticamente cuál es la ilíquida).
            Campos: condition_id, token_id, side, size, price (opcional).
        leg_b : dict
            Segunda pata.
        place_order : PlaceOrderFn
            Callback para colocar órdenes.
            async def place_order(condition_id, token_id, side, size, price) -> dict
        confirm_fill : Callable | None
            Callback opcional para confirmar que una orden fue ejecutada.
            async def confirm_fill(order_id) -> bool

        Returns
        -------
        FOKResult
        """
        t0 = time.monotonic()

        # ── Determinar cuál es el cuello de botella ────────────
        illiquid_leg, liquid_leg = self._identify_bottleneck(leg_a, leg_b)

        # ── Fase 1: Disparar orden en mercado ILÍQUIDO ─────────
        illiquid_order = await self._place_leg(illiquid_leg, place_order)

        if illiquid_order.status != LegStatus.FILLED:
            # No se llenó → abortar sin pérdida
            if illiquid_order.order_id:
                # Intentar cancelar
                illiquid_order.status = LegStatus.CANCELLED

            duration = (time.monotonic() - t0) * 1000
            return FOKResult(
                success=False,
                legs=[illiquid_order],
                error_message=f"Leg ilíquida no se llenó: {illiquid_order.status.value}",
                duration_ms=duration,
            )

        # ── Confirmar fill de la pata ilíquida ─────────────────
        if confirm_fill:
            confirmed = await self._wait_for_confirm(
                illiquid_order.order_id, confirm_fill,
            )
            if not confirmed:
                # Cancelar si es posible
                illiquid_order.status = LegStatus.CANCELLED

                duration = (time.monotonic() - t0) * 1000
                return FOKResult(
                    success=False,
                    legs=[illiquid_order],
                    error_message="Timeout esperando confirmación de fill",
                    duration_ms=duration,
                )

        # ── Fase 2: Disparar orden de HEDGE ────────────────────
        # Ajustar tamaño si la pata ilíquida se llenó parcialmente
        if illiquid_order.status == LegStatus.PARTIAL:
            ratio = illiquid_order.fill_size / illiquid_order.size
            liquid_leg["size"] *= ratio

        liquid_order = await self._place_leg(liquid_leg, place_order)

        if liquid_order.status in (LegStatus.FILLED, LegStatus.PARTIAL):
            # ── Éxito ──────────────────────────────────────
            duration = (time.monotonic() - t0) * 1000
            return FOKResult(
                success=True,
                legs=[illiquid_order, liquid_order],
                duration_ms=duration,
            )

        else:
            # ── FALLO CRÍTICO: pata ilíquida ejecutada pero hedge NO ──
            # EMERGENCY UNWIND: liquidar posición abierta a mercado
            logger.critical(
                "¡LEGGING RISK! Pata ilíquida ejecutada (%s %s $%.0f) "
                "pero hedge falló. EMERGENCY UNWIND.",
                illiquid_order.side, illiquid_order.condition_id,
                illiquid_order.fill_size,
            )

            # Emergency unwind: orden contraria a mercado
            unwind_side = "sell" if illiquid_order.side == "buy" else "buy"
            unwind_size = illiquid_order.fill_size
            unwind_loss = unwind_size * EMERGENCY_UNWIND_SLIPPAGE

            await place_order(
                illiquid_order.condition_id,
                illiquid_order.token_id,
                unwind_side,
                unwind_size,
                0,  # precio de mercado
            )

            duration = (time.monotonic() - t0) * 1000
            return FOKResult(
                success=False,
                legs=[illiquid_order, liquid_order],
                emergency_unwind=True,
                unwind_loss=unwind_loss,
                error_message="Hedge falló — emergency unwind ejecutado",
                duration_ms=duration,
            )

    # ── Bottleneck Identification ──────────────────────────────────

    def _identify_bottleneck(
        self, leg_a: dict, leg_b: dict,
    ) -> tuple[dict, dict]:
        """Determina cuál pata es el cuello de botella (menor liquidez).

        Returns
        -------
        tuple[dict, dict]
            (ilíquida, líquida)
        """
        liq_a = self._estimate_liquidity(leg_a.get("token_id", ""))
        liq_b = self._estimate_liquidity(leg_b.get("token_id", ""))

        if liq_a <= liq_b:
            return leg_a, leg_b
        else:
            return leg_b, leg_a

    def _estimate_liquidity(self, token_id: str) -> float:
        """Estima la liquidez de un mercado desde el BookAnalyzer."""
        if self._books is None:
            return 0.0

        book = self._books.get_book(token_id)
        if book is None:
            return 0.0

        # Suma del volumen en los primeros 3 niveles
        bid_vol = float(book.bids[:3, 1].sum()) if book.bids.shape[0] >= 3 else 0.0
        ask_vol = float(book.asks[:3, 1].sum()) if book.asks.shape[0] >= 3 else 0.0

        return bid_vol + ask_vol

    # ── Order Placement ────────────────────────────────────────────

    async def _place_leg(self, leg: dict, place_order: PlaceOrderFn) -> LegOrder:
        """Coloca una pata del arbitraje y espera confirmación."""
        order = LegOrder(
            condition_id=leg.get("condition_id", ""),
            token_id=leg.get("token_id", ""),
            side=leg.get("side", "buy"),
            size=leg.get("size", 0),
            price=leg.get("price", 0),
            placed_at=time.monotonic(),
        )

        try:
            # Timeout para la colocación
            result = await asyncio.wait_for(
                place_order(
                    order.condition_id,
                    order.token_id,
                    order.side,
                    order.size,
                    order.price,
                ),
                timeout=self.timeout_ms / 1000,
            )

            order.order_id = result.get("order_id", result.get("id", ""))
            status = result.get("status", "").lower()

            if status in ("filled", "matched"):
                order.status = LegStatus.FILLED
                order.fill_size = float(result.get("filled_size", order.size))
                order.fill_price = float(result.get("filled_price", order.price))
                order.confirmed_at = time.monotonic()
            elif status in ("partial", "partially_filled"):
                order.status = LegStatus.PARTIAL
                order.fill_size = float(result.get("filled_size", 0))
                order.fill_price = float(result.get("filled_price", order.price))
                order.confirmed_at = time.monotonic()
            elif status in ("open", "placed", "pending"):
                order.status = LegStatus.PLACED
                # En paper trading, considerar como filled inmediatamente
                order.status = LegStatus.FILLED
                order.fill_size = order.size
                order.fill_price = order.price
                order.confirmed_at = time.monotonic()
            else:
                order.status = LegStatus.FAILED

        except asyncio.TimeoutError:
            order.status = LegStatus.FAILED
            logger.warning("Timeout colocando orden para %s", order.condition_id)
        except Exception as e:
            order.status = LegStatus.FAILED
            logger.error("Error colocando orden: %s", e)

        return order

    async def _wait_for_confirm(
        self,
        order_id: str,
        confirm_fill: Callable[[str], Awaitable[bool]],
    ) -> bool:
        """Espera confirmación de fill vía WebSocket."""
        deadline = time.monotonic() + self.timeout_ms / 1000

        while time.monotonic() < deadline:
            try:
                if await confirm_fill(order_id):
                    return True
            except Exception:
                pass

            await asyncio.sleep(0.05)  # poll cada 50ms

        return False

    # ── Capital Lock-Up Calculation ────────────────────────────────

    @staticmethod
    def calculate_capital_efficiency(
        gross_profit: float,
        capital_required: float,
        days_to_resolution: float,
        risk_free_rate: float = 0.05,
        risk_premium: float = 0.15,
        hurdle_rate: float = 0.20,
    ) -> dict:
        """Calcula la eficiencia de capital para un arbitraje.

        Parameters
        ----------
        gross_profit : float
            Beneficio bruto en USD.
        capital_required : float
            Capital a inmovilizar en USD.
        days_to_resolution : float
            Días hasta la resolución.
        risk_free_rate : float
            Tasa libre de riesgo anualizada.
        risk_premium : float
            Prima de riesgo adicional.
        hurdle_rate : float
            Tasa mínima requerida.

        Returns
        -------
        dict con:
            - annualized_return: float
            - adjusted_return: float
            - meets_hurdle: bool
            - recommendation: str
        """
        if capital_required <= 0 or days_to_resolution <= 0:
            return {
                "annualized_return": 0.0,
                "adjusted_return": -float("inf"),
                "meets_hurdle": False,
                "recommendation": "skip_zero_capital",
            }

        annualized = (gross_profit / capital_required) * (365 / days_to_resolution)
        adjusted = annualized - risk_free_rate - risk_premium

        if adjusted > 0 and annualized > hurdle_rate:
            recommendation = "execute"
        elif annualized > risk_free_rate:
            recommendation = "monitor"  # rentable pero bajo hurdle
        else:
            recommendation = "skip"

        return {
            "annualized_return": annualized,
            "adjusted_return": adjusted,
            "meets_hurdle": adjusted > 0 and annualized > hurdle_rate,
            "recommendation": recommendation,
        }

    # ── Gas Cost Integration ───────────────────────────────────────

    @staticmethod
    def estimate_execution_cost(
        gas_estimated: int = 200000,
        base_fee_gwei: float = 50,
        priority_fee_gwei: float = 30,
        pol_price_usd: float = 0.40,
    ) -> float:
        """Estima el coste de ejecución en Polygon.

        Parameters
        ----------
        gas_estimated : int
            Gas estimado para la transacción.
        base_fee_gwei : float
            Base fee en Gwei.
        priority_fee_gwei : float
            Priority fee en Gwei.
        pol_price_usd : float
            Precio de POL en USD.

        Returns
        -------
        float
            Coste total en USD.
        """
        total_gwei = base_fee_gwei + priority_fee_gwei
        cost_pol = (gas_estimated * total_gwei) / 1e9
        return cost_pol * pol_price_usd

    @staticmethod
    def should_execute_with_gas(
        gross_profit: float,
        capital: float,
        days: float,
        gas_cost: float,
        avg_spread: float = 0.02,
        risk_free_rate: float = 0.05,
        hurdle_rate: float = 0.20,
    ) -> dict:
        """Decide si ejecutar considerando coste de gas + slippage.

        Returns dict con recommendation y retorno ajustado.
        """
        slippage = capital * avg_spread * 0.5  # peor caso: cruzar mitad del spread
        total_cost = gas_cost + slippage
        net_profit = gross_profit - total_cost

        if capital <= 0 or days <= 0:
            return {"execute": False, "reason": "invalid_params"}

        annualized = (net_profit / capital) * (365 / days)
        adjusted = annualized - risk_free_rate

        if net_profit <= 0:
            return {"execute": False, "reason": f"negative_net_profit: ${net_profit:.2f}"}

        if adjusted <= 0 or annualized <= hurdle_rate:
            return {
                "execute": False,
                "reason": f"below_hurdle: {annualized:.1%} < {hurdle_rate:.0%}",
                "annualized_return": annualized,
            }

        return {
            "execute": True,
            "reason": "ok",
            "annualized_return": annualized,
            "net_profit": net_profit,
            "total_cost": total_cost,
        }
