"""
Paper Trading Engine — Simulación completa de ejecución y riesgo.

Módulo de Fase 2 de Scout Lab v2.0:
- Billetera virtual (USDC + POL)
- Registro de posiciones abiertas
- Mark-to-market en tiempo real
- Cierre automático (TP/SL, time-decay, liquidez)
- Feedback de rendimiento al PortfolioManager

Uso:
    engine = PaperTradingEngine(portfolio_manager)
    engine.open_position("momentum_follow", "Trump wins 2028?", "YES", size=100, entry=0.62)
    # El loop de mark-to-market actualiza P&L automáticamente
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Máximo de órdenes límite activas por token (evita acumulación ilimitada)
MAX_OPEN_ORDERS_PER_TOKEN = 4
# Vida máxima de una orden límite virtual (segundos) antes de cancelarse
ORDER_TTL_SECONDS = 120

# ── Constantes ─────────────────────────────────────────────────────────────────────

DEFAULT_INITIAL_USDC = 10_000.0
DEFAULT_INITIAL_POL = 100.0
POL_PRICE_USD = 0.82  # Precio estimado de POL en USD

MIN_POL_OPERATIVO = 2.0

# Criterios de cierre automático
TP_PCT = 0.15          # Take Profit: +15% sobre precio de entrada
SL_PCT = 0.10          # Stop Loss: -10% sobre precio de entrada
TAU_LIQUIDATION = 0.95  # Liquidación forzosa si tau > 95%
MAX_POSITION_AGE_H = 72  # Cierre forzoso tras 72h

# Realismo del simulador
SLIPPAGE_PCT = 0.01      # 1% de slippage aplicado al precio de cierre
POL_COMMISSION = 0.02    # 0.02 POL por trade cerrado (gas simulado)


# ── Tipos ─────────────────────────────────────────────────────────────────────────────

@dataclass
class VirtualLimitOrder:
    """Orden límite virtual pendiente de ejecución (pre-fill)."""
    id: int
    token_id: str
    market: str          # nombre legible del mercado
    strategy: str
    bid_price: float     # precio de compra (si aplica)
    ask_price: float     # precio de venta (si aplica)
    bid_size: float      # tamaño USD del lado bid
    ask_size: float      # tamaño USD del lado ask
    created_at: float = field(default_factory=time.time)


@dataclass
class VirtualPosition:
    """Posición de paper trading."""
    id: int
    market: str
    strategy: str
    side: str           # "YES" | "NO"
    size: float         # USD invertidos
    entry: float        # Precio de entrada
    mark: float = 0.0   # Precio mark-to-market
    pnl: float = 0.0
    pnl_pct: float = 0.0
    tau_pct: float = 0.0
    toxicity: float = 0.0
    liquidation_zone: bool = False
    opened_at: float = field(default_factory=time.time)
    closed_at: float | None = None
    close_reason: str | None = None  # "tp", "sl", "tau", "manual", "expired"


@dataclass
class VirtualWallet:
    """Estado de la billetera virtual."""
    usdc_free: float = DEFAULT_INITIAL_USDC
    usdc_collateral: float = 0.0
    pol_balance: float = DEFAULT_INITIAL_POL

    @property
    def usdc_total(self) -> float:
        return self.usdc_free + self.usdc_collateral

    @property
    def pol_usd_value(self) -> float:
        return self.pol_balance * POL_PRICE_USD

    def to_dict(self) -> dict:
        return {
            "usdc_free": round(self.usdc_free, 2),
            "usdc_collateral": round(self.usdc_collateral, 2),
            "usdc_total": round(self.usdc_total, 2),
            "pol_balance": round(self.pol_balance, 2),
            "pol_usd_value": round(self.pol_usd_value, 2),
            "ctf_allowance": True,
            "ctf_contract": "0x4D97DCd7C0408F728A009Ff07556F758a0969709",
        }


# ── PaperTradingEngine ───────────────────────────────────────────────────────────

class PaperTradingEngine:
    """Motor de paper trading con wallet virtual y mark-to-market."""

    def __init__(
        self,
        portfolio_manager=None,
        initial_usdc: float = DEFAULT_INITIAL_USDC,
        initial_pol: float = DEFAULT_INITIAL_POL,
        on_trade_close: callable | None = None,  # Callback para notificar cierre de trade
    ):
        self.pm = portfolio_manager
        self.on_trade_close = on_trade_close  # Callback: (strategy, pnl) -> None
        self.wallet = VirtualWallet(
            usdc_free=initial_usdc,
            pol_balance=initial_pol,
        )
        self._positions: list[VirtualPosition] = []
        self._position_counter = 0
        self._trade_history: list[dict] = []
        self._lock = asyncio.Lock()

        # ── Virtual Limit Order Book (Cross Engine) ────────────────────────
        self._open_orders: list[VirtualLimitOrder] = []
        self._order_counter = 0

    # ── Cross Engine: Virtual Limit Orders ─────────────────────────────────────────

    def register_mm_quote(
        self,
        token_id: str,
        market: str,
        bid_price: float,
        ask_price: float,
        bid_size: float,
        ask_size: float,
        strategy: str = "market_making",
    ) -> VirtualLimitOrder:
        """Registra un quote de Market Making como orden límite virtual activa.

        El Cross Engine evaluará estas órdenes en cada `cross_and_fill()` para
        determinar si el precio real del mercado las cruza y ejecutarlas.

        Parameters
        ----------
        token_id : str
            ID del token del mercado.
        market : str
            Nombre legible del mercado.
        bid_price, ask_price : float
            Precios bid y ask del quote.
        bid_size, ask_size : float
            Tamaños en USD para cada lado.
        strategy : str
            Nombre del brazo del Bandit.

        Returns
        -------
        VirtualLimitOrder
            La orden registrada.
        """
        now = time.time()

        # Cancelar órdenes antiguas (TTL expirado) para este token
        self._open_orders = [
            o for o in self._open_orders
            if not (o.token_id == token_id and (now - o.created_at) > ORDER_TTL_SECONDS)
        ]

        # Limitar órdenes activas por token
        token_orders = [o for o in self._open_orders if o.token_id == token_id]
        if len(token_orders) >= MAX_OPEN_ORDERS_PER_TOKEN:
            # Eliminar la más antigua
            oldest = min(token_orders, key=lambda o: o.created_at)
            self._open_orders.remove(oldest)

        self._order_counter += 1
        order = VirtualLimitOrder(
            id=self._order_counter,
            token_id=token_id,
            market=market,
            strategy=strategy,
            bid_price=bid_price,
            ask_price=ask_price,
            bid_size=bid_size,
            ask_size=ask_size,
        )
        self._open_orders.append(order)
        logger.debug(
            "CrossEngine: orden límite #%d registrada | %s | bid=%.4f ask=%.4f",
            order.id, token_id[:16], bid_price, ask_price,
        )
        return order

    async def cross_and_fill(
        self,
        token_id: str,
        real_best_bid: float,
        real_best_ask: float,
    ) -> list[VirtualPosition]:
        """Cruza el precio real del mercado contra las órdenes límite virtuales.

        Lógica de cruce (market-making pasivo):
        - Si el Best Ask real BAJA hasta cruzar nuestro Virtual Bid → Fill BID:
          Alguien en el mercado está dispuesto a vender más barato que nuestro bid.
          Simulamos que compramos YES → posición LONG YES.
        - Si el Best Bid real SUBE hasta cruzar nuestro Virtual Ask → Fill ASK:
          Alguien en el mercado paga más que nuestro ask.
          Simulamos que vendemos NO → posición SHORT (LONG NO).

        Parameters
        ----------
        token_id : str
            ID del token cuyo book acaba de actualizarse.
        real_best_bid : float
            Mejor bid real actual del mercado (Polymarket CLOB).
        real_best_ask : float
            Mejor ask real actual del mercado (Polymarket CLOB).

        Returns
        -------
        list[VirtualPosition]
            Posiciones abiertas por fills ejecutados en este ciclo.
        """
        filled_positions: list[VirtualPosition] = []
        if real_best_bid <= 0 or real_best_ask <= 0:
            return filled_positions

        now = time.time()
        orders_to_fill: list[tuple[VirtualLimitOrder, str, float, float]] = []

        # Identificar órdenes a ejecutar (sin lock para la lectura)
        for order in list(self._open_orders):
            if order.token_id != token_id:
                continue

            # TTL check
            if (now - order.created_at) > ORDER_TTL_SECONDS:
                continue

            # ── Fill BID: real_best_ask <= our_bid_price ─────────────────
            # El mercado ofrece vender a un precio ≤ lo que nosotros pagamos.
            # Esto significa que nuestra orden de compra se ejecuta.
            if real_best_ask <= order.bid_price and order.bid_size >= 50:
                orders_to_fill.append((order, "YES", order.bid_price, order.bid_size))

            # ── Fill ASK: real_best_bid >= our_ask_price ─────────────────
            # El mercado quiere comprar a un precio ≥ lo que nosotros pedimos.
            # Nuestra orden de venta se ejecuta → abrimos posición NO.
            elif real_best_bid >= order.ask_price and order.ask_size >= 50:
                orders_to_fill.append((order, "NO", order.ask_price, order.ask_size))

        # Ejecutar fills
        for order, side, fill_price, fill_size in orders_to_fill:
            # Eliminar la orden del libro (ya ejecutada)
            try:
                self._open_orders.remove(order)
            except ValueError:
                continue  # ya fue eliminada por otro fill concurrente

            size = min(fill_size, self.wallet.usdc_free)
            if size < 50:
                logger.debug("CrossEngine: fill omitido (capital libre=$%.2f)", self.wallet.usdc_free)
                continue

            pos = await self.open_position(
                strategy=order.strategy,
                market=order.market,
                side=side,
                size=size,
                entry=fill_price,
                tau_pct=random.uniform(10, 55),
                toxicity=random.uniform(0.03, 0.20),
            )

            if pos:
                filled_positions.append(pos)
                logger.info(
                    "PAPER TRADE EXECUTED | Side: %s | Price: %.4f | Size: $%.0f | "
                    "Market: %s | token: %s | real_bb=%.4f real_ba=%.4f",
                    side, fill_price, size,
                    order.market[:55], token_id[:16],
                    real_best_bid, real_best_ask,
                )

        return filled_positions

    def get_open_orders(self) -> list[dict]:
        """Retorna las órdenes límite virtuales activas (para el dashboard)."""
        now = time.time()
        return [
            {
                "id": o.id,
                "token_id": o.token_id,
                "market": o.market,
                "strategy": o.strategy,
                "bid_price": round(o.bid_price, 4),
                "ask_price": round(o.ask_price, 4),
                "bid_size": round(o.bid_size, 2),
                "ask_size": round(o.ask_size, 2),
                "age_s": round(now - o.created_at, 1),
            }
            for o in self._open_orders
            if (now - o.created_at) <= ORDER_TTL_SECONDS
        ]

    # ── Execution ───────────────────────────────────────────────────────────────────

    async def open_position(
        self,
        strategy: str,
        market: str,
        side: str,
        size: float,
        entry: float,
        tau_pct: float = 0.0,
        toxicity: float = 0.0,
    ) -> VirtualPosition | None:
        """Abre una posición virtual.

        Returns
        -------
        VirtualPosition | None
            La posición creada, o None si no hay capital suficiente.
        """
        async with self._lock:
            if size > self.wallet.usdc_free:
                logger.warning("PaperTrade: fondos insuficientes (%s < %s)", self.wallet.usdc_free, size)
                return None

            self._position_counter += 1
            pos = VirtualPosition(
                id=self._position_counter,
                market=market,
                strategy=strategy,
                side=side,
                size=size,
                entry=entry,
                mark=entry,
                tau_pct=tau_pct,
                toxicity=toxicity,
            )
            self._positions.append(pos)

            # Bloquear collateral
            self.wallet.usdc_free -= size
            self.wallet.usdc_collateral += size

            logger.info(
                "PaperTrade OPEN #%d %s %s %s @ $%.3f (size=$%.2f)",
                pos.id, strategy, side, market, entry, size,
            )
            return pos

    async def close_position(
        self,
        position_id: int,
        close_price: float | None = None,
        reason: str = "manual",
        apply_slippage: bool = True,
    ) -> dict | None:
        """Cierra una posición virtual y calcula P&L.

        Aplica slippage realista del SLIPPAGE_PCT % cruzando contra liquidez L2.
        Las comisiones se cobran en POL (gas simulado).

        Returns
        -------
        dict | None
            Resumen del trade cerrado.
        """
        async with self._lock:
            pos = next((p for p in self._positions if p.id == position_id), None)
            if pos is None or pos.closed_at is not None:
                return None

            price = close_price if close_price is not None else pos.mark

            # ── Aplicar slippage realista ──────────────────────────────
            # Al cerrar una posición YES, vendemos → recibimos un precio peor (slippage negativo).
            # Al cerrar una posición NO, compramos para cerrar → precio peor.
            if apply_slippage:
                if pos.side == "YES":
                    price = price * (1.0 - SLIPPAGE_PCT)   # vendemos más barato
                else:
                    price = price * (1.0 + SLIPPAGE_PCT)   # compramos más caro
                price = round(max(0.001, min(0.999, price)), 6)

            # Calcular P&L
            if pos.side == "YES":
                pnl = (price - pos.entry) * pos.size
            else:  # NO
                pnl = (pos.entry - price) * pos.size

            pos.pnl = round(pnl, 2)
            pos.pnl_pct = round((pnl / pos.size) * 100, 2) if pos.size > 0 else 0.0
            pos.closed_at = time.time()
            pos.close_reason = reason
            pos.mark = price

            # Liberar collateral
            self.wallet.usdc_collateral -= pos.size
            self.wallet.usdc_free += pos.size + pnl

            # Comisión en POL simulada (gas + protocolo)
            self.wallet.pol_balance = max(0.0, self.wallet.pol_balance - POL_COMMISSION)

            # Registrar en historial
            trade = {
                "id": pos.id,
                "strategy": pos.strategy,
                "market": pos.market,
                "side": pos.side,
                "size": pos.size,
                "entry": pos.entry,
                "exit": price,
                "slippage_pct": SLIPPAGE_PCT * 100 if apply_slippage else 0.0,
                "pnl": pos.pnl,
                "pnl_pct": pos.pnl_pct,
                "reason": reason,
                "closed_at": pos.closed_at,
            }
            self._trade_history.append(trade)

            # ── Feedback al PortfolioManager (Bandit) ─────────────────────
            if self.pm:
                equity_before = self.wallet.usdc_total - pnl  # equity antes de aplicar P&L
                self.pm.record_trade(pos.strategy, pnl, equity_before)
                # Actualizar Sortino con todos los trades cerrados de esta estrategia
                strategy_trades = [
                    {"pnl": t["pnl"], "amount_invested": t["size"]}
                    for t in self._trade_history
                    if t["strategy"] == pos.strategy
                ]
                sortino = self.pm.update_strategy_performance(pos.strategy, strategy_trades)
                # Log del Bandit: asignación actualizada
                self._log_bandit_allocation(pos.strategy, sortino)

            # Feedback al AdaptiveStrategyEngine via callback
            if self.on_trade_close:
                try:
                    self.on_trade_close(pos.strategy, pnl)
                except Exception as e:
                    logger.error("Error en on_trade_close callback: %s", e)

            logger.info(
                "PaperTrade CLOSE #%d [%s] %s P&L=$%.2f (%.1f%%) slippage=%.1f%% reason=%s",
                pos.id, pos.strategy, pos.market[:40],
                pnl, pos.pnl_pct, SLIPPAGE_PCT * 100, reason,
            )
            return trade

    def _log_bandit_allocation(self, updated_strategy: str, sortino: float) -> None:
        """Emite un log INFO con el estado del Bandit tras actualizar una estrategia."""
        if not self.pm:
            return
        try:
            equity = self.wallet.usdc_total
            allocations = self.pm.allocate(equity)
            parts = []
            for alloc in sorted(allocations, key=lambda a: a.fraction, reverse=True):
                state = self.pm.get_strategy_state(alloc.strategy)
                status = state.status.value.upper() if state else "?"
                pct = round(alloc.fraction * 100, 1)
                parts.append(f"{alloc.strategy}={pct}%[{status}]")
            logger.info(
                "🎰 BANDIT UPDATE [%s] sortino=%.3f | Asignaciones: %s",
                updated_strategy, sortino, "  ".join(parts),
            )
        except Exception as e:
            logger.debug("Error logging bandit: %s", e)

    # ── Mark-to-Market ───────────────────────────────────────────────────────────────────

    async def mark_to_market(self, price_source: dict[str, float] | None = None) -> None:
        """Actualiza el mark price de todas las posiciones abiertas.

        Parameters
        ----------
        price_source : dict[str, float] | None
            Diccionario {market: precio_actual}. Si es None, simula movimiento.
        """
        async with self._lock:
            for pos in self._positions:
                if pos.closed_at is not None:
                    continue

                if price_source and pos.market in price_source:
                    pos.mark = price_source[pos.market]
                else:
                    # Simulación: movimiento browniano ligero
                    drift = 0.0
                    vol = 0.002  # 0.2% volatilidad por tick
                    dt = 1.0
                    shock = random.gauss(drift * dt, vol * (dt ** 0.5))
                    pos.mark = max(0.01, min(0.99, pos.mark * (1 + shock)))

                # Recalcular P&L no realizado
                if pos.side == "YES":
                    pos.pnl = round((pos.mark - pos.entry) * pos.size, 2)
                else:
                    pos.pnl = round((pos.entry - pos.mark) * pos.size, 2)

                pos.pnl_pct = round((pos.pnl / pos.size) * 100, 2) if pos.size > 0 else 0.0

                # Liquidation zone si tau > 85%
                pos.liquidation_zone = pos.tau_pct > 85

    async def evaluate_auto_close(self) -> list[dict]:
        """Evalúa criterios de cierre automático y cierra posiciones que los cumplan.

        Returns
        -------
        list[dict]
            Trades cerrados.
        """
        closed = []
        # Copiar lista para evitar modificar durante iteración
        positions = list(self._positions)
        for pos in positions:
            if pos.closed_at is not None:
                continue

            reason = None
            if pos.pnl_pct >= TP_PCT * 100:
                reason = "tp"
            elif pos.pnl_pct <= -SL_PCT * 100:
                reason = "sl"
            elif pos.tau_pct >= TAU_LIQUIDATION * 100:
                reason = "tau"
            elif (time.time() - pos.opened_at) > MAX_POSITION_AGE_H * 3600:
                reason = "expired"

            if reason:
                trade = await self.close_position(pos.id, reason=reason)
                if trade:
                    closed.append(trade)

        return closed

    # ── Queries ────────────────────────────────────────────────────────────────────────────

    def get_positions(self, only_open: bool = True) -> list[dict]:
        """Retorna posiciones serializadas para el dashboard."""
        result = []
        for p in self._positions:
            if only_open and p.closed_at is not None:
                continue
            result.append({
                "id": p.id,
                "market": p.market,
                "strategy": p.strategy,
                "side": p.side,
                "size": p.size,
                "entry": p.entry,
                "mark": round(p.mark, 3),
                "pnl": p.pnl,
                "pnl_pct": p.pnl_pct,
                "tau_pct": p.tau_pct,
                "toxicity": p.toxicity,
                "liquidation_zone": p.liquidation_zone,
                "opened_at": p.opened_at,
            })
        return result

    def get_wallet(self) -> dict:
        """Retorna estado de la billetera virtual."""
        return self.wallet.to_dict()

    def get_trade_history(self, strategy: str | None = None) -> list[dict]:
        """Retorna historial de trades cerrados."""
        if strategy:
            return [t for t in self._trade_history if t["strategy"] == strategy]
        return list(self._trade_history)

    @property
    def open_position_count(self) -> int:
        return sum(1 for p in self._positions if p.closed_at is None)

    @property
    def total_pnl(self) -> float:
        return sum(p.pnl for p in self._positions if p.closed_at is not None)

    @property
    def unrealized_pnl(self) -> float:
        return sum(p.pnl for p in self._positions if p.closed_at is None)
