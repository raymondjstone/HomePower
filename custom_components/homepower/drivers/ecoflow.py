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
import re
import logging
import secrets
import time
from typing import Any

from ..core.models import Actual, Desired, DeviceConfig, Mode

_LOGGER = logging.getLogger(__name__)
HOSTS = {"eu": "api-e.ecoflow.com", "america": "api-a.ecoflow.com", "global": "api.ecoflow.com"}
SIGN_ERRORS = ("8521", "7000")
QUOTAS = ("backupReverseSoc", "f32ShowSoc", "soc", "cmsBattSoc", "powGetBpCms", "powGetSysGrid", "gridConnectionPower",
          "powGetPvSum", "powGetSysLoad", "cmsBattFullEnergy", "productName")
HOLD_RESERVE = 95  # STREAM accepts a backup reserve of 3-95 %; Hold raises the reserve to the current charge level


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
                "sign": sign(params, self._ak, self._sk, nonce, ts)}

    async def _call(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> Any:
        from aiohttp import ClientTimeout

        signed = body if body is not None else params
        try:
            async with self._s.request(method, self._base + path, params=params if body is None else None,
                                       json=body, headers=self._headers(signed), timeout=ClientTimeout(total=15)) as resp:
                data = await resp.json(content_type=None)
        except Exception as err:  # noqa: BLE001 - network / JSON problems all mean "can't reach"
            raise EcoFlowError(f"cannot reach EcoFlow: {err!r}") from err
        if not isinstance(data, dict):
            raise EcoFlowError("EcoFlow sent an unexpected reply")
        if str(data.get("code")) != "0":
            err = EcoFlowError(f"EcoFlow error {data.get('code')}: {data.get('message')}")
            err.code = str(data.get("code"))
            raise err
        return data.get("data")

    async def devices(self) -> list[dict]:
        return await self._call("GET", "/iot-open/sign/device/list") or []

    async def certification(self) -> dict:
        """MQTT broker address and login for live updates."""
        return await self._call("GET", "/iot-open/sign/certification") or {}

    async def quota(self, sn: str) -> dict:
        try:
            return await self._call("GET", "/iot-open/sign/device/quota/all", {"sn": sn}) or {}
        except EcoFlowError as err:
            if getattr(err, "code", "") not in SIGN_ERRORS:
                raise
        # Some accounts/regions reject the GET form's signature: ask for the readings with the signed POST form instead.
        got = await self._call("POST", "/iot-open/sign/device/quota", body={"sn": sn, "params": {"quotas": list(QUOTAS)}})
        return got if isinstance(got, dict) else {}

    async def command(self, sn: str, params: dict) -> None:
        await self._call("PUT", "/iot-open/sign/device/quota", body={
            "sn": sn, "cmdId": 17, "cmdFunc": 254, "dirDest": 1, "dirSrc": 1, "dest": 2,
            "needAck": True, "params": params})


def normalise(q: Any) -> dict:
    """Quota keys may carry a prefix ('20_1.soc') and numbers may be strings: return a flat {key: value}."""
    if isinstance(q, dict) and isinstance(q.get("quotaMap"), dict):
        q = q["quotaMap"]
    out: dict = {}
    for k, v in (q or {}).items() if isinstance(q, dict) else []:
        k = str(k).rsplit(".", 1)[-1].rsplit("-", 1)[-1]   # "20_1.soc" / "sysEnergyStreamReport-bpSoc"
        if isinstance(v, str):
            try:
                v = float(v)
            except ValueError:
                pass
        if isinstance(v, dict):
            out.update(normalise(v))
        else:
            out.setdefault(k, v)
    return out


_BAD_SOC = re.compile(r"backup|reserve|min|max|lim|cfg|set|charge|discharge|upper|lower|ctrl", re.I)


def _guess(q: dict, pattern: str, bad: re.Pattern | None = None, positive: bool = False) -> float | None:
    rx = re.compile(pattern, re.I)
    for k, v in q.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool) and rx.search(k) and not (bad and bad.search(k)) \
                and not (positive and v <= 0):
            return float(v)
    return None


def _first(q: dict, *keys: str, positive: bool = False) -> float | None:
    for k in keys:
        v = q.get(k)
        if isinstance(v, (int, float)) and not (positive and v <= 0):
            return float(v)
    return None


