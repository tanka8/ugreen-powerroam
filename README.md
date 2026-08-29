# UGREEN PowerRoam for Home Assistant

[![tests](https://github.com/tanka8/ugreen-powerroam/actions/workflows/tests.yml/badge.svg)](https://github.com/tanka8/ugreen-powerroam/actions/workflows/tests.yml)
[![hacs](https://img.shields.io/badge/HACS-custom-41BDF5.svg)](https://hacs.xyz)

Home Assistant integration for UGREEN power stations that use the **UGREEN app**
(`com.powerroam.pps`). Reverse engineered against a **PowerRoam 1200W**.

Two transports, your choice at setup:

* **Bluetooth (recommended)** - talks to the unit directly. No account, no internet,
  no WiFi. State arrives in under a second.
* **Cloud** - UGREEN's own API. Needs an account and a working internet connection,
  and updates roughly every 15 seconds.

> **Tested on one model, one account.** Everything here was worked out from a
> PowerRoam 1200W. Other PowerRoam models almost certainly share the same cloud API
> and field names, but that is an assumption, not a confirmation - see
> [Known gaps](#known-gaps).

## Why Bluetooth, and why the cloud came first

Over **IP** the device has no local control path at all. A full TCP port scan of all
65535 ports found nothing open - it only holds an outbound WiFi connection to UGREEN's
own cloud (`hw-powerapi.ugpps.com`), not a Tuya-style local API. `tuya-local`,
LocalTuya and friends are irrelevant here: this is UGREEN's own stack. That is why the
first version of this integration was cloud-only.

Over **Bluetooth** it is a different story. The app carries a complete parallel BLE
transport that it uses whenever there is no WiFi, and the power station runs an open
GATT server to serve it - no pairing, no bonding, no encryption. That transport has
since been confirmed against real hardware: 4,502 frames decoded with zero CRC
failures, and the device pushes its entire status set unprompted about every 0.6
seconds, roughly 24x faster than the cloud.

So Bluetooth is the better path in every respect except range. The cloud transport is
still there if you want to reach the unit from outside Bluetooth range, or if you have
no adapter near it.

Getting a proxy in front of the app's traffic also wasn't the usual "trust a CA on an
emulator" story: the app ships **ARM64-only** native libraries, and the emulator
setup that works for other Android reverse-engineering (x86_64 with a writable
system partition) can't run it, while an ARM64 system image can't run on an x86_64
host at all - Google dropped software ARM emulation from the current emulator. The
capture ended up done on a real phone instead, with the APK decompiled, a
`network_security_config.xml` added to trust a user CA, and re-signed with a local
debug key.

## Entities

| Entity | Type | Notes |
|---|---|---|
| AC Output | `switch` | |
| DC Output | `switch` | |
| USB Output | `switch` | |
| Flashlight | `switch` | |
| Battery | `sensor` | % |
| Battery Health | `sensor` | % |
| Battery Cycle Count | `sensor` | |
| Battery Capacity Remaining | `sensor` | raw units - see [Known gaps](#known-gaps) |
| Discharge Time Remaining | `sensor` | hours |
| Charge Time Remaining | `sensor` | hours |
| Total / AC / DC / USB Output Power | `sensor` | W |
| Input Power | `sensor` | W |
| AC Input / Output Voltage, DC Voltage | `sensor` | V |
| Battery Temperature ×2, Inverter Temperature ×2 | `sensor` | °C |
| Fault Code | `sensor` | raw code, empty when healthy |
| Work Mode | `sensor` | raw numeric mode - meaning unconfirmed |

Entities are only created for the fields this integration knows about - see
[Known gaps](#known-gaps) for what the device reports but isn't exposed yet.

Only the entities a transport can actually fill are created, so neither setup is left
with a column of permanently unavailable entities. Bluetooth adds per-cell voltages;
the cloud adds the temperature, voltage, battery-health and fault sensors that the BLE
protocol either does not carry or does not carry in a confirmed form.

| | Bluetooth | Cloud |
|---|---|---|
| All four switches | yes | yes |
| Battery %, charge/discharge time remaining | yes | yes |
| Total / AC / DC / USB power, input power | yes | yes |
| Work mode | yes | yes |
| Battery and inverter temperatures | yes | yes |
| Cell 1-7 voltage | **yes** | no |
| Battery health, cycle count, capacity remaining | no | yes |
| AC/DC voltages, fault code | no | yes |

State is **pushed** on both transports - about every 0.6 seconds over Bluetooth (then
coalesced, so Home Assistant is not woken 1.6 times a second), and roughly every 15
seconds over the cloud WebSocket. Nothing polls.

## Install

**HACS** - three dots menu, Custom repositories, add
`https://github.com/tanka8/ugreen-powerroam` as an *Integration*, then install and
restart.

**Manually** - copy `custom_components/ugreen_powerroam/` into your
`config/custom_components/` and restart.

Then **Settings, Devices & services, Add integration, UGREEN PowerRoam**, and pick a
transport.

If Home Assistant can already see the power station over Bluetooth it will usually
offer it to you unprompted, without your having to add anything by hand.

**For Bluetooth** you need a Bluetooth adapter within range of the unit and the
`bluetooth` integration set up. **Close the UGREEN phone app first** - the power
station accepts only one Bluetooth connection at a time, so the app and Home Assistant
cannot both hold it.

Setup reads the unit's serial number over BLE and uses it as the entry's identity,
which is the same identity the cloud entry uses. That means **you can migrate from
cloud to Bluetooth and keep your entity history**: remove the cloud entry, add the
Bluetooth one, and the entities reattach. It also means the two cannot be configured
side by side for one device, which is deliberate - two entries for one power station
would give you two of every entity.

**For the cloud** sign in with your UGREEN app account.

## How it works

Two calls set up the session, one drives control, one carries live state:

* **Auth** - `GET /app/v1/sa/encrypt/key` hands back an RSA public key and a `uuid`.
  Email and password are each RSA/PKCS1v1.5-encrypted with that key (this is what the
  app itself does, not something added here) and posted to `POST /app/v1/login`,
  which returns a session `token` sent as a plain header on every later request - no
  OAuth involved.
* **Device list** - `GET /app/v1/device/list` returns the account's device(s); the
  first one's `deviceModelName` doubles as its id everywhere else.
* **Control** - one generic endpoint for everything: `POST
  /app/v1/device/setDeviceInfo` with `{"deviceName": ..., "map": {"switch_ac": 1}}`.
  Every switch in this integration is the same call with a different key.
* **Telemetry** - a WebSocket at `wss://hw-powerapi.ugpps.com:8089/app/device/websocket/{userId}/{deviceName}`,
  authenticated with the same `token` header used for REST. Sending
  `{"userId": ..., "content": "ugreenSocketConnection"}` after connecting both
  subscribes and, resent periodically, acts as the keepalive. The server then pushes
  one flat JSON object per update with every field the device reports - no envelope,
  no per-field diffing.

### Bluetooth

The wire format lives in `protocol.py`, which is pure Python and fully unit tested
without hardware. Frames look like this:

```
5A A5 | A1 C0 | cmd | len (uint16 LE) | data | crc (uint16 LE)
```

CRC is Modbus CRC-16. The `A1 C0` field is direction: everything the device sends
back carries it byte-swapped as `C0 A1`. GATT service `ABF0` exposes `ABF1` to write
to and `ABF2` to subscribe to - and note the service is **not advertised**, so
discovery matches on the local name (`ugreen gs1200`) instead.

**The one trap worth knowing about**, because it bites hard: the app uses **two
different field layouts for the same `0x16` switch opcode**. What the device reports
has 12 fields; what you write has 11, with no slot for `lowBatteryWarning`. Echo a
received payload straight back as a write and every field after the first lands one
position early. Doing exactly that during development silently switched battery
preserving mode off on a real unit. `protocol.py` keeps the two layouts strictly
apart and only ever converts through named fields, and there are regression tests
pinning it.

`0x16` is also a whole-state replace rather than a patch, so a write has to know the
current state of all eleven fields. `encode_switch_state()` refuses to build a frame
from a partial state, and the BLE transport refuses to send one before the device has
reported in.

## Known gaps

* **The Bluetooth fault code is not mapped.** Opcode `0x01` carries eight fault bytes,
  all zero on a healthy unit, with no decoded layout - so there is nothing trustworthy
  to put behind the cloud's `device_fault2` yet.
* **Whether the BLE serial matches the cloud serial is unverified.** The migration
  path above assumes it does. If it turns out not to, Bluetooth and cloud entries
  will simply coexist as separate devices rather than merging.
* **Cell voltages assume a 7-cell pack.** Seven consecutive uint16s reading 3276-3280
  identify them about as conclusively as an observation can, but the offsets came from
  reading the bytes, not from the app - its own handler ignores them. A different pack
  size would need the count adjusting.
* **U-Turbo is not implemented.** It showed up in the app but never got exercised
  during capture, so which `map` key it sets is unconfirmed - guessing wrong here
  risks writing to the wrong field. If you can capture it (see
  [Contributing](#contributing)), it's a small addition.
* **The set-and-forget settings aren't exposed**: battery preservation
  (`bat_health_set`), quiet mode / tones (`low_sound_set`, `bee_sound_set_key`,
  `bee_sound_set_warning`), display brightness (`display_bright_set`), low-battery
  alarm (`low_power_al_set`), and the various shutdown timers (`time_shutdown`,
  `time_dis_shutdown`, `timeoff_set`, `timeoff_cap`, `timeoff_zoom`) are all visible
  in the telemetry but not wired up as controllable entities - each would need its
  own capture to confirm the write semantics for something most people set once.
* **`bat_cap_remain` and `work_mode` are raw, uncalibrated numbers.** Both are
  exposed for graphing/automations, but their scale and the meaning of each
  `work_mode` value weren't confirmed against a known reference during capture.
* **There is still no reauth flow.** The telemetry loop now re-logs in by itself
  after a few consecutive failures, so an expiring token no longer stops updates.
  But if the stored credentials are genuinely wrong - a changed password, say -
  nothing prompts you to fix them; the loop just keeps failing in the log.
* **Single device per account assumed.** `device/list` is read once at setup and
  only the first device is used; multi-device accounts aren't handled.
* Error messages are hardcoded English rather than translation keys.

## Contributing

If you want to add one of the settings above, or confirm a model other than the
1200W: capture the app's own traffic the same way this was built - proxy it (e.g.
mitmproxy) through a system-trusted CA and toggle the control in question, then open
a pull request with what the `map` key and value turned out to be. No live device
access is needed to review the change - it's a one-line addition to `const.py`
either way.

## Tests

```bash
pip install pytest aiohttp cryptography
python -m pytest -v
```

`tests/` runs the pure-Python parts - the BLE codec, the telemetry frame parser and
the RSA encryption used at login - with no network, no hardware and no Home
Assistant. The BLE tests run against byte strings captured from a real PowerRoam, so
they are regression tests against the device rather than against the reverse
engineered app bundle alone. The cloud parser is tested against a synthetic sample
frame
(`docs/sample_telemetry_frame.json`, shaped like a real capture but with fictional
device identifiers), with no network, no Home Assistant, and no credentials.

`tests_ha/` runs the config flow through Home Assistant itself using
`pytest-homeassistant-custom-component`. **That harness does not work on Windows** -
its autouse fixtures need a socketpair that `pytest-socket` blocks - so those run on
Linux in CI. See `tests_ha/README.md`.

CI also runs `ruff check`, `ruff format --check`, Home Assistant's `hassfest`, and
the HACS validation action.

## How this was built, and what to expect from it

Most of the work here - reverse engineering the cloud protocol from a proxied capture
of the UGREEN app, decoding the Bluetooth protocol from the app bundle and confirming
it frame by frame against real hardware, writing the integration and its tests, and
this README - was done by **Claude**, Anthropic's AI assistant. I directed it, made the calls it asked me to
make, tested against my own device, and I run the result at home. I am not presenting
it as my own unaided work.

**I make no ownership claim over any of it.** MIT licensed, take it, fork it, do what
you like. Nothing here is UGREEN's, endorsed by UGREEN, or affiliated with them.

**There is no promise of support.** This scratched an itch in my house. I may fix
things, I may not, and I may lose interest entirely. It talks to an undocumented API
that UGREEN can break whenever they like, and if that happens I make no commitment to
chase it. Issues and pull requests are welcome, but please treat a response as a
favour rather than an expectation.

If that is not a footing you are comfortable relying on, fork it - genuinely, that is
the sensible move for anything you actually depend on.

## Licence

MIT.
