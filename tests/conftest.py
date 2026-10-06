"""Test fixtures: a mock Moncton MyAccount portal server.

The mock reproduces the real portal's wire format as captured in
September 2026: a CSRF-guarded form login that re-renders itself on bad
credentials, cookie-bound (JSESSIONID) sessions, the account-selection
page's "watching the account" markup, the billed-consumption HTML table,
and the smart-meter page's JavaScript arrays — including the quirk that
a to-date of today makes the portal ignore the requested range and
return its default 30-day window.
"""

from __future__ import annotations

import asyncio
import itertools
import sys
from datetime import date, timedelta
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# The HA test plugin blocks sockets via pytest-socket, and Windows' default
# proactor event loop needs a socketpair just to initialize. The session
# event loop is created before any fixture can run, so the policy must be
# swapped at import time (this mirrors what HA core does on Windows).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    # Windows event loops also construct a self-pipe socket on creation,
    # which pytest-socket blocks (Unix loops use os.pipe, so upstream HA
    # tests never hit this). Tests here talk to a localhost mock server on
    # purpose, so neuter the socket construction guard on Windows; the
    # plugin's DNS guard still applies and allows 127.0.0.1.
    import pytest_socket

    pytest_socket.disable_socket = lambda *a, **k: None  # noqa: ARG005

from homeassistant.util import dt as dt_util  # noqa: E402

from custom_components.monctonwater.api import MonctonWaterClient  # noqa: E402

VALID_CREDS = {"testuser": "s3cret", "otheruser": "0th3r"}
ACCOUNT_NUMBER = "123456-789012"
OTHER_ACCOUNT_NUMBER = "654321-210987"
ACCOUNTS = {"testuser": ACCOUNT_NUMBER, "otheruser": OTHER_ACCOUNT_NUMBER}
METER_ID = "123456"
SERVICE_ADDRESS = "1 TEST ST, MONCTON, NB E1A 1A1"

# The mock's smart meter reports from this day...
SMART_METER_START_OFFSET_DAYS = 121
# ...but like the real portal (730 days, seen live) it only keeps a
# rolling window of meter data, the oldest day it still has cut short
# (here: its first 12 hours read 0). The horizon falls inside the meter
# era, as on the live account.
RETENTION_DAYS = 110

CSRF_TOKEN = "test-csrf-token-123"


def mock_today() -> date:
    """The one "today" every test, the mock portal and HA agree on.

    HA's configured zone (US/Pacific in tests), not the machine's local
    date: the integration derives its dates from HA's clock, so a
    machine-local mock disagreed with it for hours every night.
    """
    return dt_util.now().date()


def smart_meter_start(today: date = None) -> date:
    today = today or mock_today()
    return today - timedelta(days=SMART_METER_START_OFFSET_DAYS)


def retention_start() -> date:
    """The oldest day the mock portal still has (cut short)."""
    return mock_today() - timedelta(days=RETENTION_DAYS)


def first_available() -> date:
    """The oldest day the mock portal serves at all."""
    return max(smart_meter_start(), retention_start())


def daily_value(day: date) -> float:
    """Deterministic per-day consumption (same day always same m³)."""
    return round(0.3 + (day.toordinal() % 17) / 10, 3)


def hourly_values(day: date) -> list[float]:
    """Deterministic 24 hour-beginning values summing to daily_value."""
    per_hour = round(daily_value(day) / 24, 5)
    values = [per_hour] * 23
    values.append(round(daily_value(day) - per_hour * 23, 5))
    return values


def published_until(state: dict) -> date:
    """The last day the mock portal has published (default: yesterday)."""
    return state.get("published_through") or mock_today() - timedelta(days=1)


def published_hourly(state: dict, day: date) -> list[float]:
    """The day's hourly values as currently published.

    ``state["published_hours"]`` maps a day to how many of its hours the
    portal has published so far (the real portal publishes yesterday
    progressively); later hours read 0 until then.
    """
    published = state.get("published_hours", {}).get(day, 24)
    first_hour = 12 if day == retention_start() else 0
    return [
        v if first_hour <= hour < published else 0.0
        for hour, v in enumerate(hourly_values(day))
    ]


def published_daily(state: dict, day: date) -> float:
    """The day's daily total as currently published."""
    if day in state.get("published_hours", {}) or day == retention_start():
        return round(sum(published_hourly(state, day)), 5)
    return daily_value(day)


def billed_readings(today: date = None) -> list[tuple[date, float]]:
    """Eight quarterly billed periods, the last read ~1 month ago.

    The newest periods overlap the smart meter era so tests can verify
    that the backfill replaces their spread estimates with real reads.
    """
    today = today or mock_today()
    readings = []
    read_date = today - timedelta(days=30)
    for _ in range(8):
        readings.append((read_date, 40.0 + (read_date.toordinal() % 60)))
        read_date -= timedelta(days=91)
    return readings


