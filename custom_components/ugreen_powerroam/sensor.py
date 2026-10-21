"""Sensors for the UGREEN PowerRoam."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import RestoreSensor, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MAX_LENGTH_STATE_STATE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    BLE_FRAME_STALE_AFTER,
    BLE_LINK_SENSORS,
    BLE_SENSORS,
    CLOUD_SENSORS,
    DISABLED_BY_DEFAULT_SENSORS,
    DOMAIN,
    ENERGY_SENSORS,
    SENSORS,
    SUGGESTED_HOURS,
    TRANSPORT_BLE,
)
from .energy import PowerIntegrator

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
    if key in ENERGY_SENSORS:
        return UgreenEnergySensor
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


class UgreenEnergySensor(RestoreSensor, UgreenSensor):
    """Cumulative energy for the Energy dashboard, summed up from a power reading.

    The unit reports watts and no energy counter, so the total is integrated
    here (see energy.py for how) and restored across restarts. Two things keep
    that honest:

    * It only counts time the reading is live. A dropped link, a cloud socket
      that is down, or a Bluetooth connection that is open but has stopped
      delivering frames all stop the clock rather than holding the last value.
    * It is advanced both when the hub reports and on a timer. The Bluetooth hub
      only reports a change, so a steady load would otherwise leave the total
      frozen for hours and then jump, smearing the energy across the wrong hours
      of the dashboard.

    The total is available even while the link is down. It is a running count,
    not a live reading, and what it holds is still true.
    """

    _attr_should_poll = True

    def __init__(self, api, hub, key: str) -> None:
        super().__init__(api, hub, key)
        self._power_key = ENERGY_SENSORS[key]
        self._integrator = PowerIntegrator()

    @property
    def native_value(self) -> float:
        # Whole watt-hours: fine enough for a dashboard, and it keeps the
        # recorder from storing a new state on every tiny increment.
        return round(self._integrator.total_kwh, 3)

    @property
    def available(self) -> bool:
        return True

    def _live_power(self) -> float | None:
        """The power reading to integrate, or None if it cannot be trusted."""
        hub = self._hub
        if not hub.available:
            return None
        # Only the Bluetooth hub can tell a stalled link from a quiet one.
        if hasattr(hub, "last_frame_age"):
            age = hub.last_frame_age
            if age is None or age > BLE_FRAME_STALE_AFTER:
                return None
        value = hub.data.get(self._power_key)
        return value if isinstance(value, int | float) else None

    def _advance(self) -> None:
        self._integrator.update(time.monotonic(), self._live_power())

    def _on_hub_update(self) -> None:
        self._advance()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        if (last := await self.async_get_last_sensor_data()) is not None:
            try:
                restored = float(last.native_value)
            except (TypeError, ValueError):
                restored = 0.0
            self._integrator = PowerIntegrator(restored)
        self._advance()
        self._remove_listener = self._hub.add_listener(self._on_hub_update)

    async def async_update(self) -> None:
        self._advance()
