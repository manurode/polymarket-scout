"""Scorer — compute a composite score from trading signals."""

import json
import logging

logger = logging.getLogger(__name__)

# (signal_value_key, threshold, inverted)
# inverted=True means *lower* values are more intense.
INTENSITY_THRESHOLDS: dict[str, tuple[str, float, bool]] = {
    "momentum_up": ("change_pct", 0.05, False),
    "momentum_down": ("change_pct", 0.05, False),
    "volume_spike": ("ratio", 3.0, False),
    "spread_tight": ("spread", 0.03, True),
    "spread_wide": ("spread", 0.10, False),
    "new_interest": ("volume", 10000, False),
}

MAX_SCORE = 100


def calculate_score(signals: list[dict]) -> tuple[int, str]:
    """Calculate a composite score (0-100) from a list of signal dicts.

    Each signal must carry at least ``signal_type``, ``weight``, and the
    value key listed in :data:`INTENSITY_THRESHOLDS`.

    Returns ``(score, json_detail)`` where *json_detail* is a JSON
    string describing each signal's contribution.
    """
    if not signals:
        return 0, "{}"

    detail_rows: list[dict] = []
    total = 0

    for sig in signals:
        stype = sig.get("signal_type", "unknown")
        weight = sig.get("weight", 0)

        # Determine intensity
        info = INTENSITY_THRESHOLDS.get(stype)
        if info is None:
            # Unknown signal type → intensity = 1.0
            intensity = 1.0
        else:
            key, threshold, inverted = info
            value = sig.get(key, 0)
            if threshold == 0:
                intensity = 0.0
            elif inverted:
                # Lower value → more intense; clamp at 1.0
                intensity = min(1.0, threshold / abs(value)) if value != 0 else 1.0
            else:
                intensity = min(1.0, abs(value) / threshold)

        contribution = min(weight, int(weight * intensity))

        detail_rows.append({
            "signal_type": stype,
            "weight": weight,
            "intensity": round(intensity, 4),
            "contribution": contribution,
        })

        total += contribution

    capped = min(total, MAX_SCORE)
    detail_json = json.dumps(detail_rows)

    logger.debug("Score: %d/%d from %d signals", capped, MAX_SCORE, len(signals))
    return capped, detail_json
