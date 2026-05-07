"""CLI — command-line interface for Polymarket Scout."""

import argparse
import logging

import yaml

from src.scanner import PolymarketScanner
from src.tracker import Tracker
from src.signals import detect_all
from src.scorer import calculate_score
from src.alerter import should_alert, format_alert

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

        # Persist each signal
        for signal in signals:
            tracker.save_signal(
                condition_id=condition_id,
                signal_type=signal["signal_type"],
                score=score,
                detail=detail_json,
                timestamp=snapshot["timestamp"],
                cooldown_minutes=cooldown_minutes,
            )

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


def main():
    """Entry point for the Polymarket Scout CLI."""
    parser = argparse.ArgumentParser(
        description="Polymarket Scout — market signal scanner"
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="scan",
        choices=["scan", "report", "backfill"],
        help="Command to run (default: scan)",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to configuration file (default: config.yaml)",
    )

    args = parser.parse_args()

    if args.command == "scan":
        alerts = run_scan(args.config)
        if alerts:
            print("\n---\n".join(alerts))
            print(f"\n{len(alerts)} alert(s) generated.")
        else:
            print("No alerts generated.")
    elif args.command in ("report", "backfill"):
        print("Not yet implemented")


if __name__ == "__main__":
    main()
