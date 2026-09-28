"""DataUpdateCoordinator for the Moncton Water integration."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import BilledReading, DailyReading, MonctonWaterClient
from .const import (
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
    STATS_GEN,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .exceptions import MonctonWaterApiError, MonctonWaterAuthError

_LOGGER = logging.getLogger(__name__)

ATTR_DAILY_M3 = "daily_m3"

# How many days of daily readings to fetch per refresh. One billing
# quarter is ~91 days, so this always covers the whole current period
# even right before the next read date.
REFRESH_WINDOW_DAYS = 100


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
        # Rows captured on the first refresh, consumed by the one-time
        # statistics import once the entities are registered.
        self.history_billed: list[BilledReading] = []
        self.history_daily: list[DailyReading] = []
        self.stats_imported = False
        self.backfill_complete = False
        self.hourly_complete = False
        self._hourly_through: date | None = None
        self._hourly_seed: float | None = None
        self._last_cumulative_m3: float | None = None
        # The portal's billed table is a rolling window (~13 periods):
        # when a new quarter bills, the oldest drops off. The counter
        # and the statistics backfill must work from the full history,
        # so readings are merged into persisted storage as they appear.
        self._billed_history: dict[date, float] = {}
        self._billed_dirty = False

    async def async_load_stored(self) -> None:
        """Load backfill progress and the monotonic-counter floor."""
        raw = await self._store.async_load() or {}
        if raw and raw.get("stats_gen") != STATS_GEN:
            # v0.1 wrote per-period amounts into the statistics sum
            # column; the dashboard renders sum deltas, so those rows
            # must be re-imported with cumulative sums. The cumulative
            # counter's floor survives the reset — losing it would let
            # the sensor dip and book a negative.
            _LOGGER.info("Resetting backfill progress to repair statistics convention")
            raw = {"last_cumulative_m3": raw.get("last_cumulative_m3")}
        self.stats_imported = bool(raw.get("stats_imported"))
        self.backfill_complete = bool(raw.get("backfill_complete"))
        self.hourly_complete = bool(raw.get("hourly_complete"))
        through = raw.get("hourly_through")
        self._hourly_through = date.fromisoformat(through) if through else None
        self._hourly_seed = raw.get("hourly_seed")
        self._last_cumulative_m3 = raw.get("last_cumulative_m3")
        self._billed_history = {
            date.fromisoformat(item[0]): float(item[1])
            for item in raw.get("billed_history") or []
        }

    def merged_billed(self, fetched: list[BilledReading]) -> list[BilledReading]:
        """Merge freshly fetched readings over the persisted history.

        Fetched values win for their dates (the portal may revise);
        readings the rolling window dropped are retained from storage.
        Returns the merged list sorted ascending.
        """
        changed = False
        for reading in fetched:
            if self._billed_history.get(reading.read_date) != reading.consumption_m3:
                changed = True
            self._billed_history[reading.read_date] = reading.consumption_m3
        self._billed_dirty = changed or self._billed_dirty
        return [
            BilledReading(read_date=day, consumption_m3=value)
            for day, value in sorted(self._billed_history.items())
        ]

    async def mark_stats_imported(self) -> None:
        """Persist that the day-resolution statistics import completed."""
        self.stats_imported = True
        await self._async_save_stored()

    async def mark_backfill_complete(self) -> None:
        """Persist that the daily-resolution upgrade backfill completed."""
        self.backfill_complete = True
        await self._async_save_stored()

    async def mark_hourly_complete(self) -> None:
        """Persist that the hourly backfill completed."""
        self.hourly_complete = True
        await self._async_save_stored()

    async def store_hourly_progress(self, through: date, seed: float) -> None:
        """Persist the hourly import's resume point and running total."""
        self._hourly_through = through
        self._hourly_seed = seed
        await self._async_save_stored()

    def stored_hourly_through(self) -> date | None:
        """Return the last day imported at hourly resolution."""
        return self._hourly_through

    def stored_hourly_seed(self) -> float | None:
        """Return the running total through the last hourly-imported day."""
        return self._hourly_seed

    async def _async_save_stored(self) -> None:
        await self._store.async_save(
            {
                "stats_gen": STATS_GEN,
                "stats_imported": self.stats_imported,
                "backfill_complete": self.backfill_complete,
                "hourly_complete": self.hourly_complete,
                "hourly_through": (
                    self._hourly_through.isoformat()
                    if self._hourly_through
                    else None
                ),
                "hourly_seed": self._hourly_seed,
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
        transparent. Only a failed login (bad credentials) surfaces as
        ConfigEntryAuthFailed.
        """
        try:
            if not self.client.logged_in:
                await self.client.bootstrap(self._username, self._password)
            try:
                return await self._async_fetch_all()
            except MonctonWaterAuthError:
                _LOGGER.debug("Portal session expired; logging in again")
                self.client.invalidate()
                await self.client.bootstrap(self._username, self._password)
                return await self._async_fetch_all()
        except MonctonWaterAuthError as err:
            raise ConfigEntryAuthFailed(
                f"Could not sign in to Moncton MyAccount: {err}"
            ) from err
        except MonctonWaterApiError as err:
            raise UpdateFailed(f"Moncton portal error: {err}") from err
        except (TimeoutError, aiohttp.ClientError) as err:
            raise UpdateFailed(
                f"Error communicating with Moncton MyAccount: {err}"
            ) from err

    async def _async_fetch_all(self) -> dict[str, Any]:
        now = dt_util.now()
        today = now.date()

        billed = self.merged_billed(await self.client.get_billed_readings())
        daily = await self.client.get_daily_readings(
            today - timedelta(days=REFRESH_WINDOW_DAYS), today
        )

        last_billed_date = billed[-1].read_date if billed else None

        # Billed periods anchor the counter; daily rows after the last
        # billed read date carry the current period to date. Daily rows
        # on/before that date are already inside the billed books.
        total = sum(reading.consumption_m3 for reading in billed)
        current_period = [
            reading
            for reading in daily
            if last_billed_date is None or reading.day > last_billed_date
        ]
        total += sum(reading.consumption_m3 for reading in current_period)
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

        if not self.stats_imported and not self.history_billed:
            self.history_billed = billed
            # The full fetched window (real readings override the spread
            # of overlapping billing periods) — not just the current
            # period, so the phase-1 series totals the same amount the
            # later backfill phases will.
            self.history_daily = daily

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
            "billed_total_m3": round(total - sum(r.consumption_m3 for r in current_period), 3),
            "last_daily_m3": last_daily.consumption_m3 if last_daily else None,
            "last_daily_date": last_daily.day if last_daily else None,
            "last_billed_m3": billed[-1].consumption_m3 if billed else None,
            "last_billed_date": last_billed_date,
            "daily_average_m3": daily_average,
            "billed_periods": len(billed),
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
