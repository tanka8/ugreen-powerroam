"""Which sensors each transport creates, and how they behave once created.

These set the config entry up through Home Assistant with the network and the
Bluetooth connection patched out, then look at what landed in the entity
registry. The registry is the right place to look: it records entities that
start disabled, which never get a state at all.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.components.sensor import SensorExtraStoredData
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EntityCategory
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.ugreen_powerroam import protocol
from custom_components.ugreen_powerroam import sensor as sensor_module
from custom_components.ugreen_powerroam.const import (
    BLE_RAW_KEYS,
    BLE_SENSORS,
    CLOUD_RAW_FIELDS,
    DOMAIN,
)
from custom_components.ugreen_powerroam.sensor import (
    UgreenBleLinkSensor,
    UgreenEnergySensor,
    UgreenSensor,
)

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


def ble_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=SERIAL,
        data={
            "transport": "ble",
            "address": ADDRESS,
            "device_name": "ugreen gs1200",
            "sn": SERIAL,
        },
    )


async def setup_ble(hass, entry: MockConfigEntry | None = None) -> MockConfigEntry:
    """Set the Bluetooth transport up without an adapter or a unit.

    Pass an entry that is already added to Home Assistant to prepare the
    registry or the restore cache before the entities exist.
    """
    if entry is None:
        entry = ble_entry()
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
    assert not {"ble_rssi", "ble_last_frame_age"} & keys
    # What the cloud does fill is untouched.
    assert {"battery_percentage", "ac_vol", "dc_vol", "device_fault2"} <= keys

    await hass.config_entries.async_unload(entry.entry_id)


async def test_bluetooth_is_offered_exactly_what_it_can_fill(hass) -> None:
    entry = await setup_ble(hass)

    keys = set(sensor_entries(hass, entry))

    assert keys == BLE_SENSORS
    assert "ac_in_vol" not in keys

    await hass.config_entries.async_unload(entry.entry_id)


# -- connection-quality sensors ----------------------------------------------


async def test_link_sensors_are_diagnostic_and_start_disabled(hass) -> None:
    entry = await setup_ble(hass)

    entries = sensor_entries(hass, entry)

    for key in ("ble_rssi", "ble_last_frame_age"):
        assert entries[key].disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert entries[key].entity_category is EntityCategory.DIAGNOSTIC
    # Only the troubleshooting entities start disabled.
    assert entries["battery_percentage"].disabled_by is None

    await hass.config_entries.async_unload(entry.entry_id)


def link_sensor(key: str, **hub_attributes) -> UgreenBleLinkSensor:
    """A link sensor over a stand-in for the Bluetooth device."""
    hub = SimpleNamespace(
        unique_id_base=SERIAL,
        device_name="ugreen gs1200",
        sn=SERIAL,
        available=False,
        data={},
        rssi=None,
        last_frame_age=None,
        last_advertisement=lambda: None,
    )
    for name, value in hub_attributes.items():
        setattr(hub, name, value)
    return UgreenBleLinkSensor(hub, hub, key)


def test_rssi_comes_from_the_last_advertisement() -> None:
    heard = SimpleNamespace(
        rssi=-67, source="11:22:33:44:55:66", time=time.monotonic() - 12
    )
    sensor = link_sensor("ble_rssi", rssi=-67, last_advertisement=lambda: heard)

    assert sensor.native_value == -67
    assert sensor.available is True
    attributes = sensor.extra_state_attributes
    assert attributes["source"] == "11:22:33:44:55:66"
    assert 12 <= attributes["advertisement_age"] <= 14


def test_rssi_is_unavailable_until_an_adapter_has_heard_the_unit() -> None:
    sensor = link_sensor("ble_rssi")

    assert sensor.native_value is None
    assert sensor.available is False
    assert sensor.extra_state_attributes is None


def test_last_frame_age_stays_available_while_the_link_is_down() -> None:
    sensor = link_sensor("ble_last_frame_age", available=False, last_frame_age=41.6)

    assert sensor.native_value == 42
    assert sensor.available is True
    assert sensor.extra_state_attributes is None


def test_last_frame_age_is_unavailable_before_the_first_frame() -> None:
    assert link_sensor("ble_last_frame_age").available is False


# -- raw-value sensors --------------------------------------------------------


async def test_bluetooth_raw_frame_sensors_start_disabled(hass) -> None:
    entry = await setup_ble(hass)

    entries = sensor_entries(hass, entry)

    assert set(BLE_RAW_KEYS.values()) <= set(entries)
    assert not set(CLOUD_RAW_FIELDS) & set(entries)
    for key in BLE_RAW_KEYS.values():
        assert entries[key].disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert entries[key].entity_category is EntityCategory.DIAGNOSTIC

    await hass.config_entries.async_unload(entry.entry_id)


async def test_cloud_raw_field_sensors_start_disabled(hass) -> None:
    entry = await setup_cloud(hass)

    entries = sensor_entries(hass, entry)

    assert set(CLOUD_RAW_FIELDS) <= set(entries)
    assert not set(BLE_RAW_KEYS.values()) & set(entries)
    for key in CLOUD_RAW_FIELDS:
        assert entries[key].disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert entries[key].entity_category is EntityCategory.DIAGNOSTIC

    await hass.config_entries.async_unload(entry.entry_id)


def raw_sensor(key: str, value) -> UgreenSensor:
    hub = SimpleNamespace(
        unique_id_base=SERIAL,
        device_name="ugreen gs1200",
        sn=SERIAL,
        available=True,
        data={key: value},
    )
    return UgreenSensor(hub, hub, key)


def test_a_raw_value_is_shown_exactly_as_received() -> None:
    # The cloud sends some of these as strings, some as numbers.
    assert raw_sensor("timeoff_zoom", "0").native_value == "0"
    assert raw_sensor("low_power_al_set", 2).native_value == 2
    assert raw_sensor("raw_frame_17", "000102030405").native_value == "000102030405"


def test_a_raw_frame_too_long_for_a_state_is_cut_rather_than_lost() -> None:
    sensor = raw_sensor("raw_frame_17", "ab" * 200)

    assert sensor.native_value == "ab" * 127 + "a"
    assert len(sensor.native_value) == 255


# -- energy sensors -----------------------------------------------------------


async def test_energy_sensors_are_offered_on_the_cloud_too(hass) -> None:
    entry = await setup_cloud(hass)

    entries = sensor_entries(hass, entry)

    assert {"energy_input", "energy_output"} <= set(entries)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_energy_sensors_are_for_the_energy_dashboard(hass) -> None:
    entry = await setup_ble(hass)
    registry = er.async_get(hass)

    for key in ("energy_input", "energy_output"):
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{SERIAL}_{key}")
        state = hass.states.get(entity_id)
        assert registry.async_get(entity_id).disabled_by is None
        assert state.attributes["device_class"] == "energy"
        assert state.attributes["state_class"] == "total_increasing"
        assert state.attributes["unit_of_measurement"] == "kWh"
        # A new install starts counting from zero and is available at once.
        assert state.state == "0.0"

    await hass.config_entries.async_unload(entry.entry_id)


class ConnectedClient:
    """Enough of a bleak client for UgreenBleDevice.available."""

    is_connected = True


def totals_frame(input_w: int, output_w: int) -> bytes:
    data = input_w.to_bytes(2, "little") + output_w.to_bytes(2, "little")
    body = protocol.REPLY_PREFIX + bytes([protocol.OP_TOTALS]) + b"\x04\x00" + data
    return protocol.HEADER + body + protocol.crc16_modbus(body).to_bytes(2, "little")


async def test_energy_accrues_while_live_and_pauses_while_the_link_is_down(
    hass, monkeypatch
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(
        sensor_module, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    entry = await setup_ble(hass)
    device = hass.data[DOMAIN][entry.entry_id]["hub"]
    registry = er.async_get(hass)
    energy_in = registry.async_get_entity_id("sensor", DOMAIN, f"{SERIAL}_energy_input")
    energy_out = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{SERIAL}_energy_output"
    )

    def report(input_w: int, output_w: int) -> None:
        device._on_notify(None, bytearray(totals_frame(input_w, output_w)))
        # Skip the two second debounce, which would leave a timer behind.
        device._schedule_notify(immediate=True)

    def kwh(entity_id: str) -> float:
        return float(hass.states.get(entity_id).state)

    device._client = ConnectedClient()
    report(1000, 500)  # the first reading only starts the clock

    clock[0] += 3600
    report(1000, 500)
    assert kwh(energy_in) == pytest.approx(1.0)
    assert kwh(energy_out) == pytest.approx(0.5)

    # The link drops a minute later: that minute counts, the outage does not.
    device._client = None
    clock[0] += 60
    device._schedule_notify(immediate=True)
    clock[0] += 7200
    device._schedule_notify(immediate=True)
    assert kwh(energy_in) == pytest.approx(1.0 + 1000 * 60 / 3.6e6, abs=0.001)
    assert hass.states.get(energy_in).state != "unavailable"

    # It comes back and counting resumes from the first reading, not before it.
    device._client = ConnectedClient()
    report(1000, 500)
    clock[0] += 3600
    report(1000, 500)
    assert kwh(energy_in) == pytest.approx(2.0 + 1000 * 60 / 3.6e6, abs=0.001)

    await hass.config_entries.async_unload(entry.entry_id)


async def test_energy_total_is_restored_after_a_restart(hass) -> None:
    entry = ble_entry()
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{SERIAL}_energy_output",
        config_entry=entry,
        suggested_object_id="total_output_energy",
    )
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State("sensor.total_output_energy", "12.345"),
                SensorExtraStoredData(12.345, "kWh").as_dict(),
            )
        ],
    )

    await setup_ble(hass, entry)

    assert float(hass.states.get("sensor.total_output_energy").state) == 12.345
    await hass.config_entries.async_unload(entry.entry_id)


def energy_sensor(**hub_attributes) -> UgreenEnergySensor:
    hub = SimpleNamespace(
        unique_id_base=SERIAL,
        device_name="ugreen gs1200",
        sn=SERIAL,
        available=True,
        data={"charge_power_all": 300, "discharge_pow": 120},
    )
    for name, value in hub_attributes.items():
        setattr(hub, name, value)
    return UgreenEnergySensor(hub, hub, "energy_input")


def test_the_cloud_hub_is_trusted_while_its_socket_is_up() -> None:
    # It has no last_frame_age: its own stall timeout drops the socket instead.
    assert energy_sensor()._live_power() == 300
    assert energy_sensor(available=False)._live_power() is None


def test_a_bluetooth_link_that_has_stopped_delivering_frames_is_not_live() -> None:
    assert energy_sensor(last_frame_age=1.0)._live_power() == 300
    assert energy_sensor(last_frame_age=60.0)._live_power() is None
    # Connected, but no frame yet since (re)connecting.
    assert energy_sensor(last_frame_age=None)._live_power() is None


def test_a_reading_that_is_not_a_number_is_not_integrated() -> None:
    sensor = energy_sensor(data={"charge_power_all": "n/a"})

    assert sensor._live_power() is None
