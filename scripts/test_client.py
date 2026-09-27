"""Standalone harness for the Moncton Water client (no Home Assistant).

Verifies the client against the live portal before installing the
integration, and writes raw HTML captures to scripts/out/ for parser
tests.

Usage (from the repository root):

    python -m venv .venv
    .venv/Scripts/pip install aiohttp            # Windows
    set MONCTONWATER_USERNAME=<your username>
    set MONCTONWATER_PASSWORD=<your password>
    .venv/Scripts/python scripts/test_client.py

Alternatively, seed an existing browser session instead of logging in
(useful when the login flow is being debugged):

    set MONCTONWATER_COOKIES=JSESSIONID=...; capricornWCMid=...
    .venv/Scripts/python scripts/test_client.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components"))

from monctonwater.api import (  # noqa: E402
    MonctonWaterClient,
    parse_account_info,
    parse_billed_readings,
    parse_daily_readings,
)
from monctonwater.const import BASE_URL  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"


async def main() -> None:
    import aiohttp

    OUT_DIR.mkdir(exist_ok=True)
    username = os.environ.get("MONCTONWATER_USERNAME")
    password = os.environ.get("MONCTONWATER_PASSWORD")
    cookie_string = os.environ.get("MONCTONWATER_COOKIES", "")

    async with aiohttp.ClientSession() as session:
        for part in cookie_string.split(";"):
            if "=" in part:
                name, _, value = part.strip().partition("=")
                session.cookie_jar.update_cookies({name: value})

        client = MonctonWaterClient(session, base_url=BASE_URL)

        if username and password:
            print("Logging in...")
            account = await client.bootstrap(username, password)
        else:
            print("Skipping login (using seeded cookies)")
            html = await client._get_page(  # noqa: SLF001
                "/app/capricorn", {"para": "selectAccount"}
            )
            (OUT_DIR / "selectAccount.html").write_text(html, encoding="utf-8")
            account = parse_account_info(html)
            client.account = account
        print(f"Account:  {account.account_number}")
        print(f"Address:  {account.service_address}")
        print(f"Meter ID: {account.meter_id}")

        print("\n--- Billed consumption (quarterly) ---")
        billed_html = await client._request(  # noqa: SLF001
            "GET",
            "/app/capricorn",
            params={
                "para": "consumptionInquiry",
                "inquiryType": "water",
                "report": "WATCONRP",
                "tab": "WATERCON",
            },
        )
        (OUT_DIR / "consumptionInquiry.html").write_text(billed_html, encoding="utf-8")
        billed = parse_billed_readings(billed_html)
        for reading in billed:
            print(f"  {reading.read_date}  {reading.consumption_m3:>6.1f} m³")
        billed_total = sum(r.consumption_m3 for r in billed)
        print(f"  total billed: {billed_total:.1f} m³ over {len(billed)} periods")

        print("\n--- Daily readings (trailing 90 days) ---")
        today = date.today()
        daily = await client.get_daily_readings(
            today - timedelta(days=90), today
        )
        for reading in daily[-10:]:
            print(f"  {reading.day}  {reading.consumption_m3:>7.3f} m³")
        print(f"  ... {len(daily)} days, latest {daily[-1].day if daily else None}")

        print("\n--- Full daily history walk (90-day windows) ---")
        all_daily = list(daily)
        window_to = today - timedelta(days=91)
        empty_windows = 0
        while window_to >= date(today.year - 4, 1, 1) and empty_windows < 2:
            window_from = window_to - timedelta(days=90)
            try:
                batch = await client.get_daily_readings(window_from, window_to)
            except (TimeoutError, OSError) as err:
                print(f"  {window_from} .. {window_to}: request failed ({err}); retrying once")
                await asyncio.sleep(10)
                try:
                    batch = await client.get_daily_readings(window_from, window_to)
                except (TimeoutError, OSError) as err2:
                    print(f"  retry failed too ({err2}); stopping walk")
                    break
            if batch:
                print(
                    f"  {window_from} .. {window_to}: {len(batch)} days"
                    f" ({sum(r.consumption_m3 for r in batch):.1f} m³)"
                )
                all_daily.extend(batch)
                empty_windows = 0
            else:
                print(f"  {window_from} .. {window_to}: no data")
                empty_windows += 1
            window_to = window_from - timedelta(days=1)
            await asyncio.sleep(5.0)
        all_daily.sort(key=lambda r: r.day)
        print(
            f"  walked {len(all_daily)} days, "
            f"{all_daily[0].day} .. {all_daily[-1].day}, "
            f"{sum(r.consumption_m3 for r in all_daily):.1f} m³ total"
        )

        last_billed = billed[-1].read_date if billed else None
        current = sum(
            r.consumption_m3
            for r in all_daily
            if last_billed is None or r.day > last_billed
        )
        print(
            f"\nCumulative counter (billed + current period): "
            f"{billed_total + current:.1f} m³"
        )

        print("\n--- Hourly readings (yesterday) ---")
        hourly = await client.get_hourly_values(today - timedelta(days=1))
        if hourly:
            for hour, value in enumerate(hourly):
                print(f"  {hour:02d}:00  {value:>7.3f} m³")
            print(f"  sum: {sum(hourly):.3f} m³")
        else:
            print("  (no hourly data)")

        (OUT_DIR / "daily_history.txt").write_text(
            "\n".join(
                f"{r.day.isoformat()},{r.consumption_m3}" for r in all_daily
            ),
            encoding="utf-8",
        )
        print(f"\nRaw captures written to {OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
