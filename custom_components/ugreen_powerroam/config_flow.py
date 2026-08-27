"""Config flow for UGREEN PowerRoam."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import UgreenApiClient, UgreenApiError, UgreenAuthError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class UgreenConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
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
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )
