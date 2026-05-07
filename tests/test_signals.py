"""Tests for signal detectors."""

import time
import pytest

from src.signals import (
    detect_momentum,
    detect_volume_spike,
    detect_spread_anomaly,
    detect_all,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _snap(price_yes, volume=50000, spread=0.05, timestamp=None):
    """Build a minimal snapshot dict."""
    if timestamp is None:
        timestamp = int(time.time())
    return {
        "condition_id": "0xtest",
        "question": "Test market?",
        "slug": "test-market",
        "event_title": "Test Event",
        "price_yes": price_yes,
        "price_no": round(1.0 - price_yes, 4) if price_yes is not None else None,
        "spread": spread,
        "volume": volume,
        "liquidity": 10000.0,
        "timestamp": timestamp,
    }


def _snap_at(price_yes, volume, spread, ts):
    """Build a snapshot with an explicit timestamp."""
    return _snap(price_yes, volume, spread, timestamp=ts)


# ---------------------------------------------------------------------------
# detect_momentum
# ---------------------------------------------------------------------------

class TestDetectMomentum:
    def test_detect_momentum_up(self):
        """Prices [0.50, 0.55, 0.60] — 20% rise triggers momentum_up."""
        now = int(time.time())
        snapshots = [
            _snap_at(0.50, 10000, 0.05, now - 2000),
            _snap_at(0.55, 10000, 0.05, now - 1000),
            _snap_at(0.60, 10000, 0.05, now),
        ]
        result = detect_momentum(snapshots, threshold=0.05)
        assert result is not None
        assert result["signal_type"] == "momentum_up"
        assert result["change_pct"] == pytest.approx(0.2, abs=0.01)
        assert result["price_start"] == 0.50
        assert result["price_end"] == 0.60

    def test_detect_momentum_down(self):
        """Prices [0.60, 0.55, 0.50] — 16.7% drop triggers momentum_down."""
        now = int(time.time())
        snapshots = [
            _snap_at(0.60, 10000, 0.05, now - 2000),
            _snap_at(0.55, 10000, 0.05, now - 1000),
            _snap_at(0.50, 10000, 0.05, now),
        ]
        result = detect_momentum(snapshots, threshold=0.05)
        assert result is not None
        assert result["signal_type"] == "momentum_down"
        # (0.50 - 0.60) / 0.60 ≈ -0.1667
        assert result["change_pct"] == pytest.approx(-0.1667, abs=0.001)
        assert result["price_start"] == 0.60
        assert result["price_end"] == 0.50

    def test_detect_momentum_no_change(self):
        """Prices [0.60, 0.61] — only 1.67% change, below 5% threshold."""
        now = int(time.time())
        snapshots = [
            _snap_at(0.60, 10000, 0.05, now - 100),
            _snap_at(0.61, 10000, 0.05, now),
        ]
        result = detect_momentum(snapshots, threshold=0.05)
        assert result is None

    def test_detect_momentum_insufficient_data(self):
        """Only 1 snapshot should return None."""
        snapshots = [_snap(0.50)]
        result = detect_momentum(snapshots, threshold=0.05)
        assert result is None

    def test_detect_momentum_outside_window(self):
        """Snapshots outside the lookback window yield None."""
        now = int(time.time())
        snapshots = [
            _snap_at(0.50, 10000, 0.05, now - 7200),
            _snap_at(0.60, 10000, 0.05, now - 3600),
        ]
        # Window of 1800 s from latest (now-3600) → cutoff = now-5400.
        # Only the second snapshot is inside → 1 snapshot → None.
        result = detect_momentum(snapshots, window_seconds=1800)
        assert result is None

    def test_detect_momentum_zero_price(self):
        """price_start == 0 should return None."""
        now = int(time.time())
        snapshots = [
            _snap_at(0.0, 10000, 0.05, now - 100),
            _snap_at(0.50, 10000, 0.05, now),
        ]
        result = detect_momentum(snapshots, threshold=0.05)
        assert result is None


# ---------------------------------------------------------------------------
# detect_volume_spike
# ---------------------------------------------------------------------------

class TestDetectVolumeSpike:
    def test_detect_volume_spike(self):
        """3 normal volumes + 1 spike — ratio > 3.0 triggers volume_spike."""
        now = int(time.time())
        snapshots = [
            _snap_at(0.50, 100_000, 0.05, now - 3000),
            _snap_at(0.50, 110_000, 0.05, now - 2000),
            _snap_at(0.50, 120_000, 0.05, now - 1000),
            _snap_at(0.50, 500_000, 0.05, now),
        ]
        result = detect_volume_spike(snapshots, threshold=3.0)
        assert result is not None
        assert result["signal_type"] == "volume_spike"
        assert result["volume_now"] == 500_000
        # avg of [100k, 110k, 120k] = 110k → ratio ≈ 4.545
        assert result["ratio"] == pytest.approx(4.545, abs=0.01)

    def test_detect_volume_spike_no_spike(self):
        """Normal volumes — ratio < 3.0 returns None."""
        now = int(time.time())
        snapshots = [
            _snap_at(0.50, 100_000, 0.05, now - 2000),
            _snap_at(0.50, 110_000, 0.05, now - 1000),
            _snap_at(0.50, 120_000, 0.05, now),
        ]
        result = detect_volume_spike(snapshots, threshold=3.0)
        assert result is None

    def test_detect_volume_spike_insufficient_data(self):
        """Only 1 snapshot should return None."""
        snapshots = [_snap(0.50, volume=500_000)]
        result = detect_volume_spike(snapshots)
        assert result is None


# ---------------------------------------------------------------------------
# detect_spread_anomaly
# ---------------------------------------------------------------------------

class TestDetectSpreadAnomaly:
    def test_detect_spread_tight(self):
        """Spread 0.01 < 0.03 → spread_tight."""
        snapshots = [_snap(0.50, spread=0.01)]
        result = detect_spread_anomaly(snapshots, tight_threshold=0.03)
        assert result is not None
        assert result["signal_type"] == "spread_tight"
        assert result["spread"] == 0.01

    def test_detect_spread_wide(self):
        """Spread 0.15 > 0.10 → spread_wide."""
        snapshots = [_snap(0.50, spread=0.15)]
        result = detect_spread_anomaly(snapshots, wide_threshold=0.10)
        assert result is not None
        assert result["signal_type"] == "spread_wide"
        assert result["spread"] == 0.15

    def test_detect_spread_normal(self):
        """Spread 0.05 is between thresholds → None."""
        snapshots = [_snap(0.50, spread=0.05)]
        result = detect_spread_anomaly(snapshots)
        assert result is None

    def test_detect_spread_none(self):
        """Spread is None → None."""
        snapshots = [_snap(0.50)]
        snapshots[0]["spread"] = None
        result = detect_spread_anomaly(snapshots)
        assert result is None

    def test_detect_spread_empty(self):
        """Empty snapshots list → None."""
        result = detect_spread_anomaly([])
        assert result is None


# ---------------------------------------------------------------------------
# detect_all
# ---------------------------------------------------------------------------

class TestDetectAll:
    CONFIG = {
        "momentum": {"threshold": 0.05, "window_hours": 1},
        "volume_spike": {"threshold": 3.0, "window_hours": 24},
        "spread": {"tight_threshold": 0.03, "wide_threshold": 0.10},
        "new_interest": {"min_volume": 10000},
    }

    def test_detect_all_multiple_signals(self):
        """Snapshots with momentum + volume + spread changes."""
        now = int(time.time())
        snapshots = [
            _snap_at(0.50, 100_000, 0.05, now - 3000),
            _snap_at(0.55, 110_000, 0.05, now - 2000),
            _snap_at(0.60, 120_000, 0.01, now - 1000),  # tight spread
            _snap_at(0.63, 500_000, 0.01, now),           # volume spike + momentum
        ]
        results = detect_all(snapshots, self.CONFIG)

        # At least 2 signals
        assert len(results) >= 2

        types = {r["signal_type"] for r in results}
        assert "momentum_up" in types
        assert "volume_spike" in types
        # spread_tight may or may not fire depending on exact values

        # Every result must have a weight
        for r in results:
            assert "weight" in r
            assert isinstance(r["weight"], int)

    def test_detect_all_no_signals(self):
        """Stable snapshots — no signals should fire."""
        now = int(time.time())
        snapshots = [
            _snap_at(0.50, 100_000, 0.05, now - 2000),
            _snap_at(0.50, 105_000, 0.05, now - 1000),
            _snap_at(0.50, 110_000, 0.05, now),
        ]
        results = detect_all(snapshots, self.CONFIG)
        assert results == []

    def test_detect_all_new_interest(self):
        """Single snapshot with high volume → new_interest."""
        snapshots = [_snap(0.55, volume=50_000)]
        results = detect_all(snapshots, self.CONFIG)
        assert len(results) == 1
        assert results[0]["signal_type"] == "new_interest"
        assert results[0]["volume"] == 50_000
        assert results[0]["weight"] == 10

    def test_detect_all_new_interest_low_volume(self):
        """Single snapshot below min_volume → no signal."""
        snapshots = [_snap(0.55, volume=5_000)]
        results = detect_all(snapshots, self.CONFIG)
        assert results == []
