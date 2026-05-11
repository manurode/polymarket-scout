"""
Trading Logger — Log de trading puro en disco, sin ruido de sistema.

Escribe exclusivamente eventos de trading reales a ``data/trading.log``:
- Descubrimiento de nuevos mercados (primera vez)
- Apertura de posiciones (paper trades)
- Cierre de posiciones con P&L
- Cross & Fill de órdenes límite (MM)
- Cambios en el Bandit (asignación de capital)
- Equity snapshots periódicas

NUNCA escribe:
- Líneas HTTP (GET /api/... 200 OK)
- INFO del orchestrator (Radar, Top markets, etc.)
- MM QUOTE spam (cada quote individual)
- L2 SEED lines
- Health checks, degradación, etc.

El log de consola (Python root logger) permanece intacto.

Usage:
    from src.trading_logger import trading_log

    trading_log.market_discovered(token_id, question, score, profile)
    trading_log.position_opened(pos_id, strategy, market, side, size, entry)
    trading_log.position_closed(pos_id, strategy, market, pnl, reason)
    trading_log.cross_fill(token_id, side, price, size, strategy)
    trading_log.bandit_update(allocations)
    trading_log.equity_snapshot(equity, pnl, open_positions)
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

# ── Logger setup ────────────────────────────────────────────────────────────

TRADING_LOG_NAME = "trading"
TRADING_LOG_FILE = "data/trading.log"

_trading_logger = logging.getLogger(TRADING_LOG_NAME)
_trading_logger.setLevel(logging.DEBUG)
_trading_logger.propagate = False  # NO enviar al root logger (consola)

# Limpiar handlers previos (evitar duplicados en reloads)
_trading_logger.handlers.clear()

# File handler — append mode
_log_dir = Path(TRADING_LOG_FILE).parent
_log_dir.mkdir(parents=True, exist_ok=True)

_file_handler = logging.FileHandler(TRADING_LOG_FILE, encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
_trading_logger.addHandler(_file_handler)


# ── Public API ──────────────────────────────────────────────────────────────

class TradingLog:
    """Logger de trading puro. Métodos helper para cada tipo de evento."""

    def __init__(self):
        self._log = _trading_logger
        self._seen_tokens: set[str] = set()   # tokens ya loggeados como descubiertos
        self._seen_positions: set[int] = set()  # position IDs ya loggeados

    # ── Descubrimiento ───────────────────────────────────────────────────

    def market_discovered(
        self,
        token_id: str,
        condition_id: str,
        question: str,
        score: float,
        profile: str,  # "MM" o "DIRECTIONAL"
        volume_24h: float = 0,
        price: float | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """Loggear la PRIMERA VEZ que un mercado entra en un Top."""
        key = f"{profile}:{condition_id}"
        if key in self._seen_tokens:
            return  # solo la primera vez
        self._seen_tokens.add(key)

        price_str = f"{price:.4f}" if price is not None else "N/A"
        vol_str = f"${volume_24h:,.0f}" if volume_24h else "$0"
        tags_str = f" [{', '.join(tags)}]" if tags else ""

        self._log.info(
            "🔍 DESCUBIERTO [%s] token=%s score=%.4f vol24h=%s price=%s | %s%s",
            profile,
            token_id[:16],
            score,
            vol_str,
            price_str,
            question[:80],
            tags_str,
        )

    # ── Posiciones ───────────────────────────────────────────────────────

    def position_opened(
        self,
        pos_id: int,
        strategy: str,
        market: str,
        side: str,
        size: float,
        entry: float,
        token_id: str = "",
    ) -> None:
        """Loggear apertura de posición (paper trade)."""
        self._log.info(
            "📈 OPEN #%d [%s] %s %s size=$%.2f @ %.4f | %s",
            pos_id,
            strategy,
            side,
            "token=" + token_id[:16] if token_id else "",
            size,
            entry,
            market[:80],
        )
        self._seen_positions.add(pos_id)

    def position_closed(
        self,
        pos_id: int,
        strategy: str,
        market: str,
        pnl: float,
        pnl_pct: float,
        reason: str = "",
        exit_price: float = 0,
    ) -> None:
        """Loggear cierre de posición con P&L."""
        emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
        reason_str = f" reason={reason}" if reason else ""
        exit_str = f" exit=%.4f" % exit_price if exit_price else ""
        self._log.info(
            "%s CLOSE #%d [%s] P&L=$%.2f (%.1f%%)%s%s | %s",
            emoji,
            pos_id,
            strategy,
            pnl,
            pnl_pct * 100,
            exit_str,
            reason_str,
            market[:80],
        )

    # ── Cross & Fill (MM) ────────────────────────────────────────────────

    def cross_fill(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        strategy: str = "market_making",
    ) -> None:
        """Loggear ejecución de orden límite virtual (cross & fill)."""
        self._log.info(
            "⚡ FILL [%s] %s @ %.4f size=$%.2f | token=%s",
            strategy,
            side,
            price,
            size,
            token_id[:16],
        )

    def mm_quote_registered(
        self,
        token_id: str,
        market: str,
        bid_price: float,
        ask_price: float,
        bid_size: float,
        ask_size: float,
    ) -> None:
        """Loggear registro de quote MM (primera vez para este token en este ciclo)."""
        self._log.debug(
            "📋 MM QUOTE REG | token=%s bid=%.4f ($%.0f) ask=%.4f ($%.0f) | %s",
            token_id[:16],
            bid_price,
            bid_size,
            ask_price,
            ask_size,
            market[:60],
        )

    # ── Bandit ───────────────────────────────────────────────────────────

    def bandit_update(self, allocations: list, equity: float) -> None:
        """Loggear cambio en la asignación del Bandit."""
        parts = []
        for a in allocations[:4]:
            parts.append(f"{a.get('strategy','?')}={a.get('fraction',0)*100:.1f}%")
        self._log.info(
            "🧠 BANDIT | equity=$%.0f | %s",
            equity,
            "  ".join(parts),
        )

    def strategy_promoted(self, strategy: str, from_status: str, to_status: str) -> None:
        """Loggear cambio de estado de una estrategia en el Bandit."""
        self._log.info(
            "⭐ STRATEGY %s: %s → %s",
            strategy,
            from_status,
            to_status,
        )

    # ── Equity ───────────────────────────────────────────────────────────

    def equity_snapshot(
        self,
        equity: float,
        total_pnl: float,
        unrealized_pnl: float,
        open_positions: int,
    ) -> None:
        """Loggear snapshot periódica de equity."""
        pnl_emoji = "🟢" if total_pnl > 0 else "🔴" if total_pnl < 0 else "⚪"
        self._log.info(
            "💰 EQUITY | total=$%.2f %s P&L=$%.2f unrealized=$%.2f open=%d",
            equity,
            pnl_emoji,
            total_pnl,
            unrealized_pnl,
            open_positions,
        )

    # ── Sistema (solo eventos importantes) ───────────────────────────────

    def system_start(self) -> None:
        """Loggear inicio del sistema de trading."""
        self._log.info("=" * 60)
        self._log.info("🚀 SCOUT LAB TRADING LOG — %s", datetime.now(timezone.utc).isoformat())
        self._log.info("=" * 60)

    def system_stop(self, reason: str = "") -> None:
        """Loggear parada del sistema."""
        reason_str = f" — {reason}" if reason else ""
        self._log.info("⏹️ STOP%s", reason_str)

    def error(self, component: str, message: str) -> None:
        """Loggear error de trading (no HTTP/system errors)."""
        self._log.error("❌ ERROR [%s] %s", component, message)

    # ── Getters ─────────────────────────────────────────────────────────

    def get_log_path(self) -> str:
        """Ruta absoluta del archivo de log."""
        return str(Path(TRADING_LOG_FILE).resolve())


# ── Singleton ───────────────────────────────────────────────────────────────

trading_log = TradingLog()
