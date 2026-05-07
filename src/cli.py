"""CLI — command-line interface for Polymarket Scout."""

import argparse
import logging
import sys

import yaml

from src.scanner import PolymarketScanner
from src.tracker import Tracker
from src.signals import detect_all
from src.scorer import calculate_score
from src.alerter import should_alert, format_alert, send_all_telegram
from src.paper_trader import PaperTrader
from src.backtester import Backtester

logger = logging.getLogger(__name__)


def load_config(path: str = "config.yaml") -> dict:
    """Load YAML configuration from *path*."""
    with open(path, "r") as f:
        return yaml.safe_load(f)


def run_scan(config_path: str = "config.yaml") -> list[str]:
    """Run the full pipeline: scan → track → signal → score → alert.

    Returns a list of formatted alert strings, one per market that
    exceeded the alert threshold.
    """
    config = load_config(config_path)

    sc = config["scanner"]
    tr = config["tracker"]
    sig = config["signals"]
    scr = config["scorer"]

    # 1. Scan markets
    scanner = PolymarketScanner()
    snapshots = scanner.scan_markets(
        events_limit=sc["events_limit"],
        markets_per_event=sc["markets_per_event"],
        min_volume=sc["min_volume"],
    )

    if not snapshots:
        return []

    # 2. Persist snapshots
    tracker = Tracker(tr["db_path"])
    tracker.init_db()
    tracker.save_snapshots(snapshots)

    # Compute lookback: the larger of the two time‑based signal windows
    lookback_seconds = max(
        sig["momentum"]["window_hours"],
        sig["volume_spike"]["window_hours"],
    ) * 3600

    alert_threshold = scr["alert_threshold"]
    cooldown_minutes = scr.get("cooldown_minutes", 30)
    alerts: list[str] = []

    for snapshot in snapshots:
        condition_id = snapshot["condition_id"]

        # Get recent history for this market
        recent = tracker.get_recent_snapshots(
            condition_id,
            lookback_seconds=lookback_seconds,
            reference_ts=snapshot["timestamp"],
        )

        # Detect signals
        signals = detect_all(recent, sig)
        if not signals:
            continue

        # Composite score
        score, detail_json = calculate_score(signals)

        # Threshold check
        if not should_alert(score, alert_threshold):
            continue

        # Persist signals AND check cooldown — only alert if at least one
        # signal passes the cooldown gate (prevents spamming same market)
        saved_any = False
        for signal in signals:
            was_saved = tracker.save_signal(
                condition_id=condition_id,
                signal_type=signal["signal_type"],
                score=score,
                detail=detail_json,
                timestamp=snapshot["timestamp"],
                cooldown_minutes=cooldown_minutes,
            )
            if was_saved:
                saved_any = True

        # Skip alert if all signals are within cooldown
        if not saved_any:
            continue

        # Build momentum string from first momentum signal found
        momentum_str = "—"
        for signal in signals:
            if "change_pct" in signal:
                pct = signal["change_pct"] * 100  # decimal → percent
                momentum_str = f"+{pct:.1f}" if pct > 0 else f"{pct:.1f}"
                break

        # Build alert message
        alert = format_alert(score, snapshot, detail_json, momentum_str)
        alerts.append(alert)

    return alerts


