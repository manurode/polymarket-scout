"""Tests for the CLI module."""

import json
import pytest
from unittest.mock import MagicMock, patch

from src.cli import load_config, run_scan, main


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

def test_load_config():
    """load_config should open the file and call yaml.safe_load."""
    mock_data = {"scanner": {"events_limit": 5}}

    mock_file = MagicMock()
    with patch("builtins.open", return_value=mock_file) as mock_open:
        with patch("yaml.safe_load", return_value=mock_data) as mock_yaml:
            result = load_config("config.yaml")

    mock_open.assert_called_once_with("config.yaml", "r")
    mock_yaml.assert_called_once_with(mock_file.__enter__.return_value)
    assert result == mock_data


# ---------------------------------------------------------------------------
# run_scan  (pipeline integration)
# ---------------------------------------------------------------------------

def _test_config():
    return {
        "scanner": {"events_limit": 5, "markets_per_event": 3, "min_volume": 5000},
        "tracker": {"db_path": ":memory:", "retention_days": 90},
        "signals": {
            "momentum": {"threshold": 0.05, "window_hours": 1},
            "volume_spike": {"threshold": 3.0, "window_hours": 24},
            "spread": {"tight_threshold": 0.03, "wide_threshold": 0.10},
            "new_interest": {"min_volume": 10000},
        },
        "scorer": {"alert_threshold": 60, "cooldown_minutes": 30},
        "alerter": {"platform": "telegram"},
    }


def _mock_snapshot(condition_id="0xabc123"):
    return {
        "condition_id": condition_id,
        "question": "Will Trump win?",
        "slug": "trump-2024",
        "event_title": "US Election 2024",
        "price_yes": 0.60,
        "price_no": 0.40,
        "spread": 0.02,
        "volume": 3000000.0,
        "liquidity": 500000.0,
        "timestamp": 1715000000,
    }


def test_run_scan_pipeline():
    """Full pipeline: scan → track → detect → score → alert."""
    config = _test_config()
    mock_snapshots = [_mock_snapshot()]

    momentum_signal = {
        "signal_type": "momentum_up",
        "change_pct": 0.20,
        "weight": 20,
    }

    detail_json = json.dumps([
        {"signal_type": "momentum_up", "weight": 20, "intensity": 1.0, "contribution": 20},
    ])

    recent_snapshots = [
        {"condition_id": "0xabc123", "price_yes": 0.50, "volume": 1000000,
         "timestamp": 1714996400},
        {"condition_id": "0xabc123", "price_yes": 0.60, "volume": 3000000,
         "timestamp": 1715000000},
    ]

    mock_file = MagicMock()
    with patch("builtins.open", return_value=mock_file):
        with patch("yaml.safe_load", return_value=config):
            with patch("src.cli.PolymarketScanner") as MockScanner:
                mock_scanner = MagicMock()
                mock_scanner.scan_markets.return_value = mock_snapshots
                MockScanner.return_value = mock_scanner

                with patch("src.cli.Tracker") as MockTracker:
                    mock_tracker = MagicMock()
                    mock_tracker.save_snapshots.return_value = 1
                    mock_tracker.get_recent_snapshots.return_value = recent_snapshots
                    mock_tracker.save_signal.return_value = True
                    MockTracker.return_value = mock_tracker

                    with patch("src.cli.detect_all", return_value=[momentum_signal]) as mock_detect:
                        with patch("src.cli.calculate_score", return_value=(75, detail_json)) as mock_score:
                            with patch("src.cli.should_alert", return_value=True) as mock_alert:
                                alerts, _summary = run_scan()

    mock_scanner.scan_markets.assert_called_once_with(
        events_limit=5, markets_per_event=3, min_volume=5000,
    )
    mock_tracker.init_db.assert_called_once()
    mock_tracker.init_paper_trading.assert_called_once()
    mock_tracker.save_snapshots.assert_called_once_with(mock_snapshots)
    mock_tracker.get_recent_snapshots.assert_called_once_with(
        "0xabc123", lookback_seconds=24 * 3600, reference_ts=1715000000,
    )
    mock_detect.assert_called_once()
    call_args = mock_detect.call_args[0]
    assert call_args[0] == recent_snapshots
    assert call_args[1] == config["signals"]
    mock_score.assert_called_once_with([momentum_signal])
    mock_alert.assert_called_once_with(75, 60)
    mock_tracker.save_signal.assert_called_once_with(
        condition_id="0xabc123", signal_type="momentum_up", score=75,
        detail=detail_json, timestamp=1715000000, cooldown_minutes=30,
    )
    assert len(alerts) == 1
    assert "75/100" in alerts[0]
    assert "US Election 2024" in alerts[0]
    assert "+20.0%" in alerts[0]


