"""Strategies — trading decision functions that consume signals and snapshots."""

import logging

logger = logging.getLogger(__name__)

# Strategy registry: name → function
STRATEGIES: dict = {}


def register(name: str):
    """Decorator that registers a strategy function in the STRATEGIES dict."""
    def decorator(fn):
        STRATEGIES[name] = fn
        return fn
    return decorator


@register("momentum_follow")
def momentum_follow(signals: list[dict], snapshot: dict) -> dict | None:
    """Follow the momentum: if price is rising fast, bet YES. If falling, bet NO."""
    for s in signals:
        if s['signal_type'] == 'momentum_up':
            pct = s.get('change_pct', 0) * 100
            return {'action': 'YES', 'reason': f"momentum +{pct:.1f}%"}
        if s['signal_type'] == 'momentum_down':
            pct = s.get('change_pct', 0) * 100
            return {'action': 'NO', 'reason': f"momentum {pct:.1f}%"}
    return None


@register("contrarian")
def contrarian(signals: list[dict], snapshot: dict) -> dict | None:
    """Buy the dip: when price drops with high volume, bet YES (expecting rebound)."""
    has_momentum_down = any(s['signal_type'] == 'momentum_down' for s in signals)
    has_volume = any(s['signal_type'] == 'volume_spike' for s in signals)
    if has_momentum_down and has_volume:
        return {'action': 'YES', 'reason': 'dip + volume spike → expect rebound'}
    return None


@register("consensus_breakout")
def consensus_breakout(signals: list[dict], snapshot: dict) -> dict | None:
    """Tight spread + momentum up = strong consensus forming upward."""
    has_momentum_up = any(s['signal_type'] == 'momentum_up' for s in signals)
    has_tight = any(s['signal_type'] == 'spread_tight' for s in signals)
    if has_momentum_up and has_tight:
        return {'action': 'YES', 'reason': 'tight consensus + upward momentum'}
    return None


@register("volume_breakout")
def volume_breakout(signals: list[dict], snapshot: dict) -> dict | None:
    """High volume + wide spread = market discovering price → bet YES early."""
    has_volume = any(s['signal_type'] == 'volume_spike' for s in signals)
    has_wide = any(s['signal_type'] == 'spread_wide' for s in signals)
    if has_volume and has_wide:
        return {'action': 'YES', 'reason': 'volume + wide spread → price discovery'}
    return None


@register("new_market_yes")
def new_market_yes(signals: list[dict], snapshot: dict) -> dict | None:
    """New market with high initial volume → bet YES at low price (<0.50)."""
    has_new = any(s['signal_type'] == 'new_interest' for s in signals)
    price = snapshot.get('price_yes')
    if price is None:
        return None
    if has_new and price < 0.50:
        return {'action': 'YES', 'reason': f'new market at {price:.2f} → early entry'}
    return None
