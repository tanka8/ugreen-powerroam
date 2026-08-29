"""Config flow tests.

These exercise the flow through Home Assistant itself rather than by calling the
methods directly, so they cover the wiring as well as the logic.
"""

from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ugreen_powerroam.api import UgreenApiError, UgreenAuthError
from custom_components.ugreen_powerroam.const import (
    CONF_TRANSPORT,
    DOMAIN,
    TRANSPORT_CLOUD,
)

CREDENTIALS = {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "hunter2"}
DEVICE_NAME = "G00XX0000000000"
DEVICE_SN = "G00XX0000000000"

LOGIN = "custom_components.ugreen_powerroam.api.UgreenApiClient.login"


async def _fake_login(self, email, password):
    """Stand-in for a real login - sets what async_step_user reads afterwards."""
    self.device_name = DEVICE_NAME
    self.sn = DEVICE_SN


async def _start(hass):
    """Open the flow and step through the transport menu to the cloud form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cloud"}
    )


async def test_user_step_offers_both_transports(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {"cloud", "bluetooth_pick"}


async def test_user_flow_creates_entry(hass):
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM

    with (
        patch(LOGIN, autospec=True, side_effect=_fake_login),
        patch(
            "custom_components.ugreen_powerroam.async_setup_entry", return_value=True
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"UGREEN PowerRoam ({DEVICE_NAME})"
    assert result["data"] == {**CREDENTIALS, CONF_TRANSPORT: TRANSPORT_CLOUD}


@pytest.mark.parametrize(
    ("side_effect", "expected"),
    [
        (UgreenAuthError("nope"), "invalid_auth"),
        # Covers both a plain connection failure and the "no devices on this
        # account" case - api.py raises the same UgreenApiError for both.
        (UgreenApiError("down"), "cannot_connect"),
        (ValueError("something unrelated broke"), "unknown"),
    ],
)
async def test_user_flow_errors(hass, side_effect, expected):
    result = await _start(hass)
    with patch(LOGIN, side_effect=side_effect):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}


async def test_recovers_after_an_error(hass):
    """A failed attempt must leave the form usable, not wedge the flow."""
    result = await _start(hass)
    with patch(LOGIN, side_effect=UgreenAuthError("nope")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )
    assert result["errors"]

    with (
        patch(LOGIN, autospec=True, side_effect=_fake_login),
        patch(
            "custom_components.ugreen_powerroam.async_setup_entry", return_value=True
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_duplicate_device_aborts(hass):
    """The same PowerRoam must not be addable twice."""
    MockConfigEntry(
        domain=DOMAIN,
        data=CREDENTIALS,
        unique_id=DEVICE_SN,
    ).add_to_hass(hass)

    result = await _start(hass)
    with patch(LOGIN, autospec=True, side_effect=_fake_login):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# -- Bluetooth transport -----------------------------------------------------

BLE_ADDRESS = "AA:BB:CC:DD:EE:FF"
BLE_NAME = "ugreen gs1200"
BLE_SERIAL = "G00XX0000000001"

DISCOVERED = "custom_components.ugreen_powerroam.config_flow.bluetooth.async_discovered_service_info"
PROBE = "custom_components.ugreen_powerroam.ble.async_probe_serial"


class _FakeServiceInfo:
    """Just the two attributes the flow reads off a discovery."""

    def __init__(self, address, name):
        self.address = address
        self.name = name


async def _pick_bluetooth(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "bluetooth_pick"}
    )


async def test_bluetooth_flow_creates_entry_with_serial(hass):
    """A reachable unit lends its serial to the entry, matching the cloud id."""
    with patch(DISCOVERED, return_value=[_FakeServiceInfo(BLE_ADDRESS, BLE_NAME)]):
        result = await _pick_bluetooth(hass)
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"address": BLE_ADDRESS}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"

    with (
        patch(PROBE, return_value=BLE_SERIAL),
        patch(
            "custom_components.ugreen_powerroam.async_setup_entry", return_value=True
        ),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["transport"] == "ble"
    assert result["data"]["address"] == BLE_ADDRESS
    assert result["data"]["sn"] == BLE_SERIAL


async def test_bluetooth_flow_falls_back_to_mac_when_serial_unreadable(hass):
    """An unreachable serial must not block setup - the MAC identifies it."""
    with patch(DISCOVERED, return_value=[_FakeServiceInfo(BLE_ADDRESS, BLE_NAME)]):
        result = await _pick_bluetooth(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"address": BLE_ADDRESS}
        )

    with (
        patch(PROBE, side_effect=TimeoutError),
        patch(
            "custom_components.ugreen_powerroam.async_setup_entry", return_value=True
        ),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["sn"] is None
    assert result["data"]["address"] == BLE_ADDRESS


async def test_bluetooth_flow_aborts_when_nothing_found(hass):
    with patch(DISCOVERED, return_value=[]):
        result = await _pick_bluetooth(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_bluetooth_ignores_non_powerroam_devices(hass):
    """Only UGREEN-named advertisements should be offered."""
    with patch(
        DISCOVERED,
        return_value=[_FakeServiceInfo("11:22:33:44:55:66", "Some Other Gadget")],
    ):
        result = await _pick_bluetooth(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_bluetooth_duplicate_address_aborts(hass):
    MockConfigEntry(domain=DOMAIN, data={}, unique_id=BLE_ADDRESS).add_to_hass(hass)

    with patch(DISCOVERED, return_value=[_FakeServiceInfo(BLE_ADDRESS, BLE_NAME)]):
        result = await _pick_bluetooth(hass)

    # Already-configured devices are filtered out of the picker entirely.
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"
