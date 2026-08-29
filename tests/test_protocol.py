"""Tests for the BLE codec - no hardware, no bleak, no Home Assistant.

Every byte string here was captured from a real PowerRoam GS1200 during
protocol validation, so these are regression tests against the actual device
rather than against the reverse-engineered app bundle alone.
"""

from __future__ import annotations

import pytest

from .loader import load_module

protocol = load_module("protocol")

# One notification from a real device: switch state, light level, timer block
# concatenated, then zero padded - exactly how the unit sends it.
REAL_NOTIFICATION = bytes.fromhex(
    "5AA5C0A1160C00010001000000000001000100AFCC"
    "5AA5C0A113010000F986"
    "5AA5C0A1170600000000000000C67A" + "00" * 40
)

# The device's own reported switch state, 12 bytes.
REAL_SWITCH_STATE = bytes.fromhex("010001000000000001000100")


def test_crc_matches_device_frames() -> None:
    """Read-only commands must match the bytes the app puts on the wire."""
    assert protocol.encode(protocol.CMD_GET_MAC).hex().upper() == "5AA5A1C0210000F5D3"
    assert (
        protocol.encode(protocol.CMD_GET_SERIAL).hex().upper() == "5AA5A1C0050000B5D8"
    )
    assert (
        protocol.encode(protocol.CMD_GET_VERSION).hex().upper() == "5AA5A1C006000045D8"
    )


def test_decodes_concatenated_and_padded_notification() -> None:
    frames = protocol.decode_frames(REAL_NOTIFICATION)
    assert [f.cmd for f in frames] == [0x16, 0x13, 0x17]
    assert all(f.crc_ok for f in frames)
    # Responses carry the prefix byte-swapped; it is a direction field.
    assert all(f.prefix == protocol.REPLY_PREFIX for f in frames)


def test_decode_stream_reports_consumed_bytes() -> None:
    """A frame split across two notifications must survive the boundary."""
    whole = protocol.encode(0x13, b"\x01")
    head, tail = whole[:5], whole[5:]

    frames, consumed = protocol.decode_stream(head)
    assert frames == [] and consumed == 0

    frames, consumed = protocol.decode_stream(head + tail)
    assert len(frames) == 1
    assert consumed == len(whole)


def test_bad_crc_is_flagged_not_raised() -> None:
    corrupt = bytearray(protocol.encode(0x13, b"\x01"))
    corrupt[-1] ^= 0xFF
    (frame,) = protocol.decode_frames(bytes(corrupt))
    assert frame.crc_ok is False


class TestSwitchLayoutTrap:
    """The send and receive layouts for 0x16 differ, and mixing them is a bug.

    The response carries 12 fields including low_battery_warning at index 1;
    a write carries 11 and has no slot for it. Echoing a received payload back
    as a write shifts every later field by one - on real hardware that silently
    switched battery preserving mode off. These tests pin the behaviour.
    """

    def test_layouts_differ_by_low_battery_warning(self) -> None:
        assert len(protocol.SWITCH_RECV_FIELDS) == 12
        assert len(protocol.SWITCH_SEND_FIELDS) == 11
        assert "low_battery_warning" in protocol.SWITCH_RECV_FIELDS
        assert "low_battery_warning" not in protocol.SWITCH_SEND_FIELDS

    def test_decodes_real_state(self) -> None:
        state = protocol.decode_switch_state(REAL_SWITCH_STATE)
        assert state["low_noise"] is True
        assert state["usb"] is True
        assert state["battery_health"] is True
        assert state["dc"] is False
        assert state["ac"] is False
        # key_voice is stored inverted: 0x00 on the wire means on.
        assert state["key_voice"] is False

    def test_roundtrip_reproduces_the_frame_that_fixed_the_device(self) -> None:
        """Decode-then-encode must yield the exact restore frame that worked."""
        state = protocol.decode_switch_state(REAL_SWITCH_STATE)
        frame = protocol.encode_switch_state(state)
        assert frame.hex().upper() == "5AA5A1C0160B0001010000000000010001009F40"

    def test_roundtrip_is_a_true_no_op(self) -> None:
        """Re-decoding an encoded write must give back the same settings."""
        original = protocol.decode_switch_state(REAL_SWITCH_STATE)
        written = protocol.decode_frames(protocol.encode_switch_state(original))[0].data
        assert len(written) == 11

        # Re-insert the low_battery_warning slot the write layout omits, which
        # puts the payload back into the 12-field response alignment.
        reread = protocol.decode_switch_state(bytes([written[0], 0x00, *written[1:]]))
        for name in protocol.SWITCH_SEND_FIELDS:
            assert reread[name] == original[name], name

    def test_naive_byte_echo_would_corrupt_state(self) -> None:
        """Guards the actual bug: echoing received bytes shifts every field.

        This is what turned battery preserving mode off on real hardware.
        """
        echoed = REAL_SWITCH_STATE[:11]
        shifted = dict(zip(protocol.SWITCH_SEND_FIELDS, echoed, strict=True))
        correct = protocol.decode_switch_state(REAL_SWITCH_STATE)
        # battery_health lands on the byte that belonged to ac - and flips.
        assert bool(shifted["battery_health"]) is False
        assert correct["battery_health"] is True

    def test_whole_state_write_refuses_partial_input(self) -> None:
        with pytest.raises(ValueError, match="whole-state write"):
            protocol.encode_switch_state({"ac": True})


