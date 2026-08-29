#!/usr/bin/env python3
"""Check this integration's Bluetooth protocol against a PowerRoam.

Written so somebody with a model other than the 1200W can find out what their
unit actually does, without reading any of the reverse engineering first. It
needs no Home Assistant and no UGREEN account - just a Bluetooth adapter in
range and the `bleak` package.

    pip install bleak
    python scripts/hardware_check.py

By default it only ever READS. It scans, connects, dumps the GATT tree, listens
to whatever the unit pushes, asks for the serial and version, and prints a
report you can paste into an issue.

    --seconds N     how long to listen for (default 30)
    --address MAC   skip the scan and connect straight to this address
    --flashlight    ALSO test the write path, by blinking the unit's light

Nothing here can switch an AC, DC or USB output on or off, and nothing here
touches the firmware update path. That is deliberate: the flashlight is the one
control with no load attached to it, so it is the only safe thing to prove a
write with on someone else's hardware.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parent.parent
        / "custom_components"
        / "ugreen_powerroam"
    ),
)

import protocol

try:
    from bleak import BleakClient, BleakScanner
    from bleak.exc import BleakError
except ImportError:
    sys.exit("bleak is not installed. Run: pip install bleak")

SERVICE = "0000abf0-0000-1000-8000-00805f9b34fb"
WRITE = "0000abf1-0000-1000-8000-00805f9b34fb"
NOTIFY = "0000abf2-0000-1000-8000-00805f9b34fb"

KNOWN_OPCODES = {
    0x01: "fault flags (not decoded)",
    0x04: "temperatures, +40 offset",
    0x05: "serial numbers",
    0x06: "version block (not decoded)",
    0x09: "battery: cells, times, state of charge",
    0x0B: "AC power",
    0x0C: "per-port power",
    0x0F: "input/output power totals",
    0x10: "screen brightness",
    0x11: "screen timeout",
    0x12: "work mode",
    0x13: "flashlight level",
    0x14: "DC charge current",
    0x15: "low battery alarm",
    0x16: "switch state",
    0x17: "timer block (not decoded)",
    0x21: "MAC address",
}


def rule(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


async def find_devices(address: str | None):
    if address:
        device = await BleakScanner.find_device_by_address(address, timeout=30)
        return [device] if device else []
    print("Scanning for 30s...")
    found = []
    devices = await BleakScanner.discover(timeout=30, return_adv=True)
    for device, adv in devices.values():
        name = adv.local_name or device.name or ""
        if name.lower().startswith("ugreen") or SERVICE in (adv.service_uuids or []):
            found.append((device, adv))
            print(f"  found {device.address}  name={name!r}  rssi={adv.rssi}")
            print(f"        advertised service uuids: {adv.service_uuids}")
    return found


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--address")
    parser.add_argument("--flashlight", action="store_true")
    args = parser.parse_args()

    rule("1. Discovery")
    results = await find_devices(args.address)
    if not results:
        print("\nNo PowerRoam found.")
        print("Check the unit is on, in range, and that the UGREEN phone app is")
        print("closed - it holds the one Bluetooth connection the unit allows.")
        return 1

    entry = results[0]
    device = entry[0] if isinstance(entry, tuple) else entry
    print(f"\nUsing {device.address}")

    buffer = bytearray()
    seen: dict[int, int] = {}
    payloads: dict[int, str] = {}
    telemetry: dict = {}
    bad_crc = [0]

    def on_notify(_char, data: bytearray) -> None:
        buffer.extend(data)
        frames, consumed = protocol.decode_stream(bytes(buffer))
        del buffer[:consumed]
        for frame in frames:
            if not frame.crc_ok:
                bad_crc[0] += 1
                continue
            seen[frame.cmd] = seen.get(frame.cmd, 0) + 1
            payloads[frame.cmd] = frame.data.hex()
            telemetry.update(protocol.telemetry_from_frame(frame))

    async with BleakClient(device, timeout=30) as client:
        rule("2. GATT tree")
        has_service = False
        for service in client.services:
            print(f"[service] {service.uuid}")
            if service.uuid.lower() == SERVICE:
                has_service = True
            for char in service.characteristics:
                print(f"   [char] {char.uuid}  props={','.join(char.properties)}")
        if not has_service:
            print(f"\nWARNING: service {SERVICE} not present. This model may")
            print("use a different protocol entirely. The report below may be empty.")

        rule(f"3. Listening for {args.seconds}s")
        await client.start_notify(NOTIFY, on_notify)
        await asyncio.sleep(min(args.seconds, 10))

        for name, cmd in (
            ("serial", protocol.CMD_GET_SERIAL),
            ("version", protocol.CMD_GET_VERSION),
            ("MAC", protocol.CMD_GET_MAC),
        ):
            print(f"  asking for {name}...")
            await client.write_gatt_char(WRITE, protocol.encode(cmd), response=True)
            await asyncio.sleep(2)

        remaining = args.seconds - min(args.seconds, 10) - 6
        if remaining > 0:
            await asyncio.sleep(remaining)

        if args.flashlight:
            rule("4. Flashlight write test")
            print("  light ON  - watch the unit")
            await client.write_gatt_char(WRITE, protocol.encode_light(1), response=True)
            await asyncio.sleep(8)
            print("  light OFF")
            await client.write_gatt_char(WRITE, protocol.encode_light(0), response=True)
            await asyncio.sleep(5)

        await client.stop_notify(NOTIFY)

    rule("REPORT - paste this into an issue")
    print(f"address           : {device.address}")
    print(f"frames with bad CRC: {bad_crc[0]}  (should be 0)")
    if 0x05 in payloads:
        iot, serial = protocol.parse_serial(bytes.fromhex(payloads[0x05]))
        print(f"serial            : {serial}   (iot: {iot})")

    print("\nopcodes seen:")
    for cmd in sorted(seen):
        label = KNOWN_OPCODES.get(
            cmd, "*** UNKNOWN - not handled by this integration ***"
        )
        print(
            f"  0x{cmd:02x}  x{seen[cmd]:<5} len={len(payloads[cmd]) // 2:<3} {label}"
        )
        print(f"        payload: {payloads[cmd]}")

    unknown = sorted(set(seen) - set(KNOWN_OPCODES))
    missing = sorted(set(KNOWN_OPCODES) - set(seen))
    if unknown:
        print(f"\nUNKNOWN opcodes: {[f'0x{c:02x}' for c in unknown]}")
        print("These are the interesting ones - your model reports something this")
        print("integration does not know about.")
    if missing:
        print(f"\nExpected but not seen: {[f'0x{c:02x}' for c in missing]}")

    print(f"\ndecoded telemetry ({len(telemetry)} keys):")
    for key in sorted(telemetry):
        print(f"  {key:24s} = {telemetry[key]}")

    if 0x16 in payloads:
        state = protocol.decode_switch_state(bytes.fromhex(payloads[0x16]))
        print(f"\nswitch block is {len(payloads[0x16]) // 2} bytes")
        if len(payloads[0x16]) // 2 != len(protocol.SWITCH_RECV_FIELDS):
            print("  *** DIFFERENT LENGTH to the 1200W. Do not write switches on")
            print("      this model until the layout is worked out - see the 0x16")
            print("      section of the README for why that matters. ***")
        for name, value in state.items():
            print(f"  {name:20s} = {value}")

    print("\nSanity check: does the battery percentage above match the unit's screen?")
    return 0


def _run() -> int:
    """Turn the usual Bluetooth setup failures into something readable.

    Somebody running this has hardware to test and probably no interest in a
    bleak stack trace, so say what to check instead.
    """
    try:
        return asyncio.run(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 1
    except BleakError as err:
        print(f"\nBluetooth error: {err}\n")
        message = str(err).lower()
        if "bluez" in message or "dbus" in message:
            print("No usable Bluetooth stack. On Linux check that bluetoothd is")
            print("running:  systemctl status bluetooth")
        elif "not found" in message:
            print("The device was not reachable. Check it is switched on, in range,")
            print("and that the UGREEN phone app is closed - the unit allows only")
            print("one Bluetooth connection at a time.")
        else:
            print("Check the adapter is present and the unit is in range.")
        return 1


if __name__ == "__main__":
    sys.exit(_run())
