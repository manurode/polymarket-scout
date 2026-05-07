"""
AutoTrader — Bot autónomo de paper trading.
===========================================
Evalúa señales contra estrategias y coloca/cierra trades automáticamente.
Se ejecuta en cada ciclo de scan (cada 5 min).

Sistema de salida con 4 mecanismos simultáneos:
  1. Take-profit fijo (ej: +10% sobre lo invertido)
  2. Stop-loss fijo (ej: -20% sobre lo invertido)
  3. Trailing stop: si el trade llega a +8%, el SL sube a break-even
  4. Time-based close: cerrar tras 48h sin movimiento significativo
"""

import logging
import time as _time
from typing import Optional

from src.strategies import STRATEGIES

logger = logging.getLogger(__name__)


class AutoTrader:
    """Motor autónomo que opera sin intervención humana.

    En cada ciclo del scanner:
      1. Evalúa las señales de cada mercado contra las estrategias
      2. Si una estrategia da señal de compra → coloca un paper trade
      3. Al final del ciclo, revisa posiciones abiertas con lógica de salida:
         - Take-profit fijo
         - Stop-loss fijo
         - Trailing stop (sube el SL cuando hay ganancia)
         - Time-based close (cierra tras max_hold_hours)

    Parameters
    ----------
    tracker : Tracker
        Instancia con ``init_paper_trading()`` ya ejecutado.
    paper_trader : PaperTrader
        Instancia con balance inicial y posición size configurados.
    signal_config : dict
        Configuración de señales.
    auto_config : dict
        Configuración del auto-trader (ver config.yaml).
    """

    def __init__(self, tracker, paper_trader, signal_config=None,
                 auto_config=None):
        self.tracker = tracker
        self.paper_trader = paper_trader
        self.signal_config = signal_config or {}
        self.auto_config = auto_config or {}

        # ── Entry config ──
        self.max_open = self.auto_config.get("max_open_positions", 10)
        self.min_score = self.auto_config.get("min_score", 30)
        self.enabled_strategies = self.auto_config.get(
            "enabled_strategies",
            list(STRATEGIES.keys()),
        )

        # ── Exit config (sistema de salida con 4 mecanismos) ──
        exit_cfg = self.auto_config.get("exit_strategy", {})
        self.take_profit_pct = exit_cfg.get("take_profit_pct", 0.10)
        self.stop_loss_pct = exit_cfg.get("stop_loss_pct", 0.20)
        self.trailing_activate_pct = exit_cfg.get("trailing_activate_pct", 0.08)
        self.trailing_sl_pct = exit_cfg.get("trailing_sl_pct", 0.0)
        self.max_hold_hours = exit_cfg.get("max_hold_hours", 48)

        # Contadores del ciclo actual
        self.trades_placed_this_cycle: int = 0
        self.trades_closed_this_cycle: int = 0

    # ------------------------------------------------------------------
    # Entry: evaluar señales → decidir si apostar
    # ------------------------------------------------------------------

    def evaluate_and_trade(
        self,
        signals: list[dict],
        snapshot: dict,
        score: float,
    ) -> Optional[dict]:
        """Evalúa las señales de UN mercado y coloca trade si procede.

        Returns el trade dict si se colocó, None en caso contrario.
        """
        condition_id = snapshot.get("condition_id", "")

        # ¿Ya tenemos una posición abierta en este mercado?
        open_positions = self.tracker.get_open_positions()
        already_open = any(
            p["condition_id"] == condition_id for p in open_positions
        )
        if already_open:
            return None

        # ¿Límite de posiciones abiertas?
        if len(open_positions) >= self.max_open:
            logger.debug(
                "Max open positions reached (%d), skipping %s",
                self.max_open,
                snapshot.get("question", "?")[:50],
            )
            return None

        # ¿Score mínimo?
        if score < self.min_score:
            return None

        # Evaluar cada estrategia habilitada
        for strategy_name in self.enabled_strategies:
            fn = STRATEGIES.get(strategy_name)
            if fn is None:
                continue

            decision = fn(signals, snapshot)
            if decision is None:
                continue

            action = decision.get("action")
            if action not in ("YES", "NO"):
                continue

            # Precio según el lado
            price = snapshot.get(
                "price_yes" if action == "YES" else "price_no"
            )
            if price is None or price <= 0:
                continue

            # ¡Colocar trade!
            try:
                trade = self.paper_trader.place_bet(
                    condition_id=condition_id,
                    question=snapshot.get("question"),
                    side=action,
                    price=price,
                    signal_type=decision.get("reason", ""),
                    score=int(score),
                    strategy=strategy_name,
                    timestamp=snapshot.get("timestamp"),
                )
                self.trades_placed_this_cycle += 1
                logger.info(
                    "🤖 AUTO-TRADE #%d: %s %s @ %.1f%% ($%.2f) | %s | %s",
                    trade["id"],
                    action,
                    snapshot.get("question", "?")[:40],
                    price * 100,
                    trade["amount"],
                    strategy_name,
                    decision.get("reason", ""),
                )
                return trade
            except ValueError as exc:
                logger.warning("Auto-trade skipped: %s", exc)
                return None

        return None

    # ------------------------------------------------------------------
    # Exit: sistema de salida con 4 mecanismos
    # ------------------------------------------------------------------

    def check_close_conditions(self) -> list[dict]:
        """Revisa todas las posiciones abiertas y las cierra según:

        1. Take-profit fijo: si P&L >= +take_profit_pct
        2. Stop-loss fijo: si P&L <= -stop_loss_pct
        3. Trailing stop: si P&L >= trailing_activate_pct,
           el stop-loss sube a trailing_sl_pct (ej: break-even = 0%)
        4. Time-based: si lleva abierto más de max_hold_hours
           y el P&L está entre -5% y +5% (sin movimiento real)
        5. Market resolved: precio tocó 0.0 o 1.0

        Returns lista de trades cerrados en este ciclo.
        """
        closed_trades: list[dict] = []
        open_positions = self.tracker.get_open_positions()
        now = int(_time.time())

        for pos in open_positions:
            condition_id = pos["condition_id"]

            # Obtener el snapshot más reciente de este mercado
            rows = self.tracker.conn.execute(
                """
                SELECT price_yes, price_no, timestamp
                FROM snapshots
                WHERE condition_id = ?
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                (condition_id,),
            ).fetchall()

            if not rows:
                continue

            current = {
                "price_yes": rows[0][0],
                "price_no": rows[0][1],
            }

            # Calcular precio actual según el lado
            if pos["side"] == "YES":
                current_price = current["price_yes"]
            else:
                current_price = current["price_no"]

            if current_price is None or current_price <= 0:
                continue

            # Calcular P&L no realizado
            entry_price = pos["price"]
            shares = pos["shares"]
            amount = pos["amount"]

            if pos["side"] == "YES":
                unrealized_pnl = shares * (current_price - entry_price)
            else:
                unrealized_pnl = shares * (entry_price - current_price)

            pnl_pct = unrealized_pnl / amount if amount > 0 else 0
            trade_id = pos["id"]

            # ── 5. Market resolved ──
            if current_price >= 0.999 or current_price <= 0.001:
                trade = self.paper_trader.close_position(
                    trade_id, current_price
                )
                if trade:
                    closed_trades.append(trade)
                    self.trades_closed_this_cycle += 1
                    logger.info(
                        "🏁 RESOLVED #%d: %s %s @ %.1f%% → P&L $%+.2f",
                        trade_id,
                        pos.get("question", "?")[:40],
                        pos["side"],
                        current_price * 100,
                        trade.get("pnl", 0),
                    )
                continue

            # ── 1. Take-profit fijo ──
            if pnl_pct >= self.take_profit_pct:
                trade = self.paper_trader.close_position(
                    trade_id, current_price
                )
                if trade:
                    closed_trades.append(trade)
                    self.trades_closed_this_cycle += 1
                    logger.info(
                        "💰 TAKE PROFIT #%d: %s %s | +%.1f%% ($%+.2f)",
                        trade_id,
                        pos.get("question", "?")[:40],
                        pos["side"],
                        pnl_pct * 100,
                        unrealized_pnl,
                    )
                continue

            # ── 2/3. Stop-loss con trailing ──
            effective_sl = -self.stop_loss_pct
            if pnl_pct >= self.trailing_activate_pct:
                effective_sl = -self.trailing_sl_pct
                logger.debug(
                    "Trailing stop active for #%d: SL adjusted to %.0f%%",
                    trade_id, effective_sl * 100,
                )

            if pnl_pct <= effective_sl:
                reason = "TRAILING STOP" if pnl_pct > -self.stop_loss_pct else "STOP LOSS"
                trade = self.paper_trader.close_position(
                    trade_id, current_price
                )
                if trade:
                    closed_trades.append(trade)
                    self.trades_closed_this_cycle += 1
                    logger.info(
                        "🛑 %s #%d: %s %s | %.1f%% ($%+.2f)",
                        reason,
                        trade_id,
                        pos.get("question", "?")[:40],
                        pos["side"],
                        pnl_pct * 100,
                        unrealized_pnl,
                    )
                continue

            # ── 4. Time-based close ──
            entry_ts = pos.get("entry_timestamp", 0)
            hours_open = (now - entry_ts) / 3600
            if hours_open >= self.max_hold_hours:
                if abs(pnl_pct) < 0.05:
                    trade = self.paper_trader.close_position(
                        trade_id, current_price
                    )
                    if trade:
                        closed_trades.append(trade)
                        self.trades_closed_this_cycle += 1
                        logger.info(
                            "⏰ TIME CLOSE #%d: %s %s | %.0fh open | P&L $%+.2f",
                            trade_id,
                            pos.get("question", "?")[:40],
                            pos["side"],
                            hours_open,
                            unrealized_pnl,
                        )
                    continue

        return closed_trades

    # ------------------------------------------------------------------
    # Resumen del ciclo
    # ------------------------------------------------------------------

    def cycle_summary(self) -> str:
        """Devuelve un resumen legible de lo que pasó en este ciclo."""
        parts = []
        if self.trades_placed_this_cycle:
            parts.append(
                f"🎯 {self.trades_placed_this_cycle} trade(s) abierto(s)"
            )
        if self.trades_closed_this_cycle:
            parts.append(
                f"💰 {self.trades_closed_this_cycle} trade(s) cerrado(s)"
            )
        if not parts:
            open_count = len(self.tracker.get_open_positions())
            if open_count:
                parts.append(
                    f"📂 {open_count} posiciones abiertas (sin cambios)"
                )

        return " | ".join(parts) if parts else "⚪ Sin actividad"