class TestTelemetry:
    def test_battery_frame(self) -> None:
        data = bytes.fromhex("0601cf0ccc0ccc0ccf0ccf0cce0ccf0c6437000000fd59581c890000")
        (frame,) = protocol.decode_frames(protocol.encode(protocol.OP_BATTERY, data))
        out = protocol.telemetry_from_frame(frame)
        assert out["battery_percentage"] == 88
        assert out["charge_remain_time"] == 0
        assert out["cell_voltage_1"] == 3279
        assert out["cell_voltage_7"] == 3279
        # Seven cells, and no eighth invented from the bytes that follow.
        assert "cell_voltage_8" not in out

    def test_battery_frame_carries_health_cycles_and_capacity(self) -> None:
        """Verified against the cloud transport reading the same device.

        At 09:03 this payload was on the wire while the recorder held
        battery_health=100, battery_cycle_count=55 and
        battery_capacity_remaining=35100. All 321 frames captured in that
        window carried the same three values.
        """
        data = bytes.fromhex("0601cf0ccc0ccc0ccf0ccf0cce0ccf0c6437000000fd59581c890000")
        (frame,) = protocol.decode_frames(protocol.encode(protocol.OP_BATTERY, data))
        out = protocol.telemetry_from_frame(frame)
        assert out["bat_health"] == 100
        assert out["bat_cycle_num"] == 55
        assert out["bat_cap_remain"] == 35100

    def test_switch_frame_maps_to_cloud_keys(self) -> None:
        (frame,) = protocol.decode_frames(
            protocol.encode(protocol.OP_SWITCHES, REAL_SWITCH_STATE)
        )
        out = protocol.telemetry_from_frame(frame)
        assert out == {"switch_ac": 0, "switch_dc": 0, "usb_sw": 1}

    def test_port_powers_are_summed_for_usb(self) -> None:
        data = bytes.fromhex("0100020003000400" + "0500" + "0000000000")
        (frame,) = protocol.decode_frames(protocol.encode(protocol.OP_PORTS, data))
        out = protocol.telemetry_from_frame(frame)
        assert out["usb_discharge_pow"] == 1 + 2 + 3 + 4
        assert out["car_discharge_pow"] == 5

    def test_light_level_becomes_boolean_flag(self) -> None:
        for level, expected in ((0, 0), (1, 1), (3, 1)):
            (frame,) = protocol.decode_frames(
                protocol.encode(protocol.OP_LIGHT, bytes([level]))
            )
            assert protocol.telemetry_from_frame(frame)["lamp_sw"] == expected

    def test_bad_crc_yields_no_telemetry(self) -> None:
        corrupt = bytearray(protocol.encode(protocol.OP_BATTERY, bytes(28)))
        corrupt[-1] ^= 0xFF
        (frame,) = protocol.decode_frames(bytes(corrupt))
        assert protocol.telemetry_from_frame(frame) == {}

    def test_short_frame_does_not_raise(self) -> None:
        (frame,) = protocol.decode_frames(protocol.encode(protocol.OP_BATTERY, b"\x01"))
        assert protocol.telemetry_from_frame(frame) == {}

    def test_temperatures_are_offset_by_forty(self) -> None:
        """Verified against the cloud transport reading the same device.

        BLE reported 64/68/71/70 while the cloud reported 24/28/31/30 C for
        the matching sensors, an exact +40 across all four channels.
        """
        (frame,) = protocol.decode_frames(
            protocol.encode(protocol.OP_TEMPS, bytes.fromhex("4000440047004600"))
        )
        assert protocol.telemetry_from_frame(frame) == {
            "bat_temp1": 24,
            "bat_temp2": 28,
            "inverter_temp1": 31,
            "inverter_temp2": 30,
        }

    def test_sub_zero_temperatures_survive(self) -> None:
        """Cold storage is legitimate; the offset must not clamp at zero."""
        (frame,) = protocol.decode_frames(
            protocol.encode(protocol.OP_TEMPS, bytes.fromhex("2500250025002500"))
        )
        assert protocol.telemetry_from_frame(frame)["bat_temp1"] == -3


