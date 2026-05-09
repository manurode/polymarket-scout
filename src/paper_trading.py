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


# ── Tipos ─────────────────────────────────────────────────────────────────────────────

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
    ) -> dict | None:
        """Cierra una posición virtual y calcula P&L.

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

            # Gas simulado (0.01 POL por trade)
            gas_cost = 0.01
            self.wallet.pol_balance = max(0.0, self.wallet.pol_balance - gas_cost)

            # Registrar en historial
            trade = {
                "id": pos.id,
                "strategy": pos.strategy,
                "market": pos.market,
                "side": pos.side,
                "size": pos.size,
                "entry": pos.entry,
                "exit": price,
                "pnl": pos.pnl,
                "pnl_pct": pos.pnl_pct,
                "reason": reason,
                "closed_at": pos.closed_at,
            }
            self._trade_history.append(trade)

            # Feedback al PortfolioManager
            if self.pm:
                equity_before = self.wallet.usdc_total - pnl  # equity antes de aplicar P&L
                self.pm.record_trade(pos.strategy, pnl, equity_before)
                # Actualizar Sortino con el trade recién cerrado
                strategy_trades = [
                    {"pnl": t["pnl"], "amount_invested": t["size"]}
                    for t in self._trade_history
                    if t["strategy"] == pos.strategy
                ]
                self.pm.update_strategy_performance(pos.strategy, strategy_trades)
            
            # Feedback al AdaptiveStrategyEngine via callback
            if self.on_trade_close:
                try:
                    self.on_trade_close(pos.strategy, pnl)
                except Exception as e:
                    logger.error("Error en on_trade_close callback: %s", e)

            logger.info(
                "PaperTrade CLOSE #%d %s P&L=$%.2f (%.1f%%) reason=%s",
                pos.id, pos.strategy, pnl, pos.pnl_pct, reason,
            )
            return trade

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
