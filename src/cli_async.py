"""
CLI asíncrono — versión v2.0 del pipeline de Scout Lab.

Orquesta el Radar Layer, Selection Engine y Rate-Limiter usando asyncio.
Coexiste con el CLI síncrono (src/cli.py) sin modificarlo.

Usage:
    python -m src.cli_async scan           # Radar puro (solo Gamma)
    python -m src.cli_async scan --clob    # Con enriquecimiento CLOB
    python -m src.cli_async top50          # Mostrar el Top 50 actual
"""

import argparse
import asyncio
import logging
import sys
import time

import yaml

from src.async_scanner import AsyncPolymarketScanner
from src.selection_engine import SelectionEngine
from src.rate_limiter import RateLimiter, get_default_limiter

logger = logging.getLogger(__name__)


def load_config(path: str = "config.yaml") -> dict:
    """Load YAML configuration."""
    with open(path, "r") as f:
        return yaml.safe_load(f)


# ── Comando: scan ─────────────────────────────────────────────────

async def run_scan_async(config_path: str = "config.yaml", use_clob: bool = False):
    """Pipeline asíncrono: radar scan → selection engine → report.

    Parameters
    ----------
    config_path : str
        Path al archivo de configuración YAML.
    use_clob : bool
        Si True, enriquece con precios/spread del CLOB (rate-limited).
        Si False, radar scan puro (solo Gamma, ultrarrápido).
    """
    config = load_config(config_path)
    sc = config.get("scanner", {})
    sel_cfg = config.get("selection", {})

    events_limit = sc.get("events_limit", 25)
    markets_per_event = sc.get("markets_per_event", 8)
    min_volume = sc.get("min_volume", 5000)
    top_n = sel_cfg.get("top_n", 50)

    t0 = time.monotonic()

    async with AsyncPolymarketScanner() as scanner:
        if use_clob:
            snapshots = await scanner.scan_markets_async(
                events_limit=events_limit,
                markets_per_event=markets_per_event,
                min_volume=min_volume,
                enrich_clob=True,
            )
        else:
            snapshots = await scanner.radar_scan(
                events_limit=events_limit,
                markets_per_event=markets_per_event,
                min_volume=min_volume,
            )

    t_scan = time.monotonic()

    # Selection Engine: rankear
    engine = SelectionEngine(top_n=top_n)
    result = engine.rank(snapshots)

    t_rank = time.monotonic()

    # ── Output ────────────────────────────────────────────────────
    scan_time = (t_scan - t0) * 1000
    rank_time = (t_rank - t_scan) * 1000
    mode = "CLOB" if use_clob else "Radar (Gamma)"

    print(f"\n{'='*60}")
    print(f"  Scout Lab v2.0 — Async Scan ({mode})")
    print(f"  Mercados escaneados: {len(snapshots)}")
    print(f"  Top {top_n} seleccionados")
    print(f"  Tiempo scan:  {scan_time:.0f}ms")
    print(f"  Tiempo rank:  {rank_time:.0f}ms")
    print(f"  Rate-limiter: {get_default_limiter().stats()}")
    print(f"{'='*60}\n")

    # Top 10
    print(f"🏆 Top 10 Mercados:\n")
    for i, ms in enumerate(result.top[:10], 1):
        spread_str = f"{ms.spread*100:.1f}%" if ms.spread is not None else "—"
        print(
            f"  {i:2d}. [{ms.score:.3f}] {ms.question[:55]}"
        )
        print(
            f"      Vol: ${ms.volume:,.0f}  Liq: ${ms.liquidity:,.0f}  "
            f"Spread: {spread_str}"
        )

    # Entradas / Salidas
    if result.enter:
        print(f"\n🟢 Entradas al Top {top_n}: {len(result.enter)} mercado(s)")
    if result.exit:
        print(f"🔴 Salidas del Top {top_n}:  {len(result.exit)} mercado(s)")

    if not result.enter and not result.exit:
        print(f"\n⚪ Sin cambios en el Top {top_n}")

    return result


# ── Comando: top50 ────────────────────────────────────────────────

async def show_top50(config_path: str = "config.yaml"):
    """Muestra el Top 50 actual (requiere haber ejecutado scan primero)."""
    print("El Top 50 se mantiene en memoria durante la sesión.")
    print("Ejecuta 'scan' primero para generar el ranking.\n")

    # Por ahora solo mostramos instrucciones — en v2.0 completa,
    # el estado se persiste en Redis entre ejecuciones.
    config = load_config(config_path)
    sc = config.get("scanner", {})
    sel_cfg = config.get("selection", {})

    events_limit = sc.get("events_limit", 25)
    markets_per_event = sc.get("markets_per_event", 8)
    min_volume = sc.get("min_volume", 5000)
    top_n = sel_cfg.get("top_n", 50)

    print(f"Ejecutando scan rápido para mostrar Top {top_n}...\n")

    async with AsyncPolymarketScanner() as scanner:
        snapshots = await scanner.radar_scan(
            events_limit=events_limit,
            markets_per_event=markets_per_event,
            min_volume=min_volume,
        )

    engine = SelectionEngine(top_n=top_n)
    result = engine.rank(snapshots)

    print(f"🏆 Top {top_n} Mercados por Score:\n")
    for i, ms in enumerate(result.top, 1):
        spread_str = f"{ms.spread*100:.1f}%" if ms.spread is not None else "—"
        print(
            f"  {i:3d}. [{ms.score:.3f}] {ms.question[:52]}  "
            f"Vol:${ms.volume:,.0f}  Spread:{spread_str}"
        )

    return result


# ── Entry point ───────────────────────────────────────────────────

def main():
    """Entry point for the async CLI."""
    parser = argparse.ArgumentParser(
        description="Scout Lab v2.0 — Async Pipeline"
    )
    sub = parser.add_subparsers(dest="command", help="Command")

    # scan
    scan_parser = sub.add_parser("scan", help="Async market scan + ranking")
    scan_parser.add_argument("--config", default="config.yaml")
    scan_parser.add_argument(
        "--clob", action="store_true",
        help="Enrich with CLOB prices/spread (rate-limited)",
    )

    # top50
    top_parser = sub.add_parser("top50", help="Show current Top 50")

    args = parser.parse_args()

    if args.command == "scan":
        asyncio.run(run_scan_async(args.config, use_clob=args.clob))
    elif args.command == "top50":
        asyncio.run(show_top50(args.config))
    elif args.command is None:
        parser.print_help()
        print("\nTip: 'python -m src.cli_async scan' para el pipeline rápido.")
    else:
        print(f"Unknown command: {args.command}")
        parser.print_help()


if __name__ == "__main__":
    main()
