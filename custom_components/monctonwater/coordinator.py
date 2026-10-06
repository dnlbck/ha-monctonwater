"""DataUpdateCoordinator for the Moncton Water integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import BilledReading, MonctonWaterClient
from .const import (
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .exceptions import (
    MonctonWaterAuthError,
    MonctonWaterError,
    MonctonWaterSessionError,
)

_LOGGER = logging.getLogger(__name__)

ATTR_DAILY_M3 = "daily_m3"

# How many days of daily readings to fetch per refresh. One billing
# quarter is ~91 days, so this always covers the whole current period
# even right before the next read date.
REFRESH_WINDOW_DAYS = 100

# A fetched billed read this close to a stored one that vanished is the
# same read re-dated. Reads are ~91 days apart, so no real neighbour is
# ever this close.
REDATED_READ_DAYS = 15

# The billed table changes once a quarter. Re-reading it once a day still
# picks up a new bill within a day, and spares the slow portal a page
# render on every other refresh; in between, the persisted history serves.
BILLED_REFRESH_INTERVAL = timedelta(hours=24)


class MonctonWaterCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate portal data and the cumulative water counter.

    The counter is derived on every refresh from the portal's own books:

    * billed readings — one row per billing period (~quarterly) with the
      period's total m³; the deep history and the counter's anchor.
    * daily smart-meter readings — the trailing window including the
      current period to date.

    Summing every billed period plus the daily rows that fall after the
    last billed read date gives a consistent cumulative total with no
    persisted meter state (the portal publishes yesterday's usage within
    24 hours, so the daily rows and the billed books butt cleanly).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: MonctonWaterClient,
        username: str,
        password: str,
        scan_interval: timedelta | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=max(
                scan_interval or DEFAULT_SCAN_INTERVAL, MIN_SCAN_INTERVAL
            ),
        )
        self.client = client
        self._username = username
        self._password = password
        self._store = Store[dict[str, Any]](
            hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}"
        )
        # Daily totals of the trailing window at the last usage-statistic
        # import: the portal publishes yesterday progressively, so a
        # changed total means the days need importing again.
        self._import_totals: dict[str, float | None] | None = None
        # Serializes usage-statistic runs (background updates, rebuilds).
        self.statistic_lock = asyncio.Lock()
        self._last_cumulative_m3: float | None = None
        self._warned_no_daily = False
        # The portal's billed table is a rolling window (~13 periods):
        # when a new quarter bills, the oldest drops off. The counter
        # and the statistics backfill must work from the full history,
        # so readings are merged into persisted storage as they appear.
        self._billed_history: dict[date, float] = {}
        self._billed_dirty = False
        self._billed_checked: datetime | None = None

    async def async_load_stored(self) -> None:
        """Load the monotonic-counter floor and the billed history.

        Keys older versions stored (backfill flags, hourly seeds) are
        ignored: the usage statistic now keeps its own progress.
        """
        raw = await self._store.async_load() or {}
        self._import_totals = raw.get("import_totals")
        self._last_cumulative_m3 = raw.get("last_cumulative_m3")
        self._billed_history = {
            date.fromisoformat(item[0]): float(item[1])
            for item in raw.get("billed_history") or []
        }

    def merged_billed(self, fetched: list[BilledReading]) -> list[BilledReading]:
        """Merge freshly fetched readings over the persisted history.

        Fetched values win for their dates (the portal may revise). A
        stored read the portal no longer shows is retained — its rolling
        window dropped it, or the table glitched — unless a fetched read
        lies within REDATED_READ_DAYS of it: then the portal re-dated
        that read (e.g. an estimate replaced by an actual read) and
        keeping both would count the period twice. Returns the merged
        list sorted ascending.
        """
        if fetched:
            fresh = {reading.read_date: reading.consumption_m3 for reading in fetched}
            merged = dict(fresh)
            for day, value in self._billed_history.items():
                if day not in fresh and not any(
                    abs((day - other).days) <= REDATED_READ_DAYS for other in fresh
                ):
                    merged[day] = value
            if merged != self._billed_history:
                self._billed_history = merged
                self._billed_dirty = True
        return self.billed_history()

    def billed_history(self) -> list[BilledReading]:
        """Return every known billed period, sorted ascending."""
        return [
            BilledReading(read_date=day, consumption_m3=value)
            for day, value in sorted(self._billed_history.items())
        ]

    async def store_import_totals(self, totals: dict[str, float | None] | None) -> None:
        """Persist the daily totals the last statistic import settled on.

        None makes the next refresh import the trailing window again.
        """
        self._import_totals = totals
        await self._async_save_stored()

    def stored_import_totals(self) -> dict[str, float | None] | None:
        """Return the daily totals the last statistic import settled on."""
        return self._import_totals

    async def _async_save_stored(self) -> None:
        await self._store.async_save(
            {
                "import_totals": self._import_totals,
                "last_cumulative_m3": self._last_cumulative_m3,
                "billed_history": [
                    [day.isoformat(), value]
                    for day, value in sorted(self._billed_history.items())
                ],
            }
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch water data, logging in again if the session expired.

        Portal sessions lapse between refreshes routinely; re-login is
        transparent. Only rejected credentials surface as
        ConfigEntryAuthFailed — that stops polling until the user signs
        in again, so anything a fresh login can fix must not reach it.
        """
        try:
            try:
                if not self.client.logged_in:
                    await self.client.bootstrap(self._username, self._password)
                return await self._async_fetch_all()
            except MonctonWaterSessionError as err:
                _LOGGER.debug("Portal session unusable (%s); logging in again", err)
                self.client.invalidate()
                await self.client.bootstrap(self._username, self._password)
                return await self._async_fetch_all(fresh_session=True)
        except MonctonWaterAuthError as err:
            raise ConfigEntryAuthFailed(
                f"Could not sign in to Moncton MyAccount: {err}"
            ) from err
        except MonctonWaterError as err:
            raise UpdateFailed(f"Moncton portal error: {err}") from err
        except (TimeoutError, aiohttp.ClientError) as err:
            # Timeouts carry no message; the type is the useful part.
            raise UpdateFailed(
                f"Error communicating with Moncton MyAccount: {err!r}"
            ) from err

    async def _async_fetch_all(self, *, fresh_session: bool = False) -> dict[str, Any]:
        now = dt_util.now()
        today = now.date()

        if (
            self._billed_checked is None
            or now - self._billed_checked >= BILLED_REFRESH_INTERVAL
        ):
            billed = self.merged_billed(await self.client.get_billed_readings())
            self._billed_checked = now
        else:
            billed = self.billed_history()
        daily = await self.client.get_daily_readings(
            today - timedelta(days=REFRESH_WINDOW_DAYS), today
        )
        if billed and not daily:
            if not fresh_session:
                # Observed live: a long-lived portal session can serve
                # the billed table fine but empty smart-meter arrays.
                # Treat it as a stale session and let the retry path log
                # in fresh rather than recording a day with no usage.
                raise MonctonWaterSessionError("smart meter page returned no data")
            # Still empty on a brand-new session, so not a session
            # problem: an account without smart-meter data, or the meter
            # backend is down. Carry on with the billed books; the
            # counter's floor holds it until daily rows return.
            if not self._warned_no_daily:
                _LOGGER.warning(
                    "Smart meter page returned no data after a fresh login; "
                    "using billed periods only"
                )
                self._warned_no_daily = True
        elif daily:
            self._warned_no_daily = False

        last_billed_date = billed[-1].read_date if billed else None

        # Billed periods anchor the counter; daily rows after the last
        # billed read date carry the current period to date. Daily rows
        # on/before that date are already inside the billed books.
        billed_total = sum(reading.consumption_m3 for reading in billed)
        current_period = [
            reading
            for reading in daily
            if last_billed_date is None or reading.day > last_billed_date
        ]
        total = billed_total + sum(reading.consumption_m3 for reading in current_period)
        derived_m3 = round(total, 3)

        # A late meter correction can momentarily shrink the derived
        # total; the water dashboard treats a decreasing total_increasing
        # sensor as a meter reset (huge spikes). Hold the last value until
        # the books catch back up.
        cumulative_m3 = (
            max(derived_m3, self._last_cumulative_m3)
            if self._last_cumulative_m3 is not None
            else derived_m3
        )
        if cumulative_m3 != self._last_cumulative_m3 or self._billed_dirty:
            self._last_cumulative_m3 = cumulative_m3
            self._billed_dirty = False
            self.hass.async_create_task(self._async_save_stored())

        last_daily = daily[-1] if daily else None
        daily_average = (
            round(
                sum(reading.consumption_m3 for reading in daily) / len(daily), 3
            )
            if daily
            else None
        )
        return {
            "cumulative_m3": cumulative_m3,
            "derived_m3": derived_m3,
            "billed_periods": len(billed),
            "billed_total_m3": round(billed_total, 3),
            "last_daily_m3": last_daily.consumption_m3 if last_daily else None,
            "last_daily_date": last_daily.day if last_daily else None,
            "last_billed_m3": billed[-1].consumption_m3 if billed else None,
            "last_billed_date": last_billed_date,
            "daily_average_m3": daily_average,
            ATTR_DAILY_M3: {
                reading.day.isoformat(): reading.consumption_m3
                for reading in daily
            },
            "account_number": (
                self.client.account.account_number if self.client.account else None
            ),
            "meter_id": (
                self.client.account.meter_id if self.client.account else None
            ),
            "service_address": (
                self.client.account.service_address if self.client.account else None
            ),
            "last_updated": now,
        }
