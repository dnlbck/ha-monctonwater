"""Historical water usage import into the recorder statistics table.

The Energy dashboard's water section consumes long-term statistics and
renders each period's consumption as ``change = sum - prev_sum`` — the
``sum`` column must therefore be a **cumulative counter** (the running
total at the END of each period), exactly the way the recorder compiles
native statistics for ``total_increasing`` sensors. Earlier versions of
this module wrote per-period amounts into ``sum``, which rendered as
negative consumption whenever usage fell versus the previous period.

History is backfilled in three phases, each replacing the previous
phase's rows in place (``async_import_statistics`` updates same-period
rows) so no re-import ever duplicates:

1. **Immediate (day resolution)**: every billed period (~3 years,
   quarterly) spread evenly across its days, plus the trailing daily
   window already fetched by the first refresh, imported during setup.
2. **Background (real daily rows)**: the smart meter's daily history is
   re-fetched in 90-day page windows and replaces the spread estimates.
3. **Background (hourly resolution)**: the portal's CSV export (which
   serves hourly values for the session's last queried range) is walked
   in 90-day windows — three requests each — and replaces each day's
   midnight row with 24 hourly rows.

After the backfills, each coordinator refresh imports hourly rows for
newly published days so recent history keeps hourly resolution.
Imported periods always end before the entity's first native statistic,
so imports never collide with recorder-generated rows.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

import aiohttp
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .api import BilledReading, DailyReading, HourlyReading
from .const import (
    BACKFILL_MAX_DAYS,
    BACKFILL_REQUEST_PAUSE,
    DOMAIN,
    QUERY_WINDOW_DAYS,
    REIMPORT_DAYS,
)
from .coordinator import MonctonWaterCoordinator
from .exceptions import MonctonWaterError
from .sensor import UNIQUE_ID_TEMPLATE, WATER_KEY

_LOGGER = logging.getLogger(__name__)

# Billing periods are roughly quarterly; the earliest billed reading has
# no previous read to anchor its span, so assume one standard period.
ASSUMED_FIRST_PERIOD_DAYS = 91

# Rows per async_import_statistics call during the hourly backfill.
HOURLY_IMPORT_CHUNK = 2160


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


def daily_points(
    billed: list[BilledReading],
    daily: list[DailyReading],
    today: date,
) -> list[tuple[date, float]]:
    """Expand history into (day, m³) points, skipping today.

    Billed periods are spread evenly across their days; real smart-meter
    daily readings override the spread for their days (one point per
    day, so every phase's series totals the same amount — the anchor
    the re-anchoring below depends on).
    """
    points: dict[date, float] = {}
    for start, end, consumption in billed_spans(billed):
        days = (end - start).days + 1
        per_day = round(consumption / days, 5)
        for i in range(days):
            points[start + timedelta(days=i)] = per_day
    for reading in daily:
        if reading.consumption_m3 is not None:
            points[reading.day] = reading.consumption_m3
    return [p for p in sorted(points.items()) if p[0] < today]


def _metadata(statistic_id: str) -> StatisticMetaData:
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=None,
        source="recorder",
        statistic_id=statistic_id,
        unit_class="volume",
        unit_of_measurement=UnitOfVolume.CUBIC_METERS,
    )


def _rows_to_statistics(points: list[tuple[date, float]]) -> list[StatisticData]:
    """Build day rows with cumulative sums; see :func:`_reanchor`."""
    statistics: list[StatisticData] = []
    cumulative = 0.0
    for day, consumption in points:
        cumulative += consumption
        statistics.append(
            StatisticData(
                start=dt_util.start_of_local_day(day),
                state=round(cumulative, 3),
                sum=round(cumulative, 3),
            )
        )
    return _reanchor(statistics)


def _reanchor(rows: list[StatisticData]) -> list[StatisticData]:
    """Shift a cumulative series so its final row's sum is 0.

    The dashboard renders consumption as the difference of consecutive
    sums, so imported sums must be cumulative — but the recorder's
    native rows for the same entity anchor their own cumulative sum at
    0 from entity creation. Ending the imported series at 0 makes the
    imported→native boundary render as a clean gap of 0 instead of a
    huge negative spike. Deltas (the rendered consumption) are
    unaffected; only the raw ``sum`` column becomes negative for old
    history, which nothing but the statistics debug graph displays.
    """
    if not rows:
        return rows
    final = rows[-1]["sum"] or 0.0
    return [
        StatisticData(
            start=row["start"],
            state=round((row["state"] or 0.0) - final, 3),
            sum=round((row["sum"] or 0.0) - final, 3),
        )
        for row in rows
    ]


def _hourly_rows(
    readings: list[HourlyReading], seed: float
) -> tuple[list[StatisticData], float]:
    """Build cumulative hourly statistic rows; return (rows, final seed)."""
    statistics: list[StatisticData] = []
    cumulative = seed
    for reading in readings:
        day_start = dt_util.start_of_local_day(reading.day)
        for hour, value in enumerate(reading.values):
            cumulative += value
            statistics.append(
                StatisticData(
                    start=day_start + timedelta(hours=hour),
                    state=round(cumulative, 3),
                    sum=round(cumulative, 3),
                )
            )
    return statistics, round(cumulative, 3)


def _water_statistic_id(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    registry = er.async_get(hass)
    return registry.async_get_entity_id(
        "sensor",
        DOMAIN,
        UNIQUE_ID_TEMPLATE.format(entry_id=entry.entry_id, key=WATER_KEY),
    )


async def _fetch_billed(coordinator: MonctonWaterCoordinator) -> list[BilledReading]:
    if coordinator.history_billed:
        return coordinator.history_billed
    return coordinator.merged_billed(await coordinator.client.get_billed_readings())


async def async_import_history_statistics(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: MonctonWaterCoordinator
) -> None:
    """Phase 1: import the already-fetched history at day resolution."""
    if coordinator.stats_imported:
        return
    statistic_id = _water_statistic_id(hass, entry)
    if statistic_id is None:
        _LOGGER.warning("Water sensor not registered yet; will retry statistics import")
        return
    if not coordinator.history_billed:
        _LOGGER.info("No usage history available; skipping statistics import")
        await coordinator.mark_stats_imported()
        return

    points = daily_points(
        coordinator.history_billed, coordinator.history_daily, dt_util.now().date()
    )
    if points:
        async_import_statistics(hass, _metadata(statistic_id), _rows_to_statistics(points))
        _LOGGER.info(
            "Imported %s day-resolution statistics for %s", len(points), statistic_id
        )
    await coordinator.mark_stats_imported()


def _walk_windows(
    today: date, floor: date
) -> list[tuple[date, date]]:
    """90-day (from, to) windows from yesterday back to the floor."""
    windows: list[tuple[date, date]] = []
    window_to = today - timedelta(days=1)
    while window_to >= floor:
        window_from = max(window_to - timedelta(days=QUERY_WINDOW_DAYS - 1), floor)
        windows.append((window_from, window_to))
        window_to = window_from - timedelta(days=1)
    return windows


async def async_backfill_hourly_statistics(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: MonctonWaterCoordinator
) -> None:
    """Background backfill: rebuild the whole series at hourly resolution.

    Walks the portal's CSV export (the single source of truth for the
    smart-meter era — its hourly and daily numbers agree with each
    other, while the daily page's numbers drift ~2 m³ over two years)
    in 90-day windows from yesterday back to the meter's activation.
    The imported series is ONE continuous cumulative chain:

    * pre-era billing periods, spread evenly across their days, shifted
      so the last pre-era row sits at ``-era_total``;
    * the era's hourly rows chained from there, ending at **exactly 0**.

    Ending at 0 is the point: the recorder's native rows for the same
    entity anchor their own cumulative sum at 0 from entity creation,
    and the dashboard renders the difference of consecutive sums — a
    series ending anywhere else books a huge step at the imported→native
    frontier, and a per-refresh continuation would move that step to the
    current day forever. The walk is ~16 windows; an interrupted run
    rewinds and rewalks.
    """
    if coordinator.backfill_complete:
        return
    statistic_id = _water_statistic_id(hass, entry)
    if statistic_id is None:
        return
    client = coordinator.client
    today = dt_util.now().date()

    billed = await _fetch_billed(coordinator)
    earliest_billed = billed[0].read_date if billed else today - timedelta(
        days=BACKFILL_MAX_DAYS
    )

    readings: list[HourlyReading] = []
    floor = min(earliest_billed, today - timedelta(days=BACKFILL_MAX_DAYS))
    for window_from, window_to in _walk_windows(today, floor):
        try:
            batch = await client.get_hourly_csv(window_from, window_to)
        except (MonctonWaterError, TimeoutError, OSError, aiohttp.ClientError) as err:
            _LOGGER.warning(
                "Hourly backfill stopped at %s (%s); it will retry on restart",
                window_to,
                err,
            )
            return
        if not batch:
            _LOGGER.debug("Hourly backfill reached the start of data at %s", window_from)
            break
        readings.extend(batch)
        await asyncio.sleep(BACKFILL_REQUEST_PAUSE)

    if readings:
        readings.sort(key=lambda r: r.day)
        era_start = readings[0].day

        # Era hourly rows end at exactly 0 by construction: the chain
        # starts at minus the era total (accumulated in the same order
        # as the rows are built, so float addition cannot drift).
        era_total = 0.0
        for reading in readings:
            era_total += sum(reading.values)

        rows: list[StatisticData] = []
        # Pre-era spread, shifted to hand off to the era chain.
        cumulative = 0.0
        pre_era_points = [p for p in daily_points(billed, [], today) if p[0] < era_start]
        pre_era_total = sum(value for _, value in pre_era_points)
        for day, value in pre_era_points:
            cumulative += value
            shifted = cumulative - pre_era_total - era_total
            rows.append(
                StatisticData(
                    start=dt_util.start_of_local_day(day),
                    state=round(shifted, 3),
                    sum=round(shifted, 3),
                )
            )

        era_rows, _ = _hourly_rows(readings, -era_total)
        rows.extend(era_rows)

        for i in range(0, len(rows), HOURLY_IMPORT_CHUNK):
            async_import_statistics(
                hass, _metadata(statistic_id), rows[i : i + HOURLY_IMPORT_CHUNK]
            )
        await coordinator.store_hourly_progress(readings[-1].day, 0.0)
        _LOGGER.info(
            "Imported %s statistics (%s hourly days + %s spread days, "
            "%s to %s, ending at 0) for %s",
            len(rows),
            len(readings),
            len(pre_era_points),
            rows[0]["start"].date(),
            readings[-1].day,
            statistic_id,
        )
    else:
        _LOGGER.info("No smart-meter history found; keeping spread-only import")
    await coordinator.mark_backfill_complete()
    await coordinator.mark_hourly_complete()


async def _last_sum_before(
    hass: HomeAssistant, statistic_id: str, before: datetime
) -> float | None:
    """Return the cumulative sum of the last statistic row before ``before``."""
    from homeassistant.components.recorder.statistics import (
        statistics_during_period,
    )

    stats = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        before - timedelta(days=2),
        before,
        (statistic_id,),
        "hour",
        None,
        {"sum"},
    )
    rows = stats.get(statistic_id) or []
    if not rows:
        return None
    return rows[-1]["sum"]


async def async_import_recent_hourly(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: MonctonWaterCoordinator
) -> None:
    """Keep recent days at hourly resolution.

    Runs after each coordinator refresh once the hourly backfill has
    completed. Re-imports the trailing few days (the recorder's native
    rows book each day's usage as one lump when the portal publishes
    it, and compaction may rewrite recent hours), chaining the
    cumulative sums from the actual last stored row so the imported and
    native series stay continuous.
    """
    if not coordinator.hourly_complete:
        return
    statistic_id = _water_statistic_id(hass, entry)
    if statistic_id is None:
        return
    data = coordinator.data or {}
    last_daily: date | None = data.get("last_daily_date")
    through = coordinator.stored_hourly_through()
    if last_daily is None or through is None:
        return
    if last_daily <= through:
        return  # nothing new since the last hourly import

    # Re-import the trailing window, not just the new day: the
    # recorder's native rows book each day's usage as one lump when it
    # publishes, and compaction can rewrite recent hours.
    start_day = last_daily - timedelta(days=REIMPORT_DAYS)
    seed = await _last_sum_before(
        hass, statistic_id, dt_util.start_of_local_day(start_day)
    )
    if seed is None:
        return  # nothing stored yet; the next run retries

    client = coordinator.client
    rows: list[StatisticData] = []
    day = start_day
    while day <= last_daily:
        try:
            values = await client.get_hourly_values(day)
        except (MonctonWaterError, TimeoutError, OSError, aiohttp.ClientError) as err:
            _LOGGER.warning("Recent hourly import stopped at %s (%s)", day, err)
            break
        if not values:
            break  # not published yet; a later refresh picks it up
        day_rows, seed = _hourly_rows([HourlyReading(day=day, values=values)], seed)
        rows.extend(day_rows)
        day += timedelta(days=1)
    if rows:
        async_import_statistics(hass, _metadata(statistic_id), rows)
        await coordinator.store_hourly_progress(day - timedelta(days=1), seed)
        _LOGGER.info(
            "Imported %s recent hourly rows (%s to %s), through advanced to %s",
            len(rows),
            start_day,
            day - timedelta(days=1),
            day - timedelta(days=1),
        )
    else:
        _LOGGER.warning(
            "Recent hourly import produced no rows (start %s, last_daily %s)",
            start_day,
            last_daily,
        )