def cmd_backtest(args) -> None:
    """Run a backtest across historical snapshots."""
    config = load_config(args.config)
    db_path = config["tracker"]["db_path"]
    sig_cfg = config.get("signals", {})

    tracker = Tracker(db_path)
    tracker.init_db()
    tracker.init_paper_trading()

    pt = PaperTrader(tracker, initial_balance=1000.0, position_size_pct=0.05)
    bt = Backtester(tracker, pt, signal_config=sig_cfg)

    strategy = args.strategy or None
    results = bt.run(strategy_name=strategy, days_back=args.days)

    for name, data in results.items():
        report = data["report"]
        print(f"\n## Strategy: {name}")
        print(f"  Total trades:    {report['total_trades']}")
        print(f"  Closed:          {report['closed_positions']}")
        print(f"  Open:            {report['open_positions']}")
        print(f"  Wins:            {report['wins']}")
        print(f"  Losses:          {report['losses']}")
        print(f"  Win rate:        {report['win_rate']:.2%}")
        print(f"  Realized P&L:    ${report['realized_pnl']:.4f}")
        print(f"  ROI:             {report['roi']:.2%}")
        print(f"  Total invested:  ${report['total_invested']:.4f}")
        print(f"  Avg win:         ${report['avg_win']:.4f}")
        print(f"  Avg loss:        ${report['avg_loss']:.4f}")
        if report.get("best_trade") is not None:
            print(f"  Best trade:      ${report['best_trade']:.4f}")
        if report.get("worst_trade") is not None:
            print(f"  Worst trade:     ${report['worst_trade']:.4f}")

    # Summary line
    total_trades = sum(r["report"]["total_trades"] for r in results.values())
    total_pnl = sum(r["report"]["realized_pnl"] for r in results.values())
    print(f"\n→ {total_trades} trade(s) across {len(results)} strategy/strategies, "
          f"net P&L ${total_pnl:.4f}")


def cmd_portfolio(args) -> None:
    """Display current paper trading portfolio."""
    config = load_config(args.config)
    db_path = config["tracker"]["db_path"]

    tracker = Tracker(db_path)
    tracker.init_db()
    tracker.init_paper_trading()

    pt = PaperTrader(tracker)
    portfolio = pt.get_portfolio()

    print(f"\n## Paper Trading Portfolio")
    print(f"  Balance:           ${portfolio['balance']:.4f}")
    print(f"  Initial balance:   ${portfolio['initial_balance']:.4f}")
    print(f"  Total equity:      ${portfolio['total_equity']:.4f}")

    stats = portfolio["stats"]
    print(f"\n  Total trades:      {stats['total_trades']}")
    print(f"  Open positions:    {stats['open_positions']}")
    print(f"  Closed positions:  {stats['closed_positions']}")
    print(f"  Wins:              {stats['wins']}")
    print(f"  Losses:            {stats['losses']}")
    print(f"  Win rate:          {stats['win_rate']:.2%}")
    print(f"  Realized P&L:      ${stats['realized_pnl']:.4f}")
    print(f"  Invested (open):   ${stats['total_invested_open']:.4f}")

    open_positions = portfolio["open_positions"]
    if open_positions:
        print(f"\n## Open Positions ({len(open_positions)})")
        for pos in open_positions:
            print(f"  #{pos['id']}: {pos['side']} on \"{pos.get('question', '?')}\" "
                  f"@ ${pos['price']:.4f} | ${pos['amount']:.2f} | "
                  f"strategy={pos.get('strategy', '-')}")
    else:
        print("\n(no open positions)")


def cmd_paper_trade(args) -> None:
    """Place a manual paper trade."""
    config = load_config(args.config)
    db_path = config["tracker"]["db_path"]

    tracker = Tracker(db_path)
    tracker.init_db()
    tracker.init_paper_trading()

    pt = PaperTrader(tracker)

    side = args.side.upper()
    if side not in ("YES", "NO"):
        print(f"Error: side must be YES or NO, got '{args.side}'", file=sys.stderr)
        sys.exit(1)

    amount = args.amount
    if amount <= 0:
        print(f"Error: amount must be > 0, got {amount}", file=sys.stderr)
        sys.exit(1)

    # Look up the market by slug to get condition_id, question, and current price
    tracker.init_db()
    rows = tracker.conn.execute(
        "SELECT condition_id, question, price_yes, price_no "
        "FROM snapshots WHERE slug = ? ORDER BY timestamp DESC LIMIT 1",
        (args.market,),
    ).fetchall()

    if not rows:
        print(f"Error: no snapshots found for slug '{args.market}'", file=sys.stderr)
        sys.exit(1)

    condition_id, question, price_yes, price_no = rows[0]
    price = price_yes if side == "YES" else price_no

    if price is None or price <= 0:
        print(f"Error: invalid price ({price}) for side {side}", file=sys.stderr)
        sys.exit(1)

    try:
        # Override position size to use the requested amount
        trade = pt.tracker.save_paper_trade({
            "condition_id": condition_id,
            "question": question,
            "side": side,
            "amount": amount,
            "price": price,
            "shares": round(amount / price, 4),
            "signal_type": None,
            "score": None,
            "status": "open",
            "entry_timestamp": int(__import__("time").time()),
            "close_price": None,
            "close_timestamp": None,
            "pnl": None,
            "strategy": "manual",
        })
        print(f"\nTrade placed: #{trade}")
        print(f"  Market:   {question}")
        print(f"  Side:     {side}")
        print(f"  Price:    ${price:.4f}")
        print(f"  Amount:   ${amount:.2f}")
        print(f"  Shares:   {round(amount / price, 4)}")
    except Exception as exc:
        print(f"Error placing trade: {exc}", file=sys.stderr)
        sys.exit(1)


