"""
Polymarket CLOB WebSocket — Test Script Aislado
================================================
Conecta al WS de Polymarket y prueba distintos formatos de suscripción
hasta recibir datos reales de orderbook.

Usage:
    python ws_debug.py
"""
import asyncio
import json
import signal
import sys

import websockets

# ── Config ──────────────────────────────────────────────────────
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Asset real de un mercado activo (Shandong Taishan FC)
ASSET_ID = "34099726215113915552838547710031968831503752704110618104356628661877709699078"

# ── Helpers ─────────────────────────────────────────────────────

def log(msg: str, *args) -> None:
    print(f"[WS_TEST] {msg}", *args, flush=True)

# ── Subscription Formats to Try ─────────────────────────────────

# Format 1: npm client style (type=MARKET, assets_ids)
FORMAT_MARKET = {
    "type": "MARKET",
    "assets_ids": [ASSET_ID],
}

# Format 2: subscribe operation
FORMAT_SUBSCRIBE = {
    "assets_ids": [ASSET_ID],
    "operation": "subscribe",
}

# Format 3: action/channel style (mentioned by user)
FORMAT_ACTION = {
    "action": "subscribe",
    "channel": "book",
    "market": ASSET_ID,
}

# Format 4: type=subscribe with asset_id
FORMAT_TYPE_SUB = {
    "type": "subscribe",
    "channel": "book",
    "asset_id": ASSET_ID,
}

# ── Main Test ───────────────────────────────────────────────────

async def test_ws(url: str, subscribe_msg: dict, label: str) -> bool:
    """Test a single WebSocket connection with one subscription format."""
    log(f"\n{'='*60}")
    log(f"TEST: {label}")
    log(f"URL: {url}")
    log(f"Subscribe payload: {json.dumps(subscribe_msg)}")
    log(f"{'='*60}")

    msg_count = 0
    try:
        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
            max_size=2**20,
        ) as ws:
            log("✅ CONNECTED")

            # Send subscription
            await ws.send(json.dumps(subscribe_msg))
            log(f"📤 SENT: {json.dumps(subscribe_msg)[:200]}")

            # Read messages for up to 30 seconds
            log("⏳ Waiting for messages (30s timeout)...")
            while msg_count < 20:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    msg_count += 1
                    data = json.loads(raw)

                    # Pretty-print the message type
                    if isinstance(data, list):
                        log(f"📥 [{msg_count}] ARRAY ({len(data)} items):")
                        for i, item in enumerate(data[:3]):  # show first 3
                            if isinstance(item, dict):
                                keys = list(item.keys())
                                log(f"   [{i}] keys={keys}")
                                # Show first 300 chars
                                log(f"   [{i}] {json.dumps(item)[:500]}")
                    elif isinstance(data, dict):
                        keys = list(data.keys())
                        log(f"📥 [{msg_count}] DICT keys={keys}: {json.dumps(data)[:500]}")
                    else:
                        log(f"📥 [{msg_count}] {type(data).__name__}: {str(data)[:200]}")

                except asyncio.TimeoutError:
                    log(f"⏰ TIMEOUT after 30s — received {msg_count} messages")
                    break

            if msg_count == 0:
                log("❌ FAILED: No messages received")
                return False
            else:
                log(f"✅ SUCCESS: Received {msg_count} messages")
                return True

    except websockets.exceptions.InvalidStatus as e:
        log(f"❌ HTTP ERROR: {e.response.status_code} — {e}")
        return False
    except Exception as e:
        log(f"❌ ERROR: {type(e).__name__}: {e}")
        return False


async def main():
    log("Polymarket CLOB WebSocket Debugger")
    log(f"Asset: {ASSET_ID}")

    # Try each format in order
    formats = [
        (FORMAT_MARKET, "MARKET message (npm client style)"),
        (FORMAT_SUBSCRIBE, "Subscribe operation"),
        (FORMAT_ACTION, "action/channel style"),
        (FORMAT_TYPE_SUB, "type=subscribe + asset_id"),
    ]

    for msg, label in formats:
        success = await test_ws(WS_URL, msg, label)
        if success:
            log(f"\n🎉 WORKING FORMAT: {label}")
            log(f"   Payload: {json.dumps(msg)}")
            return
        await asyncio.sleep(2)  # brief pause between tests

    log("\n❌ NO FORMAT WORKED — all 4 approaches failed to receive data")


if __name__ == "__main__":
    asyncio.run(main())
