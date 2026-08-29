"""Tests for the cloud transport's failure handling.

Both scenarios here were observed on a live system: the WebSocket stayed open
but stopped delivering telemetry, and the session token expired while the
reconnect loop retried it forever. Entities silently stopped updating for an
hour in both cases.

Sync tests driving asyncio.run() rather than async ones, so the suite needs no
extra pytest plugin.
"""

from __future__ import annotations

import asyncio
import contextlib

import aiohttp
import pytest

from .loader import load_module

api = load_module("api")


class FakeApi:
    """Just the attributes UgreenTelemetryHub reads off the client."""

    def __init__(self, relogin_error: Exception | None = None, signal_at: int = 1):
        self.user_id = "u1"
        self.device_name = "G00XX0000000000"
        self.token = "stale-token"
        self.relogin_calls = 0
        self._relogin_error = relogin_error
        self._signal_at = signal_at
        # Set once relogin has been called signal_at times, so tests can wait
        # on the behaviour instead of polling for it.
        self.reached = asyncio.Event()

    async def relogin(self) -> None:
        self.relogin_calls += 1
        if self.relogin_calls >= self._signal_at:
            self.reached.set()
        if self._relogin_error is not None:
            raise self._relogin_error
        self.token = "fresh-token"


class FakeMessage:
    def __init__(self, type_, data=""):
        self.type = type_
        self.data = data


class FakeWebSocket:
    """A socket that accepts the subscribe frame and then behaves as told."""

    def __init__(self, messages=None, hang: bool = False):
        self.messages = list(messages or [])
        self.hang = hang
        self.sent: list = []

    async def send_json(self, payload):
        self.sent.append(payload)

    async def receive(self):
        if self.hang:
            await asyncio.sleep(3600)  # never returns: a half-open connection
        if not self.messages:
            return FakeMessage(aiohttp.WSMsgType.CLOSED)
        return self.messages.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, ws):
        self._ws = ws

    def ws_connect(self, *args, **kwargs):
        return self._ws


def test_silent_socket_is_detected_rather_than_hung_on(monkeypatch):
    """A connection that stops delivering must raise, not block forever."""
    monkeypatch.setattr(api, "WS_STALL_TIMEOUT", 0.05)
    monkeypatch.setattr(api, "WS_KEEPALIVE_INTERVAL", 3600)
    hub = api.UgreenTelemetryHub(FakeApi(), FakeSession(FakeWebSocket(hang=True)))

    async def scenario():
        with pytest.raises(api.UgreenApiError, match="no telemetry"):
            await hub._connect_once()

    asyncio.run(scenario())


def test_closed_socket_raises(monkeypatch):
    monkeypatch.setattr(api, "WS_KEEPALIVE_INTERVAL", 3600)
    ws = FakeWebSocket([FakeMessage(aiohttp.WSMsgType.CLOSE)])
    hub = api.UgreenTelemetryHub(FakeApi(), FakeSession(ws))

    async def scenario():
        with pytest.raises(api.UgreenApiError, match="websocket closed"):
            await hub._connect_once()

    asyncio.run(scenario())


def test_telemetry_is_stored_and_listeners_fire(monkeypatch):
    monkeypatch.setattr(api, "WS_KEEPALIVE_INTERVAL", 3600)
    frame = '{"battery_percentage": 42}'
    ws = FakeWebSocket(
        [
            FakeMessage(aiohttp.WSMsgType.TEXT, '{"message": "success"}'),
            FakeMessage(aiohttp.WSMsgType.TEXT, frame),
            FakeMessage(aiohttp.WSMsgType.CLOSE),
        ]
    )
    hub = api.UgreenTelemetryHub(FakeApi(), FakeSession(ws))
    fired = []
    hub.add_listener(lambda: fired.append(1))

    async def scenario():
        with pytest.raises(api.UgreenApiError):
            await hub._connect_once()

    asyncio.run(scenario())
    assert hub.data == {"battery_percentage": 42}
    assert len(fired) == 1
    # The subscribe frame must go out before anything is read.
    assert ws.sent[0]["content"] == "ugreenSocketConnection"


def _run_loop_until(hub, reached: asyncio.Event, limit: float = 5.0) -> None:
    """Drive _run() until the event fires or the limit, then stop it cleanly."""

    async def scenario():
        task = asyncio.create_task(hub._run())
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(limit):
                await reached.wait()

        hub._stopped = True
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_relogins_after_repeated_failures(monkeypatch):
    """A stale token is never recovered by retrying with it."""
    monkeypatch.setattr(api, "WS_BACKOFF_START", 0)
    monkeypatch.setattr(api, "WS_BACKOFF_MAX", 0)
    monkeypatch.setattr(api, "WS_RELOGIN_AFTER_FAILURES", 3)

    fake_api = FakeApi()
    hub = api.UgreenTelemetryHub(fake_api, None)

    async def always_fail():
        raise api.UgreenApiError("401")

    hub._connect_once = always_fail
    _run_loop_until(hub, fake_api.reached)

    assert fake_api.relogin_calls >= 1
    assert fake_api.token == "fresh-token"


def test_loop_survives_a_failing_relogin(monkeypatch):
    """A broken re-login must not kill the loop - the network may come back."""
    monkeypatch.setattr(api, "WS_BACKOFF_START", 0)
    monkeypatch.setattr(api, "WS_BACKOFF_MAX", 0)
    monkeypatch.setattr(api, "WS_RELOGIN_AFTER_FAILURES", 2)

    fake_api = FakeApi(relogin_error=api.UgreenAuthError("still down"), signal_at=3)
    hub = api.UgreenTelemetryHub(fake_api, None)

    async def always_fail():
        raise api.UgreenApiError("boom")

    hub._connect_once = always_fail
    _run_loop_until(hub, fake_api.reached)

    # It kept trying rather than giving up after the first failed re-login.
    assert fake_api.relogin_calls >= 3


def test_successful_connection_resets_the_failure_count(monkeypatch):
    """A good connection must clear the counter, so occasional blips never
    accumulate into a needless re-login."""
    monkeypatch.setattr(api, "WS_BACKOFF_START", 0)
    monkeypatch.setattr(api, "WS_BACKOFF_MAX", 0)
    monkeypatch.setattr(api, "WS_RELOGIN_AFTER_FAILURES", 3)

    fake_api = FakeApi()
    hub = api.UgreenTelemetryHub(fake_api, None)
    attempts = []
    enough = asyncio.Event()

    async def fail_then_succeed():
        attempts.append(1)
        if len(attempts) >= 12:
            enough.set()
        if len(attempts) % 2 == 1:
            raise api.UgreenApiError("blip")

    hub._connect_once = fail_then_succeed
    _run_loop_until(hub, enough, limit=3.0)

    assert len(attempts) >= 12
    assert fake_api.relogin_calls == 0
