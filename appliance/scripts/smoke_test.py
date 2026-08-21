#!/usr/bin/env python3
from __future__ import annotations
import asyncio, os, uuid
import httpx

async def main() -> int:
    base = os.getenv("FLOODMAN_TEST_BASE_URL", "http://127.0.0.1:8003")
    async with httpx.AsyncClient(timeout=15.0) as client:
        ready = await client.get(f"{base}/ready")
        ready.raise_for_status()
        print(ready.json())
    return 0

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
