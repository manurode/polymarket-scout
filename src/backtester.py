"""
Backtester — Simula estrategias sobre datos históricos reales.

Toma los snapshots guardados por PriceHistory y los reproduce en orden
cronológico, ejecutando el SignalPipeline como si fuera tiempo real.

Uso:
    store = PriceHistory()
    bt = Backtester(store)
    results = bt.run(days=7, initial_capital=10000)
    # results: {"total_pnl": ..., "trades": [...], "equity_curve": [...]}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.price_history import PriceHistory
from src.signal_pipeline import SignalPipeline, Signal

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    """Un trade simulado en backtesting."""
    timestamp: int
    market: str
    strategy: str
    side: str
    entry_price: float
    exit_price: float
    size: float
    pnl: float
    pnl_pct: float
    exit_reason: str = "close"


@dataclass
class BacktestResult:
    """Resultado completo de un backtest."""
    initial_capital: float
    final_equity: float
    total_pnl: float
    total_pnl_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    max_drawdown: float
    max_drawdown_pct: float
    sharpe_ratio: float
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[dict] = field(default_factory=list)
    strategy_breakdown: dict[str, dict] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


class Backtester:
    """Motor de backtesting sobre datos históricos."""

    def __init__(self, price_store: PriceHistory | None = None):
        self.price_store = price_store or PriceHistory()
        self.pipeline = SignalPipeline()

    def run(
        self,
        days: int = 7,
        initial_capital: float = 10000.0,
        max_positions: int = 6,
        position_size_pct: float = 0.10,
        tp_pct: float = 0.15,
        sl_pct: float = 0.10,
    ) -> BacktestResult:
        """Ejecuta backtest sobre N días de historial.

        Parameters
        ----------
        days : int
            Días hacia atrás a simular.
        initial_capital : float
            Capital inicial en USD.
        max_positions : int
            Máximo de posiciones simultáneas.
        position_size_pct : float
            % del capital por posición.
        tp_pct : float
            Take profit (% sobre entrada).
        sl_pct : float
            Stop loss (% sobre entrada).

        Returns
        -------
        BacktestResult
        """
        # ── Cargar datos ──
        all_snapshots = []
        for i in range(days, 0, -1):
            import time
            date_str = time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
            day_data = self.price_store.load_day(date_str)
            all_snapshots.extend(day_data)

        if len(all_snapshots) < 10:
            return BacktestResult(
                initial_capital=initial_capital,
                final_equity=initial_capital,
                total_pnl=0,
                total_pnl_pct=0,
                total_trades=0,
                winning_trades=0,
                losing_trades=0,
                win_rate=0,
                max_drawdown=0,
                max_drawdown_pct=0,
                sharpe_ratio=0,
                errors=["Not enough historical data (need >10 snapshots)"],
            )

        # ── Agrupar snapshots en "scans" (cada 60s simulados) ──
        scans = self._group_into_scans(all_snapshots, interval_s=60)

        # ── Estado de la simulación ──
        equity = initial_capital
        free_capital = initial_capital
        positions: list[dict] = []
        trades: list[BacktestTrade] = []
        equity_curve: list[dict] = []
        peak_equity = initial_capital
        max_drawdown = 0.0

        strategy_pnl: dict[str, dict] = {}

        # ── Bucle principal (recorrer scans en orden) ──
        for scan_idx, scan in enumerate(scans):
            # Convertir dicts planos a formato de snapshot para el pipeline
            snapshots_for_pipeline = [
                {
                    "condition_id": s.get("cid", ""),
                    "question": s.get("q", ""),
                    "price_yes": s.get("p"),
                    "volume": s.get("v", 0),
                    "spread": s.get("s"),
                }
                for s in scan
            ]

            # ── Mark-to-market de posiciones existentes ──
            for pos in positions:
                # Buscar precio actual para este mercado
                current_price = None
                for snap in scan:
                    if snap.get("cid") == pos["condition_id"]:
                        current_price = snap.get("p")
                        break

                if current_price is None:
                    continue

                entry = pos["entry"]
                if pos["side"] == "YES":
                    unrealized = (current_price - entry) * pos["size"]
                else:
                    unrealized = (entry - current_price) * pos["size"]

                pos["unrealized_pnl"] = unrealized
                pos["current_price"] = current_price

                # ── TP/SL check ──
                pnl_pct = (current_price - entry) / entry if entry > 0 else 0
                if pos["side"] == "NO":
                    pnl_pct = -pnl_pct

                exit_reason = None
                exit_price = current_price

                if pnl_pct >= tp_pct:
                    exit_reason = "tp"
                    exit_price = current_price
                elif pnl_pct <= -sl_pct:
                    exit_reason = "sl"
                    exit_price = current_price

                if exit_reason:
                    # Cerrar posición
                    if pos["side"] == "YES":
                        pnl = (exit_price - entry) * pos["size"]
                    else:
                        pnl = (entry - exit_price) * pos["size"]

                    free_capital += pos["size"] + pnl
                    equity = free_capital + sum(
                        p.get("size", 0) for p in positions if p != pos
                    )

                    trade = BacktestTrade(
                        timestamp=scan[0].get("t", 0) if scan else 0,
                        market=pos["market"],
                        strategy=pos["strategy"],
                        side=pos["side"],
                        entry_price=entry,
                        exit_price=exit_price,
                        size=pos["size"],
                        pnl=round(pnl, 2),
                        pnl_pct=round(pnl_pct * 100, 2),
                        exit_reason=exit_reason,
                    )
                    trades.append(trade)

                    # Strategy breakdown
                    strat = pos["strategy"]
                    if strat not in strategy_pnl:
                        strategy_pnl[strat] = {"pnl": 0, "trades": 0, "wins": 0}
                    strategy_pnl[strat]["pnl"] += pnl
                    strategy_pnl[strat]["trades"] += 1
                    if pnl > 0:
                        strategy_pnl[strat]["wins"] += 1

                    positions.remove(pos)

            # ── Generar señales ──
            signals = self.pipeline.generate(snapshots_for_pipeline, cooldown_s=0)

            # ── Ejecutar señales ──
            for sig in signals[:2]:  # max 2 por scan
                if len(positions) >= max_positions:
                    break

                size = min(free_capital * position_size_pct, free_capital * 0.5)
                if size < 50:
                    continue

                entry = sig.entry_price
                if entry <= 0.01 or entry >= 0.99:
                    continue

                free_capital -= size

                positions.append({
                    "condition_id": sig.condition_id,
                    "market": sig.question[:60],
                    "strategy": sig.strategy,
                    "side": sig.side,
                    "entry": entry,
                    "size": size,
                    "current_price": entry,
                    "unrealized_pnl": 0.0,
                })

            # ── Actualizar equity curve ──
            unrealized_total = sum(p.get("unrealized_pnl", 0) for p in positions)
            current_equity = free_capital + sum(p["size"] for p in positions) + unrealized_total
            equity = current_equity

            if current_equity > peak_equity:
                peak_equity = current_equity
            drawdown = (peak_equity - current_equity) / peak_equity if peak_equity > 0 else 0
            if drawdown > max_drawdown:
                max_drawdown = drawdown

            if scan_idx % 10 == 0:  # cada ~10 minutos simulados
                equity_curve.append({
                    "scan": scan_idx,
                    "equity": round(current_equity, 2),
                    "drawdown_pct": round(drawdown * 100, 2),
                })

        # ── Cerrar posiciones restantes al último precio ──
        for pos in positions:
            last_price = pos["current_price"]
            entry = pos["entry"]
            if pos["side"] == "YES":
                pnl = (last_price - entry) * pos["size"]
            else:
                pnl = (entry - last_price) * pos["size"]

            free_capital += pos["size"] + pnl

            trade = BacktestTrade(
                timestamp=0,
                market=pos["market"],
                strategy=pos["strategy"],
                side=pos["side"],
                entry_price=entry,
                exit_price=last_price,
                size=pos["size"],
                pnl=round(pnl, 2),
                pnl_pct=round((pnl / pos["size"]) * 100, 2) if pos["size"] > 0 else 0,
                exit_reason="eod",
            )
            trades.append(trade)

            strat = pos["strategy"]
            if strat not in strategy_pnl:
                strategy_pnl[strat] = {"pnl": 0, "trades": 0, "wins": 0}
            strategy_pnl[strat]["pnl"] += pnl
            strategy_pnl[strat]["trades"] += 1
            if pnl > 0:
                strategy_pnl[strat]["wins"] += 1

        # ── Calcular métricas finales ──
        final_equity = free_capital
        total_pnl = final_equity - initial_capital
        total_pnl_pct = (total_pnl / initial_capital) * 100

        winning = sum(1 for t in trades if t.pnl > 0)
        losing = sum(1 for t in trades if t.pnl <= 0)

        # Sharpe ratio simplificado
        returns = []
        for i in range(1, len(equity_curve)):
            r = (equity_curve[i]["equity"] - equity_curve[i-1]["equity"]) / equity_curve[i-1]["equity"]
            returns.append(r)
        avg_return = sum(returns) / len(returns) if returns else 0
        variance = sum((r - avg_return) ** 2 for r in returns) / len(returns) if returns else 0
        sharpe = (avg_return / (variance ** 0.5)) * (252 ** 0.5) if variance > 0 else 0

        return BacktestResult(
            initial_capital=initial_capital,
            final_equity=round(final_equity, 2),
            total_pnl=round(total_pnl, 2),
            total_pnl_pct=round(total_pnl_pct, 2),
            total_trades=len(trades),
            winning_trades=winning,
            losing_trades=losing,
            win_rate=round(winning / len(trades) * 100, 1) if trades else 0,
            max_drawdown=round(max_drawdown * 100, 2),
            max_drawdown_pct=round(max_drawdown * 100, 2),
            sharpe_ratio=round(sharpe, 2),
            trades=trades,
            equity_curve=equity_curve,
            strategy_breakdown=strategy_pnl,
        )

    def _group_into_scans(self, snapshots: list[dict], interval_s: int = 60) -> list[list[dict]]:
        """Agrupa snapshots en 'scans' simulados por ventana de tiempo."""
        if not snapshots:
            return []

        snapshots.sort(key=lambda s: s.get("t", 0))
        scans: list[list[dict]] = []
        current_scan: list[dict] = []
        scan_start = snapshots[0].get("t", 0)

        for snap in snapshots:
            t = snap.get("t", 0)
            if t - scan_start > interval_s:
                if current_scan:
                    scans.append(current_scan)
                current_scan = []
                scan_start = t
            current_scan.append(snap)

        if current_scan:
            scans.append(current_scan)

        return scans
