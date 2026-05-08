import asyncio
from dashboard.server import create_app
from src.orchestrator import ScoutOrchestrator
import uvicorn

async def main():
    config = {
        "selection": {"top_n": 50},
        "auto_trader": {
            "enabled_strategies": [
                "momentum_follow", "contrarian", "consensus_breakout",
                "volume_breakout", "market_making", "correlation_arb",
            ]
        },
        "paper_trading": {
            "initial_usdc": 10000.0,
            "initial_pol": 100.0,
        },
    }

    orchestrator = ScoutOrchestrator(config)
    await orchestrator.start()

    app = create_app(orchestrator=orchestrator)

    config_uvicorn = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config_uvicorn)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())