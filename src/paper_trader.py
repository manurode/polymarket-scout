"""PaperTrader — Simulated trading engine on top of the Tracker."""

import time as _time
from typing import Optional


class PaperTrader:
    """Simulate trades using a virtual cash balance.

    Parameters
    ----------
    tracker : Tracker
        An instance of Tracker with ``init_paper_trading()`` already called.
    initial_balance : float
        Starting cash (default 1000.0).
    position_size_pct : float
        Fraction of current balance to risk per trade (default 0.05 = 5%).
    """

    def __init__(
        self,
        tracker,
        initial_balance: float = 1000.0,
        position_size_pct: float = 0.05,
    ):
        self.tracker = tracker
        self.initial_balance = initial_balance
        self.position_size_pct = position_size_pct

    def _now(self) -> int:
        return int(_time.time())

    def get_balance(self) -> float:
        """Return available cash.

        Available balance = initial_balance - sum(amount of open positions).
        """
        open_positions = self.tracker.get_open_positions()
        invested = sum(p["amount"] for p in open_positions)
        return round(self.initial_balance - invested, 4)

    def place_bet(
        self,
        condition_id: str,
        question: Optional[str],
        side: str,
        price: float,
        signal_type: Optional[str] = None,
        score: Optional[int] = None,
        strategy: Optional[str] = None,
        timestamp: Optional[int] = None,
    ) -> dict:
        """Place a paper trade and return the trade dict.

        The amount wagered is ``balance * position_size_pct``.
        Shares purchased = amount / price.
        The trade is immediately persisted via the tracker.
        """
        if timestamp is None:
            timestamp = self._now()

        balance = self.get_balance()
        amount = round(balance * self.position_size_pct, 4)

        if amount <= 0:
            raise ValueError(
                f"Insufficient balance: ${balance:.2f} available, "
                f"need > $0.00 for a {self.position_size_pct:.0%} position"
            )

        if price <= 0:
            raise ValueError("price must be greater than 0")

        shares = round(amount / price, 4)

        trade = {
            "condition_id": condition_id,
            "question": question,
            "side": side.upper(),
            "amount": amount,
            "price": price,
            "shares": shares,
            "signal_type": signal_type,
            "score": score,
            "status": "open",
            "entry_timestamp": timestamp,
            "close_price": None,
            "close_timestamp": None,
            "pnl": None,
            "strategy": strategy,
        }

        trade["id"] = self.tracker.save_paper_trade(trade)
        return trade

    def close_position(
        self,
        trade_id: int,
        close_price: float,
        timestamp: Optional[int] = None,
    ) -> Optional[dict]:
        """Close an open paper trade by its id.

        Returns the updated trade dict including the computed P&L,
        or *None* if the trade was not found or was already closed.
        """
        if timestamp is None:
            timestamp = self._now()

        success = self.tracker.close_position(trade_id, close_price, timestamp)
        if not success:
            return None

        # Re-read the closed trade so we can return full details
        trades = self.tracker.get_paper_trades()
        for t in trades:
            if t["id"] == trade_id:
                return t
        return None

    def get_portfolio(self) -> dict:
        """Return a complete portfolio snapshot.

        Includes balance, positions, and statistics.
        """
        stats = self.tracker.get_portfolio_stats()
        open_positions = self.tracker.get_open_positions()
        balance = self.get_balance()

        # total equity = balance + realized_pnl
        total_equity = round(balance + stats["realized_pnl"], 4)

        return {
            "balance": balance,
            "initial_balance": self.initial_balance,
            "total_equity": total_equity,
            "open_positions": open_positions,
            "stats": stats,
        }
