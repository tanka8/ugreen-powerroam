"""Diagnostics for the UGREEN PowerRoam.

The download is meant to be attached to a public GitHub issue, typically by
someone with a model nobody else has, so it carries what is needed to map that
model - the last telemetry, the names of every field the cloud sent, and how
the Bluetooth decoder is coping - and redacts everything that identifies the
account or the unit.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import (
    CONF_ADDRESS,
    CONF_DEVICE_NAME,
    CONF_SN,
    CONF_TOKEN,
    CONF_USER_ID,
    DOMAIN,
    SENSORS,
    SWITCHES,
    TRANSPORT_BLE,
    TRANSPORT_CLOUD,
)

REDACTED = "**REDACTED**"

# Redacted wherever they appear, whether in the config entry, the cloud payload
# or anything nested inside either. The config entry holds the email, password,
# serial or MAC; the cloud payload repeats the serial three ways (sn_num,
# deviceModelName, and the Bluetooth id, which is the MAC without separators).
TO_REDACT = {
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_TOKEN,
    CONF_USER_ID,
    "userId",
    CONF_SN,
    "sn_num",
    "deviceModelName",
    CONF_DEVICE_NAME,
    "deviceName",
    CONF_ADDRESS,
    "bluetooth_id",
    # The config entry's own title and unique_id are built from the serial or
    # the MAC.
    "title",
    "unique_id",
}

# A telemetry field this integration has never heard of, from a model it has
# never seen, may still carry an identifier. Any key containing one of these is
# redacted as well, on top of the exact names above.
SENSITIVE_KEY_PARTS = (
    "bluetooth",
    "email",
    "mac",
    "password",
    "serial",
    "token",
    "uuid",
    "user_id",
    "userid",
)


def _sensitive_keys(data: dict[str, Any]) -> set[str]:
    return {
        key
        for key in data
        if any(part in str(key).lower() for part in SENSITIVE_KEY_PARTS)
    }


def _identifiers_as_hex(entry: ConfigEntry) -> set[str]:
    """The serial and MAC as they might appear inside a raw frame payload.

    A serial travels as ASCII; a MAC as six bytes in either order. Matching on
    the hex is blunt but it is all that can be done with a payload nobody
    understands yet.
    """
    needles: set[str] = set()
    serial = entry.data.get(CONF_SN)
    if serial:
        needles.add(str(serial).encode().hex())
    address = entry.data.get(CONF_ADDRESS)
    if address:
        try:
            mac = bytes.fromhex(str(address).replace(":", ""))
        except ValueError:
            mac = b""
        if mac:
            needles.add(mac.hex())
            needles.add(mac[::-1].hex())
    return needles


def _scrub(payload: str | None, needles: Iterable[str]) -> str | None:
    if payload is None:
        return None
    for needle in needles:
        payload = payload.replace(needle, REDACTED)
    return payload


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return everything worth attaching to a bug report, minus the secrets."""
    version = (await async_get_integration(hass, DOMAIN)).version
    diag: dict[str, Any] = {
        "integration_version": str(version) if version else None,
        "entry": {
            "title": entry.title,
            "unique_id": entry.unique_id,
            "version": entry.version,
            "data": dict(entry.data),
        },
    }

    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is None:
        # Set-up failed or the entry is disabled; the entry data above is
        # still worth having.
        diag["loaded"] = False
        return async_redact_data(diag, TO_REDACT)

    hub = runtime["hub"]
    transport = runtime["transport"]
    telemetry = dict(hub.data)

    diag["loaded"] = True
    diag["transport"] = transport
    diag["connected"] = hub.available
    diag["telemetry"] = telemetry
    # Names only, so a model whose fields this integration does not expose yet
    # can be seen at a glance.
    diag["telemetry_fields"] = sorted(telemetry)
    diag["fields_without_an_entity"] = sorted(
        key for key in telemetry if key not in SENSORS and key not in SWITCHES
    )

    to_redact = TO_REDACT | _sensitive_keys(telemetry)

    if transport == TRANSPORT_BLE:
        ble = hub.diagnostics()
        needles = _identifiers_as_hex(entry)
        for details in ble["unknown_opcodes"].values():
            details["last_payload"] = _scrub(details["last_payload"], needles)
        # The advertised name tells a GS1200 from anything else. It is the
        # address when the unit advertised no name, which is not for sharing.
        name = hub.device_name
        ble["advertised_name"] = None if name == hub.address else name
        diag["ble"] = ble
    elif transport == TRANSPORT_CLOUD:
        diag["cloud"] = {"field_count": len(telemetry)}

    return async_redact_data(diag, to_redact)
