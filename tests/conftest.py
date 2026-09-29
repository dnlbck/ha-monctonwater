"""Test fixtures: a mock Moncton MyAccount portal server.

The mock reproduces the real portal's wire format as captured in
September 2026: a CSRF-guarded form login that re-renders itself on bad
credentials, the account-selection page's "watching the account" markup,
the billed-consumption HTML table, and the smart-meter page's JavaScript
arrays — including the quirk that a to-date of today makes the portal
ignore the requested range and return its default 30-day window.
"""

from __future__ import annotations

import asyncio
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

from custom_components.monctonwater.api import MonctonWaterClient  # noqa: E402

VALID_CREDS = {"testuser": "s3cret"}
ACCOUNT_NUMBER = "123456-789012"
METER_ID = "123456"
SERVICE_ADDRESS = "1 TEST ST, MONCTON, NB E1A 1A1"

# The mock's smart meter reports from this day (100 days of history, so
# the backfill walk exercises a full window, a partial window, and an
# empty one that stops the walk).
SMART_METER_START_OFFSET_DAYS = 100

CSRF_TOKEN = "test-csrf-token-123"


def smart_meter_start(today: date = None) -> date:
    today = today or date.today()
    return today - timedelta(days=SMART_METER_START_OFFSET_DAYS)


def daily_value(day: date) -> float:
    """Deterministic per-day consumption (same day always same m³)."""
    return round(0.3 + (day.toordinal() % 17) / 10, 3)


def hourly_values(day: date) -> list[float]:
    """Deterministic 24 hour-beginning values summing to daily_value."""
    per_hour = round(daily_value(day) / 24, 5)
    values = [per_hour] * 23
    values.append(round(daily_value(day) - per_hour * 23, 5))
    return values


def billed_readings(today: date = None) -> list[tuple[date, float]]:
    """Eight quarterly billed periods, the last read ~1 month ago.

    The newest periods overlap the smart meter era so tests can verify
    that the backfill replaces their spread estimates with real reads.
    """
    today = today or date.today()
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
    return f"""<html><head><title>My Account | Moncton</title></head><body>
You are currently watching the account:
<br class="hidden-sm hidden-md hidden-lg">
{ACCOUNT_NUMBER}:
{SERVICE_ADDRESS}
<a href="/app/capricorn?para=selectAccount&userAction=refresh&inAccountNumber={ACCOUNT_NUMBER}&inMeterID={METER_ID}">Refresh</a>
</body></html>"""


def consumption_page(state: dict) -> str:
    if not state.get("logged_in"):
        return LOGIN_PAGE
    rows = []
    for read_date, consumption in billed_readings():
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
    today = date.today()
    start = smart_meter_start()
    if date_to is None or date_to >= today:
        date_from = today - timedelta(days=30)
        date_to = today - timedelta(days=1)
    days = [
        day
        for day in _daterange(max(date_from, start), min(date_to, today - timedelta(days=1)))
    ]
    dates_js = ",".join(f'"{day.isoformat()}"' for day in days)
    values_js = ",".join(f"{daily_value(day):.5f}" for day in days)
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
    today = date.today()
    if not (smart_meter_start() <= day <= today - timedelta(days=1)):
        values_js = ""
    else:
        values = hourly_values(day)
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
    start = max(from_day, smart_meter_start())
    end = min(to_day, date.today() - timedelta(days=1))
    day = start
    while day <= end:
        values = hourly_values(day)
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
    """Build the mock portal application with mutable test state."""
    app = web.Application()
    state: dict = {"logged_in": False}

    async def capricorn_get(request: web.Request) -> web.Response:
        """Dispatch on the para= query parameter like the portal's JSP."""
        para = request.query.get("para")
        if para == "index" or para is None:
            # Like the real portal: an authenticated session gets the
            # meta-refresh redirect page, not the login form.
            if state["logged_in"]:
                return web.Response(
                    text=LOGGED_IN_REDIRECT, content_type="text/html"
                )
            resp = web.Response(text=LOGIN_PAGE, content_type="text/html")
            resp.set_cookie("JSESSIONID", "mock-session")
            return resp
        if para == "ajaxDownloadConsumptionData":
            # The export serves the session's last queried range.
            return web.Response(text="mock-csv%2Fkey%3D", content_type="text/plain")
        if para == "selectAccount":
            return web.Response(text=account_page(state), content_type="text/html")
        if para == "consumptionInquiry":
            return web.Response(
                text=consumption_page(state), content_type="text/html"
            )
        if para == "smartMeterConsum":
            if request.query.get("type") == "hourly":
                day = date.fromisoformat(request.query["day"])
                return web.Response(
                    text=hourly_page(state, day), content_type="text/html"
                )
            if state.get("smart_meter_empty"):
                # Aged-session behavior: the page renders with no data.
                empty_before = smart_meter_start() - timedelta(days=200)
                return web.Response(
                    text=smart_meter_page(state, empty_before, empty_before),
                    content_type="text/html",
                )
            date_from = date.fromisoformat(request.query["fromDate"])
            date_to = date.fromisoformat(request.query["toDate"])
            state["csv_range"] = (date_from, date_to)
            app["smart_meter_calls"].append((date_from, date_to))
            return web.Response(
                text=smart_meter_page(state, date_from, date_to),
                content_type="text/html",
            )
        return web.Response(text="not found", status=404)

    async def excel_export(request: web.Request) -> web.Response:
        if "key" not in request.query:
            return web.Response(text="missing key", status=400)
        return web.Response(
            text=excel_export_csv(state),
            content_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=usage.csv"},
        )

    async def capricorn_post(request: web.Request) -> web.Response:
        """The login form posts back to para=index."""
        form = await request.post()
        if form.get("jspCSRFToken") != CSRF_TOKEN:
            # A session-bound CSRF mismatch re-renders the login page.
            return web.Response(text=LOGIN_PAGE, content_type="text/html")
        username = form.get("accessCode", "")
        password = form.get("password", "")
        if VALID_CREDS.get(username) == password:
            state["logged_in"] = True
            resp = web.Response(
                text=LOGGED_IN_REDIRECT, content_type="text/html"
            )
            resp.set_cookie("JSESSIONID", "mock-session-authed")
            resp.set_cookie("capricornWCMid", username)
            return resp
        # Failed logins re-render the login page (same URL, HTTP 200).
        return web.Response(text=LOGIN_PAGE, content_type="text/html")

    app["smart_meter_calls"] = []
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


@pytest.fixture
def hass_config_dir():
    """Point Home Assistant at the repo root so custom_components/ is found."""
    return str(REPO_ROOT)


@pytest_asyncio.fixture
async def patched_helper_session(monkeypatch):
    """Make HA's shared session tolerate the mock server's IP-literal host.

    The integration normally talks to myaccount.moncton.ca (a real
    domain), where the default cookie jar works fine.
    """
    import custom_components.monctonwater as mw_init
    from custom_components.monctonwater import config_flow

    sessions: list[aiohttp.ClientSession] = []

    def _get(hass, *args, **kwargs):
        session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        sessions.append(session)
        return session

    monkeypatch.setattr(mw_init, "async_get_clientsession", _get)
    monkeypatch.setattr(config_flow, "async_get_clientsession", _get)
    yield _get
    for session in sessions:
        await session.close()


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