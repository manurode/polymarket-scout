"""
Polymarket CLOB WebSocket — Test usando aiohttp (mismo que el bot)
=================================================================
Reproduce exactamente el flujo del WebSocketManager del bot
para encontrar por qué no se reciben mensajes.
"""
import asyncio
import json
import sys

import aiohttp

ASSET_ID = "34099726215113915552838547710031968831503752704110618104356628661877709699078"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


async def main():
    print("[AIOHTTP] Starting test...", flush=True)

    async with aiohttp.ClientSession() as session:
        # Connect — same as _connect_ws()
        ws = await session.ws_connect(WS_URL, heartbeat=30)
        print("[AIOHTTP] ✅ CONNECTED", flush=True)

        # Send MARKET message — same as _send_market_subscription()
        msg = {"type": "MARKET", "assets_ids": [ASSET_ID]}
        await ws.send_json(msg)
        print(f"[AIOHTTP] 📤 SENT: {json.dumps(msg)}", flush=True)

        # Read loop — same as _read_loop()
        msg_count = 0
        while msg_count < 5:
            try:
                raw = await asyncio.wait_for(ws.receive(), timeout=15.0)
            except asyncio.TimeoutError:
                print("[AIOHTTP] ⏰ timeout — no message in 15s", flush=True)
                break

            print(f"[AIOHTTP] msg.type={raw.type}", flush=True)

            if raw.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(raw.data)
                if isinstance(data, list):
                    print(f"[AIOHTTP] 📥 ARRAY ({len(data)} items):", flush=True)
                    for item in data[:2]:
                        if isinstance(item, dict):
                            print(f"[AIOHTTP]    keys={list(item.keys())}", flush=True)
                            print(f"[AIOHTTP]    {json.dumps(item)[:600]}", flush=True)
                elif isinstance(data, dict):
                    print(f"[AIOHTTP] 📥 DICT: {json.dumps(data)[:500]}", flush=True)
                msg_count += 1

            elif raw.type == aiohttp.WSMsgType.CLOSED:
                print("[AIOHTTP] ❌ CLOSED by server", flush=True)
                break
            elif raw.type == aiohttp.WSMsgType.ERROR:
                print(f"[AIOHTTP] ❌ ERROR: {ws.exception()}", flush=True)
                break

        print(f"[AIOHTTP] Done — {msg_count} messages received", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