def main():
    """Entry point for the Polymarket Scout CLI."""
    parser = argparse.ArgumentParser(
        description="Polymarket Scout — market signal scanner"
    )
    sub = parser.add_subparsers(dest="command", help="Command to run")

    # --- scan ---
    scan_parser = sub.add_parser("scan", help="Scan markets and generate alerts")
    scan_parser.add_argument(
        "--config", default="config.yaml",
        help="Path to configuration file (default: config.yaml)",
    )

    # --- backtest ---
    backtest_parser = sub.add_parser("backtest", help="Run backtest on historical data")
    backtest_parser.add_argument(
        "--config", default="config.yaml",
        help="Path to configuration file (default: config.yaml)",
    )
    backtest_parser.add_argument(
        "--strategy", type=str, default=None,
        help="Filter to a single strategy (e.g., momentum_follow, contrarian)",
    )
    backtest_parser.add_argument(
        "--days", type=int, default=30,
        help="How many days of history to backtest (default: 30)",
    )

    # --- portfolio ---
    portfolio_parser = sub.add_parser("portfolio", help="Show paper trading portfolio")
    portfolio_parser.add_argument(
        "--config", default="config.yaml",
        help="Path to configuration file (default: config.yaml)",
    )

    # --- paper-trade ---
    pt_parser = sub.add_parser("paper-trade", help="Place a manual paper trade")
    pt_parser.add_argument(
        "--config", default="config.yaml",
        help="Path to configuration file (default: config.yaml)",
    )
    pt_parser.add_argument(
        "--market", type=str, required=True,
        help="Market slug to trade",
    )
    pt_parser.add_argument(
        "--side", type=str, required=True,
        help="Bet side: YES or NO",
    )
    pt_parser.add_argument(
        "--amount", type=float, required=True,
        help="Amount to wager in USD",
    )

    args = parser.parse_args()

    if args.command == "scan":
        config = load_config(args.config)
        alerts = run_scan(args.config)

        # Print to stdout
        if alerts:
            print("\n---\n".join(alerts))
            print(f"\n{len(alerts)} alert(s) generated.")
        else:
            print("No alerts generated.")

        # Auto-send to Telegram if configured
        alerter_cfg = config.get("alerter", {})
        bot_token = alerter_cfg.get("bot_token")
        chat_id = alerter_cfg.get("chat_id")
        if alerts and bot_token and chat_id:
            try:
                sent = send_all_telegram(alerts, bot_token, chat_id)
                print(f"→ Sent {sent}/{len(alerts)} alerts to Telegram")
            except Exception as exc:
                logger.warning("Telegram delivery failed: %s", exc)

    elif args.command == "backtest":
        cmd_backtest(args)

    elif args.command == "portfolio":
        cmd_portfolio(args)

    elif args.command == "paper-trade":
        cmd_paper_trade(args)

    elif args.command is None:
        # Default to scan when no subcommand given (backward compat)
        parser.print_help()
        print("\nTip: try 'python -m src.cli scan' for the default pipeline.")

    else:
        print(f"Unknown command: {args.command}")
        parser.print_help()


if __name__ == "__main__":
    main()
