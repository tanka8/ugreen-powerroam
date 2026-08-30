"""Tests for the Bluetooth transport's own logic.

These live in tests_ha/ rather than tests/ because ble.py imports Home
Assistant, bleak and bleak_retry_connector at module level. They do not need a
running Home Assistant instance though: UgreenBleDevice only ever touches
``hass.loop``, so a stub with a real event loop is enough. Nothing here opens a
connection - the client is replaced with a recorder and frames are fed straight
into the notification handler.

The wire format itself is covered without any of this in tests/test_protocol.py.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bleak.exc import BleakError

from custom_components.ugreen_powerroam import protocol
from custom_components.ugreen_powerroam.ble import (
    MAX_BUFFER,
    UgreenBleDevice,
    UgreenBleError,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"
IOT_SERIAL = "IOT000000000001"
DEVICE_SERIAL = "DEV000000000002"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations():
    """Override the conftest fixture - these tests never load the integration.

    Without this every test here would pull in the ``hass`` fixture, which is
    what makes the Home Assistant harness Windows-only.
    """
    return


class FakeClient:
    """Stands in for a connected BleakClient and records what was written."""

    def __init__(self, connected: bool = True) -> None:
        self.is_connected = connected
        self.writes: list[bytes] = []
        self.fail_with: Exception | None = None

    async def write_gatt_char(self, uuid, data, response=True) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.writes.append(bytes(data))


class FakeHandle:
    """What call_later hands back - cancellable, and firable on demand."""

    def __init__(self, callback) -> None:
        self._callback = callback
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        self._callback()


class FakeLoop:
    """Just enough loop for the debounce, and deterministic with it.

    A real loop would make the ~1.6 Hz coalescing untestable without sleeping;
    here the pending callback is held until a test chooses to fire it.
    """

    def __init__(self) -> None:
        self.handles: list[FakeHandle] = []

    def call_later(self, delay, callback) -> FakeHandle:
        handle = FakeHandle(callback)
        self.handles.append(handle)
        return handle

    def call_soon_threadsafe(self, callback, *args) -> None:
        callback(*args)

    @property
    def pending(self) -> list[FakeHandle]:
        return [h for h in self.handles if not h.cancelled]


@pytest.fixture
def loop():
    return FakeLoop()


@pytest.fixture
def hass_stub(loop):
    return SimpleNamespace(loop=loop)


@pytest.fixture
def device(hass_stub):
    return UgreenBleDevice(hass_stub, ADDRESS, device_name="PowerRoam", sn=IOT_SERIAL)


def device_frame(cmd: int, data: bytes) -> bytes:
    body = protocol.REPLY_PREFIX + bytes([cmd]) + len(data).to_bytes(2, "little") + data
    return protocol.HEADER + body + protocol.crc16_modbus(body).to_bytes(2, "little")


def switch_payload(**overrides: int) -> bytes:
    values = dict.fromkeys(protocol.SWITCH_RECV_FIELDS, 0)
    values.update(overrides)
    return bytes(values[name] for name in protocol.SWITCH_RECV_FIELDS)


def totals_frame(input_power: int, output_power: int) -> bytes:
    data = input_power.to_bytes(2, "little") + output_power.to_bytes(2, "little")
    return device_frame(protocol.OP_TOTALS, data)


# -- identity ---------------------------------------------------------------


def test_unique_id_base_prefers_the_serial(hass_stub) -> None:
    device = UgreenBleDevice(hass_stub, ADDRESS, sn=IOT_SERIAL)

    assert device.unique_id_base == IOT_SERIAL


def test_unique_id_base_falls_back_to_the_address(hass_stub) -> None:
    device = UgreenBleDevice(hass_stub, ADDRESS)

    assert device.unique_id_base == ADDRESS
    assert device.device_name == ADDRESS


def test_learning_a_serial_later_does_not_move_the_unique_id(hass_stub) -> None:
    """If this moved, every entity would re-register and lose its history."""
    device = UgreenBleDevice(hass_stub, ADDRESS)

    device._on_notify(None, bytearray(device_frame(protocol.CMD_GET_SERIAL, b"")))
    payload = (IOT_SERIAL + DEVICE_SERIAL).encode("ascii")
    device._on_notify(None, bytearray(device_frame(protocol.CMD_GET_SERIAL, payload)))

    assert device.sn == IOT_SERIAL
    assert device.unique_id_base == ADDRESS


def test_serial_learned_on_the_wire_is_the_one_the_cloud_uses(hass_stub) -> None:
    device = UgreenBleDevice(hass_stub, ADDRESS)
    payload = (IOT_SERIAL + DEVICE_SERIAL).encode("ascii")

    device._on_notify(None, bytearray(device_frame(protocol.CMD_GET_SERIAL, payload)))

    assert device.sn == IOT_SERIAL


def test_a_known_serial_is_not_overwritten(device) -> None:
    payload = ("OTHER0000000001" + DEVICE_SERIAL).encode("ascii")

    device._on_notify(None, bytearray(device_frame(protocol.CMD_GET_SERIAL, payload)))

    assert device.sn == IOT_SERIAL


# -- availability -----------------------------------------------------------


def test_unavailable_until_connected(device) -> None:
    assert device.available is False


def test_available_tracks_the_client(device) -> None:
    device._client = FakeClient()
    assert device.available is True

    device._client.is_connected = False
    assert device.available is False


# -- inbound notifications --------------------------------------------------


def test_notification_populates_cloud_shaped_keys(device) -> None:
    device._on_notify(None, bytearray(totals_frame(320, 75)))

    assert device.data["charge_power_all"] == 320
    assert device.data["discharge_pow"] == 75


def test_several_frames_in_one_notification_are_all_applied(device) -> None:
    payload = totals_frame(10, 20) + device_frame(protocol.OP_WORK_MODE, b"\x02")

    device._on_notify(None, bytearray(payload))

    assert device.data["charge_power_all"] == 10
    assert device.data["work_mode"] == 2


def test_frame_split_across_notifications_is_reassembled(device) -> None:
    whole = totals_frame(320, 75)

    device._on_notify(None, bytearray(whole[:8]))
    assert device.data == {}

    device._on_notify(None, bytearray(whole[8:]))
    assert device.data["charge_power_all"] == 320


def test_zero_padding_does_not_accumulate_in_the_buffer(device) -> None:
    for _ in range(5):
        device._on_notify(None, bytearray(totals_frame(1, 2) + b"\x00" * 16))

    assert len(device._buffer) == 0


def test_bad_crc_frames_are_dropped(device) -> None:
    raw = bytearray(totals_frame(320, 75))
    raw[7] ^= 0xFF

    device._on_notify(None, raw)

    assert device.data == {}


def test_a_desynchronised_buffer_is_discarded(device) -> None:
    """Junk that never resynchronises must not grow without bound."""
    device._on_notify(None, bytearray(b"\x5a" * (MAX_BUFFER + 64)))

    assert len(device._buffer) <= MAX_BUFFER


def test_switch_frames_capture_the_full_switch_block(device) -> None:
    device._on_notify(
        None,
        bytearray(
            device_frame(protocol.OP_SWITCHES, switch_payload(ac=1, battery_health=1))
        ),
    )

    assert device._switch_state["ac"] is True
    assert device._switch_state["battery_health"] is True
    assert device.data["switch_ac"] == 1


def test_listeners_fire_only_when_a_value_actually_changes(device, loop) -> None:
    calls: list[int] = []
    device.add_listener(lambda: calls.append(1))

    device._on_notify(None, bytearray(totals_frame(320, 75)))
    assert len(loop.pending) == 1
    loop.pending[0].fire()
    assert calls == [1]

    # Same values again - nothing changed, so nothing new should be scheduled.
    device._on_notify(None, bytearray(totals_frame(320, 75)))

    assert device._notify_handle is None
    assert calls == [1]


def test_removing_a_listener_stops_it_being_called(device) -> None:
    calls: list[int] = []
    remove = device.add_listener(lambda: calls.append(1))

    remove()
    device._schedule_notify(immediate=True)

    assert calls == []


def test_updates_are_coalesced_rather_than_fired_per_frame(device, loop) -> None:
    """Three status cycles must wake the entities once, not three times."""
    calls: list[int] = []
    device.add_listener(lambda: calls.append(1))

    device._on_notify(None, bytearray(totals_frame(1, 2)))
    device._on_notify(None, bytearray(totals_frame(3, 4)))
    device._on_notify(None, bytearray(totals_frame(5, 6)))

    assert calls == []  # debounced, not yet fired
    assert len(loop.pending) == 1

    loop.pending[0].fire()

    assert calls == [1]
    assert device.data["charge_power_all"] == 5  # the newest value, not the first


# -- control writes ---------------------------------------------------------


async def test_flashlight_write_encodes_a_light_command(device) -> None:
    device._client = FakeClient()

    await device.set_device_info("lamp_sw", 1)

    assert device._client.writes == [protocol.encode_light(1)]
    assert device.data["lamp_sw"] == 1


async def test_flashlight_off_encodes_level_zero(device) -> None:
    device._client = FakeClient()

    await device.set_device_info("lamp_sw", 0)

    assert device._client.writes == [protocol.encode_light(0)]
    assert device.data["lamp_sw"] == 0


async def test_switch_write_refuses_before_the_state_is_known(device) -> None:
    """0x16 is a whole-block write; guessing would change untouched settings."""
    device._client = FakeClient()

    with pytest.raises(UgreenBleError, match="not known yet"):
        await device.set_device_info("switch_ac", 1)

    assert device._client.writes == []


async def test_switch_write_flips_only_the_requested_field(device) -> None:
    device._client = FakeClient()
    device._on_notify(
        None,
        bytearray(
            device_frame(
                protocol.OP_SWITCHES, switch_payload(battery_health=1, low_noise=1)
            )
        ),
    )

    await device.set_device_info("switch_ac", 1)

    body = device._client.writes[0][7:-2]
    sent = dict(zip(protocol.SWITCH_SEND_FIELDS, body, strict=True))

    assert sent["ac"] == 1
    assert sent["battery_health"] == 1  # preserved, not clobbered
    assert sent["low_noise"] == 1
    assert sent["dc"] == 0
    assert device.data["switch_ac"] == 1


async def test_switch_write_updates_the_cached_state(device) -> None:
    device._client = FakeClient()
    device._on_notify(
        None, bytearray(device_frame(protocol.OP_SWITCHES, switch_payload()))
    )

    await device.set_device_info("switch_dc", 1)

    assert device._switch_state["dc"] is True


async def test_uncontrollable_key_is_rejected(device) -> None:
    device._client = FakeClient()

    with pytest.raises(UgreenBleError, match="cannot be controlled"):
        await device.set_device_info("battery_percentage", 50)


async def test_write_without_a_connection_raises(device) -> None:
    with pytest.raises(UgreenBleError, match="not connected"):
        await device.set_device_info("lamp_sw", 1)


async def test_write_to_a_disconnected_client_raises(device) -> None:
    device._client = FakeClient(connected=False)

    with pytest.raises(UgreenBleError, match="not connected"):
        await device.set_device_info("lamp_sw", 1)


async def test_bleak_errors_surface_as_a_readable_error(device) -> None:
    """A failed toggle should reach the user as a message, not a traceback."""
    device._client = FakeClient()
    device._client.fail_with = BleakError("characteristic not found")

    with pytest.raises(UgreenBleError, match="write failed"):
        await device.set_device_info("lamp_sw", 1)


async def test_a_control_write_notifies_immediately(device) -> None:
    calls: list[int] = []
    device._client = FakeClient()
    device.add_listener(lambda: calls.append(1))

    await device.set_device_info("lamp_sw", 1)

    assert calls == [1]


# -- lifecycle --------------------------------------------------------------


async def test_stop_cancels_a_pending_debounce(device) -> None:
    device._on_notify(None, bytearray(totals_frame(1, 2)))
    assert device._notify_handle is not None

    await device.stop()

    assert device._notify_handle is None


async def test_stop_is_safe_before_start(device) -> None:
    await device.stop()

    assert device.available is False
