"""Reproduce the config-flow login path exactly as HA runs it."""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components"))

import aiohttp  # noqa: E402

from monctonwater.api import MonctonWaterClient  # noqa: E402
from monctonwater.const import BASE_URL  # noqa: E402


async def main() -> None:
    async with aiohttp.ClientSession() as session:
        client = MonctonWaterClient(session, base_url=BASE_URL)
        started = time.monotonic()
        try:
            account = await client.bootstrap("diagnostic-user", "wrong-password")
            print(f"UNEXPECTED SUCCESS in {time.monotonic() - started:.1f}s: {account}")
        except Exception as err:
            print(
                f"{type(err).__module__}.{type(err).__name__}: {err} "
                f"after {time.monotonic() - started:.1f}s"
            )


if __name__ == "__main__":
    asyncio.run(main())
