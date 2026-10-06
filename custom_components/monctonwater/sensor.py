"""Sensor platform for the Moncton Water integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import MonctonWaterCoordinator

WATER_KEY = "water_usage"
UNIQUE_ID_TEMPLATE = "{entry_id}_{key}"


@dataclass(frozen=True, kw_only=True)
class MonctonWaterSensorEntityDescription(SensorEntityDescription):
    """Describes a Moncton Water sensor."""

    value_fn: Callable[[dict[str, Any]], float | str | None]


SENSORS: tuple[MonctonWaterSensorEntityDescription, ...] = (
    MonctonWaterSensorEntityDescription(
        key=WATER_KEY,
        translation_key="water_usage",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
        value_fn=lambda data: data.get("cumulative_m3"),
    ),
    MonctonWaterSensorEntityDescription(
        key="last_daily_water",
        translation_key="last_daily_water",
        # No device_class: water forbids the measurement state class, and
        # these are per-day amounts, not meter totals.
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: data.get("last_daily_m3"),
    ),
    MonctonWaterSensorEntityDescription(
        key="last_billed_water",
        translation_key="last_billed_water",
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda data: data.get("last_billed_m3"),
    ),
    MonctonWaterSensorEntityDescription(
        key="daily_average_water",
        translation_key="daily_average_water",
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: data.get("daily_average_m3"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Moncton Water sensors from a config entry."""
    coordinator: MonctonWaterCoordinator = entry.runtime_data
    async_add_entities(
        MonctonWaterSensorEntity(coordinator, entry, description)
        for description in SENSORS
    )


class MonctonWaterSensorEntity(
    CoordinatorEntity[MonctonWaterCoordinator], SensorEntity
):
    """A sensor backed by the Moncton Water coordinator."""

    _attr_has_entity_name = True
    # The ~100-day window changes daily; recording it would store a fresh
    # copy in the database every day for no history value.
    _unrecorded_attributes = frozenset({"daily_m3"})

    def __init__(
        self,
        coordinator: MonctonWaterCoordinator,
        entry: ConfigEntry,
        description: MonctonWaterSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = UNIQUE_ID_TEMPLATE.format(
            entry_id=entry.entry_id, key=description.key
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Moncton Water",
            manufacturer="City of Moncton",
            model="Water Smart Meter",
        )

    @property
    def native_value(self) -> float | str | None:
        return self.entity_description.value_fn(self.coordinator.data or {})

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key != WATER_KEY:
            return None
        data = self.coordinator.data or {}
        return {
            "derived_m3": data.get("derived_m3"),
            "billed_periods": data.get("billed_periods"),
            "billed_total_m3": data.get("billed_total_m3"),
            "account_number": data.get("account_number"),
            "meter_id": data.get("meter_id"),
            "service_address": data.get("service_address"),
            "last_daily_date": (
                data["last_daily_date"].isoformat()
                if data.get("last_daily_date")
                else None
            ),
            "last_billed_date": (
                data["last_billed_date"].isoformat()
                if data.get("last_billed_date")
                else None
            ),
            "daily_m3": data.get("daily_m3"),
        }
