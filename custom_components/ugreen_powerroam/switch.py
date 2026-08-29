"""Switches for the UGREEN PowerRoam (AC/DC/USB output, flashlight)."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SWITCHES


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    api, hub = data["api"], data["hub"]

    async_add_entities(UgreenSwitch(api, hub, key) for key in SWITCHES)


class UgreenSwitch(SwitchEntity):
    _attr_should_poll = False

    def __init__(self, api, hub, key: str) -> None:
        self._api = api
        self._hub = hub
        self._key = key
        self._attr_name = SWITCHES[key]
        self._attr_unique_id = f"{api.unique_id_base}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, api.unique_id_base)},
            name="UGREEN PowerRoam",
            manufacturer="UGREEN",
            model=api.device_name,
            serial_number=api.sn,
        )

    @property
    def is_on(self) -> bool | None:
        value = self._hub.data.get(self._key)
        return None if value is None else bool(value)

    @property
    def available(self) -> bool:
        return self._hub.available and self._key in self._hub.data

    async def async_turn_on(self, **kwargs) -> None:
        await self._api.set_device_info(self._key, 1)
        # Optimistic update - the WebSocket push will confirm shortly after.
        self._hub.data[self._key] = 1
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self._api.set_device_info(self._key, 0)
        self._hub.data[self._key] = 0
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self._remove_listener = self._hub.add_listener(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._remove_listener()
