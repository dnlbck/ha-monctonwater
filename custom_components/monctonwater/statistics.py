"""Usage history as an external long-term statistic.

The Energy dashboard renders a statistic's consumption per period as
``sum - previous sum``, so a statistic must be one cumulative chain. The
portal publishes usage a day late (yesterday appears within 24 hours,
in stages), and the water sensor's own statistic cannot hold that
history: HA compiles it from the sensor's live state, booking each day
when it publishes. Importing history into it as well leaves two
cumulative chains in one statistic, and they never line up.

So the history lives in a statistic of its own,
``monctonwater:water_usage_<account>``, written only by this module:

* before the smart meter: billed periods spread evenly across their
  days (one row per day, at local midnight);
* the smart-meter era: hourly rows from the portal's CSV export, the
  single source of truth for that era (its hourly and daily numbers
  agree; the daily page's drift ~2 m³ over two years). The portal keeps
  two years of meter data, so a backfill reaches back that far; hourly
  rows imported earlier stay in the statistic after the portal drops
  them.

Every run reads the statistic's last row back from the recorder and
continues the chain from it. An empty statistic — first setup, or
cleared by the user — gets the full backfill; afterwards new days are
appended (catching up after any downtime), and the trailing days are
imported again while the portal is still revising them.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta

import aiohttp
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .api import BilledReading, HourlyReading, MonctonWaterClient
from .const import (
    BACKFILL_MAX_DAYS,
    BACKFILL_REQUEST_PAUSE,
    CONF_BACKFILL_DAILY,
    DOMAIN,
    QUERY_WINDOW_DAYS,
    REIMPORT_DAYS,
)
from .coordinator import ATTR_DAILY_M3, MonctonWaterCoordinator
from .exceptions import MonctonWaterApiError, MonctonWaterError, MonctonWaterSessionError

_LOGGER = logging.getLogger(__name__)

# Billing periods are roughly quarterly; the earliest billed reading has
# no previous read to anchor its span, so assume one standard period.
ASSUMED_FIRST_PERIOD_DAYS = 91

# Rows per statistics import call.
IMPORT_CHUNK = 2160

# A re-imported day counts as settled once its hourly rows add up to the
# portal's daily total within this; until then each refresh retries.
SETTLED_TOLERANCE_M3 = 0.02

# How far back to look for the row a re-import continues from.
SEED_LOOKBACK_DAYS = 14

# What a portal run can fail with; the next refresh tries again.
_PORTAL_ERRORS = (MonctonWaterError, TimeoutError, OSError, aiohttp.ClientError)


def _account(entry: ConfigEntry) -> str:
    return (entry.unique_id or "").removeprefix("account_") or entry.entry_id


def statistic_id(entry: ConfigEntry) -> str:
    """Return the entry's usage statistic: monctonwater:water_usage_<account>."""
    slug = re.sub(r"[^a-z0-9]+", "_", _account(entry).lower()).strip("_")
    return f"{DOMAIN}:water_usage_{slug}"


def _metadata(entry: ConfigEntry) -> StatisticMetaData:
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=f"Moncton Water usage ({_account(entry)})",
        source=DOMAIN,
        statistic_id=statistic_id(entry),
        unit_class="volume",
        unit_of_measurement=UnitOfVolume.CUBIC_METERS,
    )


def billed_spans(
    billed: list[BilledReading],
) -> list[tuple[date, date, float]]:
    """Turn read-date readings into (start, end, m³) period spans.

    A reading billed at read date R covers the days since the previous
    read date; the earliest reading is assumed to span one standard
    quarterly period ending at its read date.
    """
    spans: list[tuple[date, date, float]] = []
    for i, reading in enumerate(billed):
        end = reading.read_date
        if i == 0:
            start = end - timedelta(days=ASSUMED_FIRST_PERIOD_DAYS - 1)
        else:
            start = billed[i - 1].read_date + timedelta(days=1)
        if end >= start:
            spans.append((start, end, reading.consumption_m3))
    return spans


