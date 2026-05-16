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
MM_SL_PCT = 0.03       # Micro-Stop Loss para Market Making: -3% máximo
TAU_LIQUIDATION = 0.95  # Liquidación forzosa si tau > 95%
MAX_POSITION_AGE_H = 72  # Cierre forzoso tras 72h

# ── Active Exit Logic (Mainnet Prep) ─────────────────────────────────────────
# Time-Decay Stale Trade Guard
DIRECTIONAL_TTL_HOURS = 4.0       # TTL para estrategias direccionales
DIRECTIONAL_TP_TARGET = TP_PCT    # Objetivo de TP para direccionales (15%)
STALE_TRADE_PNL_THRESHOLD = 0.50  # Si PnL < 50% del objetivo TP → stale trade

# Trailing Stop (asegurar ganancias reales)
TRAILING_ACTIVATION_PCT = 3.0     # Activar trailing cuando unrealized PnL >= +3%
TRAILING_DISTANCE_PCT = 1.5       # Distancia del trailing stop (1.5%)

# Momentum Reversal Exit
MOMENTUM_DIRECTIONAL_STRATEGIES = frozenset({
    "momentum_follow", "momentum",
})
MOMENTUM_REVERSAL_THRESHOLD = 0.005  # 0.5% — cruce de línea cero

# Realismo del simulador
SLIPPAGE_PCT = 0.01      # 1% de slippage en mercados normales
SLIPPAGE_THIN_BOOK = 0.03  # 3% de slippage en libros finos (order_count < 20, size > $50)
THIN_BOOK_ORDER_THRESHOLD = 20   # Umbral de órdenes para considerar libro fino
THIN_BOOK_SIZE_THRESHOLD = 50.0  # Tamaño mínimo (USD) para aplicar penalización
POL_COMMISSION = 0.02    # 0.02 POL por trade cerrado (gas simulado)

# ── Reglas de Ejecución Institucional (Anti-Espejismos) ─────────────────────
GHOST_LIQUIDITY_MIN_SIZE = 25.0   # Rule 2: ignorar niveles < $25 para MTM
PRICE_FLOOR = 0.02                 # Rule 5: precio mínimo permitido
PRICE_CEILING = 0.98               # Rule 5: precio máximo permitido
TP_LIMIT_ORDER_TTL = 300           # Rule 4: TTL de órdenes límite de TP (5 min)
MAX_BOOK_SWEEP_LEVELS = 20         # Rule 1: niveles máximos a barrer del L2


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
    # ── Active Exit Logic: Trailing Stop ───────────────────────────────
    trailing_activated: bool = False   # Se activa cuando PnL >= +3%
    trailing_peak_pnl_pct: float = 0.0  # Pico máximo de PnL% alcanzado


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


