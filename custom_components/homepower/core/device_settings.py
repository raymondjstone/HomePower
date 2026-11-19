"""The settings of a battery that can be seen and changed from the panel, the way its vendor's own Home Assistant integration offers them. HA-free.

Each setting is described with its current value, its limits and (when it cannot be changed right now) the reason. A change is checked
here against the same ranges and rules the vendor integrations use before anything is sent:

  * Anker Solarbank (anker-charging/ha-anker-solix-official): work mode 10064 (only the modes the battery's own mask 0x8006 allows),
    charge limit 60000 (80-100), discharge limit 60001 (0-20), backup reserve 60002 (0-100, only while the backup reserve is on, kept
    between the discharge limit and the charge limit). Backup reserve on/off 60003 is read-only there too: it is switched in the app.
  * Anker X1 (afewyards/anker-x1-ha): work mode 10064, export / import limit mode 10074 / 10077 and value 10075 / 10078 (0-30000 W in
    fixed mode, 0-100 % in percent mode), comms watchdog 10080 (30-3600 s).
  * EcoFlow STREAM over the developer API (tolwi/hassio-ecoflow-cloud, public/stream_ac.py): AC 1 / AC 2 outputs, feed-in control,
    the self-powered and AI operating modes, and the backup reserve level (3-95, at least 3 above the discharge limit).

Anything HomePower itself drives on a battery (the work mode of an Anker, the reserve and self-powered mode of an EcoFlow) is shown but
locked while HomePower controls that battery: it would only be put back on the next cycle.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------------------------------------------- Anker
X1_WORK_MODES = {0: "Self-consumption", 1: "Time-of-use", 2: "Backup only", 4: "User-defined", 5: "Socket aggregation",
                 16: "Dynamic tariff (set in the app)", 20: "App-managed"}
# Solarbank work mode -> (bit in the battery's own mode mask 0x8006, name). Third-party control (3) is HomePower's own and not offered.
SB_WORK_MODES = {0: (0, "Self-consumption"), 1: (1, "Time-of-use"), 4: (2, "Custom mode"), 6: (3, "Smart / AI mode"),
                 5: (4, "Socket overlay mode"), 7: (6, "Dynamic pricing")}
LIMIT_MODES = {0: "Disabled", 1: "Percentage of rated power", 2: "Fixed power"}

X1_REGISTERS = (10064, 10074, 10075, 10076, 10077, 10078, 10079, 10080)
SB_REGISTERS = (10064, 32774, 32775, 60000, 60001, 60002, 60003)


def anker_registers(x1: bool) -> tuple[int, ...]:
    return X1_REGISTERS if x1 else SB_REGISTERS


def _s(key, label, kind, group, value, *, unit="", lo=None, hi=None, step=None, options=None, hint="", locked="") -> dict:
    return {"key": key, "label": label, "kind": kind, "group": group, "value": value, "unit": unit, "min": lo, "max": hi, "step": step,
            "options": [{"value": v, "label": t} for v, t in (options or {}).items()], "hint": hint, "locked": locked}


def _on(v) -> bool:
    """A switch value as EcoFlow sends it: a real boolean, a number, or (rarely) text."""
    return v.strip().lower() in ("true", "1", "on", "yes") if isinstance(v, str) else bool(v)


def _int(v, default: int) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError, OverflowError):
        return default


def _u32(regs: dict, addr: int, order: str | None) -> int | None:
    a, b = regs.get(addr), regs.get(addr + 1)
    if a is None or b is None or order is None:
        return None
    return (a << 16 | b) if order == "big" else (b << 16 | a)


def anker_settings(x1: bool, regs: dict[int, int], order: str | None, controlled: bool) -> list[dict]:
    """The Anker settings this battery answered for. regs: register address -> value."""
    out: list[dict] = []
    mode = regs.get(10064)
    lock_mode = "HomePower is managing this battery's mode. Turn on \"monitor only\" in Edit to set it yourself." if controlled else ""
    if x1:
        if mode is not None:
            opts = dict(X1_WORK_MODES)
            if mode not in opts:
                opts[mode] = f"{mode} (not in Anker's list)"
            out.append(_s("work_mode", "Work mode", "select", "Controls", mode, options=opts, locked=lock_mode,
                          hint="Dynamic tariff and App-managed are what the battery reports while the Anker app runs its own schedule; writing them does not restart that schedule."))
        for key, label, mreg in (("export_limit", "Export limit", 10074), ("import_limit", "Import limit", 10077)):
            m = regs.get(mreg)
            if m is None:
                continue
            opts = dict(LIMIT_MODES)
            if m not in opts:
                opts[m] = f"{m} (not in Anker's list)"
            out.append(_s(key + "_mode", label + " mode", "select", "Configuration", m, options=opts,
                          hint="Changing the mode does not convert the limit value: set the value afterwards. HomePower sets this itself when you give it an import or export limit in Settings; a limit it did not set is only ever tightened."))
            v = _u32(regs, mreg + 1, order)
            if v is not None:
                pct = m == 1
                out.append(_s(key, label, "number", "Configuration", v, unit="%" if pct else "W", lo=0, hi=100 if pct else 30000, step=1 if pct else 100,
                              hint="Percent of the inverter's rated power in percentage mode, otherwise watts." + ("" if m else " The limit is off while the mode is Disabled.")))
        wd = regs.get(10080)
        if wd is not None:
            out.append(_s("watchdog", "Comms watchdog", "number", "Configuration", wd, unit="s", lo=30, hi=3600, step=1,
                          hint="If nothing talks to the battery for this long it goes back to the app's mode. HomePower sets it when it takes control."))
        return out
    # Solarbank family
    mask, caps = regs.get(32774), regs.get(32775)
    if mode is not None and mask:
        opts = {v: name for v, (bit, name) in SB_WORK_MODES.items() if mask >> bit & 1}
        if mode not in opts:
            opts[mode] = {3: "Third-party control (HomePower)"}.get(mode, f"{mode} (not in Anker's list)")
        out.append(_s("work_mode", "Operating mode", "select", "Controls", mode, options=opts, locked=lock_mode,
                      hint="Only the modes this battery supports are listed. Third-party control is what HomePower uses to drive the battery."))
    backup_on = regs.get(60003) == 1
    for key, label, addr, lo, hi, bit in (("charge_limit", "Charging limit", 60000, 80, 100, 0), ("discharge_limit", "Discharge limit", 60001, 0, 20, 1),
                                          ("backup_reserve", "Backup reserve", 60002, 0, 100, 2)):
        v = regs.get(addr)
        if v is None or (caps is not None and not caps >> bit & 1):
            continue
        locked = ""
        if key == "backup_reserve" and not backup_on:
            locked = ("Switch the backup reserve on in the Anker app first." if regs.get(60003) is not None
                      else "This battery did not say whether its backup reserve is on, so it is not changed from here.")
        out.append(_s(key, label, "number", "Controls", v, unit="%", lo=lo, hi=hi, step=1, locked=locked,
                      hint="While the backup reserve is on, the discharge limit must not be above the reserve, and the reserve must be below the charging limit." if key != "charge_limit" else
                      "While the backup reserve is on, this must be above the discharge limit and the reserve."))
    if regs.get(60003) is not None and (caps is None or caps >> 3 & 1):
        out.append(_s("backup_enabled", "Backup reserve switch", "info", "Controls", "On" if backup_on else "Off",
                      hint="Switched in the Anker app (Anker's own Home Assistant integration cannot change it either)."))
    return out


def anker_write_plan(x1: bool, key: str, value, regs: dict[int, int], order: str | None, controlled: bool) -> list[tuple[int, list[int]]]:
    """[(first register, words)] to write for one change, or ValueError saying why not. regs must be fresh."""
    found = {s["key"]: s for s in anker_settings(x1, regs, order, controlled)}
    s = found.get(key)
    if s is None or s["kind"] == "info":
        raise ValueError("this battery does not have that setting")
    if s["locked"]:
        raise ValueError(s["locked"])
    try:
        n = int(value)
        if isinstance(value, bool) or (isinstance(value, float) and value != n) or (isinstance(value, str) and str(n) != value.strip()):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{s['label']} needs a whole number") from None
    if s["kind"] == "select":
        if key == "work_mode":
            allowed = set(X1_WORK_MODES) if x1 else {v for v, (bit, _) in SB_WORK_MODES.items() if (regs.get(32774) or 0) >> bit & 1}
        else:
            allowed = set(LIMIT_MODES)
        if n not in allowed:
            raise ValueError(f"{n} is not one of the choices for {s['label']}")
        return [({"work_mode": 10064, "export_limit_mode": 10074, "import_limit_mode": 10077}[key], [n])]
    lo, hi = s["min"], s["max"]
    if not lo <= n <= hi:
        raise ValueError(f"{s['label']} must be between {lo} and {hi} {s['unit']}".strip())
    if x1:
        if key == "watchdog":
            return [(10080, [n])]
        if order is None:
            raise ValueError("the battery's 32-bit word order is not known yet, so no limit was written; try again in a minute")
        base = 10075 if key == "export_limit" else 10078
        v = n & 0xFFFFFFFF
        return [(base, [v >> 16, v & 0xFFFF] if order == "big" else [v & 0xFFFF, v >> 16])]
    # Solarbank limits: the same cross-checks the official integration makes while the backup reserve is on
    ch, dis, res = (n if key == "charge_limit" else regs.get(60000)), (n if key == "discharge_limit" else regs.get(60001)), (n if key == "backup_reserve" else regs.get(60002))
    if regs.get(60003) == 1 and None not in (ch, dis, res):
        if key == "charge_limit" and not (ch > dis and ch > res):
            raise ValueError(f"The charging limit must be above the discharge limit ({dis}%) and the backup reserve ({res}%)")
        if key == "discharge_limit" and not (dis <= res and dis < ch):
            raise ValueError(f"The discharge limit must not be above the backup reserve ({res}%) and must be below the charging limit ({ch}%)")
        if key == "backup_reserve" and not (res >= dis and res < ch):
            raise ValueError(f"The backup reserve must be at least the discharge limit ({dis}%) and below the charging limit ({ch}%)")
    return [({"charge_limit": 60000, "discharge_limit": 60001, "backup_reserve": 60002}[key], [n])]


# -------------------------------------------------------------------------------------------------------------- EcoFlow
_AC_KEYS = {"ac1": ("relay2Onoff", "cfgRelay2Onoff", "AC 1 output"), "ac2": ("relay3Onoff", "cfgRelay3Onoff", "AC 2 output")}
# (the key HomePower stores - its EcoFlow reader keeps only the last part of a dotted key - the command field, the label)
_MODES = {"self_powered": ("operateSelfPoweredOpen", "operateSelfPoweredOpen", "Operating mode: self-powered"),
          "ai_mode": ("operateIntelligentScheduleModeOpen", "operateIntelligentScheduleModeOpen", "Operating mode: AI mode")}


def ecoflow_settings(quota: dict, controlled: bool) -> list[dict]:
    """The STREAM settings the developer API lets an owner change, for the keys this battery actually sent."""
    out: list[dict] = []
    for key, (qk, _cfg, label) in _AC_KEYS.items():
        if qk in quota:
            out.append(_s(key, label, "switch", "Controls", _on(quota[qk])))
    if "feedGridMode" in quota:
        out.append(_s("feed_in", "Feed-in control", "switch", "Controls", _int(quota["feedGridMode"], 0) == 2, hint="Lets the battery feed power into the house grid connection."))
    lock = "HomePower is managing this battery. Turn on \"monitor only\" in Edit to change it yourself." if controlled else ""
    for key, (qk, _cfg, label) in _MODES.items():
        if qk in quota:
            out.append(_s(key, label, "switch", "Controls", _on(quota[qk]), locked=lock))
    if "backupReverseSoc" in quota and isinstance(quota["backupReverseSoc"], (int, float)):
        lo = max(3, _int(quota.get("cmsMinDsgSoc"), 0) + 3)
        hi = min(95, _int(quota.get("cmsMaxChgSoc"), 100) or 100)
        out.append(_s("backup_reserve", "Backup reserve level", "number", "Configuration", int(quota["backupReverseSoc"]), unit="%", lo=lo, hi=max(lo, hi), step=1, locked=lock,
                      hint="The battery keeps this much charge back for a power cut. HomePower raises it to hold or charge the battery."))
    return out


def ecoflow_write_params(key: str, value, quota: dict, controlled: bool) -> dict:
    """The developer-API command parameters for one change, or ValueError saying why not."""
    found = {s["key"]: s for s in ecoflow_settings(quota, controlled)}
    s = found.get(key)
    if s is None:
        raise ValueError("this battery does not have that setting")
    if s["locked"]:
        raise ValueError(s["locked"])
    if s["kind"] == "switch":
        on = value if isinstance(value, bool) else {"1": True, "0": False, "true": True, "false": False, "on": True, "off": False}.get(str(value).strip().lower())
        if on is None:
            raise ValueError(f"{s['label']} is on or off")
        if key in _AC_KEYS:
            return {_AC_KEYS[key][1]: on}
        if key == "feed_in":
            return {"cfgFeedGridMode": 2 if on else 1}
        return {"cfgEnergyStrategyOperateMode": {_MODES[key][1]: on}}
    try:
        n = int(value)
        if isinstance(value, bool) or (isinstance(value, float) and value != n):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{s['label']} needs a whole number") from None
    if not s["min"] <= n <= s["max"]:
        raise ValueError(f"{s['label']} must be between {s['min']} and {s['max']} %")
    return {"cfgBackupReverseSoc": n}


# ------------------------------------------------------------------------------------- reading and writing through a driver
async def read_settings(driver, controlled: bool) -> tuple[str, list[dict], dict]:
    """(family, settings, the fresh readings they came from) for a battery that has settings that can be changed; family "" if none."""
    if hasattr(driver, "read_registers") and hasattr(driver, "scan"):          # an Anker battery
        x1 = bool(driver.config.extra.get("house_meter"))
        wanted = list(anker_registers(x1))
        raw = await driver.read_registers([a for a in wanted if a != 32775])
        if 32775 in wanted:        # read on its own: in a batch with 32774 a battery without it answers a silent 0, which would hide every limit
            raw.update(await driver.read_registers([32775]))
        regs = {int(k): v for k, v in raw.items()}
        return "anker", anker_settings(x1, regs, driver._order, controlled), regs
    quota = getattr(driver, "last_quota", None)
    if hasattr(driver, "api") and hasattr(driver, "sn") and isinstance(quota, dict) and quota:        # an EcoFlow STREAM over the developer API
        if getattr(driver, "_is5000", False):
            return "ecoflow", [], quota          # the STREAM 5000 series takes a different command set: not offered yet
        return "ecoflow", ecoflow_settings(quota, controlled), quota
    return "", [], {}


async def apply_change(driver, key: str, value, controlled: bool) -> str:
    """Check one change against the vendor's rules and send it. Returns the family; ValueError says why a change is refused."""
    family, _, state = await read_settings(driver, controlled)
    if not family:
        raise ValueError("this battery has no settings that can be changed here")
    if family == "anker":
        plan = anker_write_plan(bool(driver.config.extra.get("house_meter")), key, value, state, driver._order, controlled)
        for addr, words in plan:
            if len(words) == 1:
                await driver.client.write_register(addr, words[0])
            else:
                await driver.client.write_multiple(addr, words)
    else:
        await driver.api.command(driver.sn, ecoflow_write_params(key, value, state, controlled))
    return family
