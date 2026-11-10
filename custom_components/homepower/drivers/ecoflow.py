"""EcoFlow STREAM family over EcoFlow's own developer (IoT Open) API.

You need an Access Key and Secret Key from https://developer.ecoflow.com/ (your EcoFlow account).
Hosts: api-e.ecoflow.com (Europe) and api.ecoflow.com (global/US). Requests are HMAC-SHA256 signed.

These batteries expose no charge/discharge power setpoint through this API, only the operating
mode and the backup reserve, so HomePower offers Auto and Hold: Hold raises the backup reserve so the
battery will not discharge (solar can still charge it); Auto restores your normal reserve.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from typing import Any

from ..core.models import Actual, Desired, DeviceConfig, Mode

_LOGGER = logging.getLogger(__name__)
HOSTS = {"eu": "api-e.ecoflow.com", "global": "api.ecoflow.com"}
HOLD_RESERVE = 95  # STREAM accepts a backup reserve of 3-95 %


class EcoFlowError(Exception):
    pass


def flatten(obj: Any, prefix: str = "") -> dict[str, str]:
    """Flatten nested params the way EcoFlow's signature expects (a.b=1, list[0]=x, true/false)."""
    out: dict[str, str] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = str(obj).lower() if isinstance(obj, bool) else str(obj)
    return out


def sign(params: dict | None, access_key: str, secret_key: str, nonce: str, timestamp: str) -> str:
    flat = flatten(params or {})
    base = "&".join(f"{k}={flat[k]}" for k in sorted(flat))
    tail = f"accessKey={access_key}&nonce={nonce}&timestamp={timestamp}"
    msg = f"{base}&{tail}" if base else tail
    return hmac.new(secret_key.encode(), msg.encode(), hashlib.sha256).hexdigest()


class EcoFlowApi:
    def __init__(self, session, region: str, access_key: str, secret_key: str) -> None:
        self._s = session
        self._base = f"https://{HOSTS.get(region, HOSTS['global'])}"
        self._ak, self._sk = access_key, secret_key

    def _headers(self, params: dict | None) -> dict[str, str]:
        nonce, ts = str(secrets.randbelow(900000) + 100000), str(int(time.time() * 1000))
        return {"accessKey": self._ak, "nonce": nonce, "timestamp": ts,
                "sign": sign(params, self._ak, self._sk, nonce, ts), "Content-Type": "application/json;charset=UTF-8"}

    async def _call(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> Any:
        from aiohttp import ClientTimeout

        signed = body if body is not None else params
        try:
            async with self._s.request(method, self._base + path, params=params if body is None else None,
                                       json=body, headers=self._headers(signed), timeout=ClientTimeout(total=15)) as resp:
                data = await resp.json(content_type=None)
        except Exception as err:  # noqa: BLE001 - network / JSON problems all mean "can't reach"
            raise EcoFlowError(f"cannot reach EcoFlow: {err!r}") from err
        if str(data.get("code")) != "0":
            raise EcoFlowError(f"EcoFlow error {data.get('code')}: {data.get('message')}")
        return data.get("data")

    async def devices(self) -> list[dict]:
        return await self._call("GET", "/iot-open/sign/device/list") or []

    async def quota(self, sn: str) -> dict:
        return await self._call("GET", "/iot-open/sign/device/quota/all", {"sn": sn}) or {}

    async def command(self, sn: str, params: dict) -> None:
        await self._call("PUT", "/iot-open/sign/device/quota", body={
            "sn": sn, "cmdId": 17, "cmdFunc": 254, "dirDest": 1, "dirSrc": 1, "dest": 2,
            "needAck": True, "params": params})


def _first(q: dict, *keys: str, positive: bool = False) -> float | None:
    for k in keys:
        v = q.get(k)
        if isinstance(v, (int, float)) and not (positive and v <= 0):
            return float(v)
    return None


class EcoFlowStreamDriver:
    def __init__(self, config: DeviceConfig, session) -> None:
        self.config = config
        c = config.extra["connection"]
        self.sn = c["sn"]
        self.api = EcoFlowApi(session, c.get("region", "global"), c["access_key"], c["secret_key"])
        self._cache: tuple[float, Actual] | None = None  # the cloud API is rate limited: poll at most every 30 s

    async def close(self) -> None:
        return None

    @staticmethod
    def _decode(q: dict) -> Actual:
        reserve = _first(q, "backupReverseSoc")
        soc = next((float(q[k]) for k in ("f32ShowSoc", "soc", "cmsBattSoc") if isinstance(q.get(k), (int, float)) and q[k] > 0), None)
        return Actual(
            online=True,
            # Ultra / Ultra X report cmsBattSoc as 0 and publish the real value as soc / f32ShowSoc.
            soc=soc,
            power_w=_first(q, "powGetBpCms"),  # EcoFlow: discharge negative, charge positive
            mode=(Mode.HOLD if reserve is not None and reserve >= HOLD_RESERVE else Mode.AUTO) if reserve is not None else None,
            grid_w=_first(q, "powGetSysGrid", "gridConnectionPower"),
            pv_w=_first(q, "powGetPvSum"), load_w=_first(q, "powGetSysLoad"),
        )

    async def read(self) -> Actual:
        if self._cache and time.monotonic() - self._cache[0] < 30:
            return self._cache[1]
        try:
            actual = self._decode(await self.api.quota(self.sn))
            self._cache = (time.monotonic(), actual)
            return actual
        except EcoFlowError as err:
            _LOGGER.debug("%s read failed: %s", self.config.device_id, err)
            return Actual(online=False)

    async def probe(self) -> dict:
        q = await self.api.quota(self.sn)
        a = self._decode(q)
        return {"soc": a.soc, "power_w": a.power_w, "mode": a.mode.value if a.mode else None,
                "grid_w": a.grid_w, "pv_w": a.pv_w, "load_w": a.load_w, "model": q.get("productName") or None,
                "serial": self.sn, "capacity_wh": int(q["cmsBattFullEnergy"]) if isinstance(q.get("cmsBattFullEnergy"), (int, float)) and q["cmsBattFullEnergy"] > 0 else None,
                "max_charge_w": None, "max_discharge_w": None, "min_soc": None,
                "reserve": q.get("backupReverseSoc")}

    async def apply(self, desired: Desired) -> None:
        self._cache = None  # next read must show the effect of this command
        if desired.mode is Mode.HOLD:
            await self.api.command(self.sn, {"cfgBackupReverseSoc": HOLD_RESERVE})
        elif desired.mode is Mode.AUTO:
            await self.api.command(self.sn, {"cfgEnergyStrategyOperateMode": {"operateSelfPoweredOpen": True}})
            await self.api.command(self.sn, {"cfgBackupReverseSoc": max(3, self.config.caps.min_soc)})
        else:
            raise ValueError(f"EcoFlow STREAM cannot {desired.mode.value} through the API")
