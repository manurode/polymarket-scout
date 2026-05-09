"""
Backtester Adaptativo — Simula estrategias con aprendizaje en tiempo real.

Extiende el backtester base para:
1. Usar AdaptiveStrategyEngine en lugar de SignalPipeline
2. Permitir que el sistema aprenda y adapte parámetros durante el backtest
3. Medir el rendimiento de cada estrategia individualmente
4. Simular el efecto de desactivar estrategias perdedoras

Uso:
    store = PriceHistory()
    bt = AdaptiveBacktester(store)
    results = bt.run(days=7, initial_capital=10000, adaptive=True)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from src.price_history import PriceHistory
from src.signal_pipeline import Signal
from src.adaptive_strategy_engine import AdaptiveStrategyEngine, MarketRegime

logger = logging.getLogger(__name__)


@dataclass
class AdaptiveBacktestTrade:
    """Trade con metadatos adicionales para análisis."""
    timestamp: int
    market: str
    strategy: str
    side: str
    entry_price: float
    exit_price: float
    size: float
    pnl: float
    pnl_pct: float
    exit_reason: str
    regime: str = ""           # régimen del mercado al entrar
    confidence: float = 0.0     # confianza de la señal
    filtered: bool = False      # si fue filtrado por el sistema adaptativo
    filter_reason: str = ""     # por qué fue filtrado


@dataclass
class AdaptiveBacktestResult:
    """Resultado extendido con análisis adaptativo."""
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
    trades: list[AdaptiveBacktestTrade]
    equity_curve: list[dict]
    strategy_breakdown: dict[str, dict]
    regime_performance: dict[str, dict]  # rendimiento por régimen
    adaptive_stats: dict                 # estadísticas del sistema adaptativo
    errors: list[str]


class AdaptiveBacktester:
    """
    Motor de backtesting con aprendizaje adaptativo.
    
    Cuando adaptive=True, el sistema:
    - Ajusta umbrales según rendimiento
    - Desactiva estrategias perdedoras
    - Filtra señales en malos régimen
    """

    def __init__(
        self,
        price_store: PriceHistory | None = None,
        state_file: str = "data/backtest_adaptive_state.json",
    ):
        self.price_store = price_store or PriceHistory()
        self.adaptive_engine = AdaptiveStrategyEngine(state_file=state_file)
        
        # Estadísticas de filtrado
        self.signals_generated = 0
        self.signals_filtered = 0
        self.signals_by_filter_reason: dict[str, int] = {}

    def run(
        self,
        days: int = 7,
        initial_capital: float = 10000.0,
        max_positions: int = 6,
        position_size_pct: float = 0.10,
        tp_pct: float = 0.15,
        sl_pct: float = 0.10,
        adaptive: bool = True,
        learning_mode: bool = True,
    ) -> AdaptiveBacktestResult:
        """
        Ejecuta backtest adaptativo.
        
        Parameters
        ----------
        days : int
            Días hacia atrás a simular.
        initial_capital : float
            Capital inicial.
        max_positions : int
            Máximo de posiciones simultáneas.
        position_size_pct : float
            % del capital por posición.
        tp_pct : float
            Take profit (%).
        sl_pct : float
            Stop loss (%).
        adaptive : bool
            Si True, usa el sistema adaptativo para filtrar señales.
        learning_mode : bool
            Si True, actualiza parámetros durante el backtest.
        
        Returns
        -------
        AdaptiveBacktestResult
        """
        # ── Cargar datos ──
        all_snapshots = []
        for i in range(days, 0, -1):
            date_str = time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
            day_data = self.price_store.load_day(date_str)
            all_snapshots.extend(day_data)

        if len(all_snapshots) < 10:
            return AdaptiveBacktestResult(
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
                trades=[],
                equity_curve=[],
                strategy_breakdown={},
                regime_performance={},
                adaptive_stats={},
                errors=["Not enough historical data"],
            )

        # ── Agrupar en scans ──
        scans = self._group_into_scans(all_snapshots, interval_s=60)
        
        # ── Estado de simulación ──
        equity = initial_capital
        free_capital = initial_capital
        positions: list[dict] = []
        trades: list[AdaptiveBacktestTrade] = []
        equity_curve: list[dict] = []
        peak_equity = initial_capital
        max_drawdown = 0.0

        strategy_pnl: dict[str, dict] = {}
        regime_stats: dict[str, dict] = {
            MarketRegime.TRENDING: {"trades": 0, "wins": 0, "pnl": 0},
            MarketRegime.RANGING: {"trades": 0, "wins": 0, "pnl": 0},
            MarketRegime.UNKNOWN: {"trades": 0, "wins": 0, "pnl": 0},
        }

        # ── Bucle principal ──
        for scan_idx, scan in enumerate(scans):
            # Preparar snapshots para el pipeline
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

            # ── Mark-to-market y cierre de posiciones ──
            for pos in list(positions):
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

                # TP/SL check
                pnl_pct = (current_price - entry) / entry if entry > 0 else 0
                if pos["side"] == "NO":
                    pnl_pct = -pnl_pct

                exit_reason = None
                if pnl_pct >= tp_pct:
                    exit_reason = "tp"
                elif pnl_pct <= -sl_pct:
                    exit_reason = "sl"

                if exit_reason:
                    # Cerrar posición
                    if pos["side"] == "YES":
                        pnl = (current_price - entry) * pos["size"]
                    else:
                        pnl = (entry - current_price) * pos["size"]

                    free_capital += pos["size"] + pnl
                    
                    trade = AdaptiveBacktestTrade(
                        timestamp=scan[0].get("t", 0) if scan else 0,
                        market=pos["market"],
                        strategy=pos["strategy"],
                        side=pos["side"],
                        entry_price=entry,
                        exit_price=current_price,
                        size=pos["size"],
                        pnl=round(pnl, 2),
                        pnl_pct=round(pnl_pct * 100, 2),
                        exit_reason=exit_reason,
                        regime=pos.get("regime", ""),
                        confidence=pos.get("confidence", 0),
                    )
                    trades.append(trade)

                    # Actualizar stats
                    self._update_stats(strategy_pnl, regime_stats, pos, pnl)
                    
                    # Aprender del resultado
                    if learning_mode:
                        self.adaptive_engine.update_from_trade(pos["strategy"], pnl)

                    positions.remove(pos)

            # ── Generar señales ──
            if adaptive:
                signals = self.adaptive_engine.generate_adaptive_signals(
                    snapshots_for_pipeline, cooldown_s=0
                )
            else:
                signals = self.adaptive_engine.pipeline.generate(
                    snapshots_for_pipeline, cooldown_s=0
                )

            # ── Ejecutar señales ──
            for sig in signals[:2]:  # máx 2 por scan
                if len(positions) >= max_positions:
                    break

                size = min(free_capital * position_size_pct, free_capital * 0.5)
                if size < 50:
                    continue

                entry = sig.entry_price
                if entry <= 0.01 or entry >= 0.99:
                    continue

                # Obtener régimen actual
                history = self.adaptive_engine.pipeline._history.get(sig.condition_id)
                regime = self.adaptive_engine.detect_regime(history) if history else MarketRegime.UNKNOWN

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
                    "regime": regime,
                    "confidence": sig.confidence,
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

            if scan_idx % 10 == 0:
                equity_curve.append({
                    "scan": scan_idx,
                    "equity": round(current_equity, 2),
                    "drawdown_pct": round(drawdown * 100, 2),
                })

        # ── Cerrar posiciones restantes ──
        for pos in positions:
            last_price = pos["current_price"]
            entry = pos["entry"]
            if pos["side"] == "YES":
                pnl = (last_price - entry) * pos["size"]
            else:
                pnl = (entry - last_price) * pos["size"]

            free_capital += pos["size"] + pnl

            trade = AdaptiveBacktestTrade(
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
                regime=pos.get("regime", ""),
                confidence=pos.get("confidence", 0),
            )
            trades.append(trade)
            
            self._update_stats(strategy_pnl, regime_stats, pos, pnl)
            if learning_mode:
                self.adaptive_engine.update_from_trade(pos["strategy"], pnl)

        # ── Calcular métricas finales ──
        final_equity = free_capital
        total_pnl = final_equity - initial_capital
        total_pnl_pct = (total_pnl / initial_capital) * 100

        winning = sum(1 for t in trades if t.pnl > 0)
        losing = sum(1 for t in trades if t.pnl <= 0)

        # Sharpe ratio
        returns = []
        for i in range(1, len(equity_curve)):
            r = (equity_curve[i]["equity"] - equity_curve[i-1]["equity"]) / equity_curve[i-1]["equity"]
            returns.append(r)
        avg_return = sum(returns) / len(returns) if returns else 0
        variance = sum((r - avg_return) ** 2 for r in returns) / len(returns) if returns else 0
        sharpe = (avg_return / (variance ** 0.5)) * (252 ** 0.5) if variance > 0 else 0

        # Estado adaptativo final
        adaptive_report = self.adaptive_engine.get_status_report()

        return AdaptiveBacktestResult(
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
            regime_performance=regime_stats,
            adaptive_stats=adaptive_report,
            errors=[],
        )

    def _update_stats(
        self,
        strategy_pnl: dict,
        regime_stats: dict,
        pos: dict,
        pnl: float
    ) -> None:
        """Actualiza estadísticas de estrategia y régimen."""
        strat = pos["strategy"]
        if strat not in strategy_pnl:
            strategy_pnl[strat] = {"pnl": 0, "trades": 0, "wins": 0}
        strategy_pnl[strat]["pnl"] += pnl
        strategy_pnl[strat]["trades"] += 1
        if pnl > 0:
            strategy_pnl[strat]["wins"] += 1

        regime = pos.get("regime", MarketRegime.UNKNOWN)
        if regime in regime_stats:
            regime_stats[regime]["trades"] += 1
            regime_stats[regime]["pnl"] += pnl
            if pnl > 0:
                regime_stats[regime]["wins"] += 1

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

    def compare_modes(
        self,
        days: int = 7,
        initial_capital: float = 10000.0,
    ) -> dict:
        """
        Compara el rendimiento con y sin adaptación.
        
        Returns:
            Comparación de ambos modos.
        """
        logger.info("=== Comparando modos de backtest ===")
        
        # Correr sin adaptación
        logger.info("Corriendo backtest SIN adaptación...")
        result_static = self.run(
            days=days,
            initial_capital=initial_capital,
            adaptive=False,
            learning_mode=False,
        )
        
        # Resetear estado adaptativo
        self.adaptive_engine = AdaptiveStrategyEngine(
            state_file="data/backtest_adaptive_temp.json"
        )
        
        # Correr con adaptación
        logger.info("Corriendo backtest CON adaptación...")
        result_adaptive = self.run(
            days=days,
            initial_capital=initial_capital,
            adaptive=True,
            learning_mode=True,
        )
        
        return {
            "static": {
                "total_pnl": result_static.total_pnl,
                "total_trades": result_static.total_trades,
                "win_rate": result_static.win_rate,
                "sharpe": result_static.sharpe_ratio,
                "max_drawdown": result_static.max_drawdown_pct,
            },
            "adaptive": {
                "total_pnl": result_adaptive.total_pnl,
                "total_trades": result_adaptive.total_trades,
                "win_rate": result_adaptive.win_rate,
                "sharpe": result_adaptive.sharpe_ratio,
                "max_drawdown": result_adaptive.max_drawdown_pct,
            },
            "improvement": {
                "pnl_delta": result_adaptive.total_pnl - result_static.total_pnl,
                "win_rate_delta": result_adaptive.win_rate - result_static.win_rate,
            },
            "regime_analysis": result_adaptive.regime_performance,
            "adaptive_state": result_adaptive.adaptive_stats,
        }
