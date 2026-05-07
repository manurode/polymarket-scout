"""Alerter — format and dispatch notifications."""

import json
import logging

logger = logging.getLogger(__name__)

SIGNAL_EMOJI = {
    "momentum_up": "\U0001f680",   # 🚀
    "momentum_down": "\U0001f4c9",  # 📉
    "volume_spike": "\U0001f4ca",   # 📊
    "spread_tight": "\U0001f91d",   # 🤝
    "spread_wide": "\U0001f308",    # 🌈
    "new_interest": "\U0001f195",   # 🆕
}


def should_alert(score: int, threshold: int = 60) -> bool:
    """Return True when *score* meets or exceeds *threshold*."""
    return score >= threshold


def _fmt_volume(volume: float) -> str:
    """Format a dollar volume compactly.

    * ≥ 1 000 000 → ``$X.XM``
    * ≥ 1 000     → ``$X.XK``
    * otherwise    → ``$X``
    """
    if volume >= 1_000_000:
        return f"${volume / 1_000_000:.1f}M"
    if volume >= 1_000:
        return f"${volume / 1_000:.1f}K"
    return f"${volume:,.0f}"


def _fmt_momentum(momentum_str: str) -> str:
    """Format a momentum string for display.

    If *momentum_str* looks like a percentage (e.g. ``"12.0"`` or
    ``"-5.0"``) it is prefixed with a sign and suffixed with ``%``.
    The default placeholder ``"—"`` is reproduced as-is.
    """
    if not momentum_str or momentum_str == "—":
        return "—"
    try:
        val = float(momentum_str)
        if val > 0:
            return f"+{val:.1f}%"
        return f"{val:.1f}%"
    except ValueError:
        return momentum_str


def format_alert(
    score: int,
    snapshot: dict,
    signals_detail: str,
    momentum_str: str = "—",
) -> str:
    """Build a Telegram-formatted alert message.

    Parameters
    ----------
    score : int
        Composite score (0-100).
    snapshot : dict
        The latest market snapshot with keys such as ``question``,
        ``price_yes``, ``volume``, ``spread``, ``event_title``, ``slug``.
    signals_detail : str
        JSON string produced by :func:`scorer.calculate_score`.
    momentum_str : str
        Pre-formatted momentum string (e.g. ``"+12.0%"``).
    """
    event_title = snapshot.get("event_title", "Unknown Event")
    question = snapshot.get("question", "Unknown market")
    slug = snapshot.get("slug", "")

    price_yes = snapshot.get("price_yes")
    price_pct = f"{price_yes * 100:.1f}%" if price_yes is not None else "—"

    volume = snapshot.get("volume", 0)
    volume_str = _fmt_volume(volume)

    spread = snapshot.get("spread")
    spread_str = f"{spread * 100:.1f}%" if spread is not None else "—"

    mom_str = _fmt_momentum(momentum_str)

    # Parse signals detail for signal list
    signals_list: list[str] = []
    try:
        details = json.loads(signals_detail)
        for d in details:
            stype = d.get("signal_type", "unknown")
            emoji = SIGNAL_EMOJI.get(stype, "")
            contribution = d.get("contribution", 0)
            signals_list.append(f"  {emoji} {stype} (+{contribution})")
    except (json.JSONDecodeError, TypeError):
        pass

    lines = [
        f"\U0001f514 **{score}/100 \u2014 {event_title}**",
        "",
        f"**{question}**",
        f"Price Yes: {price_pct}",
        f"Momentum: {mom_str}",
        f"Volume: {volume_str}",
        f"Spread: {spread_str}",
        "",
    ]

    if signals_list:
        lines.extend(signals_list)
    else:
        lines.append("  (no signals)")

    if slug:
        lines.append("")
        lines.append(f"[View on Polymarket](https://polymarket.com/event/{slug})")

    return "\n".join(lines)


def dispatch_alerts(alerts: list[str], platform: str = "telegram") -> int:
    """Log each alert message and return the count of dispatched alerts.

    Parameters
    ----------
    alerts : list[str]
        Pre-formatted alert messages (one per alert).
    platform : str
        Target platform identifier (default ``"telegram"``).
    """
    for alert in alerts:
        logger.info("Dispatching alert via %s:\n%s", platform, alert)
    return len(alerts)
