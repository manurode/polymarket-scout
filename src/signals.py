"""Signal detectors — momentum, volume spike, spread anomaly, and combined detection."""

import logging

logger = logging.getLogger(__name__)


def detect_momentum(
    snapshots: list[dict],
    threshold: float = 0.05,
    window_seconds: int = 3600,
) -> dict | None:
    """Detect price momentum within a lookback window.

    Filters snapshots to those within *window_seconds* of the latest
    timestamp, then compares the first (oldest) and last (newest)
    ``price_yes`` values.  Returns a signal dict when the absolute
    percentage change meets or exceeds *threshold*.

    Returns ``None`` when:

    * fewer than 2 snapshots are in the window,
    * the price change is below the threshold,
    * ``price_start`` is 0 or ``None``.
    """
    if len(snapshots) < 2:
        return None

    # Sort by timestamp ascending to get a reliable first/last order.
    sorted_snaps = sorted(snapshots, key=lambda s: s.get("timestamp", 0))

    latest_ts = sorted_snaps[-1]["timestamp"]
    cutoff = latest_ts - window_seconds

    windowed = [s for s in sorted_snaps if s["timestamp"] >= cutoff]

    if len(windowed) < 2:
        return None

    price_start = windowed[0].get("price_yes")
    price_end = windowed[-1].get("price_yes")

    if price_start is None or price_start == 0:
        return None
    if price_end is None:
        return None

    change_pct = (price_end - price_start) / price_start

    if abs(change_pct) < threshold:
        return None

    signal_type = "momentum_up" if change_pct > 0 else "momentum_down"

    return {
        "signal_type": signal_type,
        "change_pct": round(change_pct, 4),
        "price_start": price_start,
        "price_end": price_end,
    }


def detect_volume_spike(
    snapshots: list[dict],
    threshold: float = 3.0,
) -> dict | None:
    """Detect a volume spike in the latest snapshot.

    Compares the most recent snapshot's volume against the average of all
    preceding snapshots.  A signal is emitted when the ratio
    ``volume_now / volume_avg`` reaches or exceeds *threshold*.

    Returns ``None`` when:

    * fewer than 2 snapshots are available,
    * ``volume_now`` ≤ 0, or
    * ``volume_avg`` ≤ 0.
    """
    if len(snapshots) < 2:
        return None

    # Sort by timestamp so the last element is the most recent.
    sorted_snaps = sorted(snapshots, key=lambda s: s.get("timestamp", 0))

    latest = sorted_snaps[-1]
    previous = sorted_snaps[:-1]

    vol_now = latest.get("volume", 0)
    if vol_now <= 0:
        return None

    vols = [s.get("volume", 0) for s in previous]
    vol_avg = sum(vols) / len(vols) if vols else 0
    if vol_avg <= 0:
        return None

    ratio = vol_now / vol_avg
    if ratio < threshold:
        return None

    return {
        "signal_type": "volume_spike",
        "ratio": round(ratio, 4),
        "volume_now": vol_now,
        "volume_avg": round(vol_avg, 2),
    }


def detect_spread_anomaly(
    snapshots: list[dict],
    tight_threshold: float = 0.03,
    wide_threshold: float = 0.10,
) -> dict | None:
    """Detect unusually tight or wide spreads in the latest snapshot.

    Returns a ``"spread_tight"`` signal when the spread is ≤
    *tight_threshold*, or a ``"spread_wide"`` signal when it is ≥
    *wide_threshold*.

    Returns ``None`` when:

    * *snapshots* is empty,
    * the latest spread is ``None``, or
    * the spread is within normal bounds.
    """
    if not snapshots:
        return None

    latest = snapshots[-1]
    spread = latest.get("spread")

    if spread is None:
        return None

    if spread <= tight_threshold:
        return {"signal_type": "spread_tight", "spread": spread}

    if spread >= wide_threshold:
        return {"signal_type": "spread_wide", "spread": spread}

    return None


def detect_all(snapshots: list[dict], config: dict) -> list[dict]:
    """Run every signal detector and return the active signals.

    Each returned dict includes a ``"weight"`` key:

    * momentum – 20
    * volume_spike – 20
    * spread_tight – 15
    * spread_wide – 10
    * new_interest – 10

    The *new_interest* detector fires when there is exactly one snapshot
    and its volume is at least ``config["new_interest"]["min_volume"]``.
    """
    results: list[dict] = []

    # Momentum
    mom = detect_momentum(
        snapshots,
        threshold=config["momentum"]["threshold"],
        window_seconds=config["momentum"]["window_hours"] * 3600,
    )
    if mom:
        mom["weight"] = 20
        results.append(mom)

    # Volume spike
    vol = detect_volume_spike(
        snapshots,
        threshold=config["volume_spike"]["threshold"],
    )
    if vol:
        vol["weight"] = 20
        results.append(vol)

    # Spread anomaly
    spread = detect_spread_anomaly(
        snapshots,
        tight_threshold=config["spread"]["tight_threshold"],
        wide_threshold=config["spread"]["wide_threshold"],
    )
    if spread:
        weight_map = {"spread_tight": 15, "spread_wide": 10}
        spread["weight"] = weight_map.get(spread["signal_type"], 10)
        results.append(spread)

    # New interest: only one snapshot with sufficient volume
    if len(snapshots) == 1:
        vol_val = snapshots[0].get("volume", 0)
        min_vol = config["new_interest"]["min_volume"]
        if vol_val >= min_vol:
            results.append({
                "signal_type": "new_interest",
                "volume": vol_val,
                "weight": 10,
            })

    return results