class EcoFlowStreamDriver:
    def __init__(self, config: DeviceConfig, session, hub=None) -> None:
        self.config = config
        c = config.extra["connection"]
        self.sn = c["sn"]
        self.api = EcoFlowApi(session, c.get("region", "global"), c["access_key"], c["secret_key"])
        self.last_quota: dict = {}
        self.last_error: str | None = None
        self._cache: tuple[float, Actual] | None = None  # the cloud API is rate limited: poll at most every 30 s
        self.hub = hub                  # shared live (MQTT) connection for this account, or None
        self._state: dict = {}          # last full reading with live updates merged in
        self._pushed = 0.0              # when a live update last arrived
        self._polled = 0.0              # when the full reading was last fetched

    async def close(self) -> None:
        if self.hub is not None:
            self.hub.unregister(self.sn, self.merge_push)

    def merge_push(self, flat: dict) -> None:
        """A live update arrived: fold it into the current reading (called on the event loop)."""
        if not self._state:
            return                       # wait for the first full reading so nothing is missing
        self._state.update(flat)
        self.last_quota = dict(self._state)
        self._pushed = time.monotonic()

    @staticmethod
    def _decode(q: dict, min_soc: float | None = None) -> Actual:
        reserve = _first(q, "backupReverseSoc")
        soc = next((float(q[k]) for k in ("f32ShowSoc", "soc", "cmsBattSoc") if isinstance(q.get(k), (int, float)) and q[k] > 0), None)
        if soc is None:
            soc = _guess(q, r"soc", _BAD_SOC, positive=True)
        # Newer STREAM models (e.g. the AC 5000) report sysEnergyStreamReport-bpSoc / bpPwr / gridPwr / pvPwr / loadPwr.
        if soc is None:
            soc = _first(q, "bpSoc", positive=True)
        power = _first(q, "powGetBpCms", "bpPwr")
        if power is None:
            power = _guess(q, r"(bp|batt|bat).*(pow|pwr|watt)|(pow|pwr).*(bp|batt)", re.compile(r"cfg|set|max|lim|volt|temp", re.I))
        return Actual(
            online=True,
            # Ultra / Ultra X report cmsBattSoc as 0 and publish the real value as soc / f32ShowSoc.
            soc=soc,
            power_w=power,  # EcoFlow: discharge negative, charge positive
            # Models without backupReverseSoc (AC 5000) are read-only and always run their own logic, so report Auto.
            # Hold = the backup reserve sits at (or above) the charge level, so the battery will not discharge.
            mode=Mode.HOLD if reserve is not None and (reserve >= HOLD_RESERVE or (
                soc is not None and reserve >= soc - 1.5 and reserve > (min_soc or 0) + 2)) else Mode.AUTO,
            grid_w=_first(q, "powGetSysGrid", "gridPwr", "gridConnectionPower"),
            pv_w=_first(q, "powGetPvSum", "pvPwr"), load_w=_first(q, "powGetSysLoad", "loadPwr"),
        )

    async def read(self) -> Actual:
        now = time.monotonic()
        if self.hub is not None:
            self.hub.register(self.sn, self.merge_push)   # no-op once registered; retries a failed connection later
        if self._pushed and now - self._pushed < 120 and now - self._polled < 300 and self._state:
            return self._decode(self._state, self.config.caps.min_soc)             # live data is flowing: no need to poll
        if self._cache and now - self._cache[0] < 30:
            return self._cache[1]
        try:
            q = normalise(await self.api.quota(self.sn))
            self._state, self._polled, self._pushed = dict(q), time.monotonic(), 0.0
            self.last_quota, self.last_error = q, None
            actual = self._decode(q, self.config.caps.min_soc)
            if actual.soc is None and q:
                _LOGGER.warning("%s: EcoFlow returned no charge level; readings it did return: %s",
                                self.config.device_id, ", ".join(sorted(q)[:60]))
            self._cache = (time.monotonic(), actual)
            return actual
        except EcoFlowError as err:
            self.last_error = str(err)
            _LOGGER.warning("%s: EcoFlow read failed: %s", self.config.device_id, err)
            return Actual(online=False)

    async def probe(self) -> dict:
        q = normalise(await self.api.quota(self.sn))
        self.last_quota = q
        a = self._decode(q)
        return {"soc": a.soc, "power_w": a.power_w, "mode": a.mode.value if a.mode else None,
                "grid_w": a.grid_w, "pv_w": a.pv_w, "load_w": a.load_w, "model": q.get("productName") or None,
                "serial": self.sn, "capacity_wh": int(q["cmsBattFullEnergy"]) if isinstance(q.get("cmsBattFullEnergy"), (int, float)) and q["cmsBattFullEnergy"] > 0 else None,
                "max_charge_w": None, "max_discharge_w": None, "min_soc": None,
                "reserve": q.get("backupReverseSoc")}

    async def apply(self, desired: Desired) -> None:
        self._cache, self._pushed = None, 0.0   # next read must come from the cloud and show the effect of this command
        if self.last_quota and "backupReverseSoc" not in self.last_quota:
            # A model that reports different settings (e.g. AC 5000): its command format is not known, so never guess.
            if desired.mode is Mode.AUTO:
                return
            raise ValueError("this EcoFlow model's settings are not known to HomePower: read-only for now")
        if desired.mode is Mode.HOLD:
            # Raise the reserve to where the battery is now: it stops discharging, and is not told to charge from the grid
            # (a reserve above the charge level could make it top up at whatever the price is).
            soc = self._decode(self._state or self.last_quota).soc
            if soc is None:
                raise ValueError("cannot hold: the battery's charge level is not known yet")
            await self.api.command(self.sn, {"cfgBackupReverseSoc": int(max(3, min(HOLD_RESERVE, soc)))})
        elif desired.mode is Mode.AUTO:
            await self.api.command(self.sn, {"cfgEnergyStrategyOperateMode": {"operateSelfPoweredOpen": True}})
            await self.api.command(self.sn, {"cfgBackupReverseSoc": max(3, self.config.caps.min_soc)})
        else:
            raise ValueError(f"EcoFlow STREAM cannot {desired.mode.value} through the API")