def spread_days(
    spans: list[tuple[date, date, float]], first: date, last: date
) -> list[tuple[date, float]]:
    """(day, m³) for every day in [first, last] a billed span covers.

    Each period's consumption is spread evenly across its days, so a
    whole period adds back up to its bill.
    """
    days: list[tuple[date, float]] = []
    for start, end, consumption in spans:
        per_day = consumption / ((end - start).days + 1)
        day = max(start, first)
        while day <= min(end, last):
            days.append((day, per_day))
            day += timedelta(days=1)
    return days


def _day_rows(
    days: list[tuple[date, float]], seed: float
) -> tuple[list[StatisticData], float]:
    """One row per day at local midnight; return (rows, running total)."""
    rows: list[StatisticData] = []
    total = seed
    for day, consumption in days:
        total += consumption
        rows.append(
            StatisticData(
                start=dt_util.start_of_local_day(day),
                state=round(total, 3),
                sum=round(total, 3),
            )
        )
    return rows, total


def _hourly_rows(
    readings: list[HourlyReading], seed: float
) -> tuple[list[StatisticData], float]:
    """Hour-beginning rows (local wall clock); return (rows, running total)."""
    rows: list[StatisticData] = []
    total = seed
    for reading in readings:
        day_start = dt_util.start_of_local_day(reading.day)
        for hour, value in enumerate(reading.values):
            total += value
            rows.append(
                StatisticData(
                    start=day_start + timedelta(hours=hour),
                    state=round(total, 3),
                    sum=round(total, 3),
                )
            )
    return rows, total


def _import(hass: HomeAssistant, entry: ConfigEntry, rows: list[StatisticData]) -> None:
    metadata = _metadata(entry)
    for i in range(0, len(rows), IMPORT_CHUNK):
        async_add_external_statistics(hass, metadata, rows[i : i + IMPORT_CHUNK])


async def _last_row(
    hass: HomeAssistant, statistic: str
) -> tuple[datetime, float] | None:
    """Return the (start, sum) of the statistic's last row, if any."""
    # The recorder's own executor: database access from HA's generic one
    # is slower and logs a "without the database executor" warning.
    stats = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic, False, {"sum"}
    )
    rows = stats.get(statistic)
    if not rows:
        return None
    return dt_util.utc_from_timestamp(rows[0]["start"]), rows[0]["sum"] or 0.0


async def _sum_before(
    hass: HomeAssistant, statistic: str, before: datetime
) -> float | None:
    """Return the sum of the statistic's last row before ``before``."""
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        before - timedelta(days=SEED_LOOKBACK_DAYS),
        before,
        {statistic},
        "hour",
        None,
        {"sum"},
    )
    rows = stats.get(statistic) or []
    return rows[-1]["sum"] if rows else None


async def _portal_call[_T](
    client: MonctonWaterClient, call: Callable[[], Awaitable[_T]]
) -> _T:
    """Run a portal call, signing in again once if the session lapsed.

    The backfill walk easily outlives the portal's short sessions.
    """
    try:
        return await call()
    except MonctonWaterSessionError:
        client.invalidate()
        await client.ensure_session()
        return await call()


def _walk_windows(today: date, floor: date) -> list[tuple[date, date]]:
    """90-day (from, to) windows from yesterday back to the floor."""
    windows: list[tuple[date, date]] = []
    window_to = today - timedelta(days=1)
    while window_to >= floor:
        window_from = max(window_to - timedelta(days=QUERY_WINDOW_DAYS - 1), floor)
        windows.append((window_from, window_to))
        window_to = window_from - timedelta(days=1)
    return windows


