"""Client for UGREEN's PowerRoam cloud API (hw-powerapi.ugpps.com).

Reverse engineered from the com.powerroam.pps Android app's traffic. Not an
official/documented API - it can change without notice.

Auth flow (matches what the app does):
  1. GET  /app/v1/sa/encrypt/key         -> {encryptKey (RSA pubkey, b64 DER), uuid}
  2. RSA/PKCS1v1.5-encrypt email & password with that key, each base64-encoded
  3. POST /app/v1/login                  -> {token, userId, device: [...]}
  4. GET  /app/v1/device/list            -> deviceModelName (used as device id)
  5. WSS  /app/device/websocket/{userId}/{deviceModelName}, header token: <token>
     -> send {"userId": ..., "content": "ugreenSocketConnection"} to subscribe
     -> server pushes a flat JSON object of live telemetry periodically

Control:
  POST /app/v1/device/setDeviceInfo {"deviceName": <deviceModelName>, "map": {key: value}}
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Callable

import aiohttp

from .const import (
    API_BASE,
    WS_BACKOFF_MAX,
    WS_BACKOFF_START,
    WS_KEEPALIVE_INTERVAL,
    WS_RELOGIN_AFTER_FAILURES,
    WS_STALL_TIMEOUT,
    WS_URL_TEMPLATE,
)

_LOGGER = logging.getLogger(__name__)


class UgreenAuthError(Exception):
    """Raised when login fails (bad credentials, or account/password encrypt mismatch)."""


class UgreenApiError(Exception):
    """Raised for any other non-2xx / errcode!=200 API response."""


def parse_telemetry_frame(raw: str) -> dict | None:
    """Parse one WebSocket text frame; return the telemetry dict, or None.

    The server also sends small ack frames like {"message": "success"} for
    the subscribe/keepalive messages - those are distinguished from real
    telemetry by not having a battery_percentage field. Pulled out as a pure
    function so it's testable without a live connection - see tests/.
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and "battery_percentage" in payload:
        return payload
    return None


