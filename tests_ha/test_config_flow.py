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
from custom_components.ugreen_powerroam.const import DOMAIN

CREDENTIALS = {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "hunter2"}
DEVICE_NAME = "G00XX0000000000"
DEVICE_SN = "G00XX0000000000"

LOGIN = "custom_components.ugreen_powerroam.api.UgreenApiClient.login"


async def _fake_login(self, email, password):
    """Stand-in for a real login - sets what async_step_user reads afterwards."""
    self.device_name = DEVICE_NAME
    self.sn = DEVICE_SN


async def _start(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


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
    assert result["data"] == CREDENTIALS


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
