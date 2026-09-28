"""Probe current portal state: billed table, daily window, hourly lag."""

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components"))

import aiohttp  # noqa: E402

from monctonwater.api import MonctonWaterClient  # noqa: E402
from monctonwater.const import BASE_URL  # noqa: E402


async def main() -> None:
    env = dict(
        line.split("=", 1)
        for line in Path("scripts/out/portal.env").read_text().splitlines()
        if "=" in line
    )
    async with aiohttp.ClientSession() as session:
        client = MonctonWaterClient(session, base_url=BASE_URL)
        await client.bootstrap(env["MONCTONWATER_USERNAME"], env["MONCTONWATER_PASSWORD"])

        billed = await client.get_billed_readings()
        print("--- billed (newest 4) ---")
        total = 0.0
        for r in billed:
            total += r.consumption_m3
        for r in billed[-4:]:
            print(f"  {r.read_date}  {r.consumption_m3}")
        print(f"  billed total: {total:.1f} m3 over {len(billed)} periods")

        today = date.today()
        daily = await client.get_daily_readings(today - timedelta(days=100), today)
        last_billed = billed[-1].read_date
        print(f"\n--- daily window: {len(daily)} days, {daily[0].day}..{daily[-1].day} ---")
        current = [r for r in daily if r.day > last_billed]
        print(f"days after last billed ({last_billed}): {len(current)}")
        for r in current:
            print(f"  {r.day}  {r.consumption_m3}")
        derived = total + sum(r.consumption_m3 for r in current)
        print(f"derived cumulative: {derived:.3f} m3 (sensor floor: 880.612)")

        # hourly availability for the last 3 days
        print("\n--- hourly availability ---")
        for offset in (1, 2, 3):
            day = today - timedelta(days=offset)
            values = await client.get_hourly_values(day)
            print(
                f"  {day}: {len(values)} values"
                + (f", sum {sum(values):.3f}" if values else " (EMPTY)")
            )
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