def test_identity_is_the_serial_the_cloud_uses() -> None:
    """Regression: the 0x05 response carries two serials and they are not
    interchangeable.

    The cloud identifies this device by the IoT serial (the first one). v1.1.0
    used the device serial, so a Bluetooth entry could never recognise the
    cloud entry for the same unit - the duplicate guard never fired and the
    device came up twice with two of every entity and no shared history.
    Payload below is a real capture from the device that showed it.
    """
    payload = bytes.fromhex(
        "473132464532333035313130333337473132424330313232313336363636"
    )
    assert protocol.parse_identity(payload) == "G12FE2305110337"
    # Explicitly NOT the device serial.
    assert protocol.parse_identity(payload) != "G12BC0122136666"


def test_identity_is_none_for_the_short_form() -> None:
    """The 15-byte reply carries only the device serial, which is the wrong
    string to identify by - callers must fall back to the MAC instead."""
    assert protocol.parse_identity(b"G12BC0122136666") is None


def test_parse_serial_both_forms() -> None:
    long_form = bytes.fromhex(
        "473132464532333035313130333337473132424330313232313336363636"
    )
    assert protocol.parse_serial(long_form) == ("G12FE2305110337", "G12BC0122136666")
    short = b"G12BC0122136666"
    assert protocol.parse_serial(short) == (None, "G12BC0122136666")


def test_light_encoding_matches_hardware() -> None:
    assert protocol.encode_light(1).hex().upper() == "5AA5A1C0130100018DFF"
    assert protocol.encode_light(0).hex().upper() == "5AA5A1C0130100004C3F"


class TestStreamResync:
    """The device pads each notification with zeros after the last frame.

    Those bytes can never start a frame, so they must be consumed rather than
    left to accumulate in the caller's buffer indefinitely.
    """

    def test_trailing_padding_is_consumed(self) -> None:
        frames, consumed = protocol.decode_stream(REAL_NOTIFICATION)
        assert len(frames) == 3
        assert consumed == len(REAL_NOTIFICATION)

    def test_leading_junk_is_skipped(self) -> None:
        whole = protocol.encode(0x13, b"\x01")
        frames, consumed = protocol.decode_stream(b"\x00\xff\x11" + whole)
        assert len(frames) == 1
        assert consumed == 3 + len(whole)

    def test_split_header_is_kept_for_the_next_packet(self) -> None:
        """A lone trailing 0x5A may be half a header, so it must survive."""
        frames, consumed = protocol.decode_stream(b"\x00\x00\x5a")
        assert frames == []
        assert consumed == 2

    def test_buffer_does_not_grow_across_repeated_packets(self) -> None:
        buffer = bytearray()
        for _ in range(50):
            buffer.extend(REAL_NOTIFICATION)
            _, consumed = protocol.decode_stream(bytes(buffer))
            del buffer[:consumed]
        assert len(buffer) == 0
