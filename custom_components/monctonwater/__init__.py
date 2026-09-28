"""The Moncton Water integration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import MonctonWaterClient, build_ssl_context
from .const import (
    BASE_URL,
    CONF_BACKFILL_DAILY,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import MonctonWaterCoordinator
from .statistics import (
    async_backfill_hourly_statistics,
    async_import_history_statistics,
    async_import_recent_hourly,
)

PLATFORMS: list[str] = ["sensor"]

type MonctonWaterConfigEntry = ConfigEntry[MonctonWaterCoordinator]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: MonctonWaterConfigEntry
) -> bool:
    """Set up Moncton Water from a config entry."""
    if CONF_USERNAME not in entry.data or CONF_PASSWORD not in entry.data:
        raise ConfigEntryAuthFailed(
            "This entry has no stored credentials; sign in again"
        )

    client = MonctonWaterClient(
        async_get_clientsession(hass),
        base_url=BASE_URL,
        # CA-bundle loading touches the filesystem; keep it off the loop.
        ssl_context=await hass.async_add_executor_job(build_ssl_context),
    )
    coordinator = MonctonWaterCoordinator(
        hass,
        entry,
        client,
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        scan_interval=timedelta(
            hours=entry.options.get(
                CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL.total_seconds() / 3600
            )
        ),
    )
    await coordinator.async_load_stored()
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Keep recent days at hourly resolution. Run inline after the first
    # refresh (deterministic at every setup) in addition to the
    # post-refresh listener below; both are idempotent.
    try:
        await async_import_recent_hourly(hass, entry, coordinator)
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Recent hourly import failed at setup", exc_info=True)

    # Phase 1 (immediate): import the fetched history at day resolution. A
    # missing recorder (rare) must not break setup; the import retries on
    # restart.
    try:
        await async_import_history_statistics(hass, entry, coordinator)
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Statistics backfill failed; will retry on restart", exc_info=True)

    # Background backfill: rebuild the statistics series at hourly
    # resolution from the portal's CSV export (one continuous chain that
    # ends at 0 where the recorder's native rows begin). The CSV export
    # serves whatever range the session last queried, so nothing else
    # may touch the portal concurrently while it runs.
    if entry.options.get(CONF_BACKFILL_DAILY, True):
        backfill_task = hass.async_create_task(
            async_backfill_hourly_statistics(hass, entry, coordinator),
            f"{DOMAIN}_backfill_{entry.entry_id}",
        )
        entry.async_on_unload(lambda: backfill_task.cancel())
        entry.async_on_unload(
            coordinator.async_add_listener(
                _make_hourly_followup(hass, entry, coordinator)
            )
        )

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: MonctonWaterConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(
    hass: HomeAssistant, entry: MonctonWaterConfigEntry
) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _make_hourly_followup(
    hass: HomeAssistant, entry: MonctonWaterConfigEntry, coordinator: MonctonWaterCoordinator
) -> Callable[[], None]:
    """Build the post-refresh callback that keeps new days hourly."""

    def _followup() -> None:
        hass.async_create_task(
            async_import_recent_hourly(hass, entry, coordinator),
            f"{DOMAIN}_hourly_followup_{entry.entry_id}",
        )

    return _followup
