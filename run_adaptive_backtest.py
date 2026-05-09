#!/usr/bin/env python3
"""
Script para correr backtests adaptativos y comparar con el modo estático.

Uso:
    python run_adaptive_backtest.py --days 7 --compare
    python run_adaptive_backtest.py --days 7 --adaptive-only
"""

import argparse
import json
import logging
import sys
from pathlib import Path

# Añadir src al path
sys.path.insert(0, str(Path(__file__).parent))

from src.price_history import PriceHistory
from src.backtester_adaptive import AdaptiveBacktester

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def print_results(result, title="Resultados"):
    """Imprime resultados formateados."""
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")
    print(f"  Capital inicial:    ${result.initial_capital:,.2f}")
    print(f"  Capital final:      ${result.final_equity:,.2f}")
    print(f"  P&L total:          ${result.total_pnl:+,.2f} ({result.total_pnl_pct:+.2f}%)")
    print(f"  Trades totales:     {result.total_trades}")
    print(f"  Win rate:           {result.win_rate:.1f}%")
    print(f"  Max drawdown:       {result.max_drawdown_pct:.2f}%")
    print(f"  Sharpe ratio:       {result.sharpe_ratio:.2f}")
    print(f"{'='*60}")
    
    if hasattr(result, 'strategy_breakdown') and result.strategy_breakdown:
        print("\n  Desglose por estrategia:")
        for strategy, stats in result.strategy_breakdown.items():
            win_rate = (stats['wins'] / stats['trades'] * 100) if stats['trades'] > 0 else 0
            print(f"    {strategy:20s}: P&L=${stats['pnl']:+,.2f} | Trades={stats['trades']} | Win={win_rate:.1f}%")
    
    if hasattr(result, 'regime_performance') and result.regime_performance:
        print("\n  Rendimiento por régimen:")
        for regime, stats in result.regime_performance.items():
            if stats['trades'] > 0:
                win_rate = (stats['wins'] / stats['trades'] * 100)
                print(f"    {regime:15s}: P&L=${stats['pnl']:+,.2f} | Trades={stats['trades']} | Win={win_rate:.1f}%")


def save_results(result, filename):
    """Guarda resultados detallados a JSON."""
    output = {
        "summary": {
            "initial_capital": result.initial_capital,
            "final_equity": result.final_equity,
            "total_pnl": result.total_pnl,
            "total_pnl_pct": result.total_pnl_pct,
            "total_trades": result.total_trades,
            "win_rate": result.win_rate,
            "max_drawdown_pct": result.max_drawdown_pct,
            "sharpe_ratio": result.sharpe_ratio,
        },
        "strategy_breakdown": result.strategy_breakdown,
    }
    
    if hasattr(result, 'regime_performance'):
        output["regime_performance"] = result.regime_performance
    
    if hasattr(result, 'adaptive_stats'):
        output["adaptive_stats"] = result.adaptive_stats
    
    if hasattr(result, 'trades'):
        output["trades"] = [
            {
                "market": t.market,
                "strategy": t.strategy,
                "side": t.side,
                "entry": t.entry_price,
                "exit": t.exit_price,
                "pnl": t.pnl,
                "pnl_pct": t.pnl_pct,
                "exit_reason": t.exit_reason,
                "regime": getattr(t, 'regime', ''),
                "confidence": getattr(t, 'confidence', 0),
            }
            for t in result.trades
        ]
    
    with open(filename, 'w') as f:
        json.dump(output, f, indent=2)
    
    logger.info(f"Resultados guardados en {filename}")


def main():
    parser = argparse.ArgumentParser(description='Backtest Adaptativo de Polymarket Scout')
    parser.add_argument('--days', type=int, default=7, help='Días de historial a usar')
    parser.add_argument('--capital', type=float, default=10000.0, help='Capital inicial')
    parser.add_argument('--compare', action='store_true', help='Comparar modo estático vs adaptativo')
    parser.add_argument('--adaptive-only', action='store_true', help='Solo correr modo adaptativo')
    parser.add_argument('--output', type=str, default='backtest_results.json', help='Archivo de salida')
    
    args = parser.parse_args()
    
    # Verificar que hay datos históricos
    price_store = PriceHistory()
    
    all_snapshots = []
    import time
    for i in range(args.days, 0, -1):
        date_str = time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
        day_data = price_store.load_day(date_str)
        all_snapshots.extend(day_data)
    
    if len(all_snapshots) < 10:
        logger.error(f"No hay suficientes datos históricos. Encontrados: {len(all_snapshots)}")
        logger.error("Corre el sistema por al menos un día para acumular datos.")
        sys.exit(1)
    
    logger.info(f"Datos históricos cargados: {len(all_snapshots)} snapshots")
    
    bt = AdaptiveBacktester(price_store=price_store)
    
    if args.compare:
        # Comparar ambos modos
        logger.info(f"Corriendo comparación de {args.days} días...")
        comparison = bt.compare_modes(days=args.days, initial_capital=args.capital)
        
        print(f"\n{'#'*60}")
        print(f"#  COMPARACIÓN: ESTÁTICO vs ADAPTATIVO")
        print(f"{'#'*60}")
        
        print("\n  MODO ESTÁTICO:")
        s = comparison['static']
        print(f"    P&L: ${s['total_pnl']:+,.2f} | Trades: {s['total_trades']} | Win: {s['win_rate']:.1f}% | Sharpe: {s['sharpe']:.2f}")
        
        print("\n  MODO ADAPTATIVO:")
        a = comparison['adaptive']
        print(f"    P&L: ${a['total_pnl']:+,.2f} | Trades: {a['total_trades']} | Win: {a['win_rate']:.1f}% | Sharpe: {a['sharpe']:.2f}")
        
        print("\n  MEJORA:")
        i = comparison['improvement']
        print(f"    Δ P&L: ${i['pnl_delta']:+,.2f}")
        print(f"    Δ Win Rate: {i['win_rate_delta']:+.1f}%")
        
        if comparison.get('regime_analysis'):
            print("\n  ANÁLISIS POR RÉGIMEN:")
            for regime, stats in comparison['regime_analysis'].items():
                if stats['trades'] > 0:
                    win_rate = (stats['wins'] / stats['trades'] * 100)
                    print(f"    {regime:15s}: P&L=${stats['pnl']:+,.2f} | Trades={stats['trades']} | Win={win_rate:.1f}%")
        
        if comparison.get('adaptive_state'):
            print("\n  ESTADO ADAPTATIVO FINAL:")
            state = comparison['adaptive_state']
            if state.get('strategies'):
                print("    Estrategias:")
                for name, s in state['strategies'].items():
                    status = "✅" if s['enabled'] else "❌"
                    print(f"      {status} {name}: conf_min={s['min_confidence']:.2f}")
            if state.get('performance'):
                print("    Performance:")
                for name, p in state['performance'].items():
                    print(f"      {name}: {p['trades']} trades | {p['win_rate']:.0f}% win | ${p['total_pnl']:+,.2f}")
        
        # Guardar comparación
        with open('backtest_comparison.json', 'w') as f:
            json.dump(comparison, f, indent=2)
        logger.info("Comparación guardada en backtest_comparison.json")
    
    else:
        # Solo correr adaptativo
        logger.info(f"Corriendo backtest adaptativo de {args.days} días...")
        result = bt.run(
            days=args.days,
            initial_capital=args.capital,
            adaptive=True,
            learning_mode=True,
        )
        
        print_results(result, "BACKTEST ADAPTATIVO")
        save_results(result, args.output)


if __name__ == "__main__":
    main()
