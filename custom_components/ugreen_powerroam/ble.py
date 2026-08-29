"""Bluetooth LE transport for the UGREEN PowerRoam.

A local alternative to the cloud WebSocket. The device runs an open GATT
server - no pairing, no bonding, no account - and pushes its full status set
unprompted roughly every 0.6s, so this is a genuine local-push transport.

UgreenBleDevice deliberately presents the same surface the cloud path does, and
is handed to the entity platforms as both the "api" and the "hub":

    api-shaped   .sn, .device_name, .set_device_info(key, value)
    hub-shaped   .data, .add_listener(cb), .start(), .stop()

which is why sensor.py and switch.py need to know nothing about transports.

The wire format lives in protocol.py and is tested without hardware.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from . import protocol
from .const import (
    BLE_NOTIFY_UUID,
    BLE_RECONNECT_BACKOFF_MAX,
    BLE_RECONNECT_BACKOFF_START,
    BLE_STATE_DEBOUNCE,
    BLE_WRITE_UUID,
)

_LOGGER = logging.getLogger(__name__)

# Raw frames on their own logger, so they can be turned on without drowning in
# everything else this module says:
#
#   logger:
#     logs:
#       custom_components.ugreen_powerroam.frames: debug
#
# Only changed payloads are logged, not all ~22 frames a second, which keeps
# this usable for working out what an unmapped byte means - plug a load in,
# watch which bytes move.
_FRAME_LOGGER = logging.getLogger(f"{__package__}.frames")

# Which switch entity key maps onto which field of the 0x16 switch block.
SWITCH_KEY_TO_FIELD = {
    "switch_ac": "ac",
    "switch_dc": "dc",
    "usb_sw": "usb",
}

# A notification buffer should never grow past a couple of frames. If it does,
# the stream is out of sync and holding on to the bytes helps nobody.
MAX_BUFFER = 1024

# How long the config flow waits for the unit to answer a serial-number read.
SERIAL_PROBE_TIMEOUT = 20.0


class UgreenBleError(HomeAssistantError):
    """Raised when a control command cannot be delivered over Bluetooth.

    Subclasses HomeAssistantError so a failed switch toggle surfaces to the
    user as a readable message rather than an unhandled traceback.
    """


class UgreenBleDevice:
    """Owns the BLE connection, the last-known state, and control writes."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        device_name: str | None = None,
        sn: str | None = None,
    ) -> None:
        self._hass = hass
        self.address = address
        self.device_name = device_name or address
        self.sn = sn

        # Fixed at setup from the config entry, never from a serial learned
        # later on the wire: if this moved, every entity would be re-registered
        # under a new id after a restart and lose its history.
        self._unique_id_base = sn or address

        self.data: dict = {}
        self._switch_state: dict[str, int | bool] | None = None
        self._listeners: list[Callable[[], None]] = []

        self._client: BleakClientWithServiceCache | None = None
        self._buffer = bytearray()
        self._task: asyncio.Task | None = None
        self._disconnected = asyncio.Event()
        self._stopped = False
        self._notify_handle: asyncio.TimerHandle | None = None
        self._last_payloads: dict[int, str] = {}

    @property
    def available(self) -> bool:
        """Whether the data in .data is live rather than left over.

        Without this the entities keep serving whatever the last connection
        saw, indefinitely and while still looking healthy - which is the same
        silent staleness the cloud transport used to suffer from.
        """
        return self._client is not None and self._client.is_connected

    @property
    def unique_id_base(self) -> str:
        """Stable prefix for entity unique_ids, shared with the cloud client."""
        return self._unique_id_base

    # -- hub-shaped surface -------------------------------------------------

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)

        def _remove() -> None:
            if cb in self._listeners:
                self._listeners.remove(cb)

        return _remove

    def _notify_listeners(self) -> None:
        for cb in list(self._listeners):
            cb()

    def _schedule_notify(self, immediate: bool = False) -> None:
        """Coalesce the ~1.6 Hz push stream down to something HA can live with.

        Without this every status cycle would wake every entity and hammer the
        recorder. A control write passes immediate=True so the user still sees
        their switch flip straight away.
        """
        if immediate:
            if self._notify_handle is not None:
                self._notify_handle.cancel()
                self._notify_handle = None
            self._notify_listeners()
            return
        if self._notify_handle is not None:
            return

        def _fire() -> None:
            self._notify_handle = None
            self._notify_listeners()

        self._notify_handle = self._hass.loop.call_later(BLE_STATE_DEBOUNCE, _fire)

    async def start(self) -> None:
        self._stopped = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        if self._notify_handle is not None:
            self._notify_handle.cancel()
            self._notify_handle = None
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._disconnect()

    # -- api-shaped surface -------------------------------------------------

    async def set_device_info(self, key: str, value: int) -> None:
        """Send one control command, matching the cloud client's signature."""
        if key == "lamp_sw":
            await self._write(protocol.encode_light(1 if value else 0))
            self.data["lamp_sw"] = 1 if value else 0
            self._schedule_notify(immediate=True)
            return

        field = SWITCH_KEY_TO_FIELD.get(key)
        if field is None:
            raise UgreenBleError(f"{key} cannot be controlled over Bluetooth")

        # 0x16 replaces the entire switch block. Writing one without knowing
        # the current state changes settings the user never touched - during
        # development exactly that silently disabled battery preserving mode.
        if self._switch_state is None:
            raise UgreenBleError(
                "switch state not known yet - waiting for the device to report in"
            )

        desired = dict(self._switch_state)
        desired[field] = bool(value)
        await self._write(protocol.encode_switch_state(desired))

        self._switch_state = desired
        self.data[key] = 1 if value else 0
        self._schedule_notify(immediate=True)

    async def _write(self, frame: bytes) -> None:
        client = self._client
        if client is None or not client.is_connected:
            raise UgreenBleError("not connected to the PowerRoam")
        try:
            await client.write_gatt_char(BLE_WRITE_UUID, frame, response=True)
        except BleakError as err:
            raise UgreenBleError(f"write failed: {err}") from err

    # -- connection ---------------------------------------------------------

    async def _run(self) -> None:
        backoff = BLE_RECONNECT_BACKOFF_START
        while not self._stopped:
            try:
                await self._connect_once()
                backoff = BLE_RECONNECT_BACKOFF_START
            except asyncio.CancelledError:
                raise
            except Exception as err:  # the reconnect loop must survive anything
                _LOGGER.debug("PowerRoam BLE connection error: %s", err)
            if self._stopped:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BLE_RECONNECT_BACKOFF_MAX)

    async def _connect_once(self) -> None:
        ble_device = bluetooth.async_ble_device_from_address(
            self._hass, self.address, connectable=True
        )
        if ble_device is None:
            raise UgreenBleError(f"{self.address} not currently visible to any adapter")

        self._disconnected.clear()
        client = await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            self.device_name,
            disconnected_callback=self._on_disconnect,
        )
        self._client = client
        self._buffer.clear()

        try:
            await client.start_notify(BLE_NOTIFY_UUID, self._on_notify)
            # Ask for the serial once so the device identity is known even if
            # the config entry was set up from an advertisement alone.
            if self.sn is None:
                with contextlib.suppress(UgreenBleError):
                    await self._write(protocol.encode(protocol.CMD_GET_SERIAL))
            await self._disconnected.wait()
        finally:
            await self._disconnect()

    def _on_disconnect(self, _client) -> None:
        self._hass.loop.call_soon_threadsafe(self._disconnected.set)

    async def _disconnect(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        with contextlib.suppress(BleakError, asyncio.CancelledError, OSError):
            await client.disconnect()

    # -- inbound data -------------------------------------------------------

    def _on_notify(self, _char, payload: bytearray) -> None:
        self._buffer.extend(payload)
        frames, consumed = protocol.decode_stream(bytes(self._buffer))
        del self._buffer[:consumed]
        if len(self._buffer) > MAX_BUFFER:
            _LOGGER.debug("PowerRoam BLE buffer out of sync, discarding")
            self._buffer.clear()

        changed = False
        for frame in frames:
            if not frame.crc_ok:
                _LOGGER.debug("dropping BLE frame with bad CRC: %s", frame.raw.hex())
                continue
            if _FRAME_LOGGER.isEnabledFor(logging.DEBUG):
                payload = frame.data.hex()
                if self._last_payloads.get(frame.cmd) != payload:
                    self._last_payloads[frame.cmd] = payload
                    _FRAME_LOGGER.debug(
                        "0x%02x len=%d %s", frame.cmd, len(frame.data), payload
                    )

            if frame.cmd == protocol.OP_SWITCHES:
                self._switch_state = protocol.decode_switch_state(frame.data)
            elif frame.cmd == protocol.CMD_GET_SERIAL and self.sn is None:
                # The identity the cloud uses, not the device serial - see
                # protocol.parse_identity for why the distinction matters.
                identity = protocol.parse_identity(frame.data)
                if identity:
                    self.sn = identity

            for key, value in protocol.telemetry_from_frame(frame).items():
                if self.data.get(key) != value:
                    self.data[key] = value
                    changed = True

        if changed:
            self._schedule_notify()


async def async_probe_serial(hass: HomeAssistant, address: str):
    """Connect briefly and ask the unit for the serial the cloud identifies it by.

    Used by the config flow so a Bluetooth entry adopts the same identity the
    cloud entry uses, which is what lets entities keep their history when a
    device is migrated from one transport to the other, and what stops the same
    power station being added twice. Returns None if the device cannot be
    reached in time - the flow then falls back to the MAC.
    """
    ble_device = bluetooth.async_ble_device_from_address(
        hass, address, connectable=True
    )
    if ble_device is None:
        return None

    serial: str | None = None
    got_serial = asyncio.Event()

    def _on_notify(_char, payload: bytearray) -> None:
        nonlocal serial
        for frame in protocol.decode_frames(bytes(payload)):
            if frame.crc_ok and frame.cmd == protocol.CMD_GET_SERIAL:
                identity = protocol.parse_identity(frame.data)
                if identity:
                    serial = identity
                    hass.loop.call_soon_threadsafe(got_serial.set)

    client = await establish_connection(
        BleakClientWithServiceCache, ble_device, address
    )
    try:
        await client.start_notify(BLE_NOTIFY_UUID, _on_notify)
        await client.write_gatt_char(
            BLE_WRITE_UUID, protocol.encode(protocol.CMD_GET_SERIAL), response=True
        )
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(SERIAL_PROBE_TIMEOUT):
                await got_serial.wait()
    finally:
        with contextlib.suppress(BleakError, OSError):
            await client.disconnect()
    return serial
