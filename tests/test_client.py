"""Client tests against the mock portal."""

from __future__ import annotations

from datetime import date, timedelta

import aiohttp
import pytest

from conftest import (
    ACCOUNT_NUMBER,
    METER_ID,
    VALID_CREDS,
    billed_readings,
    daily_value,
    server_base,
    smart_meter_start,
)
from custom_components.monctonwater.api import MonctonWaterClient
from custom_components.monctonwater.exceptions import (
    MonctonWaterAuthError,
)

USERNAME = next(iter(VALID_CREDS))
PASSWORD = VALID_CREDS[USERNAME]


@pytest.mark.asyncio
async def test_login_success(portal):
    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(unsafe=True)
    ) as session:
        client = MonctonWaterClient(session, base_url=server_base(portal))
        account = await client.bootstrap(USERNAME, PASSWORD)
        assert account.account_number == ACCOUNT_NUMBER
        assert account.meter_id == METER_ID
        assert "TEST ST" in account.service_address
        assert client.logged_in


@pytest.mark.asyncio
async def test_login_wrong_password(portal):
    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(unsafe=True)
    ) as session:
        client = MonctonWaterClient(session, base_url=server_base(portal))
        with pytest.raises(MonctonWaterAuthError, match="[Ii]nvalid credentials"):
            await client.bootstrap(USERNAME, "wrong")


@pytest.mark.asyncio
async def test_bootstrap_reuses_surviving_session_cookies(portal):
    """A second client on the same session skips the form login.

    Mirrors setup after the config flow: the coordinator's fresh client
    shares HA's session, whose cookies still authenticate — the login
    GET returns the redirect page (no form, no CSRF) and must not raise.
    """
    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(unsafe=True)
    ) as session:
        await MonctonWaterClient(session, base_url=server_base(portal)).bootstrap(
            USERNAME, PASSWORD
        )
        second = MonctonWaterClient(session, base_url=server_base(portal))
        account = await second.bootstrap(USERNAME, PASSWORD)
        assert account.account_number == ACCOUNT_NUMBER
        assert second.logged_in


@pytest.mark.asyncio
async def test_expired_session_detected_and_recovered(portal):
    """A lapsed session surfaces as AuthError; ensure_session logs in again."""
    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(unsafe=True)
    ) as session:
        client = MonctonWaterClient(session, base_url=server_base(portal))
        await client.bootstrap(USERNAME, PASSWORD)

        portal.app["state"]["logged_in"] = False
        with pytest.raises(MonctonWaterAuthError, match="session expired"):
            await client.get_billed_readings()

        await client.ensure_session()
        portal.app["state"]["logged_in"] = True
        readings = await client.get_billed_readings()
        assert len(readings) == len(billed_readings())


@pytest.mark.asyncio
async def test_billed_readings_parsed(client):
    readings = await client.get_billed_readings()
    expected = billed_readings()
    assert readings[0].read_date == expected[-1][0]  # sorted ascending
    assert [r.read_date for r in readings] == sorted(r.read_date for r in readings)
    by_date = {r.read_date: r.consumption_m3 for r in readings}
    for read_date, consumption in expected:
        assert by_date[read_date] == pytest.approx(consumption)


@pytest.mark.asyncio
async def test_daily_readings_window_and_era(client):
    today = date.today()
    start = smart_meter_start()
    readings = await client.get_daily_readings(
        today - timedelta(days=200), today
    )
    # Clamped to yesterday, bounded by the meter era.
    assert readings[0].day == start
    assert readings[-1].day == today - timedelta(days=1)
    assert readings[10].consumption_m3 == pytest.approx(
        daily_value(readings[10].day)
    )


@pytest.mark.asyncio
async def test_daily_readings_to_date_today_clamped(client, portal):
    """A to-date of today must be clamped, not honored as a 30-day default."""
    today = date.today()
    await client.get_daily_readings(today - timedelta(days=100), today)
    requested_from, requested_to = portal.app["smart_meter_calls"][-1]
    assert requested_to == today - timedelta(days=1)


@pytest.mark.asyncio
async def test_daily_readings_before_meter_is_empty(client):
    today = date.today()
    readings = await client.get_daily_readings(
        today - timedelta(days=400), today - timedelta(days=300)
    )
    assert readings == []


@pytest.mark.asyncio
async def test_hourly_values(client):
    today = date.today()
    yesterday = today - timedelta(days=1)
    values = await client.get_hourly_values(yesterday)
    assert len(values) == 24
    assert all(v >= 0 for v in values)
    # Outside the meter era there is nothing.
    assert await client.get_hourly_values(today - timedelta(days=150)) == []


@pytest.mark.asyncio
async def test_hourly_csv_window(client):
    """The CSV flow mirrors the portal: page query sets the export range."""
    from conftest import hourly_values

    today = date.today()
    readings = await client.get_hourly_csv(today - timedelta(days=200), today)
    assert readings[0].day == smart_meter_start()
    assert readings[-1].day == today - timedelta(days=1)
    assert readings[10].values == hourly_values(readings[10].day)
    assert sum(readings[10].values) == pytest.approx(
        daily_value(readings[10].day), abs=0.001
    )
