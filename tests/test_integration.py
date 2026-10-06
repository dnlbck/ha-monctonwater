"""Home Assistant-level tests: setup, sensors, config flow, usage statistic."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from conftest import (
    ACCOUNT_NUMBER,
    OTHER_ACCOUNT_NUMBER,
    VALID_CREDS,
    billed_readings,
    daily_value,
    expire_sessions,
    mock_today,
    hourly_values,
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
    today = mock_today()
    total = sum(m3 for _, m3 in billed)
    day = max(smart_meter_start(), last_read + timedelta(days=1))
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
    # The backfill and hourly followup run as background tasks.
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


def _is_spring_forward(day: date) -> bool:
    """Whether the local day is 23 hours long (compare in UTC: aware
    datetimes sharing a tzinfo subtract as wall-clock times)."""
    start = dt_util.as_utc(dt_util.start_of_local_day(day))
    end = dt_util.as_utc(dt_util.start_of_local_day(day + timedelta(days=1)))
    return end - start < timedelta(hours=24)


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
        daily_value(mock_today() - timedelta(days=1))
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


STATISTIC_ID = f"{DOMAIN}:water_usage_{ACCOUNT_NUMBER.replace('-', '_')}"


async def _statistic_rows(
    hass: HomeAssistant, statistic_id: str = STATISTIC_ID
) -> list[dict]:
    """Every hourly-table row of a statistic, oldest first."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    await get_instance(hass).async_block_till_done()
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        dt_util.now(),
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return sorted(stats.get(statistic_id, []), key=lambda row: row["start"])


def _local(row: dict) -> datetime:
    return dt_util.as_local(dt_util.utc_from_timestamp(row["start"]))


def _day_changes(rows: list[dict]) -> dict[date, float]:
    """Each local day's rendered consumption: its rows' sum deltas."""
    changes: dict[date, float] = {}
    previous = 0.0
    for row in rows:
        day = _local(row).date()
        changes[day] = changes.get(day, 0.0) + row["sum"] - previous
        previous = row["sum"]
    return changes


async def _refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Refresh, then let the usage-statistic update finish."""
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_usage_statistic_backfill(
    recorder_mock,
    hass,
    portal,
    monctonwater_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """The backfill builds one cumulative chain in the integration's own
    statistic: billed periods spread across their days before the smart
    meter, the meter's hourly readings after, through yesterday."""
    from custom_components.monctonwater.statistics import ASSUMED_FIRST_PERIOD_DAYS

    entry = await _setup_entry(hass, portal, monctonwater_urls)
    rows = await _statistic_rows(hass)
    start = smart_meter_start()
    yesterday = mock_today() - timedelta(days=1)

    # One chain from zero that never decreases, with nothing booked after
    # yesterday: today renders nothing until the portal publishes it.
    sums = [row["sum"] for row in rows]
    assert sums[0] > 0
    assert sums == sorted(sums)
    assert _local(rows[-1]).date() == yesterday

    # The meter era is hourly, each day rendering the meter's reading.
    changes = _day_changes(rows)
    era_days = [start + timedelta(days=i) for i in range((yesterday - start).days + 1)]
    era_rows = [row for row in rows if _local(row).date() >= start]
    assert len(era_rows) == sum(23 if _is_spring_forward(day) else 24 for day in era_days)
    for day in era_days:
        assert changes[day] == pytest.approx(daily_value(day), abs=0.001), day

    # Before it, one midnight row per day: each billed period spread
    # evenly, adding back up to its bill.
    assert all(_local(row).hour == 0 for row in rows if _local(row).date() < start)
    reads = sorted(billed_readings())
    previous = reads[0][0] - timedelta(days=ASSUMED_FIRST_PERIOD_DAYS)
    for read_date, m3 in reads:
        if read_date < start:
            period = [previous + timedelta(days=i + 1) for i in range((read_date - previous).days)]
            assert sum(changes[day] for day in period) == pytest.approx(m3, abs=0.01)
        previous = read_date

    # The sensor's own statistic is the recorder's alone: nothing imported.
    assert await _statistic_rows(hass, _entity_id(hass, entry, WATER_KEY)) == []


