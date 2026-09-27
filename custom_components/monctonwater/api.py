"""Async client for the City of Moncton MyAccount portal (water).

The City of Moncton publishes no official API. This client reproduces the
requests the MyAccount portal (myaccount.moncton.ca) makes — a plain form
login, then server-rendered JSP pages whose data lives in small HTML tables
and JavaScript arrays. The wire format was reverse-engineered against live
traffic in September 2026 (see scripts/test_client.py for the standalone
harness used to validate it).

Observed portal behavior (2026-09):

- Login is a single form POST to ``/app/capricorn?para=index`` carrying a
  per-page CSRF token (``jspCSRFToken``), ``accessCode`` and ``password``.
  A failed login simply re-renders the login page (HTTP 200); there is no
  distinct error page. The session is the ``JSESSIONID`` cookie.
- Any page returns the login form when the session has expired, so session
  validity is checked by looking for ``id="login-form"`` in responses.
- ``para=consumptionInquiry`` renders ``#consumptionTable``: billed
  consumption per billing period (~quarterly), in m³, with a sortable ISO
  date column. About 13 periods (~3 years) are shown.
- ``para=smartMeterConsum`` renders daily smart-meter usage into two
  JavaScript arrays (``aTouDate`` and the chart series' ``data``), in m³.
  The page documents a 90-day window per query (enforced client-side
  only); data exists from the meter's activation date and yesterday's
  usage appears within 24 hours. With ``type=hourly&day=YYYY-MM-DD`` the
  same arrays carry 24 hourly values.
- ``selectedMeterId`` may be omitted: the portal defaults to the active
  account's water meter.

This module must not import ``homeassistant`` so it can be unit tested and
driven standalone by ``scripts/test_client.py``.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import ssl
import urllib.parse
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import aiohttp

from .exceptions import MonctonWaterApiError, MonctonWaterAuthError

_LOGGER = logging.getLogger(__name__)

# The portal's page renders can run slow under load (30–70 s was observed
# during bulk probing), so the budget is generous.
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=120)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) Gecko/20100101 "
    "Firefox/156.0"
)

_LOGIN_FORM_MARKER = 'id="login-form"'

# The portal's TLS server fails to send its intermediate CA certificate
# (Entrust OV TLS Issuing RSA CA 2 -> Sectigo R46). Browsers and Windows
# paper over this via cached/AIA-fetched intermediates; HA's container
# does not, so verification fails with "unable to get local issuer
# certificate". The intermediate is bundled and added on top of the
# default CA store — full verification, nothing disabled.
_CERTS_DIR = Path(__file__).parent / "certs"
_EXTRA_CA_BUNDLES = ("entrust_ov_tls_issuing_rsa_ca_2.pem",)


def build_ssl_context() -> ssl.SSLContext:
    """Default CAs plus the portal's missing intermediate (see above)."""
    context = ssl.create_default_context()
    for name in _EXTRA_CA_BUNDLES:
        context.load_verify_locations(cafile=str(_CERTS_DIR / name))
    return context
_CSRF_RE = re.compile(r'name="jspCSRFToken"\s+value="([^"]+)"')
_ACCOUNT_WATCHING_RE = re.compile(
    r"watching the account:(?:\s|<[^>]+>)*([\d-]+)"
    r"(?:\s|<[^>]+>)*:(?:\s|<[^>]+>)*([^<]+)",
    re.I,
)
_METER_ID_RE = re.compile(r"inMeterID=(\d+)")
_ACCOUNT_LINK_RE = re.compile(r"inAccountNumber=([\d-]+)")
_TABLE_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_TABLE_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TOU_DATE_ARRAY_RE = re.compile(r"var\s+aTouDate\s*=\s*\[(.*?)\]\s*;", re.S)
_USAGE_SERIES_DATA_RE = re.compile(
    r'name:\s*"Usage".*?data:\s*\[([^\]]*)\]', re.S
)


