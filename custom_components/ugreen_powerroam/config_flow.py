"""Config flow for UGREEN PowerRoam.

Offers a choice of transport. The cloud path asks for account credentials; the
Bluetooth path either picks up an automatic discovery or lists the PowerRoams
an adapter can currently see.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import UgreenApiClient, UgreenApiError, UgreenAuthError
from .const import (
    BLE_NAME_PREFIX,
    CONF_ADDRESS,
    CONF_DEVICE_NAME,
    CONF_SN,
    CONF_TRANSPORT,
    DOMAIN,
    TRANSPORT_BLE,
    TRANSPORT_CLOUD,
)

_LOGGER = logging.getLogger(__name__)

STEP_CLOUD_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


def _is_powerroam(name: str | None) -> bool:
    return bool(name) and name.lower().startswith(BLE_NAME_PREFIX)


class UgreenConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, str] = {}
        self._address: str | None = None
        self._name: str | None = None

    # -- entry points -------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        return self.async_show_menu(
            step_id="user", menu_options=["cloud", "bluetooth_pick"]
        )

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak):
        """Handle a PowerRoam spotted by Home Assistant's Bluetooth stack."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        self._address = discovery_info.address
        self._name = discovery_info.name or discovery_info.address
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_bluetooth_confirm()

    # -- cloud --------------------------------------------------------------

    async def async_step_cloud(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            api = UgreenApiClient(session, hass=self.hass)
            try:
                await api.login(user_input[CONF_EMAIL], user_input[CONF_PASSWORD])
            except UgreenAuthError:
                errors["base"] = "invalid_auth"
            except UgreenApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error validating UGREEN login")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(api.sn or api.device_name)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"UGREEN PowerRoam ({api.device_name})",
                    data={**user_input, CONF_TRANSPORT: TRANSPORT_CLOUD},
                )

        return self.async_show_form(
            step_id="cloud", data_schema=STEP_CLOUD_SCHEMA, errors=errors
        )

    # -- bluetooth ----------------------------------------------------------

    async def async_step_bluetooth_pick(self, user_input: dict[str, Any] | None = None):
        """List the PowerRoams currently visible to any adapter."""
        if user_input is not None:
            self._address = user_input[CONF_ADDRESS]
            self._name = self._discovered.get(self._address, self._address)
            await self.async_set_unique_id(self._address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return await self.async_step_bluetooth_confirm()

        current = self._async_current_ids()
        self._discovered = {
            info.address: info.name or info.address
            for info in bluetooth.async_discovered_service_info(self.hass, False)
            if _is_powerroam(info.name) and info.address not in current
        }
        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="bluetooth_pick",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {
                            address: f"{name} ({address})"
                            for address, name in self._discovered.items()
                        }
                    )
                }
            ),
        )

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ):
        """Confirm, and try to adopt the device's own serial as its identity."""
        assert self._address is not None

        if user_input is None:
            return self.async_show_form(
                step_id="bluetooth_confirm",
                description_placeholders={"name": self._name or self._address},
            )

        # Reading the serial lets a Bluetooth entry carry the same identity the
        # cloud entry uses, so entities keep their history across a migration.
        # Purely an optimisation - the MAC is a perfectly good fallback.
        serial: str | None = None
        try:
            from .ble import async_probe_serial

            serial = await async_probe_serial(self.hass, self._address)
        except Exception as err:  # never block setup on an optional lookup
            _LOGGER.debug("Could not read PowerRoam serial over BLE: %s", err)

        if serial:
            await self.async_set_unique_id(serial, raise_on_progress=False)
            self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=f"UGREEN PowerRoam ({self._name or self._address})",
            data={
                CONF_TRANSPORT: TRANSPORT_BLE,
                CONF_ADDRESS: self._address,
                CONF_DEVICE_NAME: self._name,
                CONF_SN: serial,
            },
        )
