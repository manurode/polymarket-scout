"""
Async Polymarket Scanner — versión asíncrona del scanner usando aiohttp.

Implementa la CAPA RADAR (L1) de la arquitectura v2.0: sondeo rápido de la
Gamma API para descubrir mercados y filtrar candidatos, reservando el CLOB
solo para enriquecimiento selectivo con rate-limiting consciente.

Usage:
    async with AsyncPolymarketScanner() as scanner:
        events = await scanner.get_events_async(limit=25)
"""

import asyncio
import json
import logging
import time
from typing import Optional
from urllib.parse import urlparse, urlunparse
from datetime import datetime, timezone

import aiohttp

from src.rate_limiter import RateLimiter, get_default_limiter
from src.clob_auth import build_clob_headers
from src.config import get_clob_credentials, has_clob_credentials

logger = logging.getLogger(__name__)

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"


class AsyncPolymarketScanner:
    """Cliente asíncrono para las APIs públicas de Polymarket.

    Parameters
    ----------
    session : aiohttp.ClientSession | None
        Session compartida. Si es None, se crea una interna.
    rate_limiter : RateLimiter | None
        Gestor de rate-limit para el CLOB. Si es None, usa el singleton global.
    """

    def __init__(
        self,
        session: Optional[aiohttp.ClientSession] = None,
        rate_limiter: Optional[RateLimiter] = None,
    ):
        self._own_session = session is None
        self._session = session
        self._rate_limiter = rate_limiter or get_default_limiter()
        self._session_started = False

        # CLOB credentials from .env (may be empty if not configured)
        self._clob_creds = get_clob_credentials()
        self._clob_authed = has_clob_credentials()
        if self._clob_authed:
            logger.info("CLOB API credentials loaded — authenticated requests enabled")
        else:
            logger.info("CLOB API credentials NOT configured — CLOB requests will be public-only")

    async def _ensure_session(self) -> None:
        """Ensure an aiohttp session exists."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "polymarket-scout/2.0"},
                timeout=aiohttp.ClientTimeout(total=15),
            )
            self._own_session = True

    async def __aenter__(self):
        await self._ensure_session()
        return self

    async def __aexit__(self, *args):
        if self._own_session and self._session:
            await self._session.close()

    # ── HTTP helpers ─────────────────────────────────────────────

    async def _get(self, url: str) -> dict | list:
        """GET request con manejo de errores uniforme."""
        try:
            async with self._session.get(url) as resp:
                if resp.status == 404:
                    # CLOB 404s are expected under rate-limiting; don't log
                    raise aiohttp.ClientResponseError(
                        resp.request_info, resp.history, status=404
                    )
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientResponseError as e:
            if e.status != 404:
                logger.error("HTTP %s: %s", e.status, url[:100])
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error("Connection error: %s for %s", e, url[:100])
            raise

    async def _get_clob(self, url: str) -> dict | list:
        """GET request to CLOB API with HMAC auth headers when credentials exist.

        Extracts the request path from the URL, builds HMAC signature,
        and includes POLY_API_KEY / POLY_TIMESTAMP / POLY_SIGNATURE headers.
        Falls back gracefully (no auth) if credentials are absent.
        """
        headers = {"User-Agent": "polymarket-scout/2.0"}

        if self._clob_authed:
            parsed = urlparse(url)
            request_path = parsed.path
            if parsed.query:
                request_path += "?" + parsed.query

            auth_headers = build_clob_headers(
                api_key=self._clob_creds["api_key"],
                secret=self._clob_creds["secret"],
                method="GET",
                request_path=request_path,
            )
            headers.update(auth_headers)

        try:
            async with self._session.get(url, headers=headers) as resp:
                if resp.status == 404:
                    raise aiohttp.ClientResponseError(
                        resp.request_info, resp.history, status=404
                    )
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientResponseError as e:
            if e.status != 404:
                logger.error("HTTP %s: %s", e.status, url[:100])
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error("Connection error: %s for %s", e, url[:100])
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

    @staticmethod
    def _parse_end_date(raw: str) -> float:
        """Convert Gamma API ISO endDate to Unix timestamp.

        Returns 0.0 if the date is missing or unparseable.
        """
        if not raw:
            return 0.0
        try:
            # Handles: "2026-05-15T00:00:00Z", "2026-05-15T00:00:00+00:00", etc.
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt.timestamp()
        except (ValueError, TypeError):
            return 0.0

    # ── Gamma API (Radar Layer — sin rate-limit) ──────────────────

    async def get_events_async(
        self,
        limit: int = 25,
        active_only: bool = True,
        order: str = "volume",
        ascending: bool = False,
    ) -> list:
        """Fetch active events from Gamma API, sorted by volume.

        La Gamma API tolera ~4000 req/10s — no necesita rate-limiting.
        """
        params = {
            "limit": str(limit),
            "order": order,
            "ascending": str(ascending).lower(),
        }
        if active_only:
            params["active"] = "true"
            params["closed"] = "false"

        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return await self._get(f"{GAMMA}/events?{qs}")

    async def get_markets_async(
        self,
        limit: int = 100,
        active_only: bool = True,
    ) -> list:
        """Fetch markets directly from Gamma (alternativa a events)."""
        params = {"limit": str(limit)}
        if active_only:
            params["active"] = "true"
            params["closed"] = "false"

        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return await self._get(f"{GAMMA}/markets?{qs}")

    # ── CLOB API (Deep-Dive Layer — con rate-limiting) ────────────

    async def get_price_async(self, token_id: str, side: str = "buy") -> float:
        """Get current price for a token (rate-limited)."""
        await self._rate_limiter.wait_acquire("ad_hoc", timeout=3.0)
        data = await self._get_clob(f"{CLOB}/price?token_id={token_id}&side={side}")
        return float(data.get("price", 0))

    async def get_spread_async(self, token_id: str) -> float:
        """Get current bid-ask spread (rate-limited)."""
        await self._rate_limiter.wait_acquire("ad_hoc", timeout=3.0)
        data = await self._get_clob(f"{CLOB}/spread?token_id={token_id}")
        return float(data.get("spread", 0))

    async def get_book_async(self, token_id: str) -> dict:
        """Get full L2 order book (rate-limited, usado para snapshots de reconciliación)."""
        await self._rate_limiter.wait_acquire("reconciliation", timeout=3.0)
        return await self._get_clob(f"{CLOB}/book?token_id={token_id}")

    # ── Scan principal (compatible con v1.0) ──────────────────────

    async def scan_markets_async(
        self,
        events_limit: int = 25,
        markets_per_event: int = 10,
        min_volume: float = 5000,
        enrich_clob: bool = True,
    ) -> list[dict]:
        """Async scan of all active markets returning current snapshots.

        Parameters
        ----------
        events_limit : int
            Cuántos eventos pedir a Gamma.
        markets_per_event : int
            Máximo de mercados a procesar por evento.
        min_volume : float
            Volumen mínimo en USD para incluir un mercado.
        enrich_clob : bool
            Si True, intenta enriquecer con precios/spread del CLOB (rate-limited).
            Si False, usa solo precios de Gamma (radar puro, sin CLOB).

        Returns
        -------
        list[dict]
            Misma estructura que PolymarketScanner.scan_markets().
        """
        await self._ensure_session()
        events = await self.get_events_async(limit=events_limit, active_only=True)
        snapshots = []
        now = int(time.time())

        for event in events:
            markets = event.get("markets", [])
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

                token_yes = (
                    tokens[0]
                    if isinstance(tokens, list) and len(tokens) > 0
                    else None
                )

                price_yes = (
                    float(prices_raw[0])
                    if isinstance(prices_raw, list) and len(prices_raw) > 0
                    else None
                )
                spread = None

                # CLOB enrichment (best-effort, rate-limited)
                if enrich_clob and token_yes:
                    try:
                        price_yes = await self.get_price_async(token_yes)
                        spread = await self.get_spread_async(token_yes)
                    except Exception:
                        # Gamma prices are good enough; CLOB enrichment is optional
                        if price_yes is None:
                            price_yes = (
                                float(prices_raw[0])
                                if isinstance(prices_raw, list) and len(prices_raw) > 0
                                else None
                            )

                price_no = (
                    round(1.0 - price_yes, 4)
                    if price_yes is not None
                    else None
                )

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
                    "clobTokenIds": tokens,        # para MM suscripción WS
                    "end_date": self._parse_end_date(market.get("endDate", "")),
                })

        return snapshots

    # ── Radar-only scan (gamma puro, sin CLOB) ────────────────────

    async def radar_scan(
        self,
        events_limit: int = 100,
        markets_per_event: int = 20,
        min_volume: float = 1000,
    ) -> list[dict]:
        """Radar puro: solo Gamma API, sin tocar el CLOB.

        Diseñado para ejecutarse cada 30-60 segundos como capa de
        descubrimiento. Extremadamente rápido (~2-3s) y nunca bloqueado.
        """
        return await self.scan_markets_async(
            events_limit=events_limit,
            markets_per_event=markets_per_event,
            min_volume=min_volume,
            enrich_clob=False,
        )