async def _fetch_hourly(
    client: MonctonWaterClient, first: date, last: date
) -> list[HourlyReading]:
    """Hourly readings for [first, last] via the CSV export, oldest first."""
    by_day: dict[date, HourlyReading] = {}
    window_from = first
    while window_from <= last:
        window_to = min(window_from + timedelta(days=QUERY_WINDOW_DAYS - 1), last)
        batch = await _portal_call(
            client,
            lambda: client.get_hourly_csv(window_from, window_to),  # noqa: B023
        )
        by_day.update((reading.day, reading) for reading in batch)
        window_from = window_to + timedelta(days=1)
        if window_from <= last:
            await asyncio.sleep(BACKFILL_REQUEST_PAUSE)
    return [by_day[day] for day in sorted(by_day)]


async def async_update_usage_statistic(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: MonctonWaterCoordinator
) -> None:
    """Create or extend the entry's usage statistic (see module docstring).

    Runs at setup and after every coordinator refresh. Portal errors are
    logged and left to the next run.
    """
    async with coordinator.statistic_lock:
        try:
            await _update(hass, entry, coordinator)
        except _PORTAL_ERRORS as err:
            _LOGGER.warning(
                "Usage statistic %s not updated (%r); the next refresh retries",
                statistic_id(entry),
                err,
            )


async def async_rebuild_usage_statistic(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: MonctonWaterCoordinator
) -> None:
    """Clear the entry's usage statistic and import it again from scratch."""
    async with coordinator.statistic_lock:
        recorder = get_instance(hass)
        recorder.async_clear_statistics([statistic_id(entry)])
        await recorder.async_block_till_done()
        await coordinator.store_import_totals(None)
        try:
            await _update(hass, entry, coordinator)
        except _PORTAL_ERRORS as err:
            raise HomeAssistantError(
                f"Rebuilding {statistic_id(entry)} failed ({err!r}); "
                "the next refresh tries again"
            ) from err


async def _update(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: MonctonWaterCoordinator
) -> None:
    if coordinator.data is None:
        return
    last = await _last_row(hass, statistic_id(entry))
    if last is None:
        await _backfill(
            hass, entry, coordinator, full=entry.options.get(CONF_BACKFILL_DAILY, True)
        )
    else:
        await _extend(hass, entry, coordinator, *last)
    # The next run reads the chain back from the recorder, so what was
    # just queued must be committed by then.
    await get_instance(hass).async_block_till_done()


async def _backfill(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: MonctonWaterCoordinator,
    *,
    full: bool,
) -> None:
    """Build the chain from scratch (empty statistic).

    ``full`` walks the CSV export from yesterday back to the start of
    the portal's meter data (two years at most: ~9 windows) and spreads
    the billed periods before it; otherwise the chain starts with the
    trailing days (or, without smart-meter data, the latest billed
    period).
    """
    client = coordinator.client
    last_daily: date | None = coordinator.data.get("last_daily_date")
    today = dt_util.now().date()
    spans = billed_spans(coordinator.billed_history())
    if full:
        floor = min(
            spans[0][0] if spans else today, today - timedelta(days=BACKFILL_MAX_DAYS)
        )
    elif last_daily is not None:
        floor = last_daily - timedelta(days=REIMPORT_DAYS)
    else:
        floor = spans[-1][0] if spans else today

    by_day: dict[date, HourlyReading] = {}
    reached_start = False
    if last_daily is not None:  # without smart-meter data, nothing to walk
        for window_from, window_to in _walk_windows(today, floor):
            batch = await _portal_call(
                client,
                lambda: client.get_hourly_csv(window_from, window_to),  # noqa: B023
            )
            if not batch:
                if not by_day:
                    # The daily page has data, so an empty newest window
                    # is a portal glitch, not an account without a meter.
                    raise MonctonWaterApiError("CSV export served no recent data")
                reached_start = True
                break
            by_day.update((reading.day, reading) for reading in batch)
            await asyncio.sleep(BACKFILL_REQUEST_PAUSE)
    readings = [by_day[day] for day in sorted(by_day)]
    if reached_start and readings:
        # The portal keeps two years (730 days) of meter data, rolling by
        # the hour, so the oldest day it still has is cut short (seen
        # live: 2024-10-06 held 0.132 of its 0.346 m³) — as is a new
        # meter's first day. The billed spread covers that day instead.
        readings = readings[1:]

    # Billed spreads cover what the meter does not: up to its first
    # reading, or every period for an account without a smart meter.
    spread_last = (
        readings[0].day - timedelta(days=1)
        if readings
        else (spans[-1][1] if spans else floor)
    )
    spread = spread_days(spans, floor, spread_last)
    rows, total = _day_rows(spread, 0.0)
    hourly, total = _hourly_rows(readings, total)
    rows.extend(hourly)
    if not rows:
        _LOGGER.info("No usage history to import yet")
        return
    _import(hass, entry, rows)
    _LOGGER.info(
        "Imported %s rows into %s: %s spread days, %s hourly days, through %s (%.3f m³)",
        len(rows),
        statistic_id(entry),
        len(spread),
        len(readings),
        readings[-1].day if readings else spread[-1][0],
        total,
    )


