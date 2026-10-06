"""The Moncton Water integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import MonctonWaterClient, build_ssl_context
from .const import (
    BASE_URL,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import MonctonWaterCoordinator
from .statistics import async_rebuild_usage_statistic, async_update_usage_statistic

PLATFORMS: list[str] = ["sensor"]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_REBUILD_HISTORY = "rebuild_history"

type MonctonWaterConfigEntry = ConfigEntry[MonctonWaterCoordinator]

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions."""

    async def _rebuild_history(call: ServiceCall) -> None:
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            await async_rebuild_usage_statistic(hass, entry, entry.runtime_data)

    hass.services.async_register(DOMAIN, SERVICE_REBUILD_HISTORY, _rebuild_history)
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: MonctonWaterConfigEntry
) -> bool:
    """Set up Moncton Water from a config entry."""
    if CONF_USERNAME not in entry.data or CONF_PASSWORD not in entry.data:
        raise ConfigEntryAuthFailed(
            "This entry has no stored credentials; sign in again"
        )

    client = MonctonWaterClient(
        # The portal session is a cookie (JSESSIONID): this entry gets a
        # jar of its own, so no other entry — nor the config flow — can
        # share or clobber it. Closed automatically on unload.
        async_create_clientsession(hass),
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

    # Keep the usage statistic current: once now (it backfills an empty
    # statistic) and after each refresh. Each run is a background task —
    # it neither delays startup nor outlives the entry — and the client
    # serializes portal requests, so the CSV export's "last queried
    # range" cannot change under a run.
    update_statistic = _make_statistic_updater(hass, entry, coordinator)
    update_statistic()
    entry.async_on_unload(coordinator.async_add_listener(update_statistic))

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


def _make_statistic_updater(
    hass: HomeAssistant, entry: MonctonWaterConfigEntry, coordinator: MonctonWaterCoordinator
) -> Callable[[], None]:
    """Build the callback that updates the usage statistic, one run at a time."""
    task: asyncio.Task[None] | None = None

    async def _run() -> None:
        try:
            await async_update_usage_statistic(hass, entry, coordinator)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Usage statistic update failed", exc_info=True)

    def _update() -> None:
        nonlocal task
        if task is not None and not task.done():
            return  # the previous run is still talking to the portal
        task = entry.async_create_background_task(
            hass, _run(), f"{DOMAIN}_usage_statistic_{entry.entry_id}"
        )

    return _update