@dataclass
class TPLimitOrder:
    """Orden límite de Take Profit (Maker-style, Rule 4).

    Se coloca cuando el precio alcanza el target de TP.
    Solo se ejecuta si el mercado la cruza con volumen real.
    """
    id: int
    position_id: int       # ID de la posición asociada
    token_id: str
    market: str
    strategy: str
    side: str              # "YES" | "NO"
    target_price: float    # Precio al que queremos cerrar (nivel límite)
    size: float            # Tamaño a cerrar
    entry: float           # Precio de entrada original
    created_at: float = field(default_factory=time.time)


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

        # ── TP Limit Orders (Maker-style, Rule 4) ──────────────────────────
        # _tp_limit_orders: dict position_id → TPLimitOrder.
        # Órdenes límite colocadas cuando el precio alcanza el target de TP.
        # Solo se ejecutan si el mercado las cruza con volumen real.
        self._tp_limit_orders: dict[int, TPLimitOrder] = {}
        self._tp_order_counter = 0

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

    # ── Reglas de Ejecución Institucional ─────────────────────────────────────

    @staticmethod
    def _check_price_sanity(price: float) -> bool:
        """Rule 5: Hard Cap de Resolución.

        Rechaza ejecuciones con precios extremos que solo ocurren
        a milisegundos de la resolución oficial del mercado.
        En dinero real, nadie regala céntimos gratis.

        Returns True si el precio es VÁLIDO (dentro de [0.02, 0.98]).
        """
        return PRICE_FLOOR <= price <= PRICE_CEILING

    @staticmethod
    def _sweep_book_vwap(
        levels: "np.ndarray",
        count: int,
        required_size: float,
        side: str,
    ) -> tuple[float, float, float]:
        """Rule 1: Depth-Aware Fill — barre el libro calculando VWAP.

        NO asume liquidez infinita en el BBO. Itera por los niveles del
        L2 hasta sumar el size completo de la orden.

        Parameters
        ----------
        levels : np.ndarray
            Array de shape (N, 2) con niveles [[price, size], ...].
            bids deben estar ordenados DESC (mejor primero).
            asks deben estar ordenados ASC (mejor primero).
        count : int
            Número de niveles activos en el array.
        required_size : float
            Tamaño total a ejecutar en USD.
        side : str
            "BUY" (consumir asks) o "SELL" (consumir bids).

        Returns
        -------
        tuple[float, float, float]
            (vwap_price, filled_size, remaining_size)
            - vwap_price: precio medio ponderado por volumen de los niveles consumidos
            - filled_size: cuánto se pudo ejecutar
            - remaining_size: cuánto quedó sin ejecutar (> 0 = partial fill)
        """
        if count == 0 or required_size <= 0:
            return 0.0, 0.0, required_size

        total_cost = 0.0
        total_size = 0.0
        remaining = required_size

        for i in range(min(count, MAX_BOOK_SWEEP_LEVELS)):
            level_price = float(levels[i, 0])
            level_size = float(levels[i, 1])

            if level_price <= 0 or level_size <= 0:
                continue

            # Rule 5: sanity check en cada nivel
            if not PaperTradingEngine._check_price_sanity(level_price):
                continue

            take = min(remaining, level_size)
            total_cost += take * level_price
            total_size += take
            remaining -= take

            if remaining <= 0.001:  # tolerancia de redondeo
                break

        vwap = total_cost / total_size if total_size > 0 else 0.0
        return round(vwap, 6), round(total_size, 2), round(remaining, 2)

    @staticmethod
    def _filter_ghost_liquidity(
        levels: "np.ndarray",
        count: int,
        min_size: float = GHOST_LIQUIDITY_MIN_SIZE,
    ) -> int:
        """Rule 2: Ghost Liquidity Filter — ignora niveles con size < $25.

        Si un market maker retira sus órdenes y queda una orden residual
        de $1 a precio 0.99, ese nivel NO debe usarse para MTM ni TP.

        Retorna el índice del primer nivel que cumple el filtro,
        o -1 si ningún nivel lo cumple.
        """
        for i in range(count):
            if float(levels[i, 1]) >= min_size:
                return i
        return -1

    async def cross_and_fill_depth_aware(
        self,
        token_id: str,
        book_snap: "BookSnapshot",
    ) -> list["VirtualPosition"]:
        """Rule 1: Depth-Aware Cross Engine — barre el L2 con VWAP.

        Versión institucional de cross_and_fill(). En lugar de asumir que
        el top-of-book (BBO) puede absorber toda la orden, itera por los
        niveles de profundidad del orderbook hasta sumar el size completo.

        Si el libro no tiene liquidez suficiente → Partial Fill.
        El precio de ejecución es el VWAP de todos los niveles consumidos.

        Parameters
        ----------
        token_id : str
            ID del token cuyo book acaba de actualizarse.
        book_snap : BookSnapshot
            Snapshot completo del L2 desde BookAnalyzer.

        Returns
        -------
        list[VirtualPosition]
            Posiciones abiertas por fills ejecutados.
        """
        filled_positions: list[VirtualPosition] = []

        if book_snap is None or book_snap.bid_count == 0 or book_snap.ask_count == 0:
            return filled_positions

        real_best_bid = float(book_snap.bids[0, 0])
        real_best_ask = float(book_snap.asks[0, 0])

        # Rule 5: sanity check en best bid/ask
        if not self._check_price_sanity(real_best_bid) or not self._check_price_sanity(real_best_ask):
            logger.warning(
                "CrossEngine PRICE SANITY FAIL %s: bb=%.4f ba=%.4f — fuera de [%.2f, %.2f]",
                token_id[:16], real_best_bid, real_best_ask, PRICE_FLOOR, PRICE_CEILING,
            )
            return filled_positions

        if real_best_bid <= 0 or real_best_ask <= 0:
            return filled_positions

        # ── Rechazo por spread ilíquido (> 5%) ─────────────────────────────
        MAX_FILL_SPREAD = 0.05
        real_spread = real_best_ask - real_best_bid
        if real_spread > MAX_FILL_SPREAD:
            logger.warning(
                "CrossEngine SPREAD BLOCK %s: spread=%.4f (%.1f%%) > %.0f%% — fill rechazado",
                token_id[:16], real_spread, real_spread * 100, MAX_FILL_SPREAD * 100,
            )
            return filled_positions

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

        # ── INVENTORY LOCK: verificar slots ──
        open_positions = [p for p in self._positions if p.closed_at is None]
        token_positions = [p for p in open_positions if p.token_id == token_id]

        is_mm_fill = order.strategy == "market_making"
        mm_positions = [p for p in token_positions if p.strategy == "market_making"]
        dir_positions = [p for p in token_positions if p.strategy != "market_making"]

        if is_mm_fill and len(mm_positions) >= 1:
            removed = self._pending_quotes.pop(token_id, None)
            if removed:
                logger.warning(
                    "CrossEngine INV BLOCK [MM Slot] %s: slot MM ocupado — quote #%d rechazada",
                    token_id[:16], removed.id,
                )
            return filled_positions

        if not is_mm_fill and len(dir_positions) >= 1:
            removed = self._pending_quotes.pop(token_id, None)
            if removed:
                logger.warning(
                    "CrossEngine INV BLOCK [Dir Slot] %s: slot Direccional ocupado — quote #%d rechazada",
                    token_id[:16], removed.id,
                )
            return filled_positions

        # ── Depth-Aware Fill: barrer el libro con VWAP ───────────────
        side: str | None = None
        required_size: float = 0.0
        book_side = None
        book_side_count: int = 0

        # Fill BID: necesitamos consumir ASKs (comprar YES)
        if real_best_ask <= order.bid_price and order.bid_size >= 50:
            if order.ask_price <= real_best_bid:
                logger.warning(
                    "CrossEngine SANITY FAIL %s: virtual_ask=%.4f <= real_bb=%.4f — fill BID rechazado",
                    token_id[:16], order.ask_price, real_best_bid,
                )
            else:
                side = "YES"
                required_size = order.bid_size
                book_side = book_snap.asks  # consumir asks para comprar YES
                book_side_count = book_snap.ask_count

        # Fill ASK: necesitamos consumir BIDs (vender NO)
        elif real_best_bid >= order.ask_price and order.ask_size >= 50:
            if order.bid_price >= real_best_ask:
                logger.warning(
                    "CrossEngine SANITY FAIL %s: virtual_bid=%.4f >= real_ba=%.4f — fill ASK rechazado",
                    token_id[:16], order.bid_price, real_best_ask,
                )
            else:
                side = "NO"
                required_size = order.ask_size
                book_side = book_snap.bids  # consumir bids para vender NO
                book_side_count = book_snap.bid_count

        if side is None or book_side is None:
            return filled_positions

        # ── Barrer el libro (VWAP Sweep) ──────────────────────────
        vwap_price, filled_size, remaining = self._sweep_book_vwap(
            book_side, book_side_count, required_size, side,
        )

        if filled_size <= 0 or vwap_price <= 0:
            logger.warning(
                "CrossEngine NO LIQUIDITY %s: side=%s required=$%.0f filled=$%.0f — partial fill",
                token_id[:16], side, required_size, filled_size,
            )
            return filled_positions

        # Rule 5: sanity check en el precio VWAP resultante
        if not self._check_price_sanity(vwap_price):
            logger.warning(
                "CrossEngine PRICE SANITY FAIL %s: vwap=%.4f fuera de [%.2f, %.2f] — fill rechazado",
                token_id[:16], vwap_price, PRICE_FLOOR, PRICE_CEILING,
            )
            return filled_positions

        # ── Ejecutar fill ─────────────────────────────────────────
        self._pending_quotes.pop(order.token_id, None)

        size = min(filled_size, self.wallet.usdc_free)
        if size < 50:
            logger.debug("CrossEngine: fill omitido (capital libre=$%.2f)", self.wallet.usdc_free)
            return filled_positions

        pos = await self.open_position(
            strategy=order.strategy,
            market=order.market,
            side=side,
            size=size,
            entry=vwap_price,
            token_id=order.token_id,
            tau_pct=random.uniform(10, 55),
            toxicity=random.uniform(0.03, 0.20),
        )

        if pos:
            if remaining > 1.0:
                logger.warning(
                    "PARTIAL FILL %s: side=%s vwap=%.4f filled=$%.0f remaining=$%.0f — posición abierta parcialmente",
                    token_id[:16], side, vwap_price, filled_size, remaining,
                )
            filled_positions.append(pos)
            trading_log.cross_fill(
                token_id=token_id,
                side=side,
                price=vwap_price,
                size=size,
                strategy=order.strategy,
            )
            logger.info(
                "PAPER TRADE EXECUTED [Depth-Aware] | Side: %s | VWAP: %.4f | Filled: $%.0f | "
                "Remaining: $%.0f | Market: %s | token: %s | Levels swept: %d",
                side, vwap_price, filled_size, remaining,
                order.market[:55], token_id[:16],
                min(book_side_count, MAX_BOOK_SWEEP_LEVELS),
            )

        return filled_positions

    # ── Rule 4: Realistic Take Profit (Maker Limit Orders) ────────────────

    def register_tp_limit_order(
        self,
        position_id: int,
        token_id: str,
        market: str,
        strategy: str,
        side: str,
        target_price: float,
        size: float,
        entry: float,
    ) -> "TPLimitOrder | None":
        """Rule 4: Registra una orden límite de Take Profit (Maker-style).

        En lugar de ejecutar un Market Sweep que arruina la rentabilidad,
        coloca una orden Limit que solo se considera filled cuando el
        precio cruzado en el CLOB la atraviesa con volumen real.

        Returns None si ya existe una orden de TP para esta posición.
        """
        if position_id in self._tp_limit_orders:
            return None  # ya existe

        self._tp_order_counter += 1
        tp_order = TPLimitOrder(
            id=self._tp_order_counter,
            position_id=position_id,
            token_id=token_id,
            market=market,
            strategy=strategy,
            side=side,
            target_price=target_price,
            size=size,
            entry=entry,
        )
        self._tp_limit_orders[position_id] = tp_order
        logger.info(
            "TP LIMIT ORDER #%d | pos=%d side=%s target=%.4f size=$%.0f | %s",
            tp_order.id, position_id, side, target_price, size, market[:50],
        )
        return tp_order

    async def check_tp_cross(
        self,
        book_snap: "BookSnapshot",
        token_id: str,
    ) -> list[dict]:
        """Rule 4: Verifica si alguna orden TP límite ha sido cruzada por el mercado.

        Para YES: si el best_bid >= target_price → cerramos vendiendo al best_bid.
        Para NO:  si el best_ask <= target_price → cerramos comprando al best_ask.

        Solo ejecuta si el nivel que cruza supera el Ghost Liquidity Filter ($25).

        Returns list[dict] de trades cerrados.
        """
        closed_trades: list[dict] = []

        if book_snap is None or book_snap.bid_count == 0 or book_snap.ask_count == 0:
            return closed_trades

        # Filtrar ghost liquidity en best bid/ask
        bb_idx = self._filter_ghost_liquidity(book_snap.bids, book_snap.bid_count)
        ba_idx = self._filter_ghost_liquidity(book_snap.asks, book_snap.ask_count)

        valid_bb = float(book_snap.bids[bb_idx, 0]) if bb_idx >= 0 else 0.0
        valid_ba = float(book_snap.asks[ba_idx, 0]) if ba_idx >= 0 else 0.0

        now = time.time()
        to_remove: list[int] = []

        for pos_id, tp_order in list(self._tp_limit_orders.items()):
            if tp_order.token_id != token_id:
                continue

            # TTL check
            if (now - tp_order.created_at) > TP_LIMIT_ORDER_TTL:
                to_remove.append(pos_id)
                logger.debug("TP LIMIT TTL expired: pos=%d target=%.4f", pos_id, tp_order.target_price)
                continue

            crossed = False
            close_price = 0.0

            if tp_order.side == "YES":
                if valid_bb > 0 and valid_bb >= tp_order.target_price:
                    crossed = True
                    close_price = valid_bb
            else:  # NO
                if valid_ba > 0 and valid_ba <= tp_order.target_price:
                    crossed = True
                    close_price = valid_ba

            if crossed:
                if not self._check_price_sanity(close_price):
                    logger.warning(
                        "TP CROSS SANITY FAIL pos=%d: price=%.4f — rechazado",
                        pos_id, close_price,
                    )
                    to_remove.append(pos_id)
                    continue

                trade = await self.close_position(
                    position_id=pos_id,
                    close_price=close_price,
                    reason="tp_limit",
                    apply_slippage=False,  # Maker: sin slippage
                )
                if trade:
                    closed_trades.append(trade)
                    logger.info(
                        "TP LIMIT FILLED #%d | pos=%d side=%s target=%.4f filled@%.4f | %s",
                        tp_order.id, pos_id, tp_order.side,
                        tp_order.target_price, close_price,
                        tp_order.market[:50],
                    )
                to_remove.append(pos_id)

        for pos_id in to_remove:
            self._tp_limit_orders.pop(pos_id, None)

        return closed_trades

    # ── Rule 2+3: MTM Institucional con Ghost Filter y Lógica NO Asimétrica ──

    async def mark_to_market_v2(
        self,
        book_analyzer: "BookAnalyzer",
    ) -> dict[str, int]:
        """Mark-to-Market institucional usando datos L2 reales del CLOB.

        Rule 2 (Ghost Liquidity Filter):
            Ignora cualquier nivel de price cuyo size sea < $25.
            Si un market maker retira sus órdenes y queda una orden
            residual de $1 a precio 0.99, NO se usa ese 0.99 para MTM.

        Rule 3 (MTM NO Asimétrico):
            exit_value_NO = 1 - VWAP_of_real_ba(required_size).
            Si el lado del Ask está vacío o no tiene liquidez suficiente,
            el PnL no se puede calcular (None) y la posición se mantiene.

        Parameters
        ----------
        book_analyzer : BookAnalyzer
            Referencia al analizador de order books con datos CLOB L2.

        Returns
        -------
        dict[str, int]
            Estadísticas: {positions_updated, positions_skipped_no_book,
                           positions_skipped_ghost, positions_no_ask_empty}
        """
        stats = {
            "positions_updated": 0,
            "positions_skipped_no_book": 0,
            "positions_skipped_ghost": 0,
            "positions_no_ask_empty": 0,
        }

        async with self._lock:
            for pos in self._positions:
                if pos.closed_at is not None:
                    continue
                if not pos.token_id:
                    stats["positions_skipped_no_book"] += 1
                    continue

                book_snap = book_analyzer.get_book(pos.token_id)
                if book_snap is None or (book_snap.bid_count == 0 and book_snap.ask_count == 0):
                    stats["positions_skipped_no_book"] += 1
                    continue

                if pos.side == "YES":
                    # ── YES: mark = best valid bid (lo que recibimos al vender) ──
                    bid_idx = self._filter_ghost_liquidity(
                        book_snap.bids, book_snap.bid_count,
                    )
                    if bid_idx < 0:
                        stats["positions_skipped_ghost"] += 1
                        logger.debug(
                            "MTM GHOST SKIP #%d [YES] %s: no bid >= $%.0f — mark congelado",
                            pos.id, pos.token_id[:16], GHOST_LIQUIDITY_MIN_SIZE,
                        )
                        continue

                    valid_bid = float(book_snap.bids[bid_idx, 0])
                    valid_bid_size = float(book_snap.bids[bid_idx, 1])

                    if not self._check_price_sanity(valid_bid):
                        stats["positions_skipped_ghost"] += 1
                        continue

                    prev_mark = pos.mark
                    pos.mark = valid_bid
                    # ── v5.6 FIX: PnL correcto para opciones binarias ──
                    # El cálculo (mark - entry) * size asume que 'size' son
                    # unidades, pero 'size' son USD invertidos.
                    # Fórmula correcta: size * (mark/entry - 1)
                    # Ej: entry=0.50, mark=0.45, size=$150 → PnL = 150*(0.45/0.50-1) = -$15.00 (correcto)
                    #    Antes: (0.45-0.50)*150 = -$7.50 (subestimaba pérdidas 2x)
                    if pos.entry > 0:
                        pos.pnl = round(pos.size * (pos.mark / pos.entry - 1.0), 2)
                        pos.pnl_pct = round((pos.mark / pos.entry - 1.0) * 100, 2)
                    else:
                        pos.pnl = 0.0
                        pos.pnl_pct = 0.0
                    stats["positions_updated"] += 1

                    logger.debug(
                        "MTM #%d [YES] %s: mark=%.4f (was %.4f) bid_size=$%.0f ghost_filter=OK",
                        pos.id, pos.token_id[:16], pos.mark, prev_mark, valid_bid_size,
                    )

                else:  # NO — Rule 3: Lógica Asimétrica
                    # ── NO: exit_value_NO = 1 - VWAP_of_real_ba(required_size) ──
                    ask_idx = self._filter_ghost_liquidity(
                        book_snap.asks, book_snap.ask_count,
                    )
                    if ask_idx < 0:
                        stats["positions_no_ask_empty"] += 1
                        logger.warning(
                            "MTM NO ASK EMPTY #%d [NO] %s: ask side vacío o < $%.0f — "
                            "PnL NO calculable, posición mantenida",
                            pos.id, pos.token_id[:16], GHOST_LIQUIDITY_MIN_SIZE,
                        )
                        continue

                    # Barrer el ask para el tamaño de la posición (VWAP)
                    required_size = pos.size
                    vwap_ask, filled, remaining = self._sweep_book_vwap(
                        book_snap.asks, book_snap.ask_count, required_size, "BUY",
                    )

                    if filled <= 0 or vwap_ask <= 0:
                        stats["positions_no_ask_empty"] += 1
                        logger.warning(
                            "MTM NO NO LIQUIDITY #%d [NO] %s: required=$%.0f filled=$%.0f — "
                            "PnL NO calculable, posición mantenida",
                            pos.id, pos.token_id[:16], required_size, filled,
                        )
                        continue

                    # exit_value_NO = 1 - VWAP(asks)
                    exit_value_no = round(1.0 - vwap_ask, 6)

                    if not self._check_price_sanity(exit_value_no) and not self._check_price_sanity(vwap_ask):
                        stats["positions_skipped_ghost"] += 1
                        continue

                    prev_mark = pos.mark
                    pos.mark = exit_value_no
                    # ── v5.6 FIX: PnL correcto para opciones binarias (NO side) ──
                    # Misma fórmula: size * (mark/entry - 1) usando precios NO.
                    # mark = exit_value_no (1 - vwap_ask), entry = precio NO original.
                    if pos.entry > 0:
                        pos.pnl = round(pos.size * (pos.mark / pos.entry - 1.0), 2)
                        pos.pnl_pct = round((pos.mark / pos.entry - 1.0) * 100, 2)
                    else:
                        pos.pnl = 0.0
                        pos.pnl_pct = 0.0
                    stats["positions_updated"] += 1

                    logger.debug(
                        "MTM #%d [NO] %s: vwap_ask=%.4f → exit_value_no=%.4f (was %.4f) "
                        "filled=$%.0f remaining=$%.0f",
                        pos.id, pos.token_id[:16], vwap_ask, pos.mark, prev_mark,
                        filled, remaining,
                    )

                pos.liquidation_zone = pos.tau_pct > 85

                # ── Active Exit: Trailing Stop peak tracking ────────────
                if pos.pnl_pct >= TRAILING_ACTIVATION_PCT:
                    if not pos.trailing_activated:
                        pos.trailing_activated = True
                        pos.trailing_peak_pnl_pct = pos.pnl_pct
                        logger.info(
                            "TRAILING ACTIVATED #%d [%s] side=%s pnl=%.1f%% peak=%.1f%%",
                            pos.id, pos.market[:40], pos.side,
                            pos.pnl_pct, pos.trailing_peak_pnl_pct,
                        )
                    elif pos.pnl_pct > pos.trailing_peak_pnl_pct:
                        prev_peak = pos.trailing_peak_pnl_pct
                        pos.trailing_peak_pnl_pct = pos.pnl_pct
                        logger.debug(
                            "TRAILING UPDATE #%d [%s] peak %.1f%% → %.1f%%",
                            pos.id, pos.market[:40], prev_peak, pos.trailing_peak_pnl_pct,
                        )

        return stats

    # ── Auto-Close con TP Maker (Rule 4) ───────────────────────────────────

    async def evaluate_auto_close_v2(
        self,
        book_analyzer: "BookAnalyzer" = None,
        signal_pipeline=None,  # SignalPipeline para Momentum Reversal Exit
        ws_healthy: bool = True,  # MAINNET: solo ejecutar SL con WS vivo
    ) -> list[dict]:
        """Evalúa criterios de cierre automático con reglas institucionales.

        Rule 4 (Realistic Take Profit):
            Cuando el precio alcanza el objetivo de TP, NO se simula una
            orden de mercado. Se coloca una orden Limit (Maker) que solo
            se ejecutará cuando el mercado la cruce con volumen real
            (verificado en check_tp_cross).

        Active Exit Logic (Mainnet Prep):
            - Time-Decay: fuerza cierre de direccionales stale (>4h, PnL < 50% TP)
            - Trailing Stop: asegura ganancias cuando PnL >= 3% y retrocede 1.5%

        SL, tau, y expired siguen usando cierre a mercado (son urgentes).

        ws_healthy: si False, el SL NO se ejecuta (datos stale pueden causar
                    falsos positivos). Time-decay, trailing stop, tau y expired
                    sí se ejecutan (no dependen de precios en tiempo real).
        """
        closed = []
        now = time.time()
        positions = list(self._positions)
        for pos in positions:
            if pos.closed_at is not None:
                continue

            reason = None

            # ── Active Exit 1: Time-Decay Stale Trade Guard ───────────
            if self._is_directional_stale(pos, now):
                reason = "time_decay"

            # ── Active Exit 2: Trailing Stop ──────────────────────────
            elif pos.trailing_activated:
                drawdown = pos.trailing_peak_pnl_pct - pos.pnl_pct
                if drawdown >= TRAILING_DISTANCE_PCT:
                    reason = "trailing_stop"
                    logger.info(
                        "TRAILING STOP #%d [%s] side=%s pnl=%.1f%% peak=%.1f%% "
                        "drawdown=%.1f%% → force close",
                        pos.id, pos.market[:40], pos.side,
                        pos.pnl_pct, pos.trailing_peak_pnl_pct, drawdown,
                    )

            # ── Standard: TP → Maker Limit Order ──────────────────────
            if pos.pnl_pct >= TP_PCT * 100:
                # ── Rule 4: TP → Maker Limit Order, NO Market Sweep ──
                if pos.token_id and book_analyzer:
                    book_snap = book_analyzer.get_book(pos.token_id)
                    if book_snap and book_snap.bid_count > 0 and book_snap.ask_count > 0:
                        target = pos.mark
                        self.register_tp_limit_order(
                            position_id=pos.id,
                            token_id=pos.token_id,
                            market=pos.market,
                            strategy=pos.strategy,
                            side=pos.side,
                            target_price=target,
                            size=pos.size,
                            entry=pos.entry,
                        )
                        logger.info(
                            "TP MAKER #%d [%s] side=%s target=%.4f size=$%.0f → orden límite registrada",
                            pos.id, pos.strategy, pos.side, target, pos.size,
                        )
                    else:
                        reason = "tp"
                else:
                    reason = "tp"

            # ── Stop Loss: MM usa Micro-SL (-3%), resto usa SL estándar (-10%) ──
            # MAINNET: solo ejecutar SL si el WS está HEALTHY.
            # Con WS caído, los precios mark-to-market son stale y pueden
            # causar falsos positivos por wicks o datos desactualizados.
            if not reason and ws_healthy:
                if pos.strategy == "market_making":
                    if pos.pnl_pct <= -MM_SL_PCT * 100:
                        reason = "sl_mm_micro"
                elif pos.pnl_pct <= -SL_PCT * 100:
                    reason = "sl"
            elif not reason and not ws_healthy and pos.pnl_pct <= -SL_PCT * 100:
                sl_pct = MM_SL_PCT if pos.strategy == "market_making" else SL_PCT
                logger.warning(
                    "⏸️ SL BLOCKED [%s] #%d | pnl=%.1f%% (SL=%.0f%%) | WS_UNHEALTHY — "
                    "SL diferido para evitar cierre por datos stale",
                    pos.strategy, pos.id, pos.pnl_pct, sl_pct * 100,
                )

            if not reason and pos.tau_pct >= TAU_LIQUIDATION * 100:
                reason = "tau"
            if not reason and (now - pos.opened_at) > MAX_POSITION_AGE_H * 3600:
                reason = "expired"

            if reason:
                trade = await self.close_position(pos.id, reason=reason)
                if trade:
                    self._tp_limit_orders.pop(pos.id, None)
                    closed.append(trade)

        return closed

    @staticmethod
    def _is_directional_stale(pos: "VirtualPosition", now: float) -> bool:
        """Time-Decay Stale Trade Guard: ¿posición direccional caducada?"""
        if pos.strategy not in MOMENTUM_DIRECTIONAL_STRATEGIES:
            return False
        age_hours = (now - pos.opened_at) / 3600.0
        if age_hours <= DIRECTIONAL_TTL_HOURS:
            return False
        # Stale si PnL no ha alcanzado el 50% del objetivo TP
        tp_target_pct = DIRECTIONAL_TP_TARGET * 100  # 15%
        half_target = tp_target_pct * STALE_TRADE_PNL_THRESHOLD  # 7.5%
        return pos.pnl_pct < half_target

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

    def get_tp_limit_orders(self) -> list[dict]:
        """Retorna las órdenes límite de Take Profit activas (para el dashboard)."""
        now = time.time()
        return [
            {
                "id": o.id,
                "position_id": o.position_id,
                "token_id": o.token_id,
                "market": o.market,
                "strategy": o.strategy,
                "side": o.side,
                "target_price": round(o.target_price, 4),
                "size": round(o.size, 2),
                "entry": round(o.entry, 4),
                "age_s": round(now - o.created_at, 1),
            }
            for o in self._tp_limit_orders.values()
            if (now - o.created_at) <= TP_LIMIT_ORDER_TTL
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

            # ── v5.6 INVENTORY SPAM LOCK ──────────────────────────────
            # Máximo 1 posición direccional por token_id.
            # Evita que se abran múltiples posiciones sobre el mismo mercado
            # en el mismo ciclo (ej. 7 posiciones en SpaceX).
            if strategy != "market_making" and token_id:
                existing_directional = [
                    p for p in self._positions
                    if p.token_id == token_id
                    and p.closed_at is None
                    and p.strategy != "market_making"
                ]
                if existing_directional:
                    logger.warning(
                        "🚫 INVENTORY SPAM LOCK | Token=%s Strategy=%s | "
                        "Reason: %d directional positions already open on this token. "
                        "Max 1 directional slot per market. Abortando.",
                        token_id[:16], strategy,
                        len(existing_directional),
                    )
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
            # ── v5.6 FIX: PnL correcto para opciones binarias ──
            # Fórmula: size * (price/entry - 1), no (price - entry) * size
            if pos.entry > 0:
                pnl = pos.size * (price / pos.entry - 1.0)
            else:
                pnl = 0.0

            pos.pnl = round(pnl, 2)
            pos.pnl_pct = round((price / pos.entry - 1.0) * 100, 2) if (pos.size > 0 and pos.entry > 0) else 0.0
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
                # ── v5.6 FIX: PnL correcto para opciones binarias ──
                # Fórmula: size * (mark/entry - 1), no (mark - entry) * size
                if pos.entry > 0:
                    pos.pnl = round(pos.size * (pos.mark / pos.entry - 1.0), 2)
                else:
                    pos.pnl = 0.0

                pos.pnl_pct = round((pos.mark / pos.entry - 1.0) * 100, 2) if (pos.size > 0 and pos.entry > 0) else 0.0

                logger.debug(
                    "MTM P&L #%d [%s] side=%s mark=%.4f entry=%.4f "
                    "pnl=$%.2f (%.1f%%) → tp_threshold=%.1f%%",
                    pos.id, pos.market[:35], pos.side,
                    pos.mark, pos.entry, pos.pnl, pos.pnl_pct,
                    TP_PCT * 100,
                )

                # Liquidation zone si tau > 85%
                pos.liquidation_zone = pos.tau_pct > 85

                # ── Active Exit: Trailing Stop peak tracking ────────────
                if pos.pnl_pct >= TRAILING_ACTIVATION_PCT:
                    if not pos.trailing_activated:
                        pos.trailing_activated = True
                        pos.trailing_peak_pnl_pct = pos.pnl_pct
                        logger.info(
                            "TRAILING ACTIVATED #%d [%s] side=%s pnl=%.1f%% peak=%.1f%%",
                            pos.id, pos.market[:40], pos.side,
                            pos.pnl_pct, pos.trailing_peak_pnl_pct,
                        )
                    elif pos.pnl_pct > pos.trailing_peak_pnl_pct:
                        pos.trailing_peak_pnl_pct = pos.pnl_pct

    async def evaluate_auto_close(self, signal_pipeline=None, ws_healthy: bool = True) -> list[dict]:
        """Evalúa criterios de cierre automático y cierra posiciones que los cumplan.

        Active Exit Logic incluida:
        - Time-Decay Stale Trade Guard (direccionales > 4h con PnL < 50% TP)
        - Trailing Stop (PnL >= 3% + drawdown 1.5%)

        ws_healthy: si False, el SL NO se ejecuta (datos stale).

        Returns
        -------
        list[dict]
            Trades cerrados.
        """
        closed = []
        now = time.time()
        # Copiar lista para evitar modificar durante iteración
        positions = list(self._positions)
        for pos in positions:
            if pos.closed_at is not None:
                continue

            reason = None

            # ── Active Exit 1: Time-Decay Stale Trade Guard ───────────
            if self._is_directional_stale(pos, now):
                reason = "time_decay"

            # ── Active Exit 2: Trailing Stop ──────────────────────────
            elif pos.trailing_activated:
                drawdown = pos.trailing_peak_pnl_pct - pos.pnl_pct
                if drawdown >= TRAILING_DISTANCE_PCT:
                    reason = "trailing_stop"
                    logger.info(
                        "TRAILING STOP #%d [%s] side=%s pnl=%.1f%% peak=%.1f%% "
                        "drawdown=%.1f%% → force close",
                        pos.id, pos.market[:40], pos.side,
                        pos.pnl_pct, pos.trailing_peak_pnl_pct, drawdown,
                    )

            if pos.pnl_pct >= TP_PCT * 100:
                reason = "tp"
            # ── Stop Loss: MM usa Micro-SL (-3%), resto usa SL estándar (-10%) ──
            # MAINNET: solo ejecutar SL si el WS está HEALTHY.
            if not reason and ws_healthy:
                if pos.strategy == "market_making":
                    if pos.pnl_pct <= -MM_SL_PCT * 100:
                        reason = "sl_mm_micro"
                elif pos.pnl_pct <= -SL_PCT * 100:
                    reason = "sl"
            elif not reason and not ws_healthy and pos.pnl_pct <= -SL_PCT * 100:
                sl_pct = MM_SL_PCT if pos.strategy == "market_making" else SL_PCT
                logger.warning(
                    "⏸️ SL BLOCKED [%s] #%d | pnl=%.1f%% (SL=%.0f%%) | WS_UNHEALTHY — "
                    "SL diferido para evitar cierre por datos stale",
                    pos.strategy, pos.id, pos.pnl_pct, sl_pct * 100,
                )
            if not reason and pos.tau_pct >= TAU_LIQUIDATION * 100:
                reason = "tau"
            if not reason and (now - pos.opened_at) > MAX_POSITION_AGE_H * 3600:
                reason = "expired"

            if reason:
                trade = await self.close_position(pos.id, reason=reason)
                if trade:
                    closed.append(trade)

        return closed

    async def evaluate_momentum_reversal(
        self,
        momentum_by_token: dict[str, float],
    ) -> list[dict]:
        """Momentum Reversal Exit: cierra posiciones direccionales si el momentum
        cruza la línea cero en dirección opuesta.

        NO espera a que salte el Stop Loss. En cada ciclo de evaluación,
        si el indicador de momentum se invirtió fuertemente en contra de la
        posición, la cierra inmediatamente.

        Parameters
        ----------
        momentum_by_token : dict[str, float]
            Mapping {token_id: momentum_value} desde el SignalPipeline.
            momentum > 0 = presión al alza (YES subiendo).
            momentum < 0 = presión a la baja (YES bajando, NO subiendo).

        Returns
        -------
        list[dict]
            Trades cerrados por inversión de momentum.
        """
        closed = []
        now = time.time()
        for pos in list(self._positions):
            if pos.closed_at is not None:
                continue
            if pos.strategy not in MOMENTUM_DIRECTIONAL_STRATEGIES:
                continue
            if not pos.token_id:
                continue

            mom = momentum_by_token.get(pos.token_id)
            if mom is None:
                continue

            force_close = False
            reversal_desc = ""

            if pos.side == "YES":
                # Long YES: momentum negativo fuerte = señal de salida
                if mom <= -MOMENTUM_REVERSAL_THRESHOLD:
                    force_close = True
                    reversal_desc = f"mom={mom:.4f} ≤ -{MOMENTUM_REVERSAL_THRESHOLD}"
            else:  # NO
                # Long NO: momentum positivo fuerte = YES subiendo, NO bajando
                if mom >= MOMENTUM_REVERSAL_THRESHOLD:
                    force_close = True
                    reversal_desc = f"mom={mom:.4f} ≥ +{MOMENTUM_REVERSAL_THRESHOLD}"

            if force_close:
                logger.info(
                    "MOMENTUM REVERSAL #%d [%s] side=%s %s → force close",
                    pos.id, pos.market[:40], pos.side, reversal_desc,
                )
                trade = await self.close_position(pos.id, reason="momentum_reversal")
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
                # Active Exit Logic
                "trailing_activated": p.trailing_activated,
                "trailing_peak_pnl_pct": p.trailing_peak_pnl_pct,
                "close_reason": p.close_reason,
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
            "tp_limit_orders_cleared": len(self._tp_limit_orders),
        }
        self._positions.clear()
        self._trade_history.clear()
        self._equity_log.clear()
        self._pending_quotes.clear()
        self._tp_limit_orders.clear()
        self._position_counter = 0
        self._order_counter = 0
        self._tp_order_counter = 0
        # Resetear billetera a valores iniciales
        self.wallet.usdc_free = DEFAULT_INITIAL_USDC
        self.wallet.usdc_collateral = 0.0
        self.wallet.pol_balance = DEFAULT_INITIAL_POL
        logger.warning(
            "🔴 PAPER TRADING NUCLEAR RESET: %d posiciones, %d trades, "
            "%d equity snapshots, %d pending quotes, %d TP limit orders eliminados. "
            "Billetera resetada a $%.0f USDC + %.0f POL.",
            result["positions_cleared"], result["trades_cleared"],
            result["equity_snapshots_cleared"], result["pending_quotes_cleared"],
            result["tp_limit_orders_cleared"],
            DEFAULT_INITIAL_USDC, DEFAULT_INITIAL_POL,
        )
        return result
