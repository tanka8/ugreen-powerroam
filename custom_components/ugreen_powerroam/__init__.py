"""The UGREEN PowerRoam integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import UgreenApiClient, UgreenAuthError, UgreenTelemetryHub
from .const import DOMAIN

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    api = UgreenApiClient(session, hass=hass)

    try:
        await api.login(entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
    except UgreenAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err

    hub = UgreenTelemetryHub(api, session)
    await hub.start()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"api": api, "hub": hub}

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        await data["hub"].stop()
    return unload_ok
