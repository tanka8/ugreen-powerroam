# Security

## What this integration handles

Your UGREEN account email and password are stored in Home Assistant's config entry,
in the same way every cloud integration stores credentials, and are sent only to
`hw-powerapi.ugpps.com` (over HTTPS/WSS). Nothing is sent anywhere else, and nothing
is collected by this project or its author.

Before sending, the password and email are RSA-encrypted with a public key the
server itself hands out per login attempt (`GET /app/v1/sa/encrypt/key`) - this is
what the official app does too, it is not something this integration adds. It is a
layer on top of TLS, not a replacement for it.

## The unit's Bluetooth control is unauthenticated

The PowerRoam's Bluetooth GATT server has no pairing, no bonding and no encryption.
Anyone within Bluetooth range can connect to it and read its state or send it
commands, including switching the AC, DC and USB outputs on and off - with this
integration, with UGREEN's app, or with any generic Bluetooth tool.

This is a property of the device's firmware, not a vulnerability in this integration.
The integration speaks the same open protocol the app does and has no way to add
authentication the unit lacks; changing it would take a firmware change from UGREEN.
The unit accepts one connection at a time, so while Home Assistant is connected nobody
else can open a second one, but when nothing is connected it is open to whoever
connects first. If something plugged into an output must not lose power unexpectedly,
treat physical range as the only access control. The README's
[Security section](README.md#security) says the same in plainer terms.

## Reporting a problem

Open a [GitHub issue](https://github.com/tanka8/ugreen-powerroam/issues). If the
problem would expose someone's credentials by being described in public, use GitHub's
[private vulnerability reporting](https://github.com/tanka8/ugreen-powerroam/security/advisories/new)
instead.

The unauthenticated Bluetooth control above is already known and documented, and there
is nothing in this integration to patch, so it does not need a private report.

Please be aware of what the README says about support: this is a hobby project, and
there is no commitment to a fix or to a response within any particular time. There is
no security support beyond a best effort.

## Before pasting logs

Log lines are not filtered by anyone - the `token` header and your email/password are
never logged by this integration, but check Home Assistant's own debug output for
anything unexpected before posting it publicly.
