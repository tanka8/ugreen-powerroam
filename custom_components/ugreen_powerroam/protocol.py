"""Pure codec for the UGREEN PowerRoam's Bluetooth LE protocol.

Reverse engineered from the com.powerroam.pps app bundle and then confirmed
frame-for-frame against a real PowerRoam GS1200 over BLE. No Home Assistant,
bleak or network imports live here on purpose: everything in this module is a
pure function over bytes, so it is exercised directly by tests/test_protocol.py
without hardware.

Framing
-------
    5A A5 | A1 C0 | cmd | len (uint16 LE) | data | crc (uint16 LE)

Total frame length is ``len + 9``. CRC is Modbus CRC-16 (poly 0xA001, init
0xFFFF) over everything from the prefix up to but excluding the CRC, appended
low byte first. Frames are self-delimiting and the device concatenates several
per notification, padding the tail with zero bytes - so decoding scans for the
``5A A5`` header rather than assuming one frame per packet.

The prefix is a direction field: commands we send carry ``A1 C0`` and every
frame the device sends back carries ``C0 A1``.

Never emit ``5A A5`` without the ``A1 C0`` prefix. The app's OTA path does
exactly that and it is how firmware is written; there is no reason for this
integration to go near it.
"""

from __future__ import annotations

from dataclasses import dataclass

HEADER = b"\x5a\xa5"
CMD_PREFIX = b"\xa1\xc0"
REPLY_PREFIX = b"\xc0\xa1"

# Read-only commands, safe to send at any time.
CMD_GET_SERIAL = 0x05
CMD_GET_VERSION = 0x06
CMD_GET_MAC = 0x21

# Writes we actually use.
CMD_SET_LIGHT = 0x13
CMD_SET_SWITCHES = 0x16

# Status opcodes the device pushes unprompted, roughly every 0.6s.
OP_FAULT = 0x01
OP_TEMPS = 0x04
OP_BATTERY = 0x09
OP_AC = 0x0B
OP_PORTS = 0x0C
OP_TOTALS = 0x0F
OP_WORK_MODE = 0x12
OP_LIGHT = 0x13
OP_SWITCHES = 0x16


