"""worker/main.py — NATS consumer for business.push.request."""
import asyncio

import nats

from .handler import handle_message


async def run() -> None:
    nc = await nats.connect("nats://localhost:4222")
    sub = await nc.subscribe("business.push.request")
    async for msg in sub.messages:
        await handle_message(msg)


if __name__ == "__main__":
    asyncio.run(run())
