"""EcoFlow STREAM family over EcoFlow's own developer (IoT Open) API.

You need an Access Key and Secret Key from https://developer.ecoflow.com/ (your EcoFlow account).
Hosts: api-e.ecoflow.com (Europe) and api.ecoflow.com (global/US). Requests are HMAC-SHA256 signed.

These batteries expose no charge/discharge power setpoint through this API, only the operating
mode and the backup reserve, so HomePower offers Auto, Hold and Charge: Hold raises the backup reserve to the
charge level so the battery will not discharge (solar can still charge it); Charge raises it to the target so the
battery tops itself up from the grid at its own rate; Auto restores your normal reserve.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import replace
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
RAISED_RESERVE = 85   # a reserve at or above this, with no saved original, is taken to be one HomePower raised (lower ones are the user's own)
HANDBACK_QUIET_S = 600  # after a hand-back the cloud may still show the raised reserve for a while: no second hand-back in that time
HOLD_RESERVE = 95  # STREAM accepts a backup reserve of 3-95 %; Hold raises the reserve to the current charge level
SELF_MODE = "SELF_CONSUMPTION"   # STREAM 5000 series operating modes: SELF_CONSUMPTION, AI_MODE, TIMER_TASK, POU_MODE


class EcoFlowError(Exception):
    pass


BACKOFF_S = 300            # after a rate limit or a refusal, leave EcoFlow alone for this long
REFUSED_CODES = ("1006",)  # the model does not accept these commands through the developer API
_SETTINGISH = re.compile(r"backup|reserve|cfg|mode|onoff|switch|limit|timer|task|enable|strateg|schedul|feed", re.I)
_TELEMETRY = re.compile(r"pwr|power|watt|volt|temp|energy|cur(rent)?$|freq|cycle|^bpSoc$|^soc$|f32|show|time$|stamp", re.I)


def setting_changes(old: dict, new: dict) -> list[dict]:
    """Settings that differ between two readings (what someone changed in the EcoFlow app): booleans, text and
    anything named like a setting; live power/voltage/charge figures are ignored."""
    out = []
    for k in sorted(set(old) | set(new)):
        a, b = old.get(k), new.get(k)
        if a == b or k not in old:
            continue                      # unchanged, or a key we had never seen (not a change)
        flag = isinstance(b, (bool, str)) or isinstance(a, (bool, str))
        if (flag or _SETTINGISH.search(k)) and not (_TELEMETRY.search(k) and not flag):
            out.append({"key": k, "from": a, "to": b})
    return out


def _is_rate_limit(err: Exception) -> bool:
    text = str(err).lower()
    return getattr(err, "code", "") == "429" or "rate limit" in text or "too many" in text or "too frequent" in text


def backs_off(err: Exception) -> bool:
    return getattr(err, "code", "") in REFUSED_CODES or _is_rate_limit(err)


def friendly_error(err: Exception) -> str:
    if getattr(err, "code", "") in REFUSED_CODES:
        return ("EcoFlow refused the command (error 1006): this model does not accept it through the developer API. "
                "HomePower will not retry for a few minutes.")
    if _is_rate_limit(err):
        return "EcoFlow says requests are too frequent (rate limit): HomePower is backing off for a few minutes."
    return str(err)


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
                status = resp.status
                data = await resp.json(content_type=None)
        except Exception as err:  # noqa: BLE001 - network / JSON problems all mean "can't reach"
            raise EcoFlowError(f"cannot reach EcoFlow: {err!r}") from err
        if status == 429:
            err = EcoFlowError("EcoFlow error 429: too many requests (rate limit)")
            err.code = "429"
            raise err
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

    async def command(self, sn: str, params: dict, cmd_id: int = 17) -> None:
        await self._call("PUT", "/iot-open/sign/device/quota", body={
            "sn": sn, "cmdId": cmd_id, "cmdFunc": 254, "dirDest": 1, "dirSrc": 1, "dest": 2,
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
    _blocked_until = 0.0     # class defaults so a driver built without __init__ (tests) still works
    _orig_val = None
    _on_orig = None
    _handed_at = -1e9


    def __init__(self, config: DeviceConfig, session, hub=None, orig: dict | None = None, on_orig=None) -> None:
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
        self._orig_val: dict | None = dict(orig) if orig else None   # STREAM 5000: reserve switch/level and mode before HomePower changed them
        self._on_orig = on_orig         # called with the new value whenever it changes, so it survives a restart
        self.changes: list[dict] = []   # recent settings changes seen between readings (made in the EcoFlow app, or by us)
        self._blocked_until = 0.0       # backing off after a rate limit or a refusal

    @property
    def _orig(self) -> dict | None:
        return self._orig_val

    @_orig.setter
    def _orig(self, value: dict | None) -> None:
        self._orig_val = value
        if self._on_orig is not None:
            try:
                self._on_orig(value)
            except Exception:  # noqa: BLE001 - failing to save must never stop a command
                _LOGGER.debug("could not save the EcoFlow originals", exc_info=True)

    def _just_handed_back(self) -> bool:
        return time.monotonic() - self._handed_at < HANDBACK_QUIET_S

    def _note_changes(self, before: dict, after: dict) -> None:
        found = setting_changes(before, after) if before else []
        if found:
            self.changes = (self.changes + [{"at": time.strftime("%Y-%m-%d %H:%M:%S"), **c} for c in found])[-40:]
            _LOGGER.info("%s: EcoFlow settings changed: %s", self.config.device_id,
                         "; ".join(f"{c['key']} {c['from']!r} -> {c['to']!r}" for c in found))

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
    def _mode(reserve: float | None, soc: float | None, min_soc: float | None) -> Mode:
        if reserve is None or reserve <= (min_soc or 0) + 3.5:
            # A reserve at its normal floor is just the battery's own control. EcoFlow keeps the reserve at least 3 points
            # above the discharge limit (a lower one is raised), so a resting battery can read a little above min_soc.
            return Mode.AUTO
        if soc is not None and reserve > soc + 1.5:
            return Mode.CHARGE
        if reserve >= HOLD_RESERVE or (soc is not None and reserve >= soc - 1.5):
            return Mode.HOLD
        return Mode.AUTO

    @staticmethod
    def _decode(q: dict, min_soc: float | None = None) -> Actual:
        reserve = _first(q, "backupReverseSoc")
        if reserve is None and q.get("backupSocOnOff"):
            reserve = _first(q, "backupSoc")      # STREAM 5000 series: the same reserve, behind an on/off switch
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
            # Charge = the reserve is above the charge level, so the battery tops itself up from the grid;
            # Hold = the reserve sits at the charge level, so it will not discharge.
            mode=EcoFlowStreamDriver._mode(reserve, soc, min_soc),
            grid_w=_first(q, "powGetSysGrid", "gridPwr", "gridConnectionPower"),
            pv_w=_first(q, "powGetPvSum", "pvPwr"), load_w=_first(q, "powGetSysLoad", "loadPwr"),
        )

    def _held_at_floor(self, actual: Actual) -> Actual:
        """A battery HomePower is holding that sits at its own floor reads as Auto (its reserve cannot go lower), but it cannot
        discharge, so it is held. Reporting that stops a Hold from being re-sent forever."""
        floor = (self.config.caps.min_soc or 0) + 3.5
        if self._orig is not None and actual.mode is Mode.AUTO and actual.soc is not None and actual.soc <= floor:
            return replace(actual, mode=Mode.HOLD)
        return actual

    async def read(self) -> Actual:
        now = time.monotonic()
        if self.hub is not None:
            self.hub.register(self.sn, self.merge_push)   # no-op once registered; retries a failed connection later
        if self._pushed and now - self._pushed < 120 and now - self._polled < 300 and self._state:
            return self._held_at_floor(self._decode(self._state, self.config.caps.min_soc))   # live data is flowing: no need to poll
        if self._cache and now - self._cache[0] < 30:
            return self._cache[1]
        if now < self._blocked_until:
            return self._cache[1] if self._cache else Actual(online=False)      # backing off: do not ask again yet
        try:
            q = normalise(await self.api.quota(self.sn))
            self._note_changes(self._state, q)
            self._state, self._polled, self._pushed = dict(q), time.monotonic(), 0.0
            self.last_quota, self.last_error = q, None
            actual = self._held_at_floor(self._decode(q, self.config.caps.min_soc))
            if actual.soc is None and q:
                _LOGGER.warning("%s: EcoFlow returned no charge level; readings it did return: %s",
                                self.config.device_id, ", ".join(sorted(q)[:60]))
            self._cache = (time.monotonic(), actual)
            return actual
        except EcoFlowError as err:
            self.last_error = friendly_error(err)
            if backs_off(err):
                self._blocked_until = time.monotonic() + BACKOFF_S
            _LOGGER.warning("%s: EcoFlow read failed: %s", self.config.device_id, self.last_error)
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

    @property
    def _is5000(self) -> bool:
        q = self.last_quota
        return "backupReverseSoc" not in q and ("backupSocOnOff" in q or "backupSoc" in q)

    async def _apply_5000(self, desired: Desired) -> None:
        """STREAM 5000 series (EcoFlow's "STREAM 5000 Series" developer document): the backup reserve is a switch plus a level
        under bpBackupConfig, and the operating mode is workMode. Hold/Charge raise the reserve exactly as on the Ultra X;
        the unit is put in self-consumption while managed (a timer-task or AI mode would run its own plan) and its own
        settings are put back on Auto."""
        q = self._state or self.last_quota
        soc = self._decode(q).soc
        if desired.mode is Mode.AUTO:
            if self._orig is None:
                level = q.get("backupSoc")
                if (q.get("backupSocOnOff") and isinstance(level, (int, float)) and level >= RAISED_RESERVE and not self._just_handed_back()
                        and self._decode(q, self.config.caps.min_soc).mode in (Mode.HOLD, Mode.CHARGE)):
                    await self.api.command(self.sn, {"bpBackupConfig": {"backupSocOnOff": False, "backupSoc": 0}}, cmd_id=38)  # restarted while held, originals lost
                return                          # otherwise it is the user's own reserve (or a stale reading): never touched
            o = self._orig                      # forgotten only once every command below has succeeded: a failed hand-back is retried with the same originals
            reserve = {"backupSocOnOff": True, "backupSoc": o["level"]} if o["on"] and o["level"] is not None else {"backupSocOnOff": False, "backupSoc": 0}
            await self.api.command(self.sn, {"bpBackupConfig": reserve}, cmd_id=38)
            if o["mode"] and o["mode"] != SELF_MODE:
                await self.api.command(self.sn, {"workMode": o["mode"]}, cmd_id=38)
            self._orig = None
            self._handed_at = time.monotonic()
            return
        if desired.mode not in (Mode.HOLD, Mode.CHARGE):
            raise ValueError(f"EcoFlow STREAM cannot {desired.mode.value} through the API")
        if soc is None:
            raise ValueError("cannot hold or charge: the battery's charge level is not known yet")
        if self._orig is None:
            level = q.get("backupSoc")
            self._orig = {"on": bool(q.get("backupSocOnOff")), "level": int(level) if isinstance(level, (int, float)) else None,
                          "mode": q.get("workMode") if isinstance(q.get("workMode"), str) else None}
        if isinstance(q.get("workMode"), str) and q["workMode"] != SELF_MODE:
            await self.api.command(self.sn, {"workMode": SELF_MODE}, cmd_id=38)
        target = soc
        if desired.mode is Mode.CHARGE:
            target = max(soc, desired.target_soc if desired.target_soc is not None else 100)
        await self.api.command(self.sn, {"bpBackupConfig": {"backupSocOnOff": True, "backupSoc": int(max(0, min(100, target)))}}, cmd_id=38)

    async def apply(self, desired: Desired) -> None:
        if time.monotonic() < self._blocked_until and desired.mode is not Mode.AUTO:
            raise ValueError(self.last_error or "EcoFlow is being given a rest after an error: trying again shortly")
        try:
            await self._apply(desired)
        except EcoFlowError as err:
            if backs_off(err):
                self._blocked_until = time.monotonic() + BACKOFF_S
            self.last_error = friendly_error(err)
            raise ValueError(self.last_error) from err

    async def _apply(self, desired: Desired) -> None:
        self._cache, self._pushed = None, 0.0   # next read must come from the cloud and show the effect of this command
        o = self._orig
        if self._is5000 or (not self.last_quota and isinstance(o, dict) and "on" in o):
            # (not read yet since a restart, but the remembered originals are a STREAM 5000's: hand back the 5000 way)
            return await self._apply_5000(desired)
        if not self.last_quota and not (isinstance(o, dict) and "reserve" in o):
            # Not read yet, so the model (and its command set) is unknown: never guess. Nothing of ours to hand back either.
            if desired.mode is Mode.AUTO:
                return
            raise ValueError("the battery has not been read yet: its model is not known, so no command was sent")
        if self.last_quota and "backupReverseSoc" not in self.last_quota:
            # A model that reports different settings (e.g. AC 5000): its command format is not known, so never guess.
            if desired.mode is Mode.AUTO:
                return
            raise ValueError("this EcoFlow model's settings are not known to HomePower: read-only for now")
        if desired.mode in (Mode.HOLD, Mode.CHARGE) and self._orig is None:
            # Remember the reserve the user had, so Auto puts exactly that back (not a made-up minimum). A reserve already
            # near the top was probably left raised by an earlier run: its original is unknown, so it is not trusted.
            cur = _first(self._state or self.last_quota, "backupReverseSoc")
            self._orig = {"reserve": int(cur) if cur is not None and cur < 90 else None}
        if desired.mode is Mode.HOLD:
            # Raise the reserve to where the battery is now: it stops discharging, and is not told to charge from the grid
            # (a reserve above the charge level could make it top up at whatever the price is).
            soc = self._decode(self._state or self.last_quota).soc
            if soc is None:
                raise ValueError("cannot hold: the battery's charge level is not known yet")
            await self.api.command(self.sn, {"cfgBackupReverseSoc": int(max(3, min(HOLD_RESERVE, soc)))})
        elif desired.mode is Mode.AUTO:
            o = self._orig                        # forgotten only after both commands succeed (a failed hand-back is retried with the same originals)
            if o is None:
                cur = _first(self._state or self.last_quota, "backupReverseSoc")
                if (cur is None or cur < RAISED_RESERVE or self._just_handed_back()
                        or self._decode(self._state or self.last_quota, self.config.caps.min_soc).mode is Mode.AUTO):
                    return                        # nothing of ours to hand back: the user's own reserve (or a stale reading) is left alone
            saved = o.get("reserve") if o else None
            await self.api.command(self.sn, {"cfgEnergyStrategyOperateMode": {"operateSelfPoweredOpen": True}})
            await self.api.command(self.sn, {"cfgBackupReverseSoc": int(saved) if saved is not None else max(3, self.config.caps.min_soc)})
            self._orig = None
            self._handed_at = time.monotonic()
        elif desired.mode is Mode.CHARGE:
            # A reserve above the charge level makes the battery top itself up from the grid, at its own rate (the power
            # cannot be set). It stops when it reaches the reserve, so the reserve is the target (95 % at most).
            soc = self._decode(self._state or self.last_quota).soc
            target = desired.target_soc if desired.target_soc is not None else HOLD_RESERVE
            if soc is not None and soc >= target:
                target = soc                      # already there: hold rather than top up further
            await self.api.command(self.sn, {"cfgBackupReverseSoc": int(max(3, min(HOLD_RESERVE, target)))})
        else:
            raise ValueError(f"EcoFlow STREAM cannot {desired.mode.value} through the API")
