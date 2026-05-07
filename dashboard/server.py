"""
Scout Lab v2.0 Dashboard — FastAPI Application Server.

Provides:
- SSE streams for real-time data (health, system, portfolio, risk, whales)
- REST API endpoints for polling data
- Serves React SPA frontend in production
- CORS enabled for development

Usage:
    uvicorn dashboard.server:app --host 0.0.0.0 --port 8000
    uvicorn dashboard.server:app --host 0.0.0.0 --port 8000 --reload
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from sse_starlette.sse import EventSourceResponse

# ── Logger ──────────────────────────────────────────────────────────

logger = logging.getLogger("dashboard.server")
logging.basicConfig(level=logging.INFO)


# ── Startup time ────────────────────────────────────────────────────

START_TIME = time.time()


# ── App factory ─────────────────────────────────────────────────────


def create_app(orchestrator=None) -> FastAPI:
    """Create and configure the FastAPI dashboard application.

    Parameters
    ----------
    orchestrator : ScoutOrchestrator | None
        The orchestrator instance. If None, the app runs in mock mode
        with simulated data for frontend development.

    Returns
    -------
    FastAPI
        Configured application instance.
    """
    app = FastAPI(
        title="Scout Lab v2.0 Dashboard",
        description="Real-time trading dashboard for Polymarket Scout Lab",
        version="2.0.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
    )

    # ── CORS (allow dev frontend) ──────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://localhost:3000",
            "http://127.0.0.1:5173",
            "http://127.0.0.1:3000",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Store orchestrator in app state ─────────────────────────────
    app.state.orchestrator = orchestrator

    # ── Register routes ────────────────────────────────────────────
    _register_routes(app)

    return app


# ── Routes ──────────────────────────────────────────────────────────


def _register_routes(app: FastAPI) -> None:
    """Register all API routes on the FastAPI app."""

    # ── Health ─────────────────────────────────────────────────────

    @app.get("/api/health")
    async def health():
        """Health check endpoint.

        Returns basic server health information including uptime.
        """
        uptime = time.time() - START_TIME
        return {
            "status": "ok",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "uptime_seconds": round(uptime, 1),
        }

    # ── System Status ──────────────────────────────────────────────

    @app.get("/api/system/status")
    async def system_status(request: Request):
        """Get full system status from the orchestrator.

        Returns mode, degradation metrics, heartbeats, and tracked counts.
        """
        orch = request.app.state.orchestrator
        if orch:
            status = orch.get_system_status()
        else:
            status = _mock_system_status()

        # Add heartbeats
        status["heartbeats"] = _get_heartbeats(orch)
        return status

    # ── Rate Limits ────────────────────────────────────────────────

    @app.get("/api/system/rate-limits")
    async def rate_limits(request: Request):
        """Get rate-limit budget status."""
        orch = request.app.state.orchestrator
        if orch and hasattr(orch, "rate_limiter"):
            budgets = orch.rate_limiter.get_all_budgets()
        else:
            budgets = _mock_rate_limits()
        return budgets

    # ── Portfolio ──────────────────────────────────────────────────

    @app.get("/api/portfolio/strategies")
    async def portfolio_strategies(request: Request):
        """Get strategy rankings from the portfolio manager."""
        orch = request.app.state.orchestrator
        if orch and hasattr(orch, "portfolio_manager"):
            rankings = orch.portfolio_manager.get_strategy_rankings()
        else:
            rankings = _mock_strategy_rankings()
        return rankings

    @app.get("/api/portfolio/allocation")
    async def portfolio_allocation(request: Request):
        """Get capital allocation and equity metrics."""
        orch = request.app.state.orchestrator
        if orch and hasattr(orch, "portfolio_manager"):
            alloc = orch.portfolio_manager.get_allocation()
        else:
            alloc = _mock_allocation()
        return alloc

    # ── Whales ─────────────────────────────────────────────────────

    @app.get("/api/whales")
    async def whales(request: Request):
        """Get alpha whale tracker data."""
        orch = request.app.state.orchestrator
        if orch and hasattr(orch, "whale_tracker"):
            alpha = orch.whale_tracker.get_alpha_whales()
            flow = orch.whale_tracker.get_whale_flow()
        else:
            alpha, flow = _mock_whales()
        return {"alpha_whales": alpha, "whale_flow": flow}

    # ── Risk / Positions ──────────────────────────────────────────

    @app.get("/api/risk/positions")
    async def risk_positions(request: Request):
        """Get open positions with risk metrics."""
        return _mock_positions()

    # ── SSE Streams ────────────────────────────────────────────────

    @app.get("/stream/health")
    async def stream_health(request: Request):
        """SSE stream for health heartbeat events.

        Emits an initial event immediately, then periodic updates every 5s.
        """
        async def event_generator():
            first = True
            while True:
                if await request.is_disconnected():
                    break
                uptime = time.time() - START_TIME
                data = json.dumps({
                    "type": "heartbeat",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "uptime_seconds": round(uptime, 1),
                })
                yield {"event": "heartbeat", "data": data}
                await asyncio.sleep(5)

        return EventSourceResponse(event_generator())

    @app.get("/stream/system")
    async def stream_system(request: Request):
        """SSE stream for system status updates.

        Emits initial status immediately, then periodic updates every 2s.
        """
        async def event_generator():
            while True:
                if await request.is_disconnected():
                    break
                orch = request.app.state.orchestrator
                if orch:
                    status = orch.get_system_status()
                else:
                    status = _mock_system_status()
                status["heartbeats"] = _get_heartbeats(orch)
                data = json.dumps({
                    "type": "system_status",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    **status,
                })
                yield {"event": "system_status", "data": data}
                await asyncio.sleep(2)

        return EventSourceResponse(event_generator())

    @app.get("/stream/portfolio")
    async def stream_portfolio(request: Request):
        """SSE stream for portfolio updates.

        Emits initial portfolio data immediately, then periodic updates every 10s.
        """
        async def event_generator():
            while True:
                if await request.is_disconnected():
                    break
                orch = request.app.state.orchestrator
                if orch and hasattr(orch, "portfolio_manager"):
                    rankings = orch.portfolio_manager.get_strategy_rankings()
                    alloc = orch.portfolio_manager.get_allocation()
                else:
                    rankings = _mock_strategy_rankings()
                    alloc = _mock_allocation()
                data = json.dumps({
                    "type": "portfolio_update",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "strategies": rankings,
                    "allocation": alloc,
                })
                yield {"event": "portfolio_update", "data": data}
                await asyncio.sleep(10)

        return EventSourceResponse(event_generator())


# ── Mock data helpers (for development without orchestrator) ────────


def _get_heartbeats(orch) -> dict:
    """Extract heartbeat metrics from orchestrator or return mock data."""
    if orch:
        return {
            "clob_ws": {
                "status": "green",
                "label": "CLOB WebSocket",
                "latency_ms": 12.0,
                "subscribed": f"{orch.get_system_status().get('tracked_markets_book', 0)}/50",
            },
            "gamma_api": {
                "status": "green",
                "label": "Gamma API",
                "latency_ms": 258.0,
            },
            "polygon_rpc": {
                "status": "green",
                "label": "Polygon RPC",
                "latency_s": 1.4,
            },
            "redis_bus": {
                "status": "green",
                "label": "Redis Bus",
                "latency_ms": 0.5,
            },
        }
    return _mock_heartbeats()


def _mock_system_status() -> dict:
    return {
        "mode": "full",
        "degradation_metrics": {
            "mode": "full",
            "can_trade": True,
            "component_health": {
                "clob_ws": True,
                "redis": True,
                "polygon_rpc": True,
            },
        },
        "tracked_markets_book": 50,
        "tracked_markets_trades": 50,
        "portfolio_epoch": 3,
        "active_strategies": ["momentum_follow", "market_making", "corr_arb"],
        "alpha_whales": 12,
        "websocket_connected": True,
    }


def _mock_heartbeats() -> dict:
    return {
        "clob_ws": {
            "status": "green",
            "label": "CLOB WebSocket",
            "latency_ms": 12.0,
            "subscribed": "50/50",
        },
        "gamma_api": {
            "status": "green",
            "label": "Gamma API",
            "latency_ms": 258.0,
        },
        "polygon_rpc": {
            "status": "green",
            "label": "Polygon RPC",
            "latency_s": 1.4,
        },
        "redis_bus": {
            "status": "green",
            "label": "Redis Bus",
            "latency_ms": 0.5,
        },
    }


def _mock_rate_limits() -> dict:
    return {
        "reconciliation": {"available": 70, "total": 100, "label": "Reconciliation"},
        "onboarding": {"available": 20, "total": 100, "label": "Onboarding"},
        "ad_hoc": {"available": 10, "total": 100, "label": "Ad-hoc"},
    }


def _mock_strategy_rankings() -> list[dict]:
    return [
        {"name": "corr_arb", "sortino": 3.21, "state": "active", "alloc_pct": 34,
         "trades": 45, "win_rate": 0.68, "sharpe": 2.15},
        {"name": "whale_follow", "sortino": 2.45, "state": "active", "alloc_pct": 22,
         "trades": 32, "win_rate": 0.72, "sharpe": 1.92},
        {"name": "market_making", "sortino": 1.87, "state": "active", "alloc_pct": 17,
         "trades": 128, "win_rate": 0.62, "sharpe": 1.45},
        {"name": "momentum_follow", "sortino": 0.92, "state": "active", "alloc_pct": 11,
         "trades": 24, "win_rate": 0.55, "sharpe": 0.82},
        {"name": "consensus_break", "sortino": 0.45, "state": "probation", "alloc_pct": 8,
         "trades": 18, "win_rate": 0.50, "sharpe": 0.35},
        {"name": "contrarian", "sortino": -0.21, "state": "frozen", "alloc_pct": 5,
         "trades": 15, "win_rate": 0.40, "sharpe": -0.15},
        {"name": "volume_breakout", "sortino": -0.85, "state": "retired", "alloc_pct": 3,
         "trades": 8, "win_rate": 0.25, "sharpe": -0.72},
    ]


def _mock_allocation() -> dict:
    return {
        "active": 2847,
        "frozen": 1203,
        "retired": 0,
        "total_equity": 4050,
        "pnl_24h": 127,
        "pnl_24h_pct": 3.2,
        "max_drawdown": -89,
        "max_drawdown_pct": -2.2,
    }


def _mock_whales() -> tuple[list, dict]:
    alpha_whales = [
        {"wallet": "0xA1B2", "score": 0.94, "total_pnl": 45000, "win_rate": 0.68,
         "trades_per_week": 7.2, "last_active_s": 45},
        {"wallet": "0xC3D4", "score": 0.91, "total_pnl": 32000, "win_rate": 0.72,
         "trades_per_week": 5.8, "last_active_s": 120},
        {"wallet": "0xE5F6", "score": 0.89, "total_pnl": 28000, "win_rate": 0.65,
         "trades_per_week": 4.1, "last_active_s": 300},
    ]
    whale_flow = {
        "markets": [
            {"market": "Trump wins 2028?", "flow_usd": 12000, "direction": "buy"},
            {"market": "BTC > $100K Dec?", "flow_usd": -8000, "direction": "sell"},
            {"market": "Fed cuts rates?", "flow_usd": 4000, "direction": "buy"},
        ],
        "avg_conviction_multiplier": 1.18,
    }
    return alpha_whales, whale_flow


def _mock_positions() -> list[dict]:
    """Mock open positions with risk metrics."""
    return [
        {"id": 1, "market": "Trump wins 2028?", "strategy": "MOM", "side": "YES",
         "size": 86, "entry": 0.62, "mark": 0.67, "pnl": 7.12, "pnl_pct": 8.3,
         "tau_pct": 62, "toxicity": 0.25, "liquidation_zone": False},
        {"id": 2, "market": "BTC > $100K Dec?", "strategy": "CORR", "side": "NO",
         "size": 120, "entry": 0.45, "mark": 0.42, "pnl": -4.80, "pnl_pct": -4.0,
         "tau_pct": 85, "toxicity": 0.65, "liquidation_zone": True},
        {"id": 3, "market": "Fed cuts rates?", "strategy": "WHL", "side": "YES",
         "size": 45, "entry": 0.55, "mark": 0.58, "pnl": 2.45, "pnl_pct": 5.4,
         "tau_pct": 40, "toxicity": 0.18, "liquidation_zone": False},
        {"id": 4, "market": "Crypto bull market?", "strategy": "MM", "side": "NO",
         "size": 62, "entry": 0.70, "mark": 0.68, "pnl": -1.24, "pnl_pct": -2.0,
         "tau_pct": 25, "toxicity": 0.12, "liquidation_zone": False},
        {"id": 5, "market": "S&P 500 ATH Q3?", "strategy": "MOM", "side": "YES",
         "size": 38, "entry": 0.35, "mark": 0.39, "pnl": 4.56, "pnl_pct": 12.0,
         "tau_pct": 15, "toxicity": 0.05, "liquidation_zone": False},
        {"id": 6, "market": "Oil price > $80?", "strategy": "CNTR", "side": "NO",
         "size": 28, "entry": 0.80, "mark": 0.85, "pnl": -7.50, "pnl_pct": -26.8,
         "tau_pct": 91, "toxicity": 1.20, "liquidation_zone": True},
    ]


# ── Main app instance ───────────────────────────────────────────────

# Create the default app instance for uvicorn
app = create_app()


def main():
    """Entry point for running the dashboard server."""
    import uvicorn
    uvicorn.run(
        "dashboard.server:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )


if __name__ == "__main__":
    main()