@pytest.mark.parametrize("gap_days", [1, 10])
async def test_usage_statistic_appends_new_days(
    recorder_mock,
    hass,
    portal,
    monctonwater_urls,
    patched_helper_session,
    enable_custom_integrations,
    gap_days,
):
    """Newly published days extend the chain: the next day, or a run of
    days after downtime, each rendering its own reading."""
    yesterday = mock_today() - timedelta(days=1)
    portal.app["state"]["published_through"] = yesterday - timedelta(days=gap_days)
    entry = await _setup_entry(hass, portal, monctonwater_urls)
    rows = await _statistic_rows(hass)
    assert _local(rows[-1]).date() == yesterday - timedelta(days=gap_days)

    portal.app["state"]["published_through"] = yesterday
    await _refresh(hass, entry)
    rows = await _statistic_rows(hass)
    assert _local(rows[-1]).date() == yesterday
    sums = [row["sum"] for row in rows]
    assert sums == sorted(sums)
    changes = _day_changes(rows)
    for i in range(gap_days + 3):
        day = yesterday - timedelta(days=i)
        assert changes[day] == pytest.approx(daily_value(day), abs=0.001), day


async def test_usage_statistic_reimports_revised_day(
    recorder_mock,
    hass,
    portal,
    monctonwater_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """The portal publishes yesterday in stages (live: 0.582 m³ at 02:00,
    revised to 0.659 m³ by 06:00): the trailing days are imported again
    whenever their totals change."""
    yesterday = mock_today() - timedelta(days=1)
    portal.app["state"]["published_hours"] = {yesterday: 12}
    entry = await _setup_entry(hass, portal, monctonwater_urls)
    partial = sum(hourly_values(yesterday)[:12])
    assert _day_changes(await _statistic_rows(hass))[yesterday] == pytest.approx(
        partial, abs=0.001
    )

    # Still partial: the import settles on the portal's current totals.
    await _refresh(hass, entry)
    assert entry.runtime_data.stored_import_totals() is not None

    # The rest of the day publishes: same last day, revised total.
    portal.app["state"]["published_hours"] = {}
    await _refresh(hass, entry)
    rows = await _statistic_rows(hass)
    assert _day_changes(rows)[yesterday] == pytest.approx(daily_value(yesterday), abs=0.001)
    sums = [row["sum"] for row in rows]
    assert sums == sorted(sums)


async def test_usage_statistic_rebuilt_after_clear(
    recorder_mock,
    hass,
    portal,
    monctonwater_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """Clearing the statistic (Developer tools > Statistics) makes the next
    refresh rebuild it from scratch."""
    from homeassistant.components.recorder import get_instance

    entry = await _setup_entry(hass, portal, monctonwater_urls)
    before = await _statistic_rows(hass)
    get_instance(hass).async_clear_statistics([STATISTIC_ID])
    assert await _statistic_rows(hass) == []

    await _refresh(hass, entry)
    after = await _statistic_rows(hass)
    assert [(row["start"], row["sum"]) for row in after] == [
        (row["start"], row["sum"]) for row in before
    ]


async def test_rebuild_history_action(
    recorder_mock,
    hass,
    portal,
    monctonwater_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """monctonwater.rebuild_history clears the statistic, stray rows
    included, and imports the history again."""
    from homeassistant.components.recorder.models import StatisticData
    from homeassistant.components.recorder.statistics import async_add_external_statistics

    from custom_components.monctonwater.statistics import _metadata

    entry = await _setup_entry(hass, portal, monctonwater_urls)
    before = await _statistic_rows(hass)
    # A stray row no refresh would ever touch.
    stray = dt_util.start_of_local_day(smart_meter_start() - timedelta(days=2000))
    async_add_external_statistics(
        hass, _metadata(entry), [StatisticData(start=stray, state=999.0, sum=999.0)]
    )
    assert len(await _statistic_rows(hass)) == len(before) + 1

    await hass.services.async_call(DOMAIN, "rebuild_history", blocking=True)
    after = await _statistic_rows(hass)
    assert [(row["start"], row["sum"]) for row in after] == [
        (row["start"], row["sum"]) for row in before
    ]


async def test_billed_only_account_usage_statistic(
    recorder_mock,
    hass,
    portal,
    monctonwater_urls,
    patched_helper_session,
    enable_custom_integrations,
    monkeypatch,
):
    """Without smart-meter data the statistic is the billed periods spread
    across their days, growing as new periods bill."""
    import conftest as cf

    portal.app["state"]["smart_meter_empty"] = True
    entry = await _setup_entry(hass, portal, monctonwater_urls)
    rows = await _statistic_rows(hass)
    reads = billed_readings()  # newest first
    assert all(_local(row).hour == 0 for row in rows)
    assert _local(rows[-1]).date() == reads[0][0]
    assert rows[-1]["sum"] == pytest.approx(sum(m3 for _, m3 in reads), abs=0.01)

    new_read = reads[0][0] + timedelta(days=21)
    original = cf.consumption_page
    monkeypatch.setattr(
        cf, "consumption_page", lambda state: original(state, [(new_read, 30.0), *reads])
    )
    await _refresh(hass, entry)
    rows = await _statistic_rows(hass)
    assert _local(rows[-1]).date() == new_read
    assert rows[-1]["sum"] == pytest.approx(sum(m3 for _, m3 in reads) + 30.0, abs=0.01)


async def test_usage_statistic_without_backfill_starts_recently(
    recorder_mock,
    hass,
    portal,
    monctonwater_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """With the backfill option off, the statistic starts with the trailing
    days instead of the whole history."""
    from custom_components.monctonwater.const import REIMPORT_DAYS

    await _setup_entry(hass, portal, monctonwater_urls, backfill_daily=False)
    yesterday = mock_today() - timedelta(days=1)
    assert sorted(_day_changes(await _statistic_rows(hass))) == [
        yesterday - timedelta(days=i) for i in range(REIMPORT_DAYS, -1, -1)
    ]


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
    await hass.async_block_till_done(wait_background_tasks=True)
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


async def test_two_accounts_side_by_side(
    recorder_mock,
    hass,
    portal,
    monctonwater_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """Separate MyAccount logins coexist: each entry keeps its own portal
    session, so after the sessions lapse each signs back in to its own
    account."""
    first = await _setup_entry(hass, portal, monctonwater_urls, backfill_daily=False)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: "otheruser", CONF_PASSWORD: VALID_CREDS["otheruser"]},
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Moncton Water {OTHER_ACCOUNT_NUMBER}"
    second = result["result"]

    expire_sessions(portal)
    for entry in (first, second):
        await entry.runtime_data.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert [entry.runtime_data.data["account_number"] for entry in (first, second)] == [
        ACCOUNT_NUMBER,
        OTHER_ACCOUNT_NUMBER,
    ]


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
    await hass.async_block_till_done(wait_background_tasks=True)
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
    expire_sessions(portal)
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


async def test_billed_table_read_once_a_day(
    recorder_mock,
    hass,
    portal,
    monctonwater_urls,
    patched_helper_session,
    enable_custom_integrations,
    monkeypatch,
):
    """The billed table changes quarterly: refreshes reuse the stored
    history and re-read the page once a day."""
    from custom_components.monctonwater import coordinator as mw_coordinator

    monkeypatch.setattr(mw_coordinator, "BILLED_REFRESH_INTERVAL", timedelta(hours=24))
    entry = await _setup_entry(hass, portal, monctonwater_urls, backfill_daily=False)
    coordinator = entry.runtime_data
    water_id = _entity_id(hass, entry, WATER_KEY)
    before = hass.states.get(water_id).state
    calls = portal.app["billed_calls"]
    assert len(calls) == 1

    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert len(calls) == 1
    assert hass.states.get(water_id).state == before

    # A day on, the page is read again.
    coordinator._billed_checked -= timedelta(hours=24)  # noqa: SLF001
    await coordinator.async_refresh()
    assert len(calls) == 2


async def test_account_without_smart_meter_data_sets_up(
    recorder_mock,
    hass,
    portal,
    monctonwater_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """No smart-meter data (billed-only account, or the meter backend is
    down) is not an auth failure: setup succeeds on the billed books and
    no re-auth flow starts."""
    portal.app["state"]["smart_meter_empty"] = True
    entry = await _setup_entry(hass, portal, monctonwater_urls, backfill_daily=False)

    assert entry.state is ConfigEntryState.LOADED
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    water = hass.states.get(_entity_id(hass, entry, WATER_KEY))
    assert float(water.state) == pytest.approx(sum(m3 for _, m3 in billed_readings()))


async def test_stale_smart_meter_page_does_not_trigger_reauth(
    recorder_mock,
    hass,
    portal,
    monctonwater_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """Empty smart-meter arrays that survive a fresh login must not stop
    polling behind a re-auth prompt: the refresh succeeds on the billed
    books and the counter holds."""
    entry = await _setup_entry(hass, portal, monctonwater_urls, backfill_daily=False)
    coordinator = entry.runtime_data
    water_id = _entity_id(hass, entry, WATER_KEY)
    before = float(hass.states.get(water_id).state)

    portal.app["state"]["smart_meter_empty"] = True
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert float(hass.states.get(water_id).state) == before


async def test_reauth_with_another_accounts_credentials_aborts(
    recorder_mock,
    hass,
    portal,
    monctonwater_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """Re-auth must stay on the entry's account; another account's
    credentials would graft its usage onto this entry's statistics."""
    entry = await _setup_entry(hass, portal, monctonwater_urls, backfill_daily=False)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: "otheruser", CONF_PASSWORD: VALID_CREDS["otheruser"]},
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data[CONF_USERNAME] == USERNAME


async def test_redated_billed_read_is_not_double_counted(
    recorder_mock,
    hass,
    portal,
    monctonwater_urls,
    patched_helper_session,
    enable_custom_integrations,
    monkeypatch,
):
    """A read the portal re-dates (an estimate replaced by an actual read)
    replaces the stored period instead of sitting beside it."""
    import conftest as cf

    entry = await _setup_entry(hass, portal, monctonwater_urls, backfill_daily=False)
    coordinator = entry.runtime_data
    water_id = _entity_id(hass, entry, WATER_KEY)
    before = float(hass.states.get(water_id).state)

    billed = billed_readings()  # newest first
    newest_date, newest_m3 = billed[0]
    redated = [(newest_date + timedelta(days=2), newest_m3), *billed[1:]]
    original = cf.consumption_page
    monkeypatch.setattr(cf, "consumption_page", lambda state: original(state, redated))
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    history = coordinator.merged_billed([])
    assert newest_date not in [r.read_date for r in history]
    assert sum(r.consumption_m3 for r in history) == pytest.approx(
        sum(m3 for _, m3 in billed)
    )
    # The counter holds (two days moved into the bill) rather than jumping
    # by a whole duplicated quarter.
    assert float(hass.states.get(water_id).state) == pytest.approx(before)


async def test_stored_billed_history_survives_upgrade(
    recorder_mock,
    hass,
    hass_storage,
    portal,
    monctonwater_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """Storage written by earlier versions (with their backfill flags)
    still yields the billed quarters the portal's rolling window has
    dropped: they cannot be fetched again."""
    from custom_components.monctonwater.const import STORAGE_KEY, STORAGE_VERSION

    monctonwater_urls(server_base(portal))
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=ACCOUNT_TITLE,
        data=FLOW_INPUT,
        options={"backfill_daily": False},
        unique_id=f"account_{ACCOUNT_NUMBER}",
    )
    entry.add_to_hass(hass)
    rolled_off = billed_readings()[-1][0] - timedelta(days=91)
    hass_storage[f"{STORAGE_KEY}.{entry.entry_id}"] = {
        "version": STORAGE_VERSION,
        "minor_version": 1,
        "key": f"{STORAGE_KEY}.{entry.entry_id}",
        "data": {
            "stats_gen": 5,
            "stats_imported": True,
            "backfill_complete": True,
            "hourly_complete": True,
            "hourly_through": "2026-09-30",
            "hourly_seed": 0.0,
            "hourly_totals": None,
            "last_cumulative_m3": 1.0,
            "billed_history": [[rolled_off.isoformat(), 55.0]],
        },
    }
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    history = entry.runtime_data.merged_billed([])
    assert rolled_off in [r.read_date for r in history]


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
