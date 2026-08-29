# UGREEN PowerRoam for Home Assistant

[![tests](https://github.com/tanka8/ugreen-powerroam/actions/workflows/tests.yml/badge.svg)](https://github.com/tanka8/ugreen-powerroam/actions/workflows/tests.yml)
[![hacs](https://img.shields.io/badge/HACS-custom-41BDF5.svg)](https://hacs.xyz)

Home Assistant integration for UGREEN power stations that use the **UGREEN app**
(`com.powerroam.pps`). Reverse engineered against a **PowerRoam 1200W**.

> **Tested on one model, one account.** Everything here was worked out from a
> PowerRoam 1200W. Other PowerRoam models almost certainly share the same cloud API
> and field names, but that is an assumption, not a confirmation - see
> [Known gaps](#known-gaps).

## Why it is cloud-based

The device has **no local control path at all**. A full TCP port scan of all 65535
ports found nothing open - it only holds an outbound WiFi connection to UGREEN's own
cloud (`hw-powerapi.ugpps.com`), not a Tuya-style local API. `tuya-local`, LocalTuya
and friends are irrelevant here: this is UGREEN's own stack.

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

State is **pushed** roughly every 15 seconds over a plain WebSocket. Nothing polls.

## Install

**HACS** - three dots menu, Custom repositories, add
`https://github.com/tanka8/ugreen-powerroam` as an *Integration*, then install and
restart.

**Manually** - copy `custom_components/ugreen_powerroam/` into your
`config/custom_components/` and restart.

Then **Settings, Devices & services, Add integration, UGREEN PowerRoam**, and sign in
with your UGREEN app account.

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

## Known gaps

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

`tests/` runs the pure-Python parts - the telemetry frame parser and the RSA
encryption used at login - against a synthetic sample frame
(`docs/sample_telemetry_frame.json`, shaped like a real capture but with fictional
device identifiers), with no network, no Home Assistant, and no credentials.

`tests_ha/` runs the config flow through Home Assistant itself using
`pytest-homeassistant-custom-component`. **That harness does not work on Windows** -
its autouse fixtures need a socketpair that `pytest-socket` blocks - so those run on
Linux in CI. See `tests_ha/README.md`.

CI also runs `ruff check`, `ruff format --check`, Home Assistant's `hassfest`, and
the HACS validation action.

## How this was built, and what to expect from it

Most of the work here - reverse engineering the protocol from a proxied capture of
the UGREEN app, writing the integration and its tests, and this README - was done by
**Claude**, Anthropic's AI assistant. I directed it, made the calls it asked me to
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
