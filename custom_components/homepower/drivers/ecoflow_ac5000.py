"""EcoFlow STREAM AC 5000 (serial prefix ES22) through EcoFlow's app (private) cloud protocol.

The developer API HomePower uses for the other STREAM batteries does not cover this model's settings, so this driver does
what the EcoFlow app does: it signs in with your EcoFlow account (email + password), gets MQTT credentials, listens to the
battery's property topic and writes settings on its set topic. Messages are protobuf; the field numbers come from the
open-source "EcoFlow Cloud" Home Assistant integration (Apache-2.0), which decoded them from the app's own traffic.

Reading: live readings are pushed by the battery (power, charge level, the paired P1 meter, limits).
Control (experimental, never tried on a real unit by this project): the only settings known are the work mode, charge
limits, the "net power in/out" limits, AC output and UPS. HomePower has no known way to force a grid charge on this model,
so it offers Auto and Hold only. Hold sets the "net power out" limit to 0 (the battery stops feeding power out) and
remembers the old limit; Auto puts it back. Everything the EcoFlow app sends is also recorded (see ``last_quota``) so the
real charge command can be found and added.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import time
import uuid
from typing import Any

from ..core.models import Actual, Desired, DeviceConfig, Mode
from . import pbwire as pb

_LOGGER = logging.getLogger(__name__)
HOSTS = {"eu": "api-e.ecoflow.com", "america": "api-a.ecoflow.com", "global": "api.ecoflow.com"}
FRESH_S = 300             # a reading older than this counts as offline
RETRY_S, AUTH_RETRY_S = 60, 900
FIRST_READING_S = 25      # how long a connection test waits for the battery's first message
PROP_POWER_LIMITS = 10    # property ids of the app's "set" command
OUT_LIMIT_FIELD4, OUT_LIMIT_MAX = 4, 800   # constants the app sends alongside the output limit
MAX_CAPTURES = 12


class Ac5000Error(Exception):
    pass


# ------------------------------------------------------------------ protocol (pure functions)
def build_set_command(sn: str, pdata: bytes, seq: int | None = None) -> bytes:
    """One property write, in the envelope the EcoFlow app uses (shared with the other STREAM models)."""
    seq = seq if seq is not None else 999900000 + random.randint(10000, 99999)
    msg = (pb.enc_bytes(1, pdata) + pb.enc_int(2, 32) + pb.enc_int(3, 2) + pb.enc_int(4, 1) + pb.enc_int(5, 1)
           + pb.enc_int(7, 3) + pb.enc_int(8, 254) + pb.enc_int(9, 38) + pb.enc_int(10, len(pdata)) + pb.enc_int(11, 1)
           + pb.enc_int(14, seq) + pb.enc_int(16, 4) + pb.enc_bytes(23, "HomeAssistant")
           + pb.enc_bytes(25, sn) + pb.enc_bytes(26, sn) + pb.enc_bytes(27, sn))
    return pb.enc_bytes(1, msg)


def out_limit_pdata(watts: int) -> bytes:
    """Property 10 ("power limits"): the net power out limit, with the constants the app sends beside it."""
    limits = pb.enc_int(1, int(watts)) + pb.enc_int(4, OUT_LIMIT_FIELD4) + pb.enc_int(5, OUT_LIMIT_MAX)
    return pb.enc_int(1, PROP_POWER_LIMITS) + pb.enc_bytes(10, limits)


def decode_frame(payload: bytes) -> dict[str, float | int | str]:
    """Readings from one MQTT message (it can carry several packets). Unknown packets and fields are skipped."""
    flat: dict[str, Any] = {}
    for raw in pb.parse(payload).get(1, []):
        if not isinstance(raw, bytes):
            continue
        header = pb.parse(raw)
        blob = (header.get(1) or [b""])[-1]
        cmd_id = pb.num(header, 9)
        if not isinstance(blob, bytes) or not blob:
            continue
        try:
            _decode_pdata(int(cmd_id or 0), pb.parse(blob), flat)
        except Exception:  # noqa: BLE001 - an odd packet must never break the others
            _LOGGER.debug("ignored an AC 5000 packet (cmd_id %s)", cmd_id, exc_info=True)
    return flat


def _put(flat: dict, key: str, value: float | None, scale: float = 1.0) -> None:
    if value is not None:
        flat[key] = value * scale


def _decode_pdata(cmd_id: int, m: dict, flat: dict) -> None:
    if cmd_id == 2:                                     # status pack: pack-scale charge level and limits
        b = pb.sub(m, 1)
        _put(flat, "packSoc", pb.num(b, 15))
        _put(flat, "cmsMaxChgSoc", pb.num(b, 7))
        _put(flat, "cmsMinDsgSoc", pb.num(b, 21))
    elif cmd_id == 39:                                  # runtime
        meter = pb.sub(m, 16)
        if 16 in m:
            flat["meterLinkUp"] = 1 if meter else 0     # an empty block means the P1 meter is not responding
        _put(flat, "meterPhaseAPower", pb.num(meter, 4))
        _put(flat, "meterPhaseBPower", pb.num(meter, 5))
        _put(flat, "meterPhaseCPower", pb.num(meter, 6))
        _put(flat, "meterTotalPower", pb.num(meter, 16))
        cfg = pb.sub(m, 33)
        for key, field in (("maxChgPow", 1), ("maxDsgPow", 2), ("cmsBattFullEnergy", 5), ("f32ShowSoc", 6),
                           ("cmsMaxChgSoc", 7), ("cmsMinDsgSoc", 8), ("feedGridModePowMax", 10)):
            _put(flat, key, pb.num(cfg, field))
        cms = pb.sub(m, 44)
        _put(flat, "soc", pb.num(cms, 1))
        _put(flat, "remainTime", pb.num(cms, 7))
        _put(flat, "bpPower", pb.num(cms, 2), 0.5)      # half-watts, magnitude only
        bms = pb.sub(pb.sub(m, 38), 1)
        _put(flat, "bmsSoc", pb.num(bms, 1))
        if "bpPower" not in flat:
            _put(flat, "bpPower", pb.num(bms, 3), 0.5)
        _put(flat, "gridPortPower", pb.num(pb.sub(pb.sub(m, 50), 1), 4))   # + into the port = charging
        _put(flat, "soc", pb.num(pb.sub(pb.sub(m, 54), 1), 2))             # the app-facing level: sent most often
        _put(flat, "upsEnabled", pb.num(pb.sub(m, 23), 6))
        _put(flat, "xboostEnabled", pb.num(pb.sub(m, 23), 5))
        limits = pb.sub(m, 10)
        _put(flat, "feedGridModePowLimit", pb.num(limits, 1))
        _put(flat, "chgPowLimit", pb.num(limits, 2))
        _put(flat, "acOutEnabled", pb.num(pb.sub(m, 19), 1))
        _put(flat, "workMode", pb.num(m, 25))
    elif cmd_id == 50:                                  # pack totals
        for key, field in (("realSoh", 52), ("cycleSoh", 53), ("accuChgEnergy", 79), ("accuDsgEnergy", 80)):
            _put(flat, key, pb.num(m, field))


def actual_from(q: dict) -> Actual:
    soc = next((float(q[k]) for k in ("soc", "f32ShowSoc", "packSoc") if isinstance(q.get(k), (int, float)) and q[k] > 0), None)
    power = q.get("gridPortPower")
    if not isinstance(power, (int, float)):
        power = q.get("bpPower")
    out = q.get("feedGridModePowLimit")
    return Actual(
        online=True, soc=soc, power_w=float(power) if isinstance(power, (int, float)) else None,
        # Hold = the net power out limit is 0, so the battery is not feeding anything out
        mode=Mode.HOLD if out == 0 else Mode.AUTO,
        grid_w=float(q["meterTotalPower"]) if q.get("meterLinkUp", 1) and isinstance(q.get("meterTotalPower"), (int, float)) else None,
    )


def describe_frame(topic: str, payload: bytes) -> str:
    """A compact record of an app command for the diagnostics: ids, the command bytes, and the raw frame."""
    try:
        hdr = pb.parse((pb.parse(payload).get(1) or [b""])[-1])
        blob = (hdr.get(1) or [b""])[-1]
        return (f"{topic.rsplit('/', 1)[-1]} cmd_func={pb.num(hdr, 8)} cmd_id={pb.num(hdr, 9)} "
                f"from={pb.text(hdr, 23)} pdata={blob.hex() if isinstance(blob, bytes) else ''}")
    except Exception:  # noqa: BLE001
        return f"{topic.rsplit('/', 1)[-1]} raw={payload.hex()[:200]}"


# ------------------------------------------------------------------ the driver
class EcoFlowAc5000Driver:
    def __init__(self, config: DeviceConfig, session) -> None:
        self.config = config
        c = config.extra["connection"]
        self.sn = c["sn"]
        self._host = HOSTS.get(c.get("region", "global"), HOSTS["global"])
        self._email, self._password = c["email"], c["password"]
        self._s = session
        self.last_quota: dict = {}
        self.last_error: str | None = None
        self._state: dict = {}
        self._seen = 0.0
        self._client = None
        self._uid: str | None = None
        self._task: asyncio.Task | None = None
        self._next_try = 0.0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._saved_out: int | None = None
        self._captures: list[str] = []
        self.connected = False

    # ---- topics
    @property
    def _data_topic(self) -> str:
        return f"/app/device/property/{self.sn}"

    def _topic(self, leaf: str) -> str:
        return f"/app/{self._uid}/{self.sn}/thing/property/{leaf}"

    # ---- connection
    def _ensure(self) -> None:
        self._loop = self._loop or asyncio.get_running_loop()
        if self._client is None and (self._task is None or self._task.done()) and time.monotonic() >= self._next_try:
            self._task = self._loop.create_task(self._start())

    async def _api(self, method: str, path: str, token: str | None = None, body: dict | None = None) -> dict:
        from aiohttp import ClientTimeout

        headers = {"lang": "en_US", "content-type": "application/json"}
        if token:
            headers["authorization"] = f"Bearer {token}"
        try:
            async with self._s.request(method, f"https://{self._host}{path}", json=body, headers=headers,
                                       timeout=ClientTimeout(total=20)) as resp:
                data = await resp.json(content_type=None)
        except Exception as err:  # noqa: BLE001
            raise Ac5000Error(f"cannot reach EcoFlow: {err!r}") from err
        if not isinstance(data, dict) or str(data.get("code")) != "0":
            msg = data.get("message") if isinstance(data, dict) else "unexpected reply"
            err = Ac5000Error(f"EcoFlow refused: {msg}")
            err.auth = path.startswith("/auth")       # type: ignore[attr-defined]
            raise err
        return data.get("data") or {}

    async def _start(self) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            self.last_error, self._next_try = "paho-mqtt is not installed", time.monotonic() + 10**9
            return
        try:
            login = await self._api("POST", "/auth/login", body={
                "email": self._email, "password": base64.b64encode(self._password.encode()).decode(),
                "scene": "IOT_APP", "userType": "ECOFLOW"})
            self._uid = str(login["user"]["userId"])
            cert = await self._api("GET", "/iot-auth/app/certification", token=login["token"])
            await self._loop.run_in_executor(None, self._connect, mqtt, cert)
            self.last_error = None
        except Exception as err:  # noqa: BLE001 - retried later
            auth = getattr(err, "auth", False)
            self.last_error = f"EcoFlow sign-in failed: {err}" if auth else f"cannot connect to EcoFlow: {err}"
            self._next_try = time.monotonic() + (AUTH_RETRY_S if auth else RETRY_S)
            _LOGGER.warning("%s: %s", self.config.device_id, self.last_error)

    def _connect(self, mqtt, cert: dict) -> None:
        client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                             client_id=f"ANDROID_{uuid.uuid4().hex.upper()}_{self._uid}")
        client.username_pw_set(str(cert["certificateAccount"]), str(cert["certificatePassword"]))
        client.tls_set()

        def on_connect(c, _u, _f, rc, _p=None):
            self.connected = not getattr(rc, "is_failure", bool(rc))
            if self.connected:
                c.subscribe([(self._data_topic, 1), (self._topic("set"), 1), (self._topic("set_reply"), 1)])
                c.publish(self._topic("get"), json.dumps({"from": "HomeAssistant", "id": str(random.randint(10**8, 10**9)),
                                                           "version": "1.1", "moduleType": 0, "operateType": "latestQuotas", "params": {}}), qos=1)
            else:
                self.last_error = f"EcoFlow's message server refused the connection ({rc})"

        def on_disconnect(*_a):
            self.connected = False

        def on_message(_c, _u, msg):
            self._loop.call_soon_threadsafe(self._dispatch, msg.topic, bytes(msg.payload))

        client.on_connect, client.on_disconnect, client.on_message = on_connect, on_disconnect, on_message
        client.reconnect_delay_set(min_delay=5, max_delay=120)
        client.connect_async(str(cert["url"]), int(cert["port"]), keepalive=60)
        client.loop_start()
        self._client = client

    def _dispatch(self, topic: str, payload: bytes) -> None:
        try:
            if topic == self._data_topic:
                flat = decode_frame(payload)
                if flat:
                    self._state.update(flat)
                    self._seen = time.monotonic()
                    self.last_quota = {**dict(self._state), **{f"app_command_{i + 1}": c for i, c in enumerate(self._captures)}}
            elif topic.endswith("/set") or topic.endswith("/set_reply"):
                # What the EcoFlow app (or HomePower) wrote: kept so an unknown command, such as a scheduled charge, can be decoded.
                self._captures = (self._captures + [describe_frame(topic, payload)])[-MAX_CAPTURES:]
                self.last_quota = {**self.last_quota, **{f"app_command_{i + 1}": c for i, c in enumerate(self._captures)}}
        except Exception:  # noqa: BLE001
            _LOGGER.debug("ignored an AC 5000 message", exc_info=True)

    async def close(self) -> None:
        client, self._client, self.connected = self._client, None, False
        if client is not None:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # ---- the driver interface
    async def read(self) -> Actual:
        self._ensure()
        if not self._seen or time.monotonic() - self._seen > FRESH_S:
            if self.last_error is None:
                self.last_error = "waiting for the battery's first message from EcoFlow"
            return Actual(online=False)
        self.last_error = None
        return actual_from(self._state)

    async def probe(self) -> dict:
        self._ensure()
        for _ in range(FIRST_READING_S * 2):
            if self._seen:
                break
            if self.last_error and self._task and self._task.done() and "waiting" not in self.last_error:
                raise Ac5000Error(self.last_error)
            await asyncio.sleep(0.5)
        if not self._seen:
            raise Ac5000Error(self.last_error or "the battery sent nothing within 25 seconds (is it online in the EcoFlow app?)")
        q, a = self._state, actual_from(self._state)
        energy = q.get("cmsBattFullEnergy")
        return {"soc": a.soc, "power_w": a.power_w, "mode": a.mode.value if a.mode else None, "grid_w": a.grid_w,
                "pv_w": None, "load_w": None, "model": "EcoFlow STREAM AC 5000", "serial": self.sn,
                "capacity_wh": int(energy) if isinstance(energy, (int, float)) and energy > 0 else None,
                "max_charge_w": None, "max_discharge_w": None,
                "min_soc": int(q["cmsMinDsgSoc"]) if isinstance(q.get("cmsMinDsgSoc"), (int, float)) else None}

    async def apply(self, desired: Desired) -> None:
        self._ensure()
        out = self._state.get("feedGridModePowLimit")
        if desired.mode is Mode.HOLD:
            if not isinstance(out, (int, float)):
                raise ValueError("cannot hold: the battery's current output limit is not known yet")
            if out > 0:
                self._saved_out = int(out)
            await self._send(out_limit_pdata(0), 0)
        elif desired.mode is Mode.AUTO:
            if out == 0:   # held (by us, possibly before a restart): give the limit back
                restore = self._saved_out or int(self._state.get("feedGridModePowMax") or 0)
                if restore <= 0:
                    raise ValueError("cannot restore the output limit: its old value is not known")
                await self._send(out_limit_pdata(restore), restore)
                self._saved_out = None
        else:
            raise ValueError(f"the EcoFlow AC 5000 cannot be told to {desired.mode.value}: no such command is known")

    async def _send(self, pdata: bytes, new_out_limit: int) -> None:
        if self._client is None or not self.connected or self._uid is None:
            raise ValueError("not connected to EcoFlow yet")
        info = self._client.publish(self._topic("set"), build_set_command(self.sn, pdata), qos=1)
        if getattr(info, "rc", 0) != 0:
            raise ValueError(f"EcoFlow's message server did not accept the command (code {info.rc})")
        self._state["feedGridModePowLimit"] = new_out_limit   # assumed until the battery's next message confirms it
