"""Probe the CSV hourly export: login, alignment vs HTML hourly, range depth."""

import asyncio
import csv
import io
import sys
import urllib.parse
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components"))

import aiohttp  # noqa: E402

from monctonwater.api import (  # noqa: E402
    MonctonWaterClient,
    parse_hourly_values,
)
from monctonwater.const import BASE_URL  # noqa: E402


async def main() -> None:
    env = {}
    for line in Path("scripts/out/portal.env").read_text().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            env[k] = v
    today = date.today()
    async with aiohttp.ClientSession() as session:
        client = MonctonWaterClient(session, base_url=BASE_URL)
        print("logging in...")
        await client.bootstrap(env["MONCTONWATER_USERNAME"], env["MONCTONWATER_PASSWORD"])
        print("login OK, account:", client.account.account_number)

        # --- 1) HTML hourly for yesterday
        yesterday = today - timedelta(days=1)
        html_hourly = await client.get_hourly_values(yesterday)
        print(f"HTML hourly {yesterday}: {len(html_hourly)} values, sum={sum(html_hourly):.3f}")

        # --- 2) CSV for a 3-day window containing yesterday
        async def fetch_csv(from_day, to_day):
            await client._get_page(  # noqa: SLF001
                "/app/capricorn",
                {
                    "para": "smartMeterConsum",
                    "inquiryType": "water",
                    "fromDate": from_day.isoformat(),
                    "toDate": to_day.isoformat(),
                },
            )
            key = (await client._request(  # noqa: SLF001
                "GET",
                "/app/capricorn",
                params={
                    "para": "ajaxDownloadConsumptionData",
                    "type": "smartmeter",
                    "inquiryType": "water",
                },
            )).strip()
            quoted = urllib.parse.quote(key, safe="")
            raw = await client._request("GET", f"/app/ExcelExport?key={quoted}")
            return raw

        raw = await fetch_csv(yesterday - timedelta(days=2), yesterday)
        rows = list(csv.reader(io.StringIO(raw)))
        print(f"CSV rows: {len(rows) - 1}, header cols: {len(rows[0])}")
        target = next((r for r in rows if r and r[0] == yesterday.isoformat()), None)
        if target:
            hourly = [float(x) for x in target[1:25]]
            total = float(target[25])
            print(f"CSV {yesterday}: hourly sum={sum(hourly):.3f} total col={total:.3f}")
            print("CSV  hourly:", [round(v, 3) for v in hourly])
            print("HTML hourly:", [round(v, 3) for v in html_hourly])
            print("aligned:", [round(v, 3) for v in hourly] == [round(v, 3) for v in html_hourly])

        # --- 3) range depth: can the CSV serve the full meter era (730+ days)?
        era_start = today - timedelta(days=730)
        raw = await fetch_csv(era_start, yesterday)
        n = len(list(csv.reader(io.StringIO(raw)))) - 1
        first_row = next(r for r in csv.reader(io.StringIO(raw)) if r and r[0][:4] == "20")
        print(f"full-era CSV: {n} rows, first date {first_row[0]}")


if __name__ == "__main__":
    asyncio.run(main())