async def _extend(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: MonctonWaterCoordinator,
    last_start: datetime,
    last_sum: float,
) -> None:
    """Continue the chain from its last row."""
    data = coordinator.data
    last_local = dt_util.as_local(last_start)
    last_day = last_local.date()
    last_daily: date | None = data.get("last_daily_date")

    if last_daily is None:
        # No smart-meter data this refresh. A chain of day rows (an
        # account without a meter) grows as new periods bill; an hourly
        # chain just waits for the meter's data to return.
        if last_local.hour == 0:
            spans = billed_spans(coordinator.billed_history())
            if spans:
                days = spread_days(spans, last_day + timedelta(days=1), spans[-1][1])
                rows, _ = _day_rows(days, last_sum)
                if rows:
                    _import(hass, entry, rows)
                    _LOGGER.info(
                        "Extended %s with %s billed days", statistic_id(entry), len(rows)
                    )
        return
    if last_daily < last_day:
        # The portal's daily window fell behind the chain (a glitch):
        # importing would leave the chain's newer rows stale. Wait.
        return

    # The trailing days are imported again until the portal stops
    # revising them: yesterday publishes in stages (live: 0.582 m³ at
    # 02:00, revised to 0.659 m³ by 06:00).
    trailing = last_daily - timedelta(days=REIMPORT_DAYS)
    daily: dict[str, float] = data.get(ATTR_DAILY_M3) or {}
    totals = {
        key: daily.get(key)
        for key in (
            (trailing + timedelta(days=i)).isoformat() for i in range(REIMPORT_DAYS + 1)
        )
    }
    if last_daily <= last_day and totals == coordinator.stored_import_totals():
        return  # nothing new or revised since the last import

    # From the day after the chain's end when catching up after downtime,
    # otherwise from the start of the trailing window.
    readings = await _fetch_hourly(
        coordinator.client, min(last_day + timedelta(days=1), trailing), last_daily
    )
    if not readings or readings[-1].day < last_day:
        # Stopping short of the chain's end would leave stale rows after
        # the re-imported ones; the export lags, so a later run retries.
        return
    first = readings[0].day
    if first > last_day:
        seed = last_sum
    else:
        seed = await _sum_before(
            hass, statistic_id(entry), dt_util.start_of_local_day(first)
        )
        if seed is None:
            return
    rows, _ = _hourly_rows(readings, seed)
    _import(hass, entry, rows)

    settled = readings[-1].day == last_daily and all(
        abs(sum(reading.values) - daily[reading.day.isoformat()]) <= SETTLED_TOLERANCE_M3
        for reading in readings
        if reading.day >= trailing and reading.day.isoformat() in daily
    )
    await coordinator.store_import_totals(totals if settled else None)
    _LOGGER.debug(
        "Imported %s hourly rows into %s (%s to %s)%s",
        len(rows),
        statistic_id(entry),
        first,
        readings[-1].day,
        "" if settled else "; not settled yet",
    )
