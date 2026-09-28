"""Home Assistant-level tests: setup, sensors, config flow, statistics backfill."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from conftest import (
    ACCOUNT_NUMBER,
    VALID_CREDS,
    billed_readings,
    daily_value,
    server_base,
    smart_meter_start,
)
from custom_components.monctonwater.const import DOMAIN
from custom_components.monctonwater.sensor import UNIQUE_ID_TEMPLATE, WATER_KEY

USERNAME = next(iter(VALID_CREDS))
PASSWORD = VALID_CREDS[USERNAME]
ACCOUNT_TITLE = f"Moncton Water {ACCOUNT_NUMBER}"
FLOW_INPUT = {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}


def _expected_sensor_cumulative() -> float:
    """The counter: billed periods plus daily rows after the last read."""
    billed = billed_readings()
    last_read = billed[0][0]
    today = date.today()
    total = sum(m3 for _, m3 in billed)
    day = max(smart_meter_start(), last_read + timedelta(days=1))
    while day <= today - timedelta(days=1):
        total += daily_value(day)
        day += timedelta(days=1)
    return round(total, 3)


def _expected_cumulative() -> float:
    """Spread total with the fetched window's days replaced by real readings."""
    billed = billed_readings()
    today = date.today()
    window_from = max(smart_meter_start(), today - timedelta(days=100))
    total = 0.0
    for i, (read, m3) in enumerate(billed):
        prev = (
            billed[i + 1][0]
            if i + 1 < len(billed)
            else read - timedelta(days=91)  # ASSUMED_FIRST_PERIOD_DAYS
        )
        # The span is (prev read + 1 .. read) — (read - prev).days days.
        span_days = (read - prev).days
        day = prev + timedelta(days=1)
        while day <= read:
            if window_from <= day <= today - timedelta(days=1):
                total += daily_value(day)
            else:
                total += m3 / span_days
            day += timedelta(days=1)
    # Days after the last read belong to no span; the real readings
    # cover them.
    last_read = billed[0][0]
    day = max(window_from, last_read + timedelta(days=1))
    while day <= today - timedelta(days=1):
        total += daily_value(day)
        day += timedelta(days=1)
    return round(total, 3)


async def _setup_entry(
    hass: HomeAssistant,
    portal,
    monctonwater_urls,
    *,
    backfill_daily: bool = True,
) -> MockConfigEntry:
    monctonwater_urls(server_base(portal))
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=ACCOUNT_TITLE,
        data=FLOW_INPUT,
        options=None if backfill_daily else {"backfill_daily": False},
        unique_id=f"account_{ACCOUNT_NUMBER}",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _entity_id(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str:
    return er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, UNIQUE_ID_TEMPLATE.format(entry_id=entry.entry_id, key=key)
    )


