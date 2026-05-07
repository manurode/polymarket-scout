"""Backtester — replay historical snapshots through strategies and paper-trade them."""

import time as _time
import logging
from typing import Optional

from src.signals import detect_all
from src.strategies import STRATEGIES

logger = logging.getLogger(__name__)


class Backtester:
    """Run strategies against historical snapshots to measure performance.

    Parameters
    ----------
    tracker : Tracker
        An instance of Tracker with ``init_paper_trading()`` already called.
    paper_trader : PaperTrader
        An instance of PaperTrader for placing simulated bets.
    signal_config : dict
        Configuration dictionary for signal detection (same shape as
        ``config.yaml`` → ``signals`` section).
    """

    def __init__(self, tracker, paper_trader, signal_config: Optional[dict] = None):
        self.tracker = tracker
        self.paper_trader = paper_trader
        self.signal_config = signal_config or self._default_signal_config()

    @staticmethod
    def _default_signal_config() -> dict:
        return {
            "momentum": {"threshold": 0.05, "window_hours": 1},
            "volume_spike": {"threshold": 3.0, "window_hours": 24},
            "spread": {"tight_threshold": 0.03, "wide_threshold": 0.10},
            "new_interest": {"min_volume": 10000},
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_all_condition_ids(self) -> list[str]:
        """Return every distinct condition_id in the snapshots table."""
        rows = self.tracker.conn.execute(
            "SELECT DISTINCT condition_id FROM snapshots ORDER BY condition_id"
        ).fetchall()
        return [r[0] for r in rows]

    def _get_market_snapshots(self, condition_id: str) -> list[dict]:
        """Return all snapshots for a market, ordered by timestamp ascending."""
        columns = [
            "condition_id", "question", "slug", "event_title",
            "price_yes", "price_no", "spread", "volume", "liquidity",
            "timestamp",
        ]
        rows = self.tracker.conn.execute(
            "SELECT " + ", ".join(columns)
            + " FROM snapshots WHERE condition_id = ? ORDER BY timestamp ASC",
            (condition_id,),
        ).fetchall()
        return [dict(zip(columns, row)) for row in rows]

    # ------------------------------------------------------------------
    # Core logic
    # ------------------------------------------------------------------

    def run_single_market(
        self, condition_id: str, strategy_name: Optional[str] = None
    ) -> list[dict]:
        """Backtest a single market and return the trades created.

        For each snapshot (except the first), signals are detected from all
        preceding snapshots, then strategies are evaluated.  When a strategy
        triggers a bet, a paper trade is recorded.

        After replay, every open trade for this market is closed at the last
        available snapshot price (mark-to-market).
        """
        snapshots = self._get_market_snapshots(condition_id)
        if len(snapshots) < 2:
            return []

        trades: list[dict] = []

        # Build up history one snapshot at a time
        history: list[dict] = []

        for snap in snapshots:
            history.append(snap)

            if len(history) < 2:
                # Not enough history to detect signals yet
                continue

            # Detect signals from the trailing history
            signals = detect_all(history, self.signal_config)
            if not signals:
                continue

            # Evaluate strategies
            names = [strategy_name] if strategy_name else list(STRATEGIES.keys())
            for name in names:
                fn = STRATEGIES.get(name)
                if fn is None:
                    continue
                decision = fn(signals, snap)
                if decision is None:
                    continue

                action = decision.get("action")
                if action not in ("YES", "NO"):
                    continue

                price = snap.get("price_yes" if action == "YES" else "price_no")
                if price is None or price <= 0:
                    continue

                # Check if we already have an open trade for this market+strategy
                existing_open = any(
                    t["status"] == "open"
                    and t["condition_id"] == condition_id
                    and t.get("strategy") == name
                    for t in trades
                )
                if existing_open:
                    continue

                try:
                    trade = self.paper_trader.place_bet(
                        condition_id=condition_id,
                        question=snap.get("question"),
                        side=action,
                        price=price,
                        signal_type=decision.get("reason", ""),
                        strategy=name,
                        timestamp=snap.get("timestamp"),
                    )
                    trades.append(trade)
                    logger.debug(
                        "Trade #%d: %s %s on %s via %s @ %.4f",
                        trade["id"], action, condition_id[-8:], name, price,
                    )
                except ValueError as exc:
                    logger.warning("Skipping trade on %s: %s", condition_id, exc)

        # Mark-to-market: close every open trade at the last snapshot price
        if trades and snapshots:
            last_snap = snapshots[-1]
            last_yes = last_snap.get("price_yes")
            last_no = last_snap.get("price_no")
            if last_yes is not None and last_no is not None:
                for t in trades:
                    if t["status"] == "closed":
                        continue
                    close_price = last_yes if t["side"] == "YES" else last_no
                    if close_price is None or close_price <= 0:
                        continue
                    self.paper_trader.close_position(
                        t["id"], close_price, timestamp=last_snap.get("timestamp"),
                    )
                    t["status"] = "closed"

        return trades

    def run(self, strategy_name: Optional[str] = None, days_back: int = 30) -> dict:
        """Run backtest across all markets with snapshots in the last *days_back* days.

        Returns a dict keyed by strategy name, each value being another dict with:
        ``trades`` (list), ``report`` (metrics dict).
        """
        now = int(_time.time())
        cutoff = now - (days_back * 86400)

        # Collect condition_ids with recent snapshots
        rows = self.tracker.conn.execute(
            "SELECT DISTINCT condition_id FROM snapshots WHERE timestamp >= ?",
            (cutoff,),
        ).fetchall()
        condition_ids = [r[0] for r in rows]

        results: dict[str, dict] = {}

        # Determine which strategies to run
        names = [strategy_name] if strategy_name else list(STRATEGIES.keys())

        for name in names:
            results[name] = {"trades": [], "report": {}}

        for cid in condition_ids:
            trades = self.run_single_market(cid, strategy_name=strategy_name)
            for t in trades:
                strat = t.get("strategy", "unknown")
                if strat in results:
                    results[strat]["trades"].append(t)

        # Generate reports
        for name in names:
            results[name]["report"] = self.generate_report(results[name]["trades"])

        return results

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def generate_report(self, trades: list[dict]) -> dict:
        """Produce performance metrics from a list of trade dicts.

        Keys returned: roi, win_rate, total_trades, wins, losses, open,
        realized_pnl, avg_win, avg_loss, best_trade, worst_trade.
        """
        closed = [t for t in trades if t.get("status") == "closed"]
        open_trades = [t for t in trades if t.get("status") == "open"]

        total_trades = len(trades)
        wins = [t for t in closed if (t.get("pnl") or 0) > 0]
        losses = [t for t in closed if (t.get("pnl") or 0) < 0]
        win_count = len(wins)
        loss_count = len(losses)
        closed_count = win_count + loss_count

        realized_pnl = sum(t.get("pnl") or 0 for t in closed)
        total_invested = sum(t.get("amount") or 0 for t in trades)
        roi = round(realized_pnl / total_invested, 4) if total_invested > 0 else 0.0
        win_rate = round(win_count / closed_count, 4) if closed_count > 0 else 0.0

        avg_win = round(sum(t["pnl"] for t in wins) / win_count, 4) if win_count > 0 else 0.0
        avg_loss = round(sum(t["pnl"] for t in losses) / loss_count, 4) if loss_count > 0 else 0.0

        # Best / worst by P&L
        sorted_closed = sorted(closed, key=lambda t: t.get("pnl") or 0)
        best_trade = sorted_closed[-1]["pnl"] if sorted_closed else None
        worst_trade = sorted_closed[0]["pnl"] if sorted_closed else None

        return {
            "total_trades": total_trades,
            "open_positions": len(open_trades),
            "closed_positions": closed_count,
            "wins": win_count,
            "losses": loss_count,
            "win_rate": win_rate,
            "realized_pnl": round(realized_pnl, 4),
            "roi": roi,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "total_invested": round(total_invested, 4),
            "best_trade": best_trade,
            "worst_trade": worst_trade,
        }
