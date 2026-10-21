"""Tests for the diagnostics download.

The point of the download is to be pasted into a public issue, so most of what
is checked here is what it must not contain: the account, the serial and the
MAC, in the config entry, in the cloud payload, and in raw Bluetooth frames.
Each test serialises the whole result and searches it, rather than checking the
keys it expects to have been redacted, so a secret that turns up somewhere
unexpected is caught too.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ugreen_powerroam import protocol
from custom_components.ugreen_powerroam.ble import UgreenBleDevice
from custom_components.ugreen_powerroam.const import DOMAIN
from custom_components.ugreen_powerroam.diagnostics import (
    REDACTED,
    async_get_config_entry_diagnostics,
)

EMAIL = "someone@example.com"
PASSWORD = "correct horse battery staple"
SERIAL = "G12FE2305110337"
BLUETOOTH_ID = "aabbccddeeff"
ADDRESS = "AA:BB:CC:DD:EE:FF"


def device_frame(cmd: int, data: bytes) -> bytes:
    body = protocol.REPLY_PREFIX + bytes([cmd]) + len(data).to_bytes(2, "little") + data
    return protocol.HEADER + body + protocol.crc16_modbus(body).to_bytes(2, "little")


def register(hass, entry: MockConfigEntry, hub, transport: str) -> None:
    """Put a hub where async_setup_entry would have left it."""
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": hub,
        "hub": hub,
        "transport": transport,
    }


async def test_cloud_diagnostics_redact_credentials_and_identifiers(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"UGREEN PowerRoam ({SERIAL})",
        unique_id=SERIAL,
        data={"email": EMAIL, "password": PASSWORD, "transport": "cloud"},
    )
    hub = SimpleNamespace(
        available=True,
        data={
            "battery_percentage": 76,
            "discharge_pow": 320,
            "sn_num": SERIAL,
            "deviceModelName": SERIAL,
            "bluetooth_id": BLUETOOTH_ID,
            # Not a field this integration knows, but named like an identifier.
            "owner_email_address": EMAIL,
            "mystery_setting": 3,
        },
    )
    register(hass, entry, hub, "cloud")

    result = await async_get_config_entry_diagnostics(hass, entry)
    dump = json.dumps(result)

    for secret in (EMAIL, PASSWORD, SERIAL, BLUETOOTH_ID):
        assert secret not in dump, secret
    assert result["entry"]["data"]["email"] == REDACTED
    assert result["entry"]["data"]["password"] == REDACTED
    assert result["entry"]["title"] == REDACTED
    assert result["entry"]["unique_id"] == REDACTED
    assert result["telemetry"]["sn_num"] == REDACTED
    assert result["telemetry"]["owner_email_address"] == REDACTED


async def test_cloud_diagnostics_keep_what_is_needed_to_map_a_model(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"transport": "cloud"})
    hub = SimpleNamespace(
        available=True,
        data={"battery_percentage": 76, "sn_num": SERIAL, "mystery_setting": 3},
    )
    register(hass, entry, hub, "cloud")

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["transport"] == "cloud"
    assert result["connected"] is True
    assert result["telemetry"]["battery_percentage"] == 76
    assert result["telemetry"]["mystery_setting"] == 3
    assert result["telemetry_fields"] == [
        "battery_percentage",
        "mystery_setting",
        "sn_num",
    ]
    # The field list is what a maintainer reads to see what a new model sends
    # that nothing exposes yet.
    assert result["fields_without_an_entity"] == ["mystery_setting", "sn_num"]


async def test_ble_diagnostics_report_decoder_counters_and_scrub_payloads(
    hass,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="UGREEN PowerRoam (ugreen gs1200)",
        unique_id=SERIAL,
        data={
            "transport": "ble",
            "address": ADDRESS,
            "device_name": "ugreen gs1200",
            "sn": SERIAL,
        },
    )
    device = UgreenBleDevice(hass, ADDRESS, device_name="ugreen gs1200", sn=SERIAL)
    register(hass, entry, device, "ble")

    good = device_frame(protocol.OP_TOTALS, (320).to_bytes(2, "little") + bytes(2))
    bad = bytearray(good)
    bad[7] ^= 0xFF
    # Two opcodes nothing in the integration knows, one of which happens to
    # carry the serial and the other the MAC (in wire order, which is reversed).
    carries_serial = device_frame(0x7E, bytes.fromhex("0102") + SERIAL.encode())
    carries_mac = device_frame(0x7F, bytes.fromhex("FFEEDDCCBBAA"))
    device._on_notify(
        None, bytearray(good + bytes(bad) + carries_serial + carries_mac + good)
    )
    await device.stop()  # cancels the debounce timer the notification started

    result = await async_get_config_entry_diagnostics(hass, entry)
    dump = json.dumps(result)
    ble = result["ble"]

    assert ble["frames_received"] == 5
    assert ble["crc_failures"] == 1
    assert ble["frames_by_opcode"] == {"0x0f": 2, "0x7e": 1, "0x7f": 1}
    assert set(ble["unknown_opcodes"]) == {"0x7e", "0x7f"}
    assert ble["unknown_opcodes"]["0x7e"]["count"] == 1
    assert REDACTED in ble["unknown_opcodes"]["0x7e"]["last_payload"]
    assert REDACTED in ble["unknown_opcodes"]["0x7f"]["last_payload"]
    assert ble["connected"] is False
    assert ble["advertised_name"] == "ugreen gs1200"
    assert result["telemetry"]["charge_power_all"] == 320

    for secret in (
        SERIAL,
        SERIAL.encode().hex(),
        ADDRESS,
        ADDRESS.replace(":", "").lower(),
        "ffeeddccbbaa",
    ):
        assert secret not in dump, secret
    assert result["entry"]["data"]["address"] == REDACTED
    assert result["entry"]["data"]["sn"] == REDACTED


async def test_ble_advertised_name_is_withheld_when_it_is_just_the_address(
    hass,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"transport": "ble"})
    device = UgreenBleDevice(hass, ADDRESS)  # no name, so device_name == address
    register(hass, entry, device, "ble")

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["ble"]["advertised_name"] is None
    assert ADDRESS not in json.dumps(result)


async def test_entry_that_is_not_loaded_still_redacts(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"UGREEN PowerRoam ({SERIAL})",
        data={"email": EMAIL, "password": PASSWORD, "transport": "cloud"},
    )
    entry.add_to_hass(hass)

    result = await async_get_config_entry_diagnostics(hass, entry)
    dump = json.dumps(result)

    assert result["loaded"] is False
    for secret in (EMAIL, PASSWORD, SERIAL):
        assert secret not in dump, secret
