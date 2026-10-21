"""Sensors for the UGREEN PowerRoam."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MAX_LENGTH_STATE_STATE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    BLE_LINK_SENSORS,
    BLE_SENSORS,
    CLOUD_SENSORS,
    DISABLED_BY_DEFAULT_SENSORS,
    DOMAIN,
    SENSORS,
    SUGGESTED_HOURS,
    TRANSPORT_BLE,
)

# How often the sensors that are not pushed to are asked for a fresh value.
SCAN_INTERVAL = timedelta(seconds=30)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    api, hub = data["api"], data["hub"]

    # Only offer sensors this transport can actually populate, so neither setup
    # is left with permanently unavailable entities.
    keys = BLE_SENSORS if data["transport"] == TRANSPORT_BLE else CLOUD_SENSORS
    async_add_entities(
        _entity_class(key)(api, hub, key) for key in SENSORS if key in keys
    )


def _entity_class(key: str) -> type[UgreenSensor]:
    if key in BLE_LINK_SENSORS:
        return UgreenBleLinkSensor
    return UgreenSensor


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
        self._attr_entity_registry_enabled_default = (
            key not in DISABLED_BY_DEFAULT_SENSORS
        )
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
        value = self._hub.data.get(self._key)
        # A raw frame in hex can run past the longest state Home Assistant will
        # store, and a state that is too long is dropped altogether.
        if isinstance(value, str) and len(value) > MAX_LENGTH_STATE_STATE:
            return value[:MAX_LENGTH_STATE_STATE]
        return value

    @property
    def available(self) -> bool:
        return self._hub.available and self._key in self._hub.data

    async def async_added_to_hass(self) -> None:
        self._remove_listener = self._hub.add_listener(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._remove_listener()


class UgreenBleLinkSensor(UgreenSensor):
    """A reading about the Bluetooth link itself, not about the power station.

    Neither value arrives in a frame, so there is nothing to push on and these
    are polled. They also stay available while the link is down, which is when
    "how long since the last frame" and "how strong was the signal" matter most.
    """

    _attr_should_poll = True

    @property
    def native_value(self) -> int | None:
        value = getattr(self._hub, BLE_LINK_SENSORS[self._key])
        return None if value is None else round(value)

    @property
    def available(self) -> bool:
        return self.native_value is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Which adapter or proxy heard the unit, and how long ago.

        That is the answer to "where should a Bluetooth proxy go": the source is
        the adapter that has the strongest view of the unit. The age matters
        because a connected unit may have stopped advertising, in which case the
        signal strength is from just before the connection was made.
        """
        if self._key != "ble_rssi":
            return None
        info = self._hub.last_advertisement()
        if info is None:
            return None
        return {
            "source": info.source,
            "advertisement_age": round(time.monotonic() - info.time),
        }
