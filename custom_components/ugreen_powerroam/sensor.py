"""Sensors for the UGREEN PowerRoam."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BLE_SENSORS, DOMAIN, SENSORS, SUGGESTED_HOURS, TRANSPORT_BLE


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    api, hub = data["api"], data["hub"]

    # Only offer sensors this transport can actually populate, so neither setup
    # is left with permanently unavailable entities.
    keys = SENSORS if data["transport"] != TRANSPORT_BLE else BLE_SENSORS
    async_add_entities(UgreenSensor(api, hub, key) for key in SENSORS if key in keys)


class UgreenSensor(SensorEntity):
    _attr_should_poll = False

    def __init__(self, api, hub, key: str) -> None:
        name, unit, device_class, state_class, entity_category, icon = SENSORS[key]
        self._hub = hub
        self._key = key
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_entity_category = (
            EntityCategory(entity_category) if entity_category else None
        )
        self._attr_icon = icon
        unique_key = key
        if key in SUGGESTED_HOURS:
            self._attr_suggested_unit_of_measurement = "h"
            self._attr_suggested_display_precision = 1
            # Bumped so this doesn't reattach to the orphaned registry entry
            # from before the unit was seconds - that entry has "s" baked
            # into its cached suggested_unit_of_measurement.
            unique_key = f"{key}_v2"
        self._attr_unique_id = f"{api.unique_id_base}_{unique_key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, api.unique_id_base)},
            name="UGREEN PowerRoam",
            manufacturer="UGREEN",
            model=api.device_name,
            serial_number=api.sn,
        )

    @property
    def native_value(self):
        return self._hub.data.get(self._key)

    @property
    def available(self) -> bool:
        return self._key in self._hub.data

    async def async_added_to_hass(self) -> None:
        self._remove_listener = self._hub.add_listener(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._remove_listener()