async def test_setup_creates_sensors(
    recorder_mock,
    hass,
    portal,
    monctonwater_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    entry = await _setup_entry(hass, portal, monctonwater_urls)

    for key in ("water_usage", "last_daily_water", "last_billed_water", "daily_average_water"):
        assert _entity_id(hass, entry, key)

    water = hass.states.get(_entity_id(hass, entry, WATER_KEY))
    assert water is not None and water.state != "unknown"
    assert water.attributes["unit_of_measurement"] == "m³"
    assert water.attributes["state_class"] == "total_increasing"
    assert water.attributes["device_class"] == "water"
    assert water.attributes["account_number"] == ACCOUNT_NUMBER
    assert water.attributes["meter_id"]

    last_daily = hass.states.get(_entity_id(hass, entry, "last_daily_water"))
    assert float(last_daily.state) == pytest.approx(
        daily_value(date.today() - timedelta(days=1))
    )


async def test_water_sensor_derives_from_billed_plus_daily(
    recorder_mock,
    hass,
    portal,
    monctonwater_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    entry = await _setup_entry(hass, portal, monctonwater_urls, backfill_daily=False)
    water = hass.states.get(_entity_id(hass, entry, WATER_KEY))
    assert float(water.state) == pytest.approx(_expected_sensor_cumulative(), abs=0.05)


async def test_history_statistics_imported(
    recorder_mock,
    hass,
    portal,
    monctonwater_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """Phase 1: day-resolution import with cumulative sums (the
    dashboard renders sum deltas, so sum must be a running total)."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    entry = await _setup_entry(hass, portal, monctonwater_urls, backfill_daily=False)
    await get_instance(hass).async_block_till_done()

    statistic_id = _entity_id(hass, entry, WATER_KEY)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        dt_util.now(),
        (statistic_id,),
        "hour",
        None,
        {"state", "sum", "change"},
    )
    rows = sorted(stats[statistic_id], key=lambda r: r["start"])
    # Billed spans spread (~8 quarters) plus the daily window after the
    # last read; every row is a day at local midnight.
    assert len(rows) > 700
    assert all(
        dt_util.as_local(dt_util.utc_from_timestamp(r["start"])).hour == 0
        for r in rows
    )
    # sum and state are running totals through the END of each day, so
    # every rendered change (sum delta) is the day's consumption. The
    # series is re-anchored to end at 0 (matching the native series'
    # zero anchor), so old sums are negative — only deltas matter.
    sums = [r["sum"] for r in rows if r["sum"] is not None]
    states = [r["state"] for r in rows if r["state"] is not None]
    assert sums == sorted(sums), "sum series decreased"
    assert states == sorted(states), "state series decreased"
    assert rows[0]["sum"] == pytest.approx(rows[0]["state"])
    changes = [
        rows[i]["sum"] - rows[i - 1]["sum"] for i in range(1, len(rows))
    ]
    assert all(c >= -0.001 for c in changes), "negative rendered consumption"
    # The series is re-anchored to end at ~0 (native rows may follow with
    # small positive sums); its head sits at roughly minus the total.
    assert rows[-1]["sum"] == pytest.approx(0.0, abs=1.0)
    assert rows[0]["sum"] == pytest.approx(-_expected_cumulative(), abs=1.5)


async def test_hourly_backfill_upgrades_resolution(
    recorder_mock,
    hass,
    portal,
    monctonwater_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """The CSV backfill rebuilds the whole series at hourly resolution,
    ending at exactly 0 where the recorder's native rows begin."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    from conftest import hourly_values

    entry = await _setup_entry(hass, portal, monctonwater_urls)
    await hass.async_block_till_done(wait_background_tasks=True)
    await get_instance(hass).async_block_till_done()

    statistic_id = _entity_id(hass, entry, WATER_KEY)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        dt_util.now(),
        (statistic_id,),
        "hour",
        None,
        {"state", "sum"},
    )
    rows = sorted(stats[statistic_id], key=lambda r: r["start"])

    def _seconds(raw: float) -> int:
        return round(raw / 1000) if raw > 1e11 else round(raw)

    by_start = {_seconds(r["start"]): r["sum"] for r in rows}

    hourly_rows = [
        r for r in rows if dt_util.as_local(dt_util.utc_from_timestamp(r["start"])).hour != 0
    ]
    assert hourly_rows, "expected hourly (non-midnight) statistic rows"
    start = smart_meter_start()
    yesterday = date.today() - timedelta(days=1)
    # 24 rows per day for every day of the meter era (hour 0 shares
    # midnight with the day row).
    assert len(hourly_rows) == 23 * 100

    # Every era day renders its real reading: the day's rendered total
    # (sum of its rows' changes, chained against the actual previous
    # row — the pre-era boundary row is a midnight day row) equals the
    # mock's daily value.
    ordered = sorted(by_start.items())
    changes_by_day: dict[date, float] = {}
    for i, (ts, value) in enumerate(ordered):
        local_day = dt_util.utc_from_timestamp(ts).astimezone(dt_util.DEFAULT_TIME_ZONE).date()
        change = value if i == 0 else value - ordered[i - 1][1]
        changes_by_day[local_day] = changes_by_day.get(local_day, 0.0) + change
    day = start
    while day <= yesterday:
        assert changes_by_day.get(day) == pytest.approx(
            daily_value(day), abs=0.02
        ), day
        day += timedelta(days=1)

    # The pre-era boundary hands off cleanly: the era's first hour
    # chains from the pre-era spread day row (at its midnight, not the
    # previous hour), and the whole series ends at exactly 0.
    first_era_ts = round(dt_util.start_of_local_day(start).timestamp())
    pre_era = by_start.get(first_era_ts - 86400)
    assert pre_era is not None, "expected a pre-era spread row before the era"
    last_imported = max(
        ts for ts in by_start
        if dt_util.utc_from_timestamp(ts).astimezone(dt_util.DEFAULT_TIME_ZONE).date() <= yesterday
    )
    assert by_start[last_imported] == pytest.approx(0.0, abs=0.01)

    # Consecutive sum deltas are never negative.
    sums_ordered = [r["sum"] for r in rows if r["sum"] is not None]
    changes = [sums_ordered[i] - sums_ordered[i - 1] for i in range(1, len(sums_ordered))]
    assert all(c >= -0.002 for c in changes), "negative rendered consumption"

    assert entry.runtime_data.backfill_complete is True
    assert entry.runtime_data.hourly_complete is True
    assert entry.runtime_data.stored_hourly_through() == yesterday


async def test_config_flow_success(
    hass, portal, monctonwater_urls, patched_helper_session, enable_custom_integrations
):
    monctonwater_urls(server_base(portal))
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        FLOW_INPUT,
    )
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == ACCOUNT_TITLE
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_config_flow_wrong_password(
    hass, portal, monctonwater_urls, patched_helper_session, enable_custom_integrations
):
    monctonwater_urls(server_base(portal))
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: USERNAME, CONF_PASSWORD: "wrong"},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_duplicate_account_rejected(
    recorder_mock,
    hass,
    portal,
    monctonwater_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    await _setup_entry(hass, portal, monctonwater_urls)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        FLOW_INPUT,
    )
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_reauth_with_credentials_recovers_entry(
    recorder_mock,
    hass,
    portal,
    monctonwater_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """Bad credentials trigger re-auth; correct ones recover the entry."""
    monctonwater_urls(server_base(portal))
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=ACCOUNT_TITLE,
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: "wrong"},
        unique_id=f"account_{ACCOUNT_NUMBER}",
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is entry.state.SETUP_ERROR

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], FLOW_INPUT
    )
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == PASSWORD
    # The reload after re-auth brings the entry up.
    assert entry.state is entry.state.LOADED
    assert hass.states.get(_entity_id(hass, entry, WATER_KEY))


