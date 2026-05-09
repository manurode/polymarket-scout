"""
Test the bot's WebSocketManager in isolation.
Uses the EXACT same code as the bot.
"""
import asyncio
import logging
import sys

# Setup logging like the bot does
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:%(name)s:%(message)s",
    stream=sys.stdout,
)

# Import the bot's WebSocketManager
from src.websocket_manager import WebSocketManager

ASSET_ID = "34099726215113915552838547710031968831503752704110618104356628661877709699078"

async def main():
    print("=== Starting WebSocketManager isolation test ===", flush=True)

    manager = WebSocketManager()

    # Track received messages
    received = []

    def on_book(asset_id, data):
        print(f"[CALLBACK] on_book: asset={asset_id[:16]}...", flush=True)
        received.append(("book", asset_id, data))

    def on_price(data):
        print(f"[CALLBACK] on_price", flush=True)
        received.append(("price", data))

    manager.on_book_delta = on_book
    manager.on_price = on_price

    # Connect
    await manager.connect()
    print("[TEST] Connected, read_loop should be running", flush=True)

    # Simulate what the orchestrator does: subscribe after connect
    await asyncio.sleep(2)
    print("[TEST] Subscribing to asset...", flush=True)
    await manager.subscribe_book(ASSET_ID)

    # Wait for messages
    print("[TEST] Waiting 20s for messages...", flush=True)
    await asyncio.sleep(20)

    print(f"[TEST] Received {len(received)} messages", flush=True)
    for r in received:
        print(f"  {r[0]}: {str(r[1])[:80]}", flush=True)

    await manager.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
