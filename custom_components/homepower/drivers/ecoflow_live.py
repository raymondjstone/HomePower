"""Live updates from EcoFlow's MQTT feed (the same account keys, no extra setup).

Polling the cloud API gives a reading every 30 seconds at best. EcoFlow also pushes changes as they happen over MQTT, so
when this works the readings are seconds old. It is strictly an extra: if aiomqtt is missing, the broker refuses us, or
the messages look different from what we expect, the driver keeps polling exactly as before.

The connection runs on Home Assistant's event loop (aiomqtt): no thread of its own, and stopping it is cancelling one task.
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiomqtt

    from .ecoflow import EcoFlowApi

_LOGGER = logging.getLogger(__name__)
RETRY_AFTER_S = 600                   # the account's MQTT certificate could not be had: try again this much later
RECONNECT_MIN_S = 5.0                 # the broker dropped us or refused us: wait this long, doubling ...
RECONNECT_MAX_S = 120.0               # ... up to this, before connecting again


def push_params(obj: Any) -> dict[str, Any]:
    """The readings inside one pushed message: under 'params'/'param'/'quotaMap', or at the top level."""
    from .ecoflow import normalise

    if not isinstance(obj, dict):
        return {}
    flat: dict[str, Any] = {}
    for key in ("params", "param", "quotaMap"):
        inner = obj.get(key)
        if isinstance(inner, dict):
            flat.update(normalise(inner))
    for k, v in obj.items():
        if k not in ("params", "param", "quotaMap") and isinstance(v, (int, float, str)) and not isinstance(v, bool):
            flat.setdefault(str(k), v)
    return flat


def parse_topic(topic: str) -> tuple[str, str] | None:
    """'/open/<account>/<sn>/quota' -> (sn, 'quota')."""
    parts = [p for p in topic.split("/") if p]
    if len(parts) >= 4 and parts[0] == "open":
        return parts[2], parts[3]
    return None


def _topics(account: str, sns: list[str]) -> list[tuple[str, int]]:
    return [(f"/open/{account}/{sn}/{kind}", 1) for sn in sns for kind in ("quota", "status")]


class EcoFlowLive:
    """One MQTT connection per EcoFlow account, shared by that account's batteries."""

    def __init__(self, api: EcoFlowApi, loop: asyncio.AbstractEventLoop) -> None:
        self.api, self.loop = api, loop
        self._devs: dict[str, Callable[[dict[str, Any]], None]] = {}
        self._client: aiomqtt.Client | None = None      # while connected
        self._account = ""
        self._task: asyncio.Task[None] | None = None     # the connection: connects, reconnects, reads the messages
        self._extra: set[asyncio.Task[None]] = set()     # subscriptions for batteries added while connected, and stopped
                                                         # connections still closing (held until done, or asyncio may drop them)
        self._next_try = 0.0
        self.error: str | None = None
        self.connected = False
        self.messages = 0
        self.enabled = False            # opt-in: a second connection on the same account could disturb other EcoFlow integrations

    def register(self, sn: str, callback: Callable[[dict[str, Any]], None]) -> None:
        new = sn not in self._devs
        self._devs[sn] = callback
        self.ensure()
        if new and self._client is not None and self.connected:      # a battery added while the connection is up: subscribe now, not at the next reconnect
            task = self.loop.create_task(self._subscribe(self._client, [sn]))
            self._extra.add(task)
            task.add_done_callback(self._extra.discard)

    def unregister(self, sn: str, callback: Callable[[dict[str, Any]], None] | None = None) -> None:
        if callback is not None and self._devs.get(sn) != callback:
            return                      # a different driver object now owns this battery (e.g. a connection test closed)
        self._devs.pop(sn, None)
        if not self._devs:
            self.stop()

    def ensure(self) -> None:
        """Start (or retry) the connection in the background; never raises."""
        if not self.enabled or (self._task and not self._task.done()) or time.monotonic() < self._next_try or not self._devs:
            return
        self._task = self.loop.create_task(self._run())

    async def _subscribe(self, client: aiomqtt.Client, sns: list[str]) -> None:
        if not sns or not self._account:
            return
        try:
            await client.subscribe(_topics(self._account, sns))
        except Exception:  # noqa: BLE001 - subscribed again at the next reconnect anyway
            _LOGGER.debug("could not subscribe %s now", sns, exc_info=True)

    async def _run(self) -> None:
        try:
            import aiomqtt
        except ImportError:
            self.error, self._next_try = "aiomqtt is not installed: using polling only", time.monotonic() + 10**9
            return
        try:
            cert = await self.api.certification()
            # loading the system's certificates reads files: done off the event loop
            tls = await self.loop.run_in_executor(None, ssl.create_default_context)
            account, password = str(cert["certificateAccount"]), str(cert["certificatePassword"])
            host, port = str(cert["url"]), int(cert["port"])
        except Exception as err:  # noqa: BLE001 - live updates are optional
            self.error, self._next_try = f"live updates unavailable: {err}", time.monotonic() + RETRY_AFTER_S
            _LOGGER.debug("EcoFlow live connection failed", exc_info=True)
            return
        self._account = account
        me = asyncio.current_task()
        delay = RECONNECT_MIN_S
        while self._task is me:             # stopped (or replaced) meanwhile: done, never reconnect
            client = aiomqtt.Client(host, port, username=account, password=password, keepalive=60, tls_context=tls,
                                    identifier=f"HomePower-{account}-{int(time.time()) % 100000}")
            try:
                async with client:
                    self._client, self.connected, self.error = client, True, None
                    delay = RECONNECT_MIN_S
                    await self._subscribe(client, list(self._devs))
                    async for msg in client.messages:
                        self._dispatch(str(msg.topic), msg.payload)
            except Exception as err:  # noqa: BLE001 - refused, dropped, or anything odd: wait, then connect again
                if self._task is not me:    # stopped while closing, and closing failed (aiomqtt raises that instead of the cancel)
                    return
                self.error = f"live updates interrupted, reconnecting: {err}"
                _LOGGER.debug("EcoFlow live connection lost", exc_info=True)
            finally:
                if self._client is client:      # (a newer connection may already be up: a stopped one closing late leaves it alone)
                    self._client, self.connected = None, False
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_S)

    def _dispatch(self, topic: str, payload: Any) -> None:
        try:
            parsed = parse_topic(topic)
            if not parsed or parsed[1] != "quota":
                return
            cb = self._devs.get(parsed[0])
            flat = push_params(json.loads(payload))
            if cb and flat:
                self.messages += 1
                cb(flat)
        except Exception:  # noqa: BLE001 - a odd message must never break anything
            _LOGGER.debug("ignored an EcoFlow live message", exc_info=True)

    def stop(self) -> None:
        """Close the connection (it says goodbye to the broker as its task ends). Never blocks, never raises."""
        task, self._task = self._task, None
        self._client, self.connected = None, False
        for t in [task, *self._extra]:
            if t is None or t.done():
                continue
            try:
                if t is task:
                    self._extra.add(t)
                    t.add_done_callback(self._extra.discard)
                t.cancel()
            except RuntimeError:            # the event loop has already closed (Home Assistant stopping): nothing left running
                pass