LOGIN_PAGE = f"""<html><head><title>My Account Login | Moncton</title></head><body>
<form id="login-form" role="form" name="login" method="post" action="/app/capricorn?para=index">
<input type="hidden" name="jspCSRFToken" value="{CSRF_TOKEN}" />
<input type="text" class="form-control" id="accessCode" name="accessCode" />
<input type="password" class="form-control" id="password" name="password" />
<input type="checkbox" name="rememberMyAccountNumber" value="Y" />
<input type="hidden" name="nextPara" value="" />
<input type="hidden" name="nextPara_attr1" value="" />
</form></body></html>"""

LOGGED_IN_REDIRECT = """<html><head><title></title>
<meta HTTP-EQUIV="REFRESH" content="0; url=/app/capricorn?para=selectAccount">
</head><body></body></html>"""


def account_page(state: dict) -> str:
    if not state.get("logged_in"):
        return LOGIN_PAGE
    account = ACCOUNTS[state["user"]]
    return f"""<html><head><title>My Account | Moncton</title></head><body>
You are currently watching the account:
<br class="hidden-sm hidden-md hidden-lg">
{account}:
{SERVICE_ADDRESS}
<a href="/app/capricorn?para=selectAccount&userAction=refresh&inAccountNumber={account}&inMeterID={METER_ID}">Refresh</a>
</body></html>"""


def consumption_page(
    state: dict, readings: list[tuple[date, float]] | None = None
) -> str:
    """Render the billed table (``readings`` newest first; default mock)."""
    if not state.get("logged_in"):
        return LOGIN_PAGE
    rows = []
    for read_date, consumption in readings if readings is not None else billed_readings():
        display = read_date.strftime("%b %d, %Y").replace(" 0", " ")
        rows.append(
            f"<tr><td class='tableColumn_0'>{display}</td>"
            f"<td style='text-align:center;' class='tableColumn_1'>{consumption:.1f}</td>"
            f"<td>{read_date.isoformat()}</td></tr>"
        )
    return f"""<html><head><title>Consumption Inquiry | Moncton</title></head><body>
<table id="consumptionTable" width="100%" class="table">
<thead><tr>
<th class="tableColumn_0" style="text-align: center;">Date</th>
<th class="tableColumn_1" style="text-align: center;">Billed Consumption in m³</th>
<th>Sortable Date</th>
</tr></thead>
<tbody>
{"".join(rows)}
</tbody></table>
</body></html>"""


def smart_meter_page(
    state: dict, date_from: date | None, date_to: date | None
) -> str:
    """Render daily/hourly smart-meter data the way the portal does.

    Reproduces the observed quirk: when the requested to-date is today
    (or beyond), the portal ignores the range and serves its default
    30-day window.
    """
    if not state.get("logged_in"):
        return LOGIN_PAGE
    today = mock_today()
    start = first_available()
    if date_to is None or date_to >= today:
        date_from = today - timedelta(days=30)
        date_to = today - timedelta(days=1)
    days = [
        day
        for day in _daterange(max(date_from, start), min(date_to, published_until(state)))
    ]
    dates_js = ",".join(f'"{day.isoformat()}"' for day in days)
    values_js = ",".join(f"{published_daily(state, day):.5f}" for day in days)
    return f"""<html><head><title>Smart Meter Consumption Inquiry | Moncton</title></head><body>
<form name="hydroInquiry" method="post" action="/app/index.jsp">
<input type="hidden" name="fromDate" value="{days[0].isoformat() if days else ''}" />
<input type="hidden" name="toDate" value="{days[-1].isoformat() if days else ''}" />
</form>
<script><!--
var aTouDate = [{dates_js}];
var aTouOffPeakAmount = [{values_js}];
var chart;
$(document).ready(function() {{
    chart = new Highcharts.Chart({{
        chart: {{ renderTo: 'chart_container', type: 'scatter' }},
        series: [{{
            type: 'column',
            name: "Usage",
            data: [{values_js}],
        }}]
    }});
}});
var ajaxURL = "/app/capricorn?para=ajaxDownloadConsumptionData&type=smartmeter&inquiryType=water";
--></script>
</body></html>"""


def hourly_page(state: dict, day: date) -> str:
    if not state.get("logged_in"):
        return LOGIN_PAGE
    if not (first_available() <= day <= published_until(state)):
        values_js = ""
    else:
        values = published_hourly(state, day)
        values_js = ",".join(f"{v:.5f}" for v in values)
    return f"""<html><head><title>Smart Meter Consumption Inquiry | Moncton</title></head><body>
<script><!--
var aTouDate = [];
var chart;
$(document).ready(function() {{
    chart = new Highcharts.Chart({{
        chart: {{ renderTo: 'chart_container', type: 'scatter' }},
        series: [{{ type: 'column', name: "Usage", data: [{values_js}] }}]
    }});
}});
--></script>
</body></html>"""


