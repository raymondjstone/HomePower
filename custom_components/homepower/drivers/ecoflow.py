"""EcoFlow STREAM family over EcoFlow's own developer (IoT Open) API.

You need an Access Key and Secret Key from https://developer.ecoflow.com/ (your EcoFlow account).
Hosts: api-e.ecoflow.com (Europe) and api.ecoflow.com (global/US). Requests are HMAC-SHA256 signed.

These batteries expose no charge/discharge power setpoint through this API, only the operating
mode and the backup reserve, so HomePower offers Auto, Hold and Charge: Hold raises the backup reserve to the
charge level so the battery will not discharge (solar can still charge it); Charge raises it to the target so the
battery tops itself up from the grid at its own rate; Auto puts back exactly the reserve (and, on the 5000 series, the
operating mode) the battery had before HomePower first changed it. Those are read from the battery just before that first
change and saved, so a restart does not lose them. When they are not known, Auto leaves the battery as it is: it never
writes a made-up value. The energy strategy of the Ultra / Ultra X is never changed.
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
WROTE_KEPT = 8   # reserve levels HomePower wrote during one takeover, kept to recognise them at hand-back
SELF_MODE = "SELF_CONSUMPTION"   # STREAM 5000 series operating modes: SELF_CONSUMPTION, AI_MODE, TIMER_TASK, POU_MODE


class EcoFlowError(Exception):
    pass


PV_SPIKE_MIN_W = 500        # a solar reading below this is never second-guessed (see _screen_pv)
PV_LIKE_CHARGE = 0.2        # "equal to the charge power" means within 20 %
PV_JUMP = 0.5               # and at least double the previous reading
PV_MEMORY_S = 120           # the previous reading counts if it is no older than this
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
    _warned_unknown = False   # logged once that a hand-back was skipped (original unknown)
    _pv_prev = None           # (time, pv_w) of the last solar reading accepted; see _screen_pv
    _pv_run = 0
    pv_ignored = 0            # solar readings held back because they looked like grid charging


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

    async def _fresh(self) -> dict:
        """The battery's settings read now, just before HomePower first changes them (the cached reading can be 30 s old,
        or older while live updates flow). Anything the fresh reading leaves out is taken from the cached one."""
        q = normalise(await self.api.quota(self.sn))
        if q:
            self._state = {**(self._state or self.last_quota), **q}
            self.last_quota = dict(self._state)
        return self._state or self.last_quota

    async def _write_reserve(self, params: dict, level: int, cmd_id: int = 17) -> None:
        """Send a reserve change of HomePower's own, noting the level first (and saving it with the originals), so hand-back
        can tell HomePower's raise from a change someone made in the EcoFlow app meanwhile. Noted before sending: a command
        that fails after it took effect still counts as ours."""
        if self._orig is not None:
            wrote = [w for w in self._orig.get("wrote", []) if w != level][-(WROTE_KEPT - 1):] + [int(level)]
            self._orig = {**self._orig, "wrote": wrote}
        await self.api.command(self.sn, params, cmd_id=cmd_id)

    def _ours(self, level: float | None, q: dict | None = None) -> bool:
        """True when a reserve reading is one HomePower wrote during this takeover. The battery keeps a reserve between its
        discharge limit + 3 and its charge limit (95 at most on the Ultra), so a value HomePower wrote may show clamped into
        that range, or rounded: within a point of what was written, or of that clamped value, still counts. (Mistaking a
        clamp for the user's own change would leave the battery stuck at a raised reserve.)"""
        if level is None:
            return False
        q = q or self._state or self.last_quota
        lo_soc = q.get("cmsMinDsgSoc") if isinstance(q.get("cmsMinDsgSoc"), (int, float)) else self.config.caps.min_soc
        hi_soc = q.get("cmsMaxChgSoc") if isinstance(q.get("cmsMaxChgSoc"), (int, float)) and q.get("cmsMaxChgSoc") > 0 else 100
        lo, hi = max(3, (lo_soc or 0) + 3), min(HOLD_RESERVE if "backupReverseSoc" in q else 100, hi_soc)
        return any(abs(level - w) <= 1 or abs(level - min(max(w, lo), max(lo, hi))) <= 1
                   for w in (self._orig or {}).get("wrote", []))

    def _changed_by_user(self, what: str, now) -> None:
        _LOGGER.info("%s: the %s was changed (to %s) while HomePower held the battery, so the new setting is kept on hand-back",
                     self.config.device_id, what, now)

    def _unknown_original(self, what: str) -> None:
        if not self._warned_unknown:
            self._warned_unknown = True
            _LOGGER.warning("%s: HomePower does not know the %s this battery had before it was changed, so it is left as it is: "
                            "set it in the EcoFlow app if it should be different", self.config.device_id, what)

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
            pv_strings=EcoFlowStreamDriver._strings(q),
        )

    @staticmethod
    def _strings(q: dict) -> tuple:
        """Each solar input's (volts, amps): plugInInfoPvVol / Amp for input 1, plugInInfoPv2Vol ... for 2-4 (named as the
        Ecoflow-Cloud integration names them). () when the battery sends none of them; missing inputs read as (0, 0)."""
        out, seen = [], False
        for n in ("", "2", "3", "4"):
            v, a = q.get(f"plugInInfoPv{n}Vol"), q.get(f"plugInInfoPv{n}Amp")
            try:
                v, a = float(v), float(a)
                if v != v or a != a or abs(v) == float("inf") or abs(a) == float("inf"):
                    raise ValueError
                seen = True
            except (TypeError, ValueError):
                v, a = 0.0, 0.0
            out.append((max(0.0, v), max(0.0, a)))
        return tuple(out + [(0.0, 0.0)] * 4) if seen else ()

    def _held_at_floor(self, actual: Actual) -> Actual:
        """A battery HomePower is holding that sits at its own floor reads as Auto (its reserve cannot go lower), but it cannot
        discharge, so it is held. Reporting that stops a Hold from being re-sent forever."""
        floor = (self.config.caps.min_soc or 0) + 3.5
        if self._orig is not None and actual.mode is Mode.AUTO and actual.soc is not None and actual.soc <= floor:
            return replace(actual, mode=Mode.HOLD)
        return actual

    def _screen_pv(self, a: Actual) -> Actual:
        """EcoFlow's cloud sometimes reports the grid-charge power as solar ("powGetPvSum") with the grid reading at exactly 0, then
        the true values on the next sample, over and over while the battery charges from the grid (tolwi/hassio-ecoflow-cloud
        issues 786 and 679: the wrong values arrive from EcoFlow already). Taken at face value that makes bought charge look like
        free solar. A single reading cannot prove it (real sun charging looks the same), so a reading is only held back when it
        has every one of the reported marks: large, equal to the battery's own charge power, the grid at exactly 0, and a jump from
        a much lower reading seen in the last two minutes. The last good value stands in, for at most two readings in a row, so
        real solar that really did jump is believed from the third reading on."""
        now, pv, bat, grid, prev = time.monotonic(), a.pv_w, a.power_w, a.grid_w, self._pv_prev
        if (pv is not None and pv >= PV_SPIKE_MIN_W and bat is not None and bat > 0 and abs(pv - bat) <= PV_LIKE_CHARGE * bat
                and grid is not None and grid == 0 and prev is not None and now - prev[0] <= PV_MEMORY_S and prev[1] < PV_JUMP * pv
                and self._pv_run < 2):
            self._pv_run += 1
            self.pv_ignored += 1
            return replace(a, pv_w=prev[1])
        self._pv_run = 0
        if pv is not None:
            self._pv_prev = (now, pv)
        return a

    async def read(self) -> Actual:
        now = time.monotonic()
        if self.hub is not None:
            self.hub.register(self.sn, self.merge_push)   # no-op once registered; retries a failed connection later
        if self._pushed and now - self._pushed < 120 and now - self._polled < 300 and self._state:
            return self._held_at_floor(self._screen_pv(self._decode(self._state, self.config.caps.min_soc)))   # live data is flowing: no need to poll
        if self._cache and now - self._cache[0] < 30:
            return self._cache[1]
        if now < self._blocked_until:
            return self._cache[1] if self._cache else Actual(online=False)      # backing off: do not ask again yet
        try:
            q = normalise(await self.api.quota(self.sn))
            self._note_changes(self._state, q)
            self._state, self._polled, self._pushed = dict(q), time.monotonic(), 0.0
            self.last_quota, self.last_error = q, None
            actual = self._held_at_floor(self._screen_pv(self._decode(q, self.config.caps.min_soc)))
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
                    self._unknown_original("backup reserve and operating mode")    # looks raised, but nothing was saved
                return                          # the user's own settings (or a stale reading): never touched
            o = self._orig                      # forgotten only once every command below has succeeded: a failed hand-back is retried with the same originals
            if o.get("on") is None and "on" in o:
                reserve = None                  # whether it was switched on is not known: left alone
                self._unknown_original("backup reserve")
            elif not o.get("on"):
                reserve = {"backupSocOnOff": False, "backupSoc": 0}                 # it was switched off: off again
            elif isinstance(o.get("level"), int):
                reserve = {"backupSocOnOff": True, "backupSoc": o["level"]}
            else:
                reserve = None                  # on, at a level that could not be read: the level is left alone
                self._unknown_original("backup reserve level")
            mode = o.get("mode") if o.get("mode") and o["mode"] != SELF_MODE else None
            if "wrote" in o:                    # (an entry from an older version has no record of what HomePower wrote)
                now = await self._fresh()       # what the battery has now: changed in the app since HomePower raised it?
                on, level, cur_mode = now.get("backupSocOnOff"), now.get("backupSoc"), now.get("workMode")
                as_found = bool(on) == bool(o.get("on")) and (not on or level == o.get("level"))
                if reserve is not None and not as_found and not (on and self._ours(level if isinstance(level, (int, float)) else None, now)):
                    self._changed_by_user("backup reserve", f"{'on' if on else 'off'} {level}" if on else "off")
                    reserve = None
                if mode and isinstance(cur_mode, str) and cur_mode not in (SELF_MODE, mode):
                    self._changed_by_user("operating mode", cur_mode)
                    mode = None
            if reserve is not None:
                await self.api.command(self.sn, {"bpBackupConfig": reserve}, cmd_id=38)
            if mode:
                await self.api.command(self.sn, {"workMode": mode}, cmd_id=38)
            self._orig = None
            self._handed_at = time.monotonic()
            return
        if desired.mode not in (Mode.HOLD, Mode.CHARGE):
            raise ValueError(f"EcoFlow STREAM cannot {desired.mode.value} through the API")
        if soc is None:
            raise ValueError("cannot hold or charge: the battery's charge level is not known yet")
        if self._orig is None:              # taking control: read what the user has now, before the first write, and save it
            q = await self._fresh()
            soc = self._decode(q).soc if self._decode(q).soc is not None else soc     # the fresh charge level for the Hold level too
            level = q.get("backupSoc")
            # "on" is None when the reading did not include the switch (EcoFlow's fallback request leaves it out): unknown, so
            # hand-back leaves the reserve alone rather than switching it off
            self._orig = {"on": bool(q["backupSocOnOff"]) if "backupSocOnOff" in q else None,
                          "level": int(level) if isinstance(level, (int, float)) else None,
                          "mode": q.get("workMode") if isinstance(q.get("workMode"), str) else None, "wrote": []}
            self._warned_unknown = False
        if isinstance(q.get("workMode"), str) and q["workMode"] != SELF_MODE:
            await self.api.command(self.sn, {"workMode": SELF_MODE}, cmd_id=38)
        target = soc
        if desired.mode is Mode.CHARGE:
            target = max(soc, desired.target_soc if desired.target_soc is not None else 100)
        level = int(max(0, min(100, target)))
        await self._write_reserve({"bpBackupConfig": {"backupSocOnOff": True, "backupSoc": level}}, level, cmd_id=38)

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
            # Taking control: read the reserve the user has now, before the first write, and save it, so Auto puts exactly
            # that back. (Even a high one: putting back what was there is leaving it as it was found.)
            cur = _first(await self._fresh(), "backupReverseSoc")
            if cur is None:
                raise ValueError("could not read the battery's backup reserve, so nothing was changed (it is needed to hand the battery back)")
            self._orig = {"reserve": int(cur), "wrote": []}
            self._warned_unknown = False
        if desired.mode is Mode.HOLD:
            # Raise the reserve to where the battery is now: it stops discharging, and is not told to charge from the grid
            # (a reserve above the charge level could make it top up at whatever the price is).
            soc = self._decode(self._state or self.last_quota).soc
            if soc is None:
                raise ValueError("cannot hold: the battery's charge level is not known yet")
            level = int(max(3, min(HOLD_RESERVE, soc)))
            await self._write_reserve({"cfgBackupReverseSoc": level}, level)
        elif desired.mode is Mode.AUTO:
            o = self._orig                        # forgotten only after the command succeeds (a failed hand-back is retried with the same originals)
            saved = o.get("reserve") if o else None
            if saved is None:
                cur = _first(self._state or self.last_quota, "backupReverseSoc")
                if not (cur is None or cur < RAISED_RESERVE or self._just_handed_back()
                        or self._decode(self._state or self.last_quota, self.config.caps.min_soc).mode is Mode.AUTO):
                    self._unknown_original("backup reserve")    # looks raised, but nothing was saved: never a made-up value
                if o is not None:
                    self._orig = None             # saved by an older version without the reserve: nothing to put back
                return
            if "wrote" in o:                      # (an entry from an older version has no record of what HomePower wrote)
                q = await self._fresh()
                cur = _first(q, "backupReverseSoc")
                if cur is not None and abs(cur - saved) > 1 and not self._ours(cur, q):
                    # changed in the EcoFlow app while HomePower held it: that newer choice stays, not the older saved one
                    self._changed_by_user("backup reserve", int(cur))
                    self._orig = None
                    self._handed_at = time.monotonic()
                    return
            # Only the reserve is put back: Hold and Charge never change the energy strategy, so the user's own (AI, time-of-use,
            # scheduled...) is still in place and is not touched.
            await self.api.command(self.sn, {"cfgBackupReverseSoc": int(saved)})
            self._orig = None
            self._handed_at = time.monotonic()
        elif desired.mode is Mode.CHARGE:
            # A reserve above the charge level makes the battery top itself up from the grid, at its own rate (the power
            # cannot be set). It stops when it reaches the reserve, so the reserve is the target (95 % at most).
            soc = self._decode(self._state or self.last_quota).soc
            target = desired.target_soc if desired.target_soc is not None else HOLD_RESERVE
            if soc is not None and soc >= target:
                target = soc                      # already there: hold rather than top up further
            level = int(max(3, min(HOLD_RESERVE, target)))
            await self._write_reserve({"cfgBackupReverseSoc": level}, level)
        else:
            raise ValueError(f"EcoFlow STREAM cannot {desired.mode.value} through the API")