@dataclass(frozen=True)
class AccountInfo:
    """The portal's active account."""

    account_number: str
    service_address: str
    meter_id: str | None = None


@dataclass(frozen=True)
class BilledReading:
    """Consumption billed for one billing period (read date)."""

    read_date: date
    consumption_m3: float


@dataclass(frozen=True)
class DailyReading:
    """Consumption recorded for one day by the smart meter."""

    day: date
    consumption_m3: float


@dataclass(frozen=True)
class HourlyReading:
    """Consumption recorded for one day, 24 hour-beginning values (m³)."""

    day: date
    values: list[float]


def is_login_page(html: str) -> bool:
    """True when the portal answered with the login page (expired session)."""
    return _LOGIN_FORM_MARKER in html


def parse_account_info(html: str) -> AccountInfo:
    """Scrape the active account from the selectAccount page."""
    match = _ACCOUNT_WATCHING_RE.search(html)
    meter_match = _METER_ID_RE.search(html)
    meter_id = meter_match.group(1).strip() if meter_match else None
    if match is not None:
        return AccountInfo(
            account_number=match.group(1).strip(),
            service_address=re.sub(r"\s+", " ", match.group(2)).strip(" ,"),
            meter_id=meter_id,
        )
    # Fallback: right after a fresh login the page may render without the
    # "watching the account" header; the account-number query parameter
    # appears in the page's own refresh/select links either way.
    link_match = _ACCOUNT_LINK_RE.search(html)
    if link_match is not None:
        return AccountInfo(
            account_number=link_match.group(1).strip(),
            service_address="",
            meter_id=meter_id,
        )
    raise MonctonWaterApiError(
        "Could not find the active account on the account page"
    )


def parse_billed_readings(html: str) -> list[BilledReading]:
    """Parse the #consumptionTable rows (billed consumption per period).

    Rows carry a display date, the billed m³, and a sortable ISO date; the
    ISO column is authoritative.
    """
    table_match = re.search(
        r'<table[^>]*id="consumptionTable".*?</table>', html, re.S
    )
    if table_match is None:
        # An empty account can render the page without the table.
        return []
    readings: list[BilledReading] = []
    for row in _TABLE_ROW_RE.findall(table_match.group(0)):
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in _TABLE_CELL_RE.findall(row)]
        if len(cells) < 2:
            continue
        iso = next((c for c in cells if _ISO_DATE_RE.fullmatch(c)), None)
        consumption = _to_float(cells[1])
        if iso is None or consumption is None:
            continue
        readings.append(
            BilledReading(read_date=date.fromisoformat(iso), consumption_m3=consumption)
        )
    readings.sort(key=lambda r: r.read_date)
    return readings


def parse_daily_readings(html: str) -> list[DailyReading]:
    """Parse the smartMeterConsum page's JavaScript arrays into daily rows.

    The page embeds ``var aTouDate = ["2026-08-27", ...]`` and a chart
    series ``name: "Usage" ... data: [0.779, ...]``; both arrays line up.
    """
    dates_match = _TOU_DATE_ARRAY_RE.search(html)
    data_match = _USAGE_SERIES_DATA_RE.search(html)
    if dates_match is None or data_match is None:
        return []
    days = re.findall(r"\d{4}-\d{2}-\d{2}", dates_match.group(1))
    values = [
        v
        for v in (_to_float(item) for item in data_match.group(1).split(","))
        if v is not None
    ]
    if len(days) != len(values):
        _LOGGER.warning(
            "Smart meter page arrays disagree (%s dates, %s values); "
            "truncating to the shorter",
            len(days),
            len(values),
        )
    return [
        DailyReading(day=date.fromisoformat(day), consumption_m3=value)
        for day, value in zip(days, values, strict=False)
    ]


def parse_hourly_values(html: str) -> list[float]:
    """Parse the smartMeterConsum hourly page into 24 hour-beginning values."""
    data_match = _USAGE_SERIES_DATA_RE.search(html)
    if data_match is None:
        return []
    return [
        v
        for v in (_to_float(item) for item in data_match.group(1).split(","))
        if v is not None
    ]


