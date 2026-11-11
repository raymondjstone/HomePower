"""Live updates from EcoFlow's MQTT feed (the same account keys, no extra setup).

Polling the cloud API gives a reading every 30 seconds at best. EcoFlow also pushes changes as they happen over MQTT, so
when this works the readings are seconds old. It is strictly an extra: if paho-mqtt is missing, the broker refuses us, or
the messages look different from what we expect, the driver keeps polling exactly as before.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)
RETRY_AFTER_S = 600


def push_params(obj: Any) -> dict:
    """The readings inside one pushed message: under 'params'/'param'/'quotaMap', or at the top level."""
    from .ecoflow import normalise

    if not isinstance(obj, dict):
        return {}
    flat: dict = {}
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


class EcoFlowLive:
    """One MQTT connection per EcoFlow account, shared by that account's batteries."""

    def __init__(self, api, loop: asyncio.AbstractEventLoop) -> None:
        self.api, self.loop = api, loop
        self._devs: dict[str, Callable[[dict], None]] = {}
        self._client = None
        self._task: asyncio.Task | None = None
        self._next_try = 0.0
        self.error: str | None = None
        self.connected = False
        self.messages = 0
        self.enabled = False            # opt-in: a second connection on the same account could disturb other EcoFlow integrations

    def register(self, sn: str, callback: Callable[[dict], None]) -> None:
        self._devs[sn] = callback
        self.ensure()

    def unregister(self, sn: str, callback: Callable[[dict], None] | None = None) -> None:
        if callback is not None and self._devs.get(sn) != callback:
            return                      # a different driver object now owns this battery (e.g. a connection test closed)
        self._devs.pop(sn, None)
        if not self._devs:
            self.stop()

    def ensure(self) -> None:
        """Start (or retry) the connection in the background; never raises."""
        if not self.enabled or self._client is not None or (self._task and not self._task.done()) or time.monotonic() < self._next_try or not self._devs:
            return
        self._task = self.loop.create_task(self._start())

    async def _start(self) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            self.error, self._next_try = "paho-mqtt is not installed: using polling only", time.monotonic() + 10**9
            return
        try:
            cert = await self.api.certification()
            await self.loop.run_in_executor(None, self._connect, mqtt, cert)
            self.error = None
        except Exception as err:  # noqa: BLE001 - live updates are optional
            self.error, self._next_try = f"live updates unavailable: {err}", time.monotonic() + RETRY_AFTER_S
            _LOGGER.debug("EcoFlow live connection failed", exc_info=True)

    def _connect(self, mqtt, cert: dict) -> None:
        account = str(cert["certificateAccount"])
        client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=f"HomePower-{account}-{int(time.time()) % 100000}")
        client.username_pw_set(account, str(cert["certificatePassword"]))
        client.tls_set()

        def on_connect(c, _u, _f, rc, _p=None):
            self.connected = not getattr(rc, "is_failure", bool(rc))
            if self.connected:
                for sn in list(self._devs):
                    c.subscribe([(f"/open/{account}/{sn}/quota", 1), (f"/open/{account}/{sn}/status", 1)])

        def on_disconnect(*_a):
            self.connected = False

        def on_message(_c, _u, msg):
            self.loop.call_soon_threadsafe(self._dispatch, msg.topic, msg.payload)

        client.on_connect, client.on_disconnect, client.on_message = on_connect, on_disconnect, on_message
        client.reconnect_delay_set(min_delay=5, max_delay=120)
        client.connect_async(str(cert["url"]), int(cert["port"]), keepalive=60)
        client.loop_start()
        self._client = client

    def _dispatch(self, topic: str, payload: bytes) -> None:
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
        client, self._client, self.connected = self._client, None, False
        if client is not None:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass
