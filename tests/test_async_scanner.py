"""Tests para AsyncPolymarketScanner — Radar Layer asíncrono."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import aiohttp
from src.async_scanner import AsyncPolymarketScanner
from src.rate_limiter import RateLimiter


# ── Helpers ───────────────────────────────────────────────────────

class _MockResponse:
    """Respuesta HTTP mockeada que soporta `async with` correctamente.

    En aiohttp, ``session.get()`` es una coroutine que retorna un
    ``ClientResponse``, el cual actúa como async context manager
    (retornándose a sí mismo al entrar).
    """

    def __init__(self, status=200, json_data=None, raise_exc=None):
        self.status = status
        self._json_data = json_data
        self._raise_exc = raise_exc

    async def json(self):
        if self._raise_exc:
            raise self._raise_exc
        return self._json_data

    def raise_for_status(self):
        if self._raise_exc:
            raise self._raise_exc

    async def __aenter__(self):
        if self._raise_exc:
            raise self._raise_exc
        return self

    async def __aexit__(self, *args):
        pass

class _MockSession:
    """Session HTTP mockeada.

    En aiohttp, ``session.get(url)`` NO es ``async def`` — retorna un
    objeto ``_RequestContextManager`` con ``__aenter__``/``__aexit__``.
    """

    def __init__(self):
        self._responses = []
        self._default = _MockResponse(status=200, json_data=[])
        self._call_count = 0

    def set_responses(self, responses: list):
        """Configura la secuencia de respuestas que devolverá get()."""
        self._responses = list(responses)
        self._call_count = 0

    def get(self, url, **kwargs):
        """Retorna la siguiente respuesta mockeada como async context manager."""
        if self._call_count < len(self._responses):
            resp = self._responses[self._call_count]
            self._call_count += 1
            return resp
        return self._default

    async def close(self):
        pass


# ── Fixtures ─────────────────────────────────────────────────────

@pytest.fixture
def mock_events_response():
    """Simula la respuesta de Gamma /events."""
    return [
        {
            "id": "evt-001",
            "title": "Evento de Prueba",
            "slug": "evento-de-prueba",
            "volume": 500000.0,
            "markets": [
                {
                    "question": "¿Ganará el equipo A?",
                    "slug": "ganara-equipo-a",
                    "conditionId": "0xabc123",
                    "outcomePrices": '["0.65", "0.35"]',
                    "outcomes": '["Yes", "No"]',
                    "clobTokenIds": '["token_yes_001", "token_no_001"]',
                    "volume": 100000.0,
                    "liquidity": 25000.0,
                },
                {
                    "question": "¿Lloverá mañana?",
                    "slug": "llovera-manana",
                    "conditionId": "0xdef456",
                    "outcomePrices": '["0.30", "0.70"]',
                    "outcomes": '["Yes", "No"]',
                    "clobTokenIds": '["token_yes_002", "token_no_002"]',
                    "volume": 50000.0,
                    "liquidity": 10000.0,
                },
            ],
        }
    ]


@pytest.fixture
def mock_clob_price_response():
    """Simula respuesta de CLOB /price."""
    return {"price": "0.66"}


@pytest.fixture
def mock_clob_spread_response():
    """Simula respuesta de CLOB /spread."""
    return {"spread": "0.02"}


@pytest.fixture
def mock_book_response():
    """Simula respuesta de CLOB /book."""
    return {
        "market": "0xabc123",
        "asset_id": "token_yes_001",
        "bids": [{"price": "0.64", "size": "500"}],
        "asks": [{"price": "0.66", "size": "300"}],
        "min_order_size": "5",
        "tick_size": "0.01",
    }


@pytest.fixture
def fast_limiter():
    """RateLimiter sin límites para tests."""
    return RateLimiter(total_rate=100.0, max_burst=100.0)


# ── Constructor / Context Manager ─────────────────────────────────

@pytest.mark.asyncio
async def test_context_manager_creates_and_closes_session():
    """El context manager crea y cierra la session automáticamente."""
    async with AsyncPolymarketScanner() as scanner:
        assert scanner._session is not None
        assert not scanner._session.closed
    assert scanner._session.closed


@pytest.mark.asyncio
async def test_accepts_external_session():
    """Acepta una session externa y no la cierra."""
    session = aiohttp.ClientSession()
    scanner = AsyncPolymarketScanner(session=session)
    assert scanner._session is session
    assert not scanner._own_session
    await session.close()


# ── parse_json_field ──────────────────────────────────────────────

def test_parse_json_field_string():
    """Convierte strings JSON a objetos Python."""
    result = AsyncPolymarketScanner.parse_json_field('["0.65", "0.35"]')
    assert result == ["0.65", "0.35"]


def test_parse_json_field_already_parsed():
    """No modifica valores que ya son listas."""
    result = AsyncPolymarketScanner.parse_json_field(["a", "b"])
    assert result == ["a", "b"]


def test_parse_json_field_invalid_json():
    """Retorna el string original si no es JSON válido."""
    result = AsyncPolymarketScanner.parse_json_field("not json")
    assert result == "not json"


# ── get_events_async ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_events_returns_list(mock_events_response):
    """get_events_async retorna la respuesta parseada de Gamma."""
    scanner = AsyncPolymarketScanner()
    scanner._own_session = False
    session = _MockSession()
    session.set_responses([_MockResponse(status=200, json_data=mock_events_response)])
    scanner._session = session

    events = await scanner.get_events_async(limit=25)
    assert len(events) == 1
    assert events[0]["title"] == "Evento de Prueba"


# ── scan_markets_async (Radar puro, sin CLOB) ─────────────────────

@pytest.mark.asyncio
async def test_scan_markets_radar_only(mock_events_response):
    """scan con enrich_clob=False solo usa Gamma (sin tocar CLOB)."""
    scanner = AsyncPolymarketScanner()
    scanner._own_session = False
    session = _MockSession()
    session.set_responses([_MockResponse(status=200, json_data=mock_events_response)])
    scanner._session = session

    snapshots = await scanner.scan_markets_async(
        events_limit=10,
        markets_per_event=5,
        enrich_clob=False,
    )

    assert len(snapshots) == 2
    assert snapshots[0]["price_yes"] == 0.65
    assert snapshots[0]["price_no"] == 0.35
    assert snapshots[0]["spread"] is None
    assert snapshots[1]["price_yes"] == 0.30


# ── scan_markets_async (con CLOB enrichment) ──────────────────────

@pytest.mark.asyncio
async def test_scan_markets_with_clob(
    mock_events_response,
    mock_clob_price_response,
    mock_clob_spread_response,
    fast_limiter,
):
    """scan con enrich_clob=True obtiene precios y spread del CLOB."""
    scanner = AsyncPolymarketScanner(rate_limiter=fast_limiter)
    scanner._own_session = False
    session = _MockSession()
    session.set_responses([
        _MockResponse(status=200, json_data=mock_events_response),  # Gamma
        _MockResponse(status=200, json_data=mock_clob_price_response),  # price mkt1
        _MockResponse(status=200, json_data=mock_clob_spread_response),  # spread mkt1
        _MockResponse(status=200, json_data=mock_clob_price_response),  # price mkt2
        _MockResponse(status=200, json_data=mock_clob_spread_response),  # spread mkt2
    ])
    scanner._session = session

    snapshots = await scanner.scan_markets_async(
        events_limit=10,
        markets_per_event=5,
        enrich_clob=True,
    )

    assert len(snapshots) == 2
    assert snapshots[0]["price_yes"] == 0.66
    assert snapshots[0]["spread"] == 0.02


# ── radar_scan ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_radar_scan_is_enrich_clob_false(mock_events_response):
    """radar_scan() es un wrapper de scan_markets_async con enrich_clob=False."""
    scanner = AsyncPolymarketScanner()
    scanner._own_session = False
    session = _MockSession()
    session.set_responses([_MockResponse(status=200, json_data=mock_events_response)])
    scanner._session = session

    snapshots = await scanner.radar_scan(events_limit=50)
    assert len(snapshots) == 2
    assert all(s["spread"] is None for s in snapshots)


# ── CLOB fallback ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scan_falls_back_to_gamma_on_clob_error(mock_events_response):
    """Si el CLOB falla, se usan los precios de Gamma sin crash."""
    scanner = AsyncPolymarketScanner()
    scanner._own_session = False

    clob_err = aiohttp.ClientResponseError(
        MagicMock(), (), status=404, message="Not Found"
    )
    session = _MockSession()
    session.set_responses([
        _MockResponse(status=200, json_data=mock_events_response),  # Gamma OK
        _MockResponse(status=404, raise_exc=clob_err),  # price mkt1 FAIL
        _MockResponse(status=404, raise_exc=clob_err),  # spread mkt1 FAIL
        _MockResponse(status=404, raise_exc=clob_err),  # price mkt2 FAIL
        _MockResponse(status=404, raise_exc=clob_err),  # spread mkt2 FAIL
    ])
    scanner._session = session

    snapshots = await scanner.scan_markets_async(
        events_limit=10,
        enrich_clob=True,
    )

    assert len(snapshots) == 2
    assert snapshots[0]["price_yes"] == 0.65  # fallback a Gamma
    assert snapshots[0]["spread"] is None


# ── min_volume filter ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_min_volume_filters_low_volume_markets(mock_events_response):
    """Mercados con volumen < min_volume no se incluyen."""
    scanner = AsyncPolymarketScanner()
    scanner._own_session = False
    session = _MockSession()
    session.set_responses([_MockResponse(status=200, json_data=mock_events_response)])
    scanner._session = session

    snapshots = await scanner.scan_markets_async(
        events_limit=10,
        min_volume=75000,
        enrich_clob=False,
    )

    assert len(snapshots) == 1
    assert snapshots[0]["volume"] == 100000.0
