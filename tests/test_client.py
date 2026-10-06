"""Client tests against the mock portal."""

from __future__ import annotations

import asyncio
import ssl
from datetime import date, datetime, timedelta, timezone

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from conftest import (
    ACCOUNT_NUMBER,
    METER_ID,
    OTHER_ACCOUNT_NUMBER,
    VALID_CREDS,
    billed_readings,
    build_app,
    daily_value,
    expire_sessions,
    mock_today,
    server_base,
    smart_meter_start,
)
from custom_components.monctonwater.api import MonctonWaterClient, portal_today
from custom_components.monctonwater.exceptions import (
    MonctonWaterApiError,
    MonctonWaterAuthError,
    MonctonWaterSessionError,
)

USERNAME = next(iter(VALID_CREDS))
PASSWORD = VALID_CREDS[USERNAME]
OTHER_USERNAME = "otheruser"


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
async def test_bootstrap_never_inherits_a_live_session(portal):
    """bootstrap always signs in afresh.

    A session still authenticated in the same cookie jar (another
    account's, or the config flow's) must not vouch for different
    credentials: wrong passwords are rejected and another user reaches
    their own account.
    """
    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(unsafe=True)
    ) as session:
        await MonctonWaterClient(session, base_url=server_base(portal)).bootstrap(
            USERNAME, PASSWORD
        )
        intruder = MonctonWaterClient(session, base_url=server_base(portal))
        with pytest.raises(MonctonWaterAuthError, match="[Ii]nvalid credentials"):
            await intruder.bootstrap(USERNAME, "wrong")

        await MonctonWaterClient(session, base_url=server_base(portal)).bootstrap(
            USERNAME, PASSWORD
        )
        other = MonctonWaterClient(session, base_url=server_base(portal))
        account = await other.bootstrap(OTHER_USERNAME, VALID_CREDS[OTHER_USERNAME])
        assert account.account_number == OTHER_ACCOUNT_NUMBER
        assert other.logged_in


@pytest.mark.asyncio
async def test_hourly_csv_is_atomic_against_concurrent_queries(client):
    """The export serves the session's last queried range, so no other
    query on the session may slip between the CSV's page query and its
    download (e.g. a coordinator refresh during the backfill walk)."""
    today = mock_today()
    window = (today - timedelta(days=60), today - timedelta(days=31))
    readings, _ = await asyncio.gather(
        client.get_hourly_csv(*window),
        client.get_daily_readings(today - timedelta(days=10), today),
    )
    expected = [window[0] + timedelta(days=i) for i in range(30)]
    assert [r.day for r in readings] == expected


def test_portal_today_follows_the_portal_calendar(freezer):
    """The to-date clamp needs the portal's (Atlantic) date, not the
    process clock's: a UTC container is already "tomorrow" every evening,
    which turned yesterday into the portal's today (its default window).

    Uses the function bound at import: the autouse mock_portal_calendar
    fixture replaces the module attribute in every test."""
    freezer.move_to("2026-10-07T01:30:00+00:00")  # 22:30 in Moncton
    assert portal_today() == date(2026, 10, 6)
    freezer.move_to("2026-10-07T03:30:00+00:00")  # 00:30 in Moncton
    assert portal_today() == date(2026, 10, 7)


@pytest.mark.asyncio
async def test_daily_readings_clamp_uses_portal_today(client, portal, monkeypatch):
    from custom_components.monctonwater import api

    today = mock_today()
    monkeypatch.setattr(api, "portal_today", lambda: today - timedelta(days=1))
    await client.get_daily_readings(today - timedelta(days=20), today)
    _, requested_to = portal.app["smart_meter_calls"][-1]
    assert requested_to == today - timedelta(days=2)


@pytest.mark.asyncio
async def test_expired_session_detected_and_recovered(portal):
    """A lapsed session surfaces as SessionError; ensure_session logs in again."""
    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(unsafe=True)
    ) as session:
        client = MonctonWaterClient(session, base_url=server_base(portal))
        await client.bootstrap(USERNAME, PASSWORD)

        expire_sessions(portal)
        with pytest.raises(MonctonWaterSessionError, match="session expired"):
            await client.get_billed_readings()

        # The client noticed the expiry, so this signs in for real.
        await client.ensure_session()
        assert client.logged_in
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
    today = mock_today()
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
    today = mock_today()
    await client.get_daily_readings(today - timedelta(days=100), today)
    requested_from, requested_to = portal.app["smart_meter_calls"][-1]
    assert requested_to == today - timedelta(days=1)


@pytest.mark.asyncio
async def test_daily_readings_before_meter_is_empty(client):
    today = mock_today()
    readings = await client.get_daily_readings(
        today - timedelta(days=400), today - timedelta(days=300)
    )
    assert readings == []


@pytest.mark.asyncio
async def test_hourly_values(client):
    today = mock_today()
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

    today = mock_today()
    readings = await client.get_hourly_csv(today - timedelta(days=200), today)
    assert readings[0].day == smart_meter_start()
    assert readings[-1].day == today - timedelta(days=1)
    assert readings[10].values == hourly_values(readings[10].day)
    assert sum(readings[10].values) == pytest.approx(
        daily_value(readings[10].day), abs=0.001
    )


@pytest.mark.asyncio
async def test_stale_session_empty_daily_recovered_by_relogin(portal):
    """A session that serves empty smart-meter arrays recovers on re-login.

    Mirrors the live failure: the billed table kept working while the
    smart-meter page returned no arrays for the aged session.
    """
    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(unsafe=True)
    ) as session:
        client = MonctonWaterClient(session, base_url=server_base(portal))
        await client.bootstrap(USERNAME, PASSWORD)

        # Age the session: the smart-meter page stops returning data.
        portal.app["state"]["smart_meter_empty"] = True
        readings = await client.get_billed_readings()
        assert readings
        assert await client.get_daily_readings(
            mock_today() - timedelta(days=10), mock_today()
        ) == []

        # A fresh login clears the portal-side state.
        client.invalidate()
        expire_sessions(portal)
        portal.app["state"]["smart_meter_empty"] = False
        await client.ensure_session()
        readings = await client.get_daily_readings(
            mock_today() - timedelta(days=10), mock_today()
        )
        assert readings


def _self_signed_server_context(tmp_path) -> ssl.SSLContext:
    """A TLS server context whose certificate no public CA vouches for."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(cert_file, key_file)
    return context


@pytest.mark.asyncio
async def test_tls_failure_points_at_the_bundled_intermediate(socket_enabled, tmp_path):
    """An unverifiable chain (as when the portal's renewed leaf comes from
    another intermediate) is reported as such, not as a bare connector
    error."""
    server = TestServer(build_app())
    await server.start_server(ssl=_self_signed_server_context(tmp_path))
    try:
        async with aiohttp.ClientSession() as session:
            client = MonctonWaterClient(
                session, base_url=f"https://{server.host}:{server.port}"
            )
            with pytest.raises(MonctonWaterApiError, match="bundled in certs/"):
                await client.bootstrap(USERNAME, PASSWORD)
    finally:
        await server.close()
