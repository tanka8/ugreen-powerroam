"""The UGREEN PowerRoam integration.

Two transports share one set of entities:

  cloud  UgreenApiClient + UgreenTelemetryHub, talking to ugpps.com over HTTPS
         and a WebSocket. Needs an account and internet.
  ble    UgreenBleDevice, talking to the unit directly over Bluetooth LE.
         Needs neither, and pushes state far more often.

Both expose the same small surface, so the entity platforms are transport
agnostic - see ble.py for the details.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import UgreenApiClient, UgreenAuthError, UgreenTelemetryHub
from .const import (
    CONF_ADDRESS,
    CONF_DEVICE_NAME,
    CONF_SN,
    CONF_TRANSPORT,
    DOMAIN,
    TRANSPORT_BLE,
    TRANSPORT_CLOUD,
)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    transport = entry.data.get(CONF_TRANSPORT, TRANSPORT_CLOUD)

    if transport == TRANSPORT_BLE:
        device = await _async_setup_ble(hass, entry)
        data = {"api": device, "hub": device, "transport": TRANSPORT_BLE}
    else:
        session = async_get_clientsession(hass)
        api = UgreenApiClient(session, hass=hass)
        try:
            await api.login(entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
        except UgreenAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err

        hub = UgreenTelemetryHub(api, session)
        await hub.start()
        data = {"api": api, "hub": hub, "transport": TRANSPORT_CLOUD}

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = data

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_setup_ble(hass: HomeAssistant, entry: ConfigEntry):
    """Start the Bluetooth transport, or ask HA to retry if it is out of range."""
    # Imported here rather than at module scope so a cloud-only setup never
    # pulls in the Bluetooth stack.
    from homeassistant.components import bluetooth

    from .ble import UgreenBleDevice

    address = entry.data[CONF_ADDRESS]

    # If no adapter can currently see the unit there is nothing to connect to.
    # Raising here lets HA retry with backoff instead of leaving dead entities.
    if bluetooth.async_ble_device_from_address(hass, address, connectable=True) is None:
        raise ConfigEntryNotReady(
            f"PowerRoam {address} is not currently visible to any Bluetooth adapter"
        )

    device = UgreenBleDevice(
        hass,
        address,
        device_name=entry.data.get(CONF_DEVICE_NAME),
        sn=entry.data.get(CONF_SN),
    )
    await device.start()
    return device


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        await data["hub"].stop()
    return unload_ok