def _to_float(value: str) -> float | None:
    try:
        return float(value.strip())
    except (TypeError, ValueError):
        return None


def parse_hourly_csv(text: str) -> list[HourlyReading]:
    """Parse the ExcelExport CSV into per-day hourly readings.

    Columns are ``"Reading Date"``, 24 hourly usage columns (mislabeled
    ``CFF`` — the values are m³), and a total column. The hourly columns
    were verified to line up with the portal's own hourly chart
    (column 1 = hour beginning 00:00). Rows without an ISO date are
    padding and skipped.
    """
    readings: list[HourlyReading] = []
    for row in csv.reader(io.StringIO(text)):
        if not row or not _ISO_DATE_RE.fullmatch(row[0].strip()):
            continue
        values = [v for v in (_to_float(cell) for cell in row[1:25]) if v is not None]
        if len(values) != 24:
            _LOGGER.warning(
                "Hourly CSV row for %s has %s values; expected 24", row[0], len(values)
            )
            continue
        readings.append(HourlyReading(day=date.fromisoformat(row[0].strip()), values=values))
    readings.sort(key=lambda r: r.day)
    return readings


class MonctonWaterClient:
    """Client for the Moncton MyAccount portal."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        base_url: str,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._session = session
        self._base = base_url.rstrip("/")
        self._username: str | None = None
        self._password: str | None = None
        self.account: AccountInfo | None = None
        # Only needed for https endpoints; None keeps aiohttp's default
        # for the plain-http mock server used in tests. HA callers pass a
        # context prebuilt in an executor so the (blocking) CA loading
        # stays off the event loop; standalone scripts build it here.
        self._ssl_context = ssl_context if ssl_context is not None else (
            build_ssl_context() if self._base.startswith("https") else None
        )

    @property
    def logged_in(self) -> bool:
        """True once a login has succeeded in this session."""
        return self.account is not None

    def invalidate(self) -> None:
        """Forget the session so the next call re-authenticates."""
        self.account = None

    def set_credentials(self, username: str, password: str) -> None:
        """Store credentials for automatic re-login."""
        self._username = username
        self._password = password

    # ------------------------------------------------------------------
    # Public data access
    # ------------------------------------------------------------------

    async def bootstrap(self, username: str, password: str) -> AccountInfo:
        """Log in and resolve the active account."""
        self.set_credentials(username, password)
        self.invalidate()
        await self._login(username, password)
        self.account = await self.get_account_info()
        return self.account

    async def ensure_session(self) -> None:
        """Re-login when the portal session has lapsed."""
        if not self.logged_in:
            if not (self._username and self._password):
                raise MonctonWaterAuthError("Client has no credentials")
            await self._login(self._username, self._password)

    async def get_account_info(self) -> AccountInfo:
        """Fetch the active account from the account-selection page."""
        html = await self._get_page(
            "/app/capricorn", {"para": "selectAccount"}
        )
        return parse_account_info(html)

    async def get_billed_readings(self) -> list[BilledReading]:
        """Fetch billed consumption per billing period (~quarterly, m³)."""
        html = await self._get_page(
            "/app/capricorn",
            {
                "para": "consumptionInquiry",
                "inquiryType": "water",
                "report": "WATCONRP",
                "tab": "WATERCON",
            },
        )
        return parse_billed_readings(html)

    async def get_daily_readings(
        self, date_from: date, date_to: date
    ) -> list[DailyReading]:
        """Fetch smart-meter daily usage for a window (max ~90 days, m³).

        ``date_to`` is clamped to yesterday: a to-date of today makes the
        portal ignore the requested range and return its default 30-day
        window instead.
        """
        date_to = min(date_to, date.today() - timedelta(days=1))
        if date_to < date_from:
            return []
        html = await self._get_page(
            "/app/capricorn",
            {
                "para": "smartMeterConsum",
                "inquiryType": "water",
                "fromDate": date_from.isoformat(),
                "toDate": date_to.isoformat(),
            },
        )
        return parse_daily_readings(html)

    async def get_hourly_values(self, day: date) -> list[float]:
        """Fetch one day's hourly usage (24 hour-beginning values, m³)."""
        html = await self._get_page(
            "/app/capricorn",
            {
                "para": "smartMeterConsum",
                "type": "hourly",
                "day": day.isoformat(),
                "inquiryType": "water",
            },
        )
        return parse_hourly_values(html)

    async def get_hourly_csv(self, date_from: date, date_to: date) -> list[HourlyReading]:
        """Fetch hourly usage for a window via the portal's CSV export.

        The export serves whatever range the session last queried, so the
        flow mirrors the browser: query the smart-meter page for the
        window (which also renders the daily arrays), ask for a download
        key, then download the CSV from ``/app/ExcelExport``. One page
        render plus two small requests per window; windows beyond ~90
        days time out server-side.
        """
        date_to = min(date_to, date.today() - timedelta(days=1))
        if date_to < date_from:
            return []
        await self._get_page(
            "/app/capricorn",
            {
                "para": "smartMeterConsum",
                "inquiryType": "water",
                "fromDate": date_from.isoformat(),
                "toDate": date_to.isoformat(),
            },
        )
        key = (
            await self._get_page(
                "/app/capricorn",
                {
                    "para": "ajaxDownloadConsumptionData",
                    "type": "smartmeter",
                    "inquiryType": "water",
                },
            )
        ).strip()
        if not key:
            raise MonctonWaterApiError("Portal returned an empty CSV download key")
        # The key arrives percent-encoded and the server decodes it once;
        # re-encoding the raw form reproduces the browser's URL exactly
        # (verified against the live portal).
        quoted = urllib.parse.quote(key, safe="")
        text = await self._request("GET", f"/app/ExcelExport?key={quoted}")
        return parse_hourly_csv(text)

    # ------------------------------------------------------------------
    # Portal plumbing
    # ------------------------------------------------------------------

    async def _login(self, username: str, password: str) -> None:
        """Submit the login form; a failed login re-renders the same page.

        An already-authenticated session (cookies survive from a prior
        login on the shared session, e.g. the config flow's validation)
        gets the meta-refresh redirect page instead of the login form —
        in that case there is nothing to do.
        """
        login_html = await self._request(
            "GET",
            "/app/capricorn",
            params={"para": "index"},
        )
        if not is_login_page(login_html):
            return
        csrf_match = _CSRF_RE.search(login_html)
        if csrf_match is None:
            raise MonctonWaterApiError(
                "Login page has no CSRF token (site changed?)"
            )
        response_html = await self._request(
            "POST",
            "/app/capricorn",
            params={"para": "index"},
            data={
                "jspCSRFToken": csrf_match.group(1),
                "accessCode": username,
                "password": password,
                "rememberMyAccountNumber": "N",
                "nextPara": "",
                "nextPara_attr1": "",
            },
        )
        if is_login_page(response_html):
            raise MonctonWaterAuthError("Invalid credentials")

    async def _get_page(
        self, path: str, params: dict[str, str]
    ) -> str:
        """GET a portal page; raise when the session has expired."""
        html = await self._request("GET", path, params=params)
        if is_login_page(html):
            raise MonctonWaterAuthError("Portal session expired")
        return html

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> str:
        url = f"{self._base}{path}"
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-CA,en;q=0.9",
            "Referer": f"{self._base}/app/capricorn?para=index",
        }
        async with self._session.request(
            method,
            url,
            params=params,
            data=data,
            headers=headers,
            timeout=_REQUEST_TIMEOUT,
            allow_redirects=True,
            ssl=self._ssl_context,
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise MonctonWaterApiError(
                    f"{method} {path} returned HTTP {resp.status}: {text[:200]}"
                )
            return text