async def test_session_expiry_relogin_recovers(
    recorder_mock,
    hass,
    portal,
    monctonwater_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """A mid-refresh session expiry re-logins transparently."""
    entry = await _setup_entry(hass, portal, monctonwater_urls, backfill_daily=False)
    water_id = _entity_id(hass, entry, WATER_KEY)
    before = float(hass.states.get(water_id).state)

    # Drop the portal session: the next refresh must fail, re-login, retry.
    portal.app["state"]["logged_in"] = False
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert entry.state is entry.state.LOADED
    assert float(hass.states.get(water_id).state) == pytest.approx(before)


async def test_cumulative_sensor_holds_on_downward_revision(
    recorder_mock,
    hass,
    portal,
    monctonwater_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """The counter never goes backwards (no meter-reset spikes)."""
    entry = await _setup_entry(hass, portal, monctonwater_urls, backfill_daily=False)
    water_id = _entity_id(hass, entry, WATER_KEY)
    before = float(hass.states.get(water_id).state)

    # Simulate the portal losing a billed period between refreshes.
    coordinator = entry.runtime_data
    original = coordinator.client.get_billed_readings

    async def revised():
        readings = await original()
        return readings[:-1]

    coordinator.client.get_billed_readings = revised  # type: ignore[method-assign]
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    after = float(hass.states.get(water_id).state)
    assert after == before


async def test_recent_hourly_followup_imports_new_days(
    recorder_mock,
    hass,
    portal,
    monctonwater_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """After the backfill, a newly published day arrives via the
    post-refresh listener: the followup re-imports the trailing window
    and advances the hourly progress."""
    entry = await _setup_entry(hass, portal, monctonwater_urls)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data
    yesterday = date.today() - timedelta(days=1)
    assert coordinator.stored_hourly_through() == yesterday

    # Simulate the portal publishing a new day after the backfill
    # completed: rewind the progress one day, then let a normal refresh
    # trigger the followup listener.
    await coordinator.store_hourly_progress(
        yesterday - timedelta(days=1), coordinator.stored_hourly_seed()
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert coordinator.stored_hourly_through() == yesterday
async def test_rolling_billed_window_preserves_history(
    recorder_mock,
    hass,
    portal,
    monctonwater_urls,
    patched_helper_session,
    enable_custom_integrations,
    monkeypatch,
):
    """The portal drops the oldest billed period when a new one bills;
    the counter must still count the remembered quarter."""
    import conftest as cf

    entry = await _setup_entry(hass, portal, monctonwater_urls, backfill_daily=False)
    coordinator = entry.runtime_data
    entity = _entity_id(hass, entry, WATER_KEY)
    before = float(hass.states.get(entity).state)
    billed_total_before = sum(m for _, m in billed_readings())

    # Portal: the oldest period drops off and a newest one (55 m3) bills.
    rolled = billed_readings()[:-1]
    rolled.insert(0, (rolled[0][0] + timedelta(days=91), 55.0))

    def patched_page(state):
        if not state.get("logged_in"):
            return cf.LOGIN_PAGE
        rows = [
            "<tr>"
            f"<td class='tableColumn_0'>{d.strftime('%b %d, %Y').replace(' 0', ' ')}</td>"
            f"<td class='tableColumn_1'>{m:.1f}</td>"
            f"<td>{d.isoformat()}</td></tr>"
            for d, m in rolled
        ]
        return (
            '<table id="consumptionTable"><thead><tr><th>Date</th>'
            "<th>Billed Consumption in m³</th><th>Sortable Date</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )

    monkeypatch.setattr(cf, "consumption_page", patched_page)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    after = float(hass.states.get(entity).state)
    # The remembered oldest quarter still counts: the new total is the
    # old full history plus the new bill (the buggy behavior held the
    # counter at `before` instead).
    assert after == pytest.approx(billed_total_before + 55.0, abs=0.1)
    assert after > before
