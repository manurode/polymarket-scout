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
from src.trading_logger import trading_log

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
SLIPPAGE_PCT = 0.01      # 1% de slippage en mercados normales
SLIPPAGE_THIN_BOOK = 0.03  # 3% de slippage en libros finos (order_count < 20, size > $50)
THIN_BOOK_ORDER_THRESHOLD = 20   # Umbral de órdenes para considerar libro fino
THIN_BOOK_SIZE_THRESHOLD = 50.0  # Tamaño mínimo (USD) para aplicar penalización
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
    token_id: str = ""  # CLOB token ID para inventory lock (cross_and_fill)
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
        self._equity_log: list[dict] = []  # [{timestamp, equity}]
        self._lock = asyncio.Lock()

        # ── Virtual Limit Order Book (Cross Engine) ────────────────────────
        # _pending_quotes: dict token_id → VirtualLimitOrder.
        # SOLO 1 orden activa por token. El MM sobrescribe en cada ciclo.
        # El Cross Engine evalúa estas quotes cuando el CLOB real las cruza.
        self._pending_quotes: dict[str, VirtualLimitOrder] = {}
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
        """Registra un quote de Market Making como orden límite virtual.

        ⚠️  SOLO 1 orden activa por token. Cada nuevo quote SOBRESCRIBE
        el anterior en _pending_quotes[token_id]. El Cross Engine evalúa
        estas órdenes en cross_and_fill() cuando el CLOB real las cruza.

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
        # SOBRESCRIBIR: solo 1 orden viva por mercado en el tablero
        self._pending_quotes[token_id] = order
        logger.debug(
            "CrossEngine: orden límite #%d registrada (sobrescrita) | %s | bid=%.4f ask=%.4f",
            order.id, token_id[:16], bid_price, ask_price,
        )
        return order

    async def cross_and_fill(
        self,
        token_id: str,
        real_best_bid: float,
        real_best_ask: float,
    ) -> list[VirtualPosition]:
        """Cruza el precio real del mercado contra la orden límite virtual en _pending_quotes.

        ⚠️  Evalúa ÚNICAMENTE la quote almacenada en _pending_quotes[token_id].
        SOLO 1 orden por mercado. El MM sobrescribe en cada ciclo.

        Inventory Lock: si ya existe una posición abierta para este token_id,
        borra la quote de _pending_quotes y rechaza el fill.

        Lógica de cruce (market-making pasivo):
        - Si el Best Ask real BAJA hasta cruzar nuestro Virtual Bid → Fill BID
        - Si el Best Bid real SUBE hasta cruzar nuestro Virtual Ask → Fill ASK

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

        # ── Rechazo por spread ilíquido (> 5%) ─────────────────────────────
        MAX_FILL_SPREAD = 0.05  # 5%
        real_spread = real_best_ask - real_best_bid
        if real_spread > MAX_FILL_SPREAD:
            logger.warning(
                "CrossEngine SPREAD BLOCK %s: spread=%.4f (%.1f%%) > %.0f%% — fill rechazado (mercado ilíquido)",
                token_id[:16], real_spread, real_spread * 100, MAX_FILL_SPREAD * 100,
            )
            return filled_positions

        # ── INVENTORY LOCK: verificar que no haya conflicto de slots ──
        # INDEPENDENT SLOTS (Paper Trading Test): 2 posiciones por token
        #   - Slot MM: exclusivo para strategy="market_making"
        #   - Slot Direccional: para estrategias direccionales (momentum_follow, contrarian, etc.)
        # Si ambos slots están ocupados, bloquear. Si solo uno está ocupado,
        # permitir la operación en el otro slot.
        open_positions = [p for p in self._positions if p.closed_at is None]
        token_positions = [p for p in open_positions if p.token_id == token_id]

        is_mm_fill = order.strategy == "market_making"
        mm_positions = [p for p in token_positions if p.strategy == "market_making"]
        dir_positions = [p for p in token_positions if p.strategy != "market_making"]

        if is_mm_fill and len(mm_positions) >= 1:
            # Slot MM ya ocupado → bloquear
            removed = self._pending_quotes.pop(token_id, None)
            if removed:
                logger.warning(
                    "CrossEngine INV BLOCK [MM Slot] %s: slot MM ocupado [%s] — quote #%d rechazada",
                    token_id[:16],
                    ", ".join(p.side for p in mm_positions),
                    removed.id,
                )
            return filled_positions

        if not is_mm_fill and len(dir_positions) >= 1:
            # Slot Direccional ya ocupado → bloquear
            removed = self._pending_quotes.pop(token_id, None)
            if removed:
                logger.warning(
                    "CrossEngine INV BLOCK [Dir Slot] %s: slot Direccional ocupado [%s] — quote #%d rechazada",
                    token_id[:16],
                    ", ".join(p.side for p in dir_positions),
                    removed.id,
                )
            return filled_positions

        # Slot libre (o el otro slot está libre) → proceder
        if token_positions:
            logger.debug(
                "CrossEngine INDEPENDENT SLOT %s: otro slot ocupado pero este libre | "
                "mm_slot=%d dir_slot=%d | continuando con %s",
                token_id[:16], len(mm_positions), len(dir_positions),
                "MM" if is_mm_fill else "Direccional",
            )

        # ── Recuperar la ÚNICA quote pendiente para este token ────────────
        now = time.time()
        order = self._pending_quotes.get(token_id)
        if order is None:
            return filled_positions

        # TTL check
        if (now - order.created_at) > ORDER_TTL_SECONDS:
            self._pending_quotes.pop(token_id, None)
            logger.debug("CrossEngine TTL expired %s: quote #%d eliminada", token_id[:16], order.id)
            return filled_positions

        orders_to_fill: list[tuple[VirtualLimitOrder, str, float, float]] = []

        # ── Fill BID: real_best_ask <= our_bid_price ─────────────────
        if real_best_ask <= order.bid_price and order.bid_size >= 50:
            # Sanity Check: rechazar si el ask virtual ≤ real best bid (spread cruzado por lag)
            if order.ask_price <= real_best_bid:
                logger.warning(
                    "CrossEngine SANITY FAIL %s: virtual_ask=%.4f <= real_bb=%.4f "
                    "— spread cruzado por lag, fill BID rechazado",
                    order.token_id[:16], order.ask_price, real_best_bid,
                )
            else:
                orders_to_fill.append((order, "YES", order.bid_price, order.bid_size))

        # ── Fill ASK: real_best_bid >= our_ask_price ─────────────────
        elif real_best_bid >= order.ask_price and order.ask_size >= 50:
            # Sanity Check: rechazar si el bid virtual ≥ real best ask
            if order.bid_price >= real_best_ask:
                logger.warning(
                    "CrossEngine SANITY FAIL %s: virtual_bid=%.4f >= real_ba=%.4f "
                    "— spread cruzado por lag, fill ASK rechazado",
                    order.token_id[:16], order.bid_price, real_best_ask,
                )
            else:
                orders_to_fill.append((order, "NO", order.ask_price, order.ask_size))

        # ── Ejecutar fills ────────────────────────────────────────────────
        for order, side, fill_price, fill_size in orders_to_fill:
            # Eliminar la quote de _pending_quotes (ejecutada)
            self._pending_quotes.pop(order.token_id, None)

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
                token_id=order.token_id,
                tau_pct=random.uniform(10, 55),
                toxicity=random.uniform(0.03, 0.20),
            )

            if pos:
                filled_positions.append(pos)
                trading_log.cross_fill(
                    token_id=token_id,
                    side=side,
                    price=fill_price,
                    size=size,
                    strategy=order.strategy,
                )
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
            for o in self._pending_quotes.values()
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
        token_id: str = "",
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
                token_id=token_id,
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
        order_count: int = 999,
    ) -> dict | None:
        """Cierra una posición virtual y calcula P&L.

        Aplica slippage realista cruzando contra liquidez L2.
        Si el libro es fino (order_count < THIN_BOOK_ORDER_THRESHOLD) y el
        tamaño supera THIN_BOOK_SIZE_THRESHOLD, se aplica la penalización de
        market impact ampliada (SLIPPAGE_THIN_BOOK = 3%).

        Las comisiones se cobran en POL (gas simulado).

        Parameters
        ----------
        order_count : int
            Número de órdenes en el libro real al momento del cierre.
            Permite al motor aplicar penalización por market impact en
            mercados con poca liquidez.

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

            # ── Selección de slippage según profundidad del libro ─────────
            thin_book = (
                order_count < THIN_BOOK_ORDER_THRESHOLD
                and pos.size > THIN_BOOK_SIZE_THRESHOLD
            )
            effective_slippage = SLIPPAGE_THIN_BOOK if thin_book else SLIPPAGE_PCT
            if thin_book:
                logger.warning(
                    "MTM MARKET IMPACT #%d [%s] side=%s size=$%.0f order_count=%d "
                    "→ slippage escalado %.0f%% (libro fino)",
                    pos.id, pos.market[:40], pos.side, pos.size,
                    order_count, effective_slippage * 100,
                )

            # ── Aplicar slippage realista ──────────────────────────────────
            # Al cerrar YES (venta), recibimos un precio peor (menor).
            # Al cerrar NO (venta de nuestra posición NO = compra de YES),
            # el coste es mayor (precio efectivo de cierre más alto).
            if apply_slippage:
                if pos.side == "YES":
                    price = price * (1.0 - effective_slippage)   # vendemos más barato
                else:
                    # Para NO: el cierre penaliza en la dirección contraria.
                    # Un precio de mark_NO más bajo es más costoso.
                    price = price * (1.0 - effective_slippage)   # mark_NO baja → peor cierre
                price = round(max(0.001, min(0.999, price)), 6)

            # ── Calcular P&L ──────────────────────────────────────────────
            # YES: pnl = (precio_cierre - precio_entrada) * size
            # NO:  pnl = (mark_NO_cierre - entry_NO) * size
            #      donde entry_NO = 1 - ask_YES_apertura
            #      y    mark_NO   = 1 - ask_YES_actual  (ya almacenado en pos.mark)
            if pos.side == "YES":
                pnl = (price - pos.entry) * pos.size
            else:  # NO — ambos precios ya están expresados en términos NO
                pnl = (price - pos.entry) * pos.size

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

            # Registrar en historial (ledger enriquecido)
            commission_usd = POL_COMMISSION * POL_PRICE_USD
            slippage_usd = round(abs(price - (close_price or pos.mark)) * pos.size, 4) if apply_slippage else 0.0
            trade = {
                "id": pos.id,
                "token_id": pos.market,  # market name also serves as reference
                "strategy": pos.strategy,
                "market": pos.market,
                "side": pos.side,
                "size": pos.size,
                "entry": pos.entry,
                "exit": round(price, 6),
                "slippage_pct": effective_slippage * 100 if apply_slippage else 0.0,
                "slippage_usd": round(slippage_usd, 4),
                "commission_usd": round(commission_usd, 4),
                "pnl": pos.pnl,
                "pnl_pct": pos.pnl_pct,
                "reason": reason,
                "opened_at": pos.opened_at,
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

            trading_log.position_closed(
                pos_id=pos.id,
                strategy=pos.strategy,
                market=pos.market,
                pnl=pnl,
                pnl_pct=pos.pnl_pct / 100.0,
                reason=reason,
                exit_price=round(price, 6),
            )

            logger.info(
                "PaperTrade CLOSE #%d [%s] %s P&L=$%.2f (%.1f%%) slippage=%.1f%% reason=%s",
                pos.id, pos.strategy, pos.market[:40],
                pnl, pos.pnl_pct, effective_slippage * 100, reason,
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
            # ── Trading log ──
            alloc_dicts = [
                {"strategy": alloc.strategy, "fraction": alloc.fraction}
                for alloc in allocations[:4]
            ]
            trading_log.bandit_update(alloc_dicts, equity)
        except Exception as e:
            logger.debug("Error logging bandit: %s", e)

    # ── Mark-to-Market ───────────────────────────────────────────────────────────────────

    async def mark_to_market(
        self,
        price_source: dict[str, float] | None = None,
        yes_ask_source: dict[str, float] | None = None,
    ) -> None:
        """Actualiza el mark price de todas las posiciones abiertas.

        Fórmula correcta por tipo de posición
        --------------------------------------
        YES  →  mark = best_bid_YES   (cuánto recibimos al vender)
        NO   →  mark = 1 - best_ask_YES  (precio inverso: el coste de cerrar)

        Si ``yes_ask_source`` está disponible se usa para calcular el mark de
        posiciones NO con la matemática correcta.  Si no, ``price_source``
        se interpreta como mid-price YES y se aplica la misma inversión.

        Parameters
        ----------
        price_source : dict[str, float] | None
            Diccionario {market: best_bid_YES}.  Si es None, simula movimiento.
        yes_ask_source : dict[str, float] | None
            Diccionario {market: best_ask_YES} para el mark de posiciones NO.
        """
        async with self._lock:
            for pos in self._positions:
                if pos.closed_at is not None:
                    continue

                prev_mark = pos.mark

                if price_source and pos.market in price_source:
                    best_bid_yes = price_source[pos.market]

                    if pos.side == "YES":
                        # ── YES: vendemos al mejor bid YES ───────────────────
                        pos.mark = best_bid_yes
                    else:
                        # ── NO: el valor de mercado de nuestra posición NO ───
                        # Compramos NO a (1 - best_ask_YES) y el mark debe
                        # reflejar cuánto valdría cerrarla ahora.
                        # mark_NO = 1 - best_ask_YES
                        # Si best_ask_YES no está disponible, usamos best_bid_YES
                        # (más conservador) como proxy del ask.
                        best_ask_yes = (
                            yes_ask_source.get(pos.market, best_bid_yes)
                            if yes_ask_source
                            else best_bid_yes
                        )
                        pos.mark = max(0.001, min(0.999, 1.0 - best_ask_yes))

                    logger.debug(
                        "MTM #%d [%s] side=%s entry=%.4f mark=%.4f→%.4f "
                        "best_bid_yes=%.4f best_ask_yes=%s",
                        pos.id, pos.market[:35], pos.side,
                        pos.entry, prev_mark, pos.mark,
                        best_bid_yes,
                        f"{yes_ask_source.get(pos.market, 'N/A') if yes_ask_source else 'N/A'}",
                    )
                else:
                    # Simulación: movimiento browniano ligero
                    drift = 0.0
                    vol = 0.002  # 0.2% volatilidad por tick
                    dt = 1.0
                    shock = random.gauss(drift * dt, vol * (dt ** 0.5))
                    if pos.side == "YES":
                        pos.mark = max(0.01, min(0.99, pos.mark * (1 + shock)))
                    else:
                        # Invertir el shock para NO: si el YES sube, el NO baja
                        pos.mark = max(0.01, min(0.99, pos.mark * (1 - shock)))

                # ── Recalcular P&L no realizado ───────────────────────────────
                if pos.side == "YES":
                    # Long YES: ganamos si el precio sube
                    pos.pnl = round((pos.mark - pos.entry) * pos.size, 2)
                else:
                    # Long NO: compramos NO a entry_NO = (1 - ask_YES_en_apertura)
                    # Ganamos si el mark_NO actual (1 - ask_YES_ahora) > entry_NO
                    # pnl = (mark_NO - entry_NO) * size
                    pos.pnl = round((pos.mark - pos.entry) * pos.size, 2)

                pos.pnl_pct = round((pos.pnl / pos.size) * 100, 2) if pos.size > 0 else 0.0

                logger.debug(
                    "MTM P&L #%d [%s] side=%s mark=%.4f entry=%.4f "
                    "pnl=$%.2f (%.1f%%) → tp_threshold=%.1f%%",
                    pos.id, pos.market[:35], pos.side,
                    pos.mark, pos.entry, pos.pnl, pos.pnl_pct,
                    TP_PCT * 100,
                )

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
        """Retorna historial de trades cerrados (orden cronológico)."""
        if strategy:
            return [t for t in self._trade_history if t["strategy"] == strategy]
        return list(self._trade_history)

    def get_closed_trades(self, limit: int = 200) -> list[dict]:
        """Retorna el Trade Ledger completo, ordenado del más reciente al más antiguo.

        Incluye token_id, estrategia, precios de entrada/salida, P&L, comisiones,
        slippage y motivo de cierre (SL, TP, Time-Decay, expired, manual).
        """
        trades = sorted(
            self._trade_history,
            key=lambda t: t.get("closed_at") or 0,
            reverse=True,
        )
        return trades[:limit]

    def record_equity_snapshot(self) -> None:
        """Guarda una foto del equity total (capital libre + collateral + P&L latente).

        Llamar desde un daemon externo cada N segundos. Los datos se usan
        para la gráfica de equity en el dashboard.
        """
        equity = self.wallet.usdc_total + self.unrealized_pnl
        self._equity_log.append({
            "timestamp": time.time(),
            "equity": round(equity, 2),
        })
        # Mantener sólo las últimas 24h (≈288 muestras a 5 min)
        if len(self._equity_log) > 300:
            self._equity_log = self._equity_log[-300:]

    def get_equity_history(self) -> list[dict]:
        """Retorna el historial de equity en orden cronológico."""
        return list(self._equity_log)

    @property
    def open_position_count(self) -> int:
        return sum(1 for p in self._positions if p.closed_at is None)

    @property
    def total_pnl(self) -> float:
        return sum(p.pnl for p in self._positions if p.closed_at is not None)

    @property
    def unrealized_pnl(self) -> float:
        return sum(p.pnl for p in self._positions if p.closed_at is None)

    # ── Nuclear Reset ───────────────────────────────────────────────────

    def reset_all_positions(self) -> dict:
        """Resetea TODAS las posiciones, historial, equity log y pending quotes.

        Usar tras detectar contaminación por trades falsos o bugs de inventario.
        La billetera vuelve a su estado inicial (USDC + POL).

        Returns
        -------
        dict
            Resumen de lo eliminado: {positions_cleared, trades_cleared,
            equity_snapshots_cleared, pending_quotes_cleared}
        """
        result = {
            "positions_cleared": len(self._positions),
            "trades_cleared": len(self._trade_history),
            "equity_snapshots_cleared": len(self._equity_log),
            "pending_quotes_cleared": len(self._pending_quotes),
        }
        self._positions.clear()
        self._trade_history.clear()
        self._equity_log.clear()
        self._pending_quotes.clear()
        self._position_counter = 0
        self._order_counter = 0
        # Resetear billetera a valores iniciales
        self.wallet.usdc_free = DEFAULT_INITIAL_USDC
        self.wallet.usdc_collateral = 0.0
        self.wallet.pol_balance = DEFAULT_INITIAL_POL
        logger.warning(
            "🔴 PAPER TRADING NUCLEAR RESET: %d posiciones, %d trades, "
            "%d equity snapshots, %d pending quotes eliminados. "
            "Billetera resetada a $%.0f USDC + %.0f POL.",
            result["positions_cleared"], result["trades_cleared"],
            result["equity_snapshots_cleared"], result["pending_quotes_cleared"],
            DEFAULT_INITIAL_USDC, DEFAULT_INITIAL_POL,
        )
        return result
