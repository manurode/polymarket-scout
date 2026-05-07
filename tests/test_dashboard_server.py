"""
Tests for dashboard FastAPI server.

Tests cover REST endpoints and server configuration.
SSE streaming tests are marked as expected-to-fail pending httpx/SSE integration fix.
"""

import pytest
import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

from src.degradation import SystemMode


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_orchestrator():
    """Mock orchestrator with minimal system status."""
    mock = MagicMock()
    mock.get_system_status.return_value = {
        "mode": "full",
        "degradation_metrics": {
            "mode": "full",
            "can_trade": True,
            "component_health": {
                "clob_ws": True,
                "redis": True,
            },
        },
        "tracked_markets_book": 50,
        "tracked_markets_trades": 50,
        "portfolio_epoch": 3,
        "active_strategies": ["momentum_follow", "market_making"],
        "alpha_whales": 12,
        "websocket_connected": True,
    }
    mock.rate_limiter = MagicMock()
    mock.rate_limiter.get_all_budgets.return_value = {
        "reconciliation": {"available": 70, "total": 100},
        "onboarding": {"available": 20, "total": 100},
        "ad_hoc": {"available": 10, "total": 100},
    }
    mock.degradation = MagicMock()
    mock.degradation.get_mode.return_value = SystemMode.FULL
    mock.degradation.state = MagicMock()
    mock.degradation.state.can_trade = True
    mock.degradation.get_degradation_metrics.return_value = {
        "mode": "full",
        "can_trade": True,
        "component_health": {
            "clob_ws": True,
            "redis": True,
        },
    }
    mock.portfolio_manager = MagicMock()
    mock.portfolio_manager.current_epoch = 3
    mock.portfolio_manager.get_strategy_rankings.return_value = [
        {"name": "corr_arb", "sortino": 3.21, "state": "active", "alloc_pct": 34},
        {"name": "whale_follow", "sortino": 2.45, "state": "active", "alloc_pct": 22},
    ]
    mock.portfolio_manager.get_allocation.return_value = {
        "active": 2847, "frozen": 1203, "retired": 0, "total_equity": 4050,
        "pnl_24h": 127, "pnl_24h_pct": 3.2, "max_drawdown": -89,
    }
    mock.whale_tracker = MagicMock()
    mock.whale_tracker.get_alpha_whales.return_value = []
    mock.whale_tracker.get_whale_flow.return_value = {}
    return mock


@pytest.fixture
def test_app(mock_orchestrator):
    """Create a test FastAPI app with mocked orchestrator."""
    from dashboard.server import create_app
    return create_app(orchestrator=mock_orchestrator)


def _make_client(app):
    """Create an httpx AsyncClient for testing."""
    from httpx import AsyncClient, ASGITransport
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# Tests: Health endpoint
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_health_returns_ok(self, test_app):
        async with _make_client(test_app) as client:
            r = await client.get("/api/health")
            assert r.status_code == 200
            data = r.json()
            assert data["status"] == "ok"
            assert "uptime_seconds" in data

    @pytest.mark.asyncio
    async def test_health_includes_timestamp_utc(self, test_app):
        async with _make_client(test_app) as client:
            r = await client.get("/api/health")
            data = r.json()
            ts = datetime.fromisoformat(data["timestamp"])
            assert ts.tzinfo is not None


# ---------------------------------------------------------------------------
# Tests: System status endpoint
# ---------------------------------------------------------------------------

class TestSystemStatusEndpoint:
    @pytest.mark.asyncio
    async def test_returns_mode_and_epoch(self, test_app):
        async with _make_client(test_app) as client:
            r = await client.get("/api/system/status")
            assert r.status_code == 200
            data = r.json()
            assert data["mode"] == "full"
            assert data["portfolio_epoch"] == 3

    @pytest.mark.asyncio
    async def test_includes_heartbeats(self, test_app):
        async with _make_client(test_app) as client:
            r = await client.get("/api/system/status")
            data = r.json()
            assert "heartbeats" in data
            assert "clob_ws" in data["heartbeats"]


# ---------------------------------------------------------------------------
# Tests: Portfolio endpoints
# ---------------------------------------------------------------------------

class TestPortfolioEndpoints:
    @pytest.mark.asyncio
    async def test_strategies_returns_list(self, test_app):
        async with _make_client(test_app) as client:
            r = await client.get("/api/portfolio/strategies")
            assert r.status_code == 200
            data = r.json()
            assert isinstance(data, list)
            if data:
                assert "name" in data[0]
                assert "sortino" in data[0]

    @pytest.mark.asyncio
    async def test_allocation_has_equity(self, test_app):
        async with _make_client(test_app) as client:
            r = await client.get("/api/portfolio/allocation")
            assert r.status_code == 200
            data = r.json()
            assert "total_equity" in data
            assert "active" in data


# ---------------------------------------------------------------------------
# Tests: Rate-limit endpoints
# ---------------------------------------------------------------------------

class TestRateLimitEndpoints:
    @pytest.mark.asyncio
    async def test_rate_limits_returns_budgets(self, test_app):
        async with _make_client(test_app) as client:
            r = await client.get("/api/system/rate-limits")
            assert r.status_code == 200
            data = r.json()
            assert "reconciliation" in data
            assert isinstance(data["reconciliation"], dict)


# ---------------------------------------------------------------------------
# Tests: Whales endpoint
# ---------------------------------------------------------------------------

class TestWhalesEndpoint:
    @pytest.mark.asyncio
    async def test_whales_returns_data(self, test_app):
        async with _make_client(test_app) as client:
            r = await client.get("/api/whales")
            assert r.status_code == 200
            data = r.json()
            assert "alpha_whales" in data
            assert "whale_flow" in data


# ---------------------------------------------------------------------------
# Tests: CORS headers
# ---------------------------------------------------------------------------

class TestCORS:
    @pytest.mark.asyncio
    async def test_preflight_returns_200(self, test_app):
        async with _make_client(test_app) as client:
            r = await client.options(
                "/api/health",
                headers={
                    "Origin": "http://localhost:5173",
                    "Access-Control-Request-Method": "GET",
                }
            )
            assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_response_has_allow_origin(self, test_app):
        async with _make_client(test_app) as client:
            r = await client.get(
                "/api/health",
                headers={"Origin": "http://localhost:5173"}
            )
            assert "access-control-allow-origin" in r.headers


# ---------------------------------------------------------------------------
# Tests: API docs
# ---------------------------------------------------------------------------

class TestAPIDocs:
    @pytest.mark.asyncio
    async def test_docs_endpoint_accessible(self, test_app):
        async with _make_client(test_app) as client:
            r = await client.get("/api/docs")
            assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_openapi_schema_valid(self, test_app):
        async with _make_client(test_app) as client:
            r = await client.get("/openapi.json")
            assert r.status_code == 200
            schema = r.json()
            assert schema["info"]["title"] == "Scout Lab v2.0 Dashboard"
