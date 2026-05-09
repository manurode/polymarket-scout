import asyncio
from dashboard.server import create_app
from src.orchestrator import ScoutOrchestrator
from src.config import get_yaml_config, get as cfg
import uvicorn

async def main():
    config = get_yaml_config()

    # Ensure paper_trading section exists
    if "paper_trading" not in config:
        config["paper_trading"] = {}
    config["paper_trading"].setdefault("initial_usdc", 10000.0)
    config["paper_trading"].setdefault("initial_pol", 100.0)

    orchestrator = ScoutOrchestrator(config)
    await orchestrator.start()

    app = create_app(orchestrator=orchestrator)

    config_uvicorn = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config_uvicorn)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())