def crc16_modbus(data: bytes) -> int:
    """Modbus CRC-16. Verified byte-for-byte against the app's own tables."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def encode(cmd: int, data: bytes = b"") -> bytes:
    """Build one outbound frame, always with the A1 C0 command prefix."""
    body = CMD_PREFIX + bytes([cmd]) + len(data).to_bytes(2, "little") + data
    return HEADER + body + crc16_modbus(body).to_bytes(2, "little")


@dataclass(frozen=True)
class Frame:
    """One decoded frame. ``crc_ok`` False means the payload is not trustworthy."""

    prefix: bytes
    cmd: int
    data: bytes
    crc_ok: bool
    raw: bytes


def decode_stream(buf: bytes) -> tuple[list[Frame], int]:
    """Decode every complete frame in ``buf``, and say how many bytes were used.

    Returns ``(frames, consumed)``. A caller reading a notification stream
    should drop ``consumed`` bytes and keep the remainder as the head of the
    next buffer, so a frame split across two notifications still decodes and
    no frame is ever emitted twice.

    Tolerates the zero padding the device appends and any leading junk by
    resynchronising on the 5A A5 header. A trailing partial frame is left
    unconsumed rather than guessed at.
    """
    frames: list[Frame] = []
    i = 0
    consumed = 0
    while i < len(buf) - 1:
        if buf[i : i + 2] != HEADER:
            i += 1
            continue
        if i + 7 > len(buf):
            break
        total = int.from_bytes(buf[i + 5 : i + 7], "little") + 9
        if i + total > len(buf):
            break
        raw = buf[i : i + total]
        frames.append(
            Frame(
                prefix=raw[2:4],
                cmd=raw[4],
                data=raw[7:-2],
                crc_ok=crc16_modbus(raw[2:-2]) == int.from_bytes(raw[-2:], "little"),
                raw=raw,
            )
        )
        i += total
        consumed = i

    # Drop trailing bytes that cannot begin a frame - notably the zero padding
    # the device appends after the last frame in a packet. Without this the
    # padding accumulates in the caller's buffer forever. A lone trailing 0x5A
    # is kept: it may be the first half of a header split across two packets.
    rest = buf[consumed:]
    index = rest.find(HEADER)
    if index > 0:
        consumed += index
    elif index == -1:
        consumed += len(rest) - (1 if rest.endswith(HEADER[:1]) else 0)

    return frames, consumed


def decode_frames(buf: bytes) -> list[Frame]:
    """Decode complete frames from a self-contained buffer, ignoring leftovers."""
    return decode_stream(buf)[0]


def u16(data: bytes, offset: int) -> int | None:
    """Little-endian uint16, or None if the frame is too short to hold one.

    Matches the app's highLowCount(low, high) helper, which builds the value
    high-byte-first from arguments passed low-byte-first.
    """
    if offset + 2 > len(data):
        return None
    return int.from_bytes(data[offset : offset + 2], "little")


# --------------------------------------------------------------------------
# The 0x16 switch frame, and the layout trap that comes with it.
#
# The app uses TWO DIFFERENT FIELD LAYOUTS for this one opcode:
#
#   * what the device REPORTS has 12 fields, including lowBatteryWarning
#   * what you WRITE has 11 fields, with no slot for lowBatteryWarning at all
#
# So every field after index 0 sits one position later in a response than it
# does in a command. Echoing a received payload straight back as a write
# shifts every setting by one - which in testing silently turned off battery
# preserving mode. Never copy bytes between the two; always decode to names
# and re-encode from names.
# --------------------------------------------------------------------------

SWITCH_RECV_FIELDS = (
    "low_noise",
    "low_battery_warning",  # <- the extra field; absent from the write layout
    "usb",
    "dc",
    "ac_frequency_hz",
    "warning_voice",
    "ac_uturbo",
    "ac",
    "battery_health",
    "locking",
    "key_voice",
    "standby",
)

SWITCH_SEND_FIELDS = (
    "low_noise",
    "usb",
    "dc",
    "ac_frequency_hz",
    "warning_voice",
    "ac_uturbo",
    "ac",
    "battery_health",
    "locking",
    "key_voice",
    "standby",
)

# key_voice is stored inverted on the wire in both directions: 0x00 means on.
INVERTED_FIELDS = frozenset({"key_voice"})

# ac_frequency_hz is parsed as an integer by the app's receive path but written
# back as a 0/1 flag by its send path. Round-tripping the raw value is a true
# no-op and is identical to the app for the 0/1 values seen in practice, so the
# raw byte is preserved here rather than reduced to a boolean.
RAW_FIELDS = frozenset({"ac_frequency_hz"})


def decode_switch_state(data: bytes) -> dict[str, int | bool]:
    """Decode a 0x16 payload using the 12-field response layout."""
    state: dict[str, int | bool] = {}
    for index, name in enumerate(SWITCH_RECV_FIELDS):
        if index >= len(data):
            break
        value = data[index]
        if name in RAW_FIELDS:
            state[name] = value
        elif name in INVERTED_FIELDS:
            state[name] = value == 0
        else:
            state[name] = value != 0
    return state


def encode_switch_state(state: dict[str, int | bool]) -> bytes:
    """Build a 0x16 write frame from a decoded state, using the 11-field layout.

    ``state`` must carry every field in SWITCH_SEND_FIELDS - 0x16 replaces the
    whole switch block rather than patching it, so a caller that does not know
    the current state would silently change settings it never intended to
    touch. Refusing here is deliberate.
    """
    missing = [name for name in SWITCH_SEND_FIELDS if name not in state]
    if missing:
        raise ValueError(
            f"0x16 is a whole-state write and needs every field; missing: {missing}"
        )

    body = bytearray()
    for name in SWITCH_SEND_FIELDS:
        value = state[name]
        if name in RAW_FIELDS:
            body.append(int(value) & 0xFF)
        elif name in INVERTED_FIELDS:
            body.append(0x00 if value else 0x01)
        else:
            body.append(0x01 if value else 0x00)
    return encode(CMD_SET_SWITCHES, bytes(body))


def encode_light(level: int) -> bytes:
    """0x13: set the flashlight level. 0 is off; the app uses 1-3 for on."""
    return encode(CMD_SET_LIGHT, bytes([level & 0x0F]))


# --------------------------------------------------------------------------
# Telemetry: BLE opcodes -> the same flat dict the cloud WebSocket produces.
#
# Keeping the key names identical to the cloud path is what lets sensor.py and
# switch.py stay transport-agnostic, and lets a device keep its entity history
# when switched from cloud to Bluetooth.
#
# Field offsets come from the distribute() switch in the app bundle unless
# noted. Anything whose meaning or scale is not confirmed is deliberately left
# out rather than guessed at - see UNMAPPED below.
# --------------------------------------------------------------------------

# Cell voltages are the one mapping NOT taken from the app: the app's 0x09
# handler ignores these bytes entirely. Seven consecutive little-endian uint16s
# reading 3276-3280 identify them as per-cell millivolts of a 7-cell LiFePO4
# pack about as conclusively as an observation can. Treated as diagnostic.
CELL_VOLTAGE_COUNT = 7
CELL_VOLTAGE_OFFSET = 2

# 0x04 carries four temperatures, offset by 40. The app calls them
# batteriesOne/TwoPower and inverterOne/TwoPower, which is misleading: on an
# idle unit drawing no measurable power they read 64/68/71/70, so they are not
# watts. Confirmed against this integration's own cloud transport, which was
# reporting 24/28/31/30 C for the matching sensors at the same moment - an
# exact +40 on all four channels, held across two capture sessions and half an
# hour of recorder history. Negative results are left as-is; sub-zero storage
# temperatures are legitimate.
TEMPERATURE_OFFSET = 40

TEMPERATURE_KEYS = ("bat_temp1", "bat_temp2", "inverter_temp1", "inverter_temp2")

# Not mapped, on purpose:
#
#   0x01  Eight fault bytes, all zero on a healthy unit. Nothing to correlate
#         against while nothing is wrong, so device_fault2 stays unmapped.
#   AC and DC voltages. 0x0B carries eight bytes of which only acPower at
#         offset 6 is known, and 0x0C carries four bytes past the port powers.
#         Every capture so far was taken with the outputs off, so those bytes
#         were all zero and could not be told apart from padding. Mapping them
#         needs a capture with AC actually running - see the README.
#   0x06  Version block, 24 bytes, structure unknown.
#   0x17  Timer block. Left alone.
#   0x10/0x11/0x14/0x15  Screen brightness, screen timeout, DC charge current
#         and low-battery alarm. These are settings rather than telemetry and
#         the cloud integration deliberately does not expose the matching
#         *_set keys either.


def telemetry_from_frame(frame: Frame) -> dict[str, int | bool]:
    """Map one decoded status frame onto cloud-shaped telemetry keys.

    Returns an empty dict for opcodes that carry nothing we publish, so a
    caller can merge the result unconditionally.
    """
    if not frame.crc_ok:
        return {}

    data = frame.data
    out: dict[str, int | bool] = {}

    if frame.cmd == OP_TEMPS:
        for index, key in enumerate(TEMPERATURE_KEYS):
            raw = u16(data, index * 2)
            if raw is None:
                break
            out[key] = raw - TEMPERATURE_OFFSET

    elif frame.cmd == OP_BATTERY:
        if len(data) > 23:
            out["battery_percentage"] = data[23]
        # Confirmed against the cloud transport reading the same device: at
        # 09:03 BLE gave 100 / 55 / 35100 for these three while the recorder
        # held battery_health=100, battery_cycle_count=55 and
        # battery_capacity_remaining=35100. All 321 frames in that window
        # agreed, and a 16-bit value landing exactly on 35100 is not chance.
        if len(data) > 16:
            out["bat_health"] = data[16]
        if len(data) > 17:
            out["bat_cycle_num"] = data[17]
        capacity = u16(data, 24)
        if capacity is not None:
            out["bat_cap_remain"] = capacity
        charge = u16(data, 19)
        discharge = u16(data, 21)
        if charge is not None:
            out["charge_remain_time"] = charge
        if discharge is not None:
            out["discharge_remain_time"] = discharge
        for cell in range(CELL_VOLTAGE_COUNT):
            millivolts = u16(data, CELL_VOLTAGE_OFFSET + cell * 2)
            if millivolts is None:
                break
            out[f"cell_voltage_{cell + 1}"] = millivolts

    elif frame.cmd == OP_TOTALS:
        input_power = u16(data, 0)
        output_power = u16(data, 2)
        if input_power is not None:
            out["charge_power_all"] = input_power
        if output_power is not None:
            out["discharge_pow"] = output_power

    elif frame.cmd == OP_AC:
        ac_power = u16(data, 6)
        if ac_power is not None:
            out["ac_discharge_pow"] = ac_power

    elif frame.cmd == OP_PORTS:
        dc_power = u16(data, 8)
        if dc_power is not None:
            out["car_discharge_pow"] = dc_power
        # The cloud reports one aggregate USB figure; BLE breaks it out per
        # port (two USB-C, two USB-A), so they are summed back together.
        ports = [u16(data, offset) for offset in (0, 2, 4, 6)]
        if all(port is not None for port in ports):
            out["usb_discharge_pow"] = sum(ports)

    elif frame.cmd == OP_WORK_MODE:
        if data:
            out["work_mode"] = data[0]

    elif frame.cmd == OP_LIGHT:
        if data:
            # Cloud lamp_sw is a plain on/off flag; BLE carries a level, and
            # any non-zero level means the light is on.
            out["lamp_sw"] = 1 if data[0] else 0

    elif frame.cmd == OP_SWITCHES:
        state = decode_switch_state(data)
        if "ac" in state:
            out["switch_ac"] = 1 if state["ac"] else 0
        if "dc" in state:
            out["switch_dc"] = 1 if state["dc"] else 0
        if "usb" in state:
            out["usb_sw"] = 1 if state["usb"] else 0

    return out


def parse_serial(data: bytes) -> tuple[str | None, str | None]:
    """Split a 0x05 payload into (iot_serial, device_serial).

    A 30-byte payload carries both as 15 ASCII characters each; a 15-byte one
    carries only the device serial.

    These are two different strings and they are easy to mix up. Use
    parse_identity() for anything that has to line up with the cloud.
    """
    text = data.decode("ascii", errors="ignore")
    if len(text) >= 30:
        return text[:15], text[15:30]
    if len(text) >= 15:
        return None, text[:15]
    return None, None


def parse_identity(data: bytes) -> str | None:
    """Return the serial the cloud transport identifies this device by.

    The 0x05 response carries the IoT serial first and the device serial
    second, and the cloud API uses the *IoT* serial - it is what comes back as
    deviceModelName and what the cloud entry's entity unique_ids are built
    from. Picking the wrong one is not a cosmetic mistake: the two entries then
    cannot recognise each other, so the duplicate guard never fires and a
    device set up on both transports gets two of every entity with no shared
    history. Shipped that way once; hence this function existing at all.

    Returns None for the short 15-byte form, which carries only the device
    serial. Callers should fall back to the MAC address rather than guess.
    """
    iot_serial, _ = parse_serial(data)
    return iot_serial