def _rsa_encrypt(der_b64: str, plaintext: str) -> str:
    """Encrypt plaintext with the server-supplied RSA public key (PKCS1v1.5).

    Runs synchronously - call via hass.async_add_executor_job, this is cheap
    (one RSA op on a short string) but still blocking I/O-free CPU work.
    """
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.serialization import load_der_public_key

    pubkey = load_der_public_key(base64.b64decode(der_b64))
    encrypted = pubkey.encrypt(plaintext.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(encrypted).decode("ascii")


class UgreenApiClient:
    def __init__(self, session: aiohttp.ClientSession, hass=None) -> None:
        self._session = session
        self._hass = hass
        self.token: str | None = None
        self.user_id: str | None = None
        self.device_name: str | None = None
        self.sn: str | None = None
        self._email: str | None = None
        self._password: str | None = None

    @property
    def unique_id_base(self) -> str:
        """Stable prefix for entity unique_ids, shared with the BLE transport."""
        return self.sn or self.device_name or ""

    def _headers(self) -> dict[str, str]:
        return {
            "wl-lang": "en",
            "token": self.token or "",
            "Content-Type": "application/json",
        }

    async def _encrypt(self, der_b64: str, plaintext: str) -> str:
        if self._hass is not None:
            return await self._hass.async_add_executor_job(
                _rsa_encrypt, der_b64, plaintext
            )
        return _rsa_encrypt(der_b64, plaintext)

    async def login(self, email: str, password: str) -> None:
        """Full login flow. Raises UgreenAuthError on bad credentials."""
        self._email = email
        self._password = password

        async with self._session.get(f"{API_BASE}/app/v1/sa/encrypt/key") as resp:
            key_resp = await resp.json(content_type=None)
        if key_resp.get("errcode") != 200:
            raise UgreenApiError(f"encrypt/key failed: {key_resp}")
        encrypt_key = key_resp["result"]["encryptKey"]
        uuid = key_resp["result"]["uuid"]

        enc_account = await self._encrypt(encrypt_key, email)
        enc_password = await self._encrypt(encrypt_key, password)

        body = {
            "account": enc_account,
            "password": enc_password,
            "code": None,
            "type": 2,
            "uuid": uuid,
        }
        async with self._session.post(
            f"{API_BASE}/app/v1/login", json=body, headers=self._headers()
        ) as resp:
            login_resp = await resp.json(content_type=None)

        if login_resp.get("errcode") != 200:
            raise UgreenAuthError(login_resp.get("errmsg", "login failed"))

        result = login_resp["result"]
        self.token = result["token"]
        self.user_id = result["userId"]

        await self.refresh_device()

    async def refresh_device(self) -> None:
        """Fetch the device list and remember the first device's identifiers."""
        async with self._session.get(
            f"{API_BASE}/app/v1/device/list?pageNum=1&pageSize=10",
            headers=self._headers(),
        ) as resp:
            list_resp = await resp.json(content_type=None)
        if list_resp.get("errcode") != 200:
            raise UgreenApiError(f"device/list failed: {list_resp}")
        devices = list_resp.get("result") or []
        if not devices:
            raise UgreenApiError("no devices on this account")
        device = devices[0]
        self.device_name = device["deviceModelName"]
        self.sn = device.get("sn")

    async def set_device_info(self, key: str, value: int) -> None:
        """Send one control command, e.g. set_device_info('switch_ac', 1)."""
        body = {"deviceName": self.device_name, "map": {key: value}}
        async with self._session.post(
            f"{API_BASE}/app/v1/device/setDeviceInfo",
            json=body,
            headers=self._headers(),
        ) as resp:
            result = await resp.json(content_type=None)
        if result.get("errcode") != 200:
            raise UgreenApiError(f"setDeviceInfo({key}={value}) failed: {result}")

    async def relogin(self) -> None:
        """Re-run login with the stored credentials (e.g. after a token expiry)."""
        if not self._email or not self._password:
            raise UgreenAuthError("no stored credentials to re-login with")
        await self.login(self._email, self._password)


class UgreenTelemetryHub:
    """Owns the persistent WebSocket connection and the last-known state."""

    def __init__(self, api: UgreenApiClient, session: aiohttp.ClientSession) -> None:
        self._api = api
        self._session = session
        self.data: dict = {}
        self._listeners: list[Callable[[], None]] = []
        self._task: asyncio.Task | None = None
        self._stopped = False
        self._connected = False

    @property
    def available(self) -> bool:
        """Whether the socket is up, so entities go unavailable instead of
        quietly serving an hour-old reading."""
        return self._connected

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)

        def _remove() -> None:
            self._listeners.remove(cb)

        return _remove

    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    async def start(self) -> None:
        self._stopped = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        backoff = WS_BACKOFF_START
        failures = 0
        while not self._stopped:
            try:
                await self._connect_once()
                backoff = WS_BACKOFF_START  # reset after a clean connection
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as err:
                failures += 1
                _LOGGER.debug(
                    "UGREEN websocket loop error (failure %s): %s", failures, err
                )
                # A stale token cannot be recovered by retrying with it, so
                # after a few consecutive failures get a fresh one. Without
                # this the loop retries a dead session indefinitely and the
                # entities silently stop updating.
                if failures >= WS_RELOGIN_AFTER_FAILURES:
                    try:
                        await self._api.relogin()
                        _LOGGER.debug("UGREEN re-login succeeded, retrying socket")
                        failures = 0
                        backoff = WS_BACKOFF_START
                    except asyncio.CancelledError:
                        raise
                    except Exception as relogin_err:
                        _LOGGER.debug("UGREEN re-login failed: %s", relogin_err)
            if self._stopped:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, WS_BACKOFF_MAX)

    async def _connect_once(self) -> None:
        url = WS_URL_TEMPLATE.format(
            user_id=self._api.user_id, device_name=self._api.device_name
        )
        headers = {"token": self._api.token or ""}

        # heartbeat stays disabled: the server was never observed to answer
        # protocol-level pings, so liveness is judged from telemetry instead,
        # which is purely client-side and cannot upset it.
        async with self._session.ws_connect(url, headers=headers, heartbeat=None) as ws:
            await ws.send_json(
                {"userId": self._api.user_id, "content": "ugreenSocketConnection"}
            )

            async def _keepalive() -> None:
                while True:
                    await asyncio.sleep(WS_KEEPALIVE_INTERVAL)
                    await ws.send_json(
                        {
                            "userId": self._api.user_id,
                            "content": "ugreenSocketConnection",
                        }
                    )

            keepalive_task = asyncio.create_task(_keepalive())
            self._connected = True
            self._notify()
            try:
                while True:
                    # Reading with a deadline is what makes a half-open socket
                    # detectable: iterating the socket directly would block
                    # here forever while the entities quietly went stale.
                    try:
                        async with asyncio.timeout(WS_STALL_TIMEOUT):
                            msg = await ws.receive()
                    except TimeoutError as err:
                        raise UgreenApiError(
                            f"no telemetry for {WS_STALL_TIMEOUT}s, reconnecting"
                        ) from err

                    if msg.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        raise UgreenApiError(f"websocket closed: {msg.type.name}")
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue

                    payload = parse_telemetry_frame(msg.data)
                    if payload is not None:
                        self.data = payload
                        self._notify()
            finally:
                keepalive_task.cancel()
                self._connected = False
                self._notify()
