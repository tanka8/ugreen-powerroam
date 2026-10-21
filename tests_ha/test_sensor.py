"""Which sensors each transport creates, and how they behave once created.

These set the config entry up through Home Assistant with the network and the
Bluetooth connection patched out, then look at what landed in the entity
registry. The registry is the right place to look: it records entities that
start disabled, which never get a state at all.
"""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ugreen_powerroam.const import BLE_SENSORS, DOMAIN

SERIAL = "G12FE2305110337"
ADDRESS = "AA:BB:CC:DD:EE:FF"

LOGIN = "custom_components.ugreen_powerroam.api.UgreenApiClient.login"
HUB_START = "custom_components.ugreen_powerroam.api.UgreenTelemetryHub.start"
BLE_START = "custom_components.ugreen_powerroam.ble.UgreenBleDevice.start"
BLE_DEVICE = "homeassistant.components.bluetooth.async_ble_device_from_address"


async def _fake_login(self, email, password):
    self.device_name = SERIAL
    self.sn = SERIAL


async def setup_cloud(hass) -> MockConfigEntry:
    """Set the cloud transport up without a network."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SERIAL,
        data={"email": "user@example.com", "password": "hunter2", "transport": "cloud"},
    )
    entry.add_to_hass(hass)
    with (
        patch(LOGIN, autospec=True, side_effect=_fake_login),
        patch(HUB_START, autospec=True),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def setup_ble(hass) -> MockConfigEntry:
    """Set the Bluetooth transport up without an adapter or a unit."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SERIAL,
        data={
            "transport": "ble",
            "address": ADDRESS,
            "device_name": "ugreen gs1200",
            "sn": SERIAL,
        },
    )
    entry.add_to_hass(hass)
    with (
        patch(BLE_DEVICE, return_value=object()),
        patch(BLE_START, autospec=True),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def sensor_entries(hass, entry: MockConfigEntry) -> dict[str, er.RegistryEntry]:
    """The entry's sensors in the registry, keyed by the integration's own key."""
    registry = er.async_get(hass)
    prefix = f"{entry.unique_id}_"
    return {
        item.unique_id.removeprefix(prefix).removesuffix("_v2"): item
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        if item.domain == "sensor"
    }


async def test_cloud_is_not_offered_sensors_only_bluetooth_can_fill(hass) -> None:
    """Regression: seven cell-voltage sensors that could never have a value."""
    entry = await setup_cloud(hass)

    keys = set(sensor_entries(hass, entry))

    assert not {key for key in keys if key.startswith("cell_voltage_")}
    # What the cloud does fill is untouched.
    assert {"battery_percentage", "ac_vol", "dc_vol", "device_fault2"} <= keys

    await hass.config_entries.async_unload(entry.entry_id)


async def test_bluetooth_is_offered_exactly_what_it_can_fill(hass) -> None:
    entry = await setup_ble(hass)

    keys = set(sensor_entries(hass, entry))

    assert keys == BLE_SENSORS
    assert "ac_in_vol" not in keys

    await hass.config_entries.async_unload(entry.entry_id)
