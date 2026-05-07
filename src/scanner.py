"""Polymarket API scanner — read-only market data."""
import json
import time
import urllib.request
import urllib.parse
import urllib.error
import logging

logger = logging.getLogger(__name__)

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"


class PolymarketScanner:
    """Client for Polymarket public APIs."""

    def __init__(self):
        self.session_headers = {"User-Agent": "polymarket-scout/1.0"}

    def _get(self, url: str) -> dict | list:
        """GET request returning parsed JSON."""
        req = urllib.request.Request(url, headers=self.session_headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            logger.error(f"HTTP {e.code}: {e.reason} for {url}")
            raise
        except urllib.error.URLError as e:
            logger.error(f"Connection error: {e.reason} for {url}")
            raise

    @staticmethod
    def parse_json_field(val):
        """Parse double-encoded JSON fields (outcomePrices, outcomes, clobTokenIds)."""
        if isinstance(val, str):
            try:
                return json.loads(val)
            except (json.JSONDecodeError, TypeError):
                return val
        return val

    def get_events(self, limit: int = 25, active_only: bool = True,
                   order: str = "volume", ascending: bool = False) -> list:
        """Fetch active events from Gamma API, sorted by volume."""
        params = {
            "limit": limit,
            "order": order,
            "ascending": str(ascending).lower(),
        }
        if active_only:
            params["active"] = "true"
            params["closed"] = "false"

        qs = urllib.parse.urlencode(params)
        return self._get(f"{GAMMA}/events?{qs}")

    def get_price(self, token_id: str, side: str = "buy") -> float:
        """Get current price for a token. Returns 0.0–1.0."""
        data = self._get(f"{CLOB}/price?token_id={token_id}&side={side}")
        return float(data.get("price", 0))

    def get_spread(self, token_id: str) -> float:
        """Get current bid-ask spread."""
        data = self._get(f"{CLOB}/spread?token_id={token_id}")
        return float(data.get("spread", 0))

    def scan_markets(self, events_limit: int = 25, markets_per_event: int = 10,
                     min_volume: float = 5000) -> list[dict]:
        """Scan all active markets and return current snapshots."""
        events = self.get_events(limit=events_limit, active_only=True)
        snapshots = []
        now = int(time.time())

        for event in events:
            markets = event.get("markets", [])
            # Sort markets by volume descending and limit
            markets_sorted = sorted(
                markets,
                key=lambda m: float(m.get("volume", 0)),
                reverse=True,
            )[:markets_per_event]

            for market in markets_sorted:
                volume = float(market.get("volume", 0))
                if volume < min_volume:
                    continue

                tokens = self.parse_json_field(market.get("clobTokenIds", "[]"))
                prices_raw = self.parse_json_field(market.get("outcomePrices", "[]"))

                token_yes = tokens[0] if isinstance(tokens, list) and len(tokens) > 0 else None

                price_yes = float(prices_raw[0]) if isinstance(prices_raw, list) and len(prices_raw) > 0 else None
                spread = None

                if token_yes:
                    try:
                        price_yes = self.get_price(token_yes)
                        spread = self.get_spread(token_yes)
                    except Exception as e:
                        logger.warning(f"Failed to get price/spread for {token_yes}: {e}")

                # Compute price_no as complement (binary markets: YES + NO = 1.0)
                price_no = round(1.0 - price_yes, 4) if price_yes is not None else None

                snapshots.append({
                    "condition_id": market.get("conditionId", ""),
                    "question": market.get("question", ""),
                    "slug": market.get("slug", ""),
                    "event_title": event.get("title", ""),
                    "price_yes": price_yes,
                    "price_no": price_no,
                    "spread": spread,
                    "volume": volume,
                    "liquidity": float(market.get("liquidity", 0)),
                    "timestamp": now,
                })

        return snapshots