def excel_export_csv(state: dict) -> str:
    """Build the hourly CSV for the session's last queried range.

    Mirrors the real export: a header with 24 (mislabelled CFF) hourly
    columns plus a total, a row per day, and padding rows the parser
    must skip.
    """
    from_day, to_day = state.get("csv_range", (None, None))
    if from_day is None:
        return ""
    header = ["Reading Date"] + [f"{h} CFF Usage" for h in range(1, 25)] + ["Total CFF Usage"]
    lines = [",".join(f'"{c}"' for c in header), "", ""]
    start = max(from_day, first_available())
    end = min(to_day, published_until(state))
    day = start
    while day <= end:
        values = published_hourly(state, day)
        cells = [day.isoformat()] + [f"{v:.5f}" for v in values] + [f"{sum(values):.5f}"]
        lines.append(",".join(cells))
        day += timedelta(days=1)
    lines.append("")
    return "\n".join(lines)


def _daterange(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def build_app() -> web.Application:
    """Build the mock portal application with mutable test state.

    Sessions are cookie-bound like the real portal's: ``state["sessions"]``
    maps each issued JSESSIONID to its signed-in user (None until the
    login form is posted). Clearing it expires every session. The CSV
    export's "last queried range" is per session too.
    """
    app = web.Application()
    state: dict = {"sessions": {}, "csv_ranges": {}}
    session_ids = itertools.count()

    def page_state(request: web.Request) -> dict:
        """The shared state plus this request's session view."""
        session_id = request.cookies.get("JSESSIONID")
        user = state["sessions"].get(session_id)
        return {
            **state,
            "logged_in": user is not None,
            "user": user,
            "csv_range": state["csv_ranges"].get(session_id, (None, None)),
        }

    async def capricorn_get(request: web.Request) -> web.Response:
        """Dispatch on the para= query parameter like the portal's JSP."""
        para = request.query.get("para")
        page = page_state(request)
        if para == "index" or para is None:
            # Like the real portal: an authenticated session gets the
            # meta-refresh redirect page, not the login form.
            if page["logged_in"]:
                return web.Response(
                    text=LOGGED_IN_REDIRECT, content_type="text/html"
                )
            resp = web.Response(text=LOGIN_PAGE, content_type="text/html")
            if request.cookies.get("JSESSIONID") not in state["sessions"]:
                session_id = f"mock-session-{next(session_ids)}"
                state["sessions"][session_id] = None
                resp.set_cookie("JSESSIONID", session_id)
            return resp
        if not page["logged_in"]:
            # Every other page answers an expired session with the login form.
            return web.Response(text=LOGIN_PAGE, content_type="text/html")
        if para == "ajaxDownloadConsumptionData":
            # The export serves the session's last queried range.
            return web.Response(text="mock-csv%2Fkey%3D", content_type="text/plain")
        if para == "selectAccount":
            return web.Response(text=account_page(page), content_type="text/html")
        if para == "consumptionInquiry":
            app["billed_calls"].append(mock_today())
            return web.Response(
                text=consumption_page(page), content_type="text/html"
            )
        if para == "smartMeterConsum":
            if request.query.get("type") == "hourly":
                day = date.fromisoformat(request.query["day"])
                return web.Response(
                    text=hourly_page(page, day), content_type="text/html"
                )
            if state.get("smart_meter_empty"):
                # Aged-session behavior: the page renders with no data.
                empty_before = smart_meter_start() - timedelta(days=200)
                return web.Response(
                    text=smart_meter_page(page, empty_before, empty_before),
                    content_type="text/html",
                )
            date_from = date.fromisoformat(request.query["fromDate"])
            date_to = date.fromisoformat(request.query["toDate"])
            state["csv_ranges"][request.cookies.get("JSESSIONID")] = (date_from, date_to)
            app["smart_meter_calls"].append((date_from, date_to))
            return web.Response(
                text=smart_meter_page(page, date_from, date_to),
                content_type="text/html",
            )
        return web.Response(text="not found", status=404)

    async def excel_export(request: web.Request) -> web.Response:
        if "key" not in request.query:
            return web.Response(text="missing key", status=400)
        page = page_state(request)
        if not page["logged_in"]:
            return web.Response(text=LOGIN_PAGE, content_type="text/html")
        return web.Response(
            text=excel_export_csv(page),
            content_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=usage.csv"},
        )

    async def capricorn_post(request: web.Request) -> web.Response:
        """The login form posts back to para=index."""
        form = await request.post()
        session_id = request.cookies.get("JSESSIONID")
        if form.get("jspCSRFToken") != CSRF_TOKEN or session_id not in state["sessions"]:
            # A session-bound CSRF mismatch re-renders the login page.
            return web.Response(text=LOGIN_PAGE, content_type="text/html")
        username = form.get("accessCode", "")
        password = form.get("password", "")
        if VALID_CREDS.get(username) == password:
            state["sessions"][session_id] = username
            resp = web.Response(
                text=LOGGED_IN_REDIRECT, content_type="text/html"
            )
            resp.set_cookie("capricornWCMid", username)
            return resp
        # Failed logins re-render the login page (same URL, HTTP 200).
        return web.Response(text=LOGIN_PAGE, content_type="text/html")

    app["smart_meter_calls"] = []
    app["billed_calls"] = []
    app["state"] = state
    app.router.add_get("/app/capricorn", capricorn_get)
    app.router.add_post("/app/capricorn", capricorn_post)
    app.router.add_get("/app/ExcelExport", excel_export)
    return app


@pytest_asyncio.fixture
async def portal(socket_enabled):
    """A running mock portal server."""
    server = TestServer(build_app())
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()


def server_base(server: TestServer) -> str:
    return f"http://{server.host}:{server.port}"


def expire_sessions(server: TestServer) -> None:
    """Expire every portal session, like the real portal's idle timeout."""
    server.app["state"]["sessions"].clear()


@pytest.fixture
def monctonwater_urls(monkeypatch):
    """Point every module that builds a client at a mock server base URL."""

    def _install(base: str) -> None:
        import custom_components.monctonwater as mw_init
        from custom_components.monctonwater import config_flow, const

        monkeypatch.setattr(const, "BASE_URL", base)
        monkeypatch.setattr(mw_init, "BASE_URL", base)
        monkeypatch.setattr(config_flow, "BASE_URL", base)

    return _install


@pytest.fixture(autouse=True)
def fast_backfill(monkeypatch):
    """Keep the daily backfill walk tiny and instant in all tests."""
    from custom_components.monctonwater import const as mw_const
    from custom_components.monctonwater import statistics as mw_stats

    monkeypatch.setattr(mw_stats, "BACKFILL_REQUEST_PAUSE", 0)
    monkeypatch.setattr(mw_const, "BACKFILL_REQUEST_PAUSE", 0)


@pytest.fixture(autouse=True)
def billed_every_refresh(monkeypatch):
    """Re-read the billed table on every refresh in tests.

    Production reads it once a day; tests that change the mock's table
    between refreshes need the change seen. test_billed_table_read_once_a_day
    restores the real interval.
    """
    from custom_components.monctonwater import coordinator as mw_coordinator

    monkeypatch.setattr(mw_coordinator, "BILLED_REFRESH_INTERVAL", timedelta(0))


@pytest.fixture(autouse=True)
def mock_portal_calendar(monkeypatch):
    """The mock portal's calendar is HA's (see mock_today)."""
    from custom_components.monctonwater import api

    monkeypatch.setattr(api, "portal_today", mock_today)


@pytest.fixture
def hass_config_dir():
    """Point Home Assistant at the repo root so custom_components/ is found."""
    return str(REPO_ROOT)


@pytest_asyncio.fixture
async def patched_helper_session(monkeypatch):
    """Make HA's client sessions tolerate the mock server's IP-literal host.

    The integration normally talks to myaccount.moncton.ca (a real
    domain), where the default cookie jar works fine.
    """
    import custom_components.monctonwater as mw_init
    from custom_components.monctonwater import config_flow

    created: list[tuple[aiohttp.ClientSession, aiohttp.BaseConnector]] = []

    def _create(hass, *args, **kwargs):
        connector = aiohttp.TCPConnector()
        session = aiohttp.ClientSession(
            connector=connector, cookie_jar=aiohttp.CookieJar(unsafe=True)
        )
        created.append((session, connector))
        return session

    monkeypatch.setattr(mw_init, "async_create_clientsession", _create)
    monkeypatch.setattr(config_flow, "async_create_clientsession", _create)
    yield _create
    for session, connector in created:
        await session.close()
        await connector.close()  # detached sessions leave theirs open


@pytest_asyncio.fixture
async def client(portal):
    """A logged-in client wired to the mock server.

    The unsafe cookie jar is needed because the mock runs on an IP
    literal; aiohttp's default jar refuses cookies from hosts that
    aren't domains.
    """
    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(unsafe=True)
    ) as session:
        c = MonctonWaterClient(session, base_url=server_base(portal))
        await c.bootstrap("testuser", "s3cret")
        yield c