def test_run_scan_no_signals():
    """When detect_all returns [] there should be zero alerts."""
    config = _test_config()
    mock_snapshots = [_mock_snapshot()]

    mock_file = MagicMock()
    with patch("builtins.open", return_value=mock_file):
        with patch("yaml.safe_load", return_value=config):
            with patch("src.cli.PolymarketScanner") as MockScanner:
                mock_scanner = MagicMock()
                mock_scanner.scan_markets.return_value = mock_snapshots
                MockScanner.return_value = mock_scanner

                with patch("src.cli.Tracker") as MockTracker:
                    mock_tracker = MagicMock()
                    mock_tracker.get_recent_snapshots.return_value = [
                        mock_snapshots[0], mock_snapshots[0],
                    ]
                    MockTracker.return_value = mock_tracker

                    with patch("src.cli.detect_all", return_value=[]) as mock_detect:
                        alerts, _summary = run_scan()

    assert alerts == []
    mock_detect.assert_called_once()


def test_run_scan_below_threshold():
    """Signals found but score below threshold → no alert."""
    config = _test_config()
    mock_snapshots = [_mock_snapshot()]

    signal = {"signal_type": "momentum_down", "change_pct": -0.03, "weight": 5}
    detail = json.dumps([
        {"signal_type": "momentum_down", "weight": 5, "intensity": 0.6, "contribution": 3},
    ])

    mock_file = MagicMock()
    with patch("builtins.open", return_value=mock_file):
        with patch("yaml.safe_load", return_value=config):
            with patch("src.cli.PolymarketScanner") as MockScanner:
                mock_scanner = MagicMock()
                mock_scanner.scan_markets.return_value = mock_snapshots
                MockScanner.return_value = mock_scanner

                with patch("src.cli.Tracker") as MockTracker:
                    mock_tracker = MagicMock()
                    mock_tracker.get_recent_snapshots.return_value = [
                        mock_snapshots[0], mock_snapshots[0],
                    ]
                    MockTracker.return_value = mock_tracker

                    with patch("src.cli.detect_all", return_value=[signal]):
                        with patch("src.cli.calculate_score", return_value=(45, detail)):
                            with patch("src.cli.should_alert", return_value=False) as mock_should:
                                alerts, _summary = run_scan()

    mock_should.assert_called_once_with(45, 60)
    assert alerts == []
    mock_tracker.save_signal.assert_not_called()


def test_run_scan_empty_snapshots():
    """When scanner returns an empty list, return early with no alerts."""
    config = _test_config()

    mock_file = MagicMock()
    with patch("builtins.open", return_value=mock_file):
        with patch("yaml.safe_load", return_value=config):
            with patch("src.cli.PolymarketScanner") as MockScanner:
                mock_scanner = MagicMock()
                mock_scanner.scan_markets.return_value = []
                MockScanner.return_value = mock_scanner

                alerts, summary = run_scan()

    assert alerts == []
    assert "No se encontraron" in summary


# ---------------------------------------------------------------------------
# main  (subparser CLI)
# ---------------------------------------------------------------------------

def test_main_scan_calls_run_scan():
    """main with 'scan' subcommand should delegate to run_scan."""
    fake_config = {"alerter": {}, "tracker": {"db_path": ":memory:"},
                   "scanner": {}, "signals": {}, "scorer": {}}
    with patch("sys.argv", ["cli.py", "scan", "--config", "my_config.yaml"]):
        with patch("src.cli.load_config", return_value=fake_config):
            with patch("src.cli.run_scan", return_value=(["alert A", "alert B"], "2 trades placed")) as mock_run:
                with patch("builtins.print") as mock_print:
                    main()

    mock_run.assert_called_once_with("my_config.yaml")
    assert mock_print.call_count >= 2


def test_main_scan_default_shows_help():
    """When no subcommand given, prints help (no default scan anymore)."""
    with patch("sys.argv", ["cli.py"]):
        with patch("builtins.print") as mock_print:
            main()

    # Should print help message
    assert mock_print.call_count >= 1


def test_main_unknown_command():
    """Unknown command exits with error (argparse SystemExit)."""
    with patch("sys.argv", ["cli.py", "report"]):
        with pytest.raises(SystemExit):
            main()
