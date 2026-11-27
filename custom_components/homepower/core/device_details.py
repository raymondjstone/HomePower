"""The full sheet for one battery (panel: the cog on a battery card). HA-free.

Turns what a battery reports into rows a person can read: the Anker registers named in Anker's own protocol documents
(settings, identity, live readings, alarms, battery packs), and the fields HomePower decodes from any battery.
Only units that the drivers already rely on are shown (watts, %, tenths of a degree, tenths of a kWh); every other
number is shown as the register holds it, so nothing is presented with a guessed scale.
"""
from __future__ import annotations

from typing import Any

import re

from .anker_x1_map import SOLARBANK_REGISTERS, X1_REGISTERS, alarm_texts

_TEXT = re.compile(r"model name|serial number|software version|hardware version|^model$", re.I)
_SIGNED = re.compile(r"power|control|setpoint|temperature", re.I)
_WATTS = re.compile(r"^(pv power|3rd-party pv power|battery power|load power|grid power|rechargeable power|dischargeable power|"
                    r"rated power|maximum active power|total pv power|maximum charge power|maximum discharge power|ac grid-side output power|"
                    r"battery pack power|pack \d+ power|backup active power|grid active power|active power \(pcs ac side\)|meter .*active power)", re.I)
_PERCENT = re.compile(r"\b(soc|soh)\b|\(%", re.I)
_ENUM_TAIL = re.compile(r"\s*\(([^()]*)\)\s*$")

# Meanings seen on real units that Anker's document does not list (from the open-source X1 integration afewyards/anker-x1-ha,
# checked on firmware 1.0.19.16). They are added to the document's own list; where both name a value, these win.
_REAL_ENUMS: dict[int, dict[int, str]] = {
    10001: {0: "standby", 1: "charging", 2: "discharging", 3: "sleep"},
    10064: {16: "dynamic tariff (set in the app)", 20: "app-managed"},
    10074: {0: "disabled", 1: "percentage of rated power", 2: "fixed power"},
    10077: {0: "disabled", 1: "percentage of rated power", 2: "fixed power"},
    10132: {0: "L/N", 1: "L1/L2/L3/N", 3: "three-phase (3W)"},
    10631: {0: "normal", 1: "offline", 3: "fault"},
}
# X1 energy registers are in 1/100 kWh on firmware 1.0.16.1, 1.0.17.8 and 1.0.19.16 but 1/10 kWh on 1.0.18.x (same integration's notes);
# any other firmware is shown as the raw number rather than guessed. A Solarbank's totals are 1/10 kWh (Anker's official integration).
_X1_ENERGY = {*range(10016, 10036, 2), 10185, 10187, 10258, 10260, 10262, 10264}
_X1_METER_ENERGY = set(range(10650, 10666, 2))        # the external meter's own counters: 0.01 kWh on every firmware reported so far
# Solarbank capability masks (Anker's official integration, Solarbank config files): which work modes and settings THIS battery supports.
_MODE_BITS = ["self-consumption", "time-of-use", "custom mode", "smart / AI mode", "socket overlay mode", "third-party control", "dynamic pricing"]
_SETTING_BITS = ["charge limit", "discharge limit", "backup reserve level", "backup reserve switch"]

# Arrays of small readings: first address -> (how many, scale, unit and what each one is). From two X1 integrations that agree
# (afewyards/anker-x1-ha and cbource/anker-solix-x1-modbus).
_ARRAYS = {
    10199: (6, 0.1, "V (Uab / Ubc / Uca / phase A / B / C)"), 10205: (3, 0.01, "A (phase A / B / C)"),
    10224: (6, 0.1, "V (Uab / Ubc / Uca / phase A / B / C)"), 10230: (3, 0.01, "A (phase A / B / C)"),
    10632: (3, 0.1, "V (phase A / B / C)"), 10635: (3, 0.01, "A (phase A / B / C)"),
}
_SCALED_1 = {10212: (0.001, "", 3), 10237: (0.001, "", 3), 10648: (0.001, "", 3), 10213: (0.01, "Hz", 2), 10238: (0.01, "Hz", 2), 10649: (0.01, "Hz", 2)}
_SB_ENERGY = {10018, 10022, 10024, 10026, 10030, 10034, 10262, 10264}


def _enum(name: str) -> tuple[str, dict[int, str]]:
    """'Work mode (0 self-consumption, 1 time-of-use)' -> ('Work mode', {0: 'self-consumption', 1: 'time-of-use'})."""
    m = _ENUM_TAIL.search(name)
    if not m:
        return name, {}
    parts = [p.strip() for p in m.group(1).split(",")]
    found = {}
    for p in parts:
        q = re.match(r"^(\d+) (.+)$", p)
        if not q:
            return name, {}
        found[int(q.group(1))] = q.group(2)
    return name[:m.start()], found


def _x1_energy_gain(firmware: str | None) -> int | None:
    m = re.match(r"^\D*(\d+)\.(\d+)\.(\d+)\.(\d+)", firmware or "")
    if not m:
        return None
    third = int(m.group(3))
    return 10 if third == 18 else 100 if third in (16, 17, 19) else None      # only 1.0.18.x counts in 0.1 kWh (a firmware quirk)


def _text(words: list[int], low_first: bool) -> str:
    raw = b"".join((w & 0xFFFF).to_bytes(2, "little" if low_first else "big") for w in words)
    return "".join(c for c in raw.decode("latin-1") if c.isprintable()).strip()


def wanted(x1: bool, packs: int | None = None) -> list[tuple[int, int, str, str, str]]:
    """The register entries worth reading for this family (no write-only ones; no packs the battery does not have)."""
    out = []
    for e in (X1_REGISTERS if x1 else SOLARBANK_REGISTERS):
        addr, n, name, access, kind = e
        if access == "WO":
            continue
        m = re.match(r"^Pack (\d+) ", name)
        if m and packs is not None and int(m.group(1)) > packs:
            continue
        out.append(e)
    return out


def addresses(entries) -> list[int]:
    return sorted({a + i for a, n, *_ in entries for i in range(n)})


def anker_rows(entries, values: dict[str, int], order: str | None, x1: bool) -> list[dict]:
    """One row per register entry the battery answered: {addr, name, access, kind, value, unit}."""
    def word(a: int) -> int | None:
        return values.get(str(a))

    fw = None
    if x1 and all(word(10112 + i) is not None for i in range(6)):
        version: list[Any] = [word(10112 + i) for i in range(6)]
        fw = _text(version, True)
    gain = _x1_energy_gain(fw) if x1 else 10
    energy = _X1_ENERGY if x1 else _SB_ENERGY
    rows = []
    for addr, n, name, access, kind in entries:
        words: list[Any] = [word(addr + i) for i in range(n)]   # none missing: checked below
        if any(w is None for w in words):
            continue
        label, enum = _enum(name)
        real = {} if (not x1 and addr in (10074, 10077)) else _REAL_ENUMS.get(addr, {})      # a Solarbank's export limit is a plain on/off
        enum = {**enum, **real} if (enum or real) else enum
        if real and label == name:
            label = _ENUM_TAIL.sub("", name)
        unit, shown = "", None
        if _TEXT.search(name):
            shown = _text(words, x1) or None
        elif kind == "alarm":
            alarms = alarm_texts(words) if x1 and addr == 10042 else []
            shown = ", ".join(alarms) if alarms else ("none" if not any(words) else "code " + " ".join(str(w) for w in words if w))
        elif addr == 10167 and n == 16:      # PV strings: voltage in 0.1 V then current in 0.01 A, string 1 first (X1)
            def s16(w):
                return w - 0x10000 if w & 0x8000 else w
            strings = [f"PV{i + 1} {max(0, s16(words[2 * i])) / 10:.1f} V {max(0, s16(words[2 * i + 1])) / 100:.2f} A" for i in range(8) if words[2 * i] or words[2 * i + 1]]
            shown = ", ".join(strings) or "no PV"
        elif not x1 and addr in (32774, 32775) and n == 1:
            bits = _MODE_BITS if addr == 32774 else _SETTING_BITS
            shown = ", ".join(t for i, t in enumerate(bits) if words[0] >> i & 1) or "none"
        elif x1 and addr in _ARRAYS and n == _ARRAYS[addr][0]:
            count, scale, unit = _ARRAYS[addr]
            if addr == 10632 and 0 < max(words) < 1000:       # firmware 1.0.19.16 reports the meter's voltage in whole volts
                scale = 1
            shown = " / ".join(f"{w * scale:.1f}" if scale >= 0.1 else f"{w * scale:.2f}" for w in words)
        elif n == 1:
            v = words[0]
            if (_SIGNED.search(name) or addr in (10212, 10237, 10648)) and v & 0x8000:
                v -= 0x10000
            if enum:
                shown = enum.get(v, f"{v} (not in Anker's list)")
            elif addr == 10156:
                shown, unit = f"{v / 10:.1f}", "°C"
            elif addr == 10253:
                shown, unit = f"{v / 10:.1f}", "V"
            elif addr == 10649:
                shown, unit = f"{v / 100:.2f}", "Hz"
            elif x1 and addr in _SCALED_1:
                scale, unit, digits = _SCALED_1[addr]
                shown = f"{v * scale:.{digits}f}"
            elif addr == 10080:
                shown, unit = str(v), "s"
            elif _PERCENT.search(name):
                shown, unit = str(v), "%"
            else:
                shown = str(v)
        elif n == 2 and order:
            v = (words[0] << 16 | words[1]) if order == "big" else (words[1] << 16 | words[0])
            if _SIGNED.search(name) and v & 0x80000000:
                v -= 0x100000000
            if addr == 10250:
                shown, unit = f"{v / 10:.1f}", "kWh"
            elif x1 and addr in _X1_METER_ENERGY:
                shown, unit = f"{v / 100:.2f}", "kWh"
            elif addr in energy:
                if gain:
                    shown, unit = f"{v / gain:.2f}", "kWh"
                else:
                    shown = f"{v} (raw: kWh x10 or x100, depends on the firmware)"
            elif addr in (10075, 10078):     # export / import limit: watts in fixed mode, percent of rated power in percent mode
                mode = word(addr - 1)
                shown, unit = str(v), "W" if (mode == 2 or (not x1 and mode == 1)) else "%" if mode == 1 else ""      # a Solarbank's limit is on/off, always in watts
            elif _WATTS.search(name):
                shown, unit = str(v), "W"
            else:
                shown = str(v)
        else:
            shown = " ".join(str(w) for w in words)
        if shown is None:
            continue
        rows.append({"addr": addr, "name": label.strip(), "access": access, "kind": kind, "value": shown, "unit": unit})
    return rows


# EcoFlow STREAM values: key -> (name, unit, kind). Names and units follow the Ecoflow-Cloud Home Assistant integration's
# STREAM definitions (tolwi/hassio-ecoflow-cloud, public/stream_ac.py), which is what an owner sees on the device page there.
# kind: "setting" = a control or configuration value, "live" = a reading, "info" = identity / diagnostic.
_EF: dict[str, tuple[str, str, str]] = {
    "relay2Onoff": ("AC 1 output", "", "setting"), "relay3Onoff": ("AC 2 output", "", "setting"),
    "operateSelfPoweredOpen": ("Operating mode: self-powered", "", "setting"),
    "operateIntelligentScheduleModeOpen": ("Operating mode: AI mode", "", "setting"),
    "operateScheduledOpen": ("Operating mode: scheduled", "", "setting"),
    "operateTouModeOpen": ("Operating mode: time-of-use", "", "setting"),
    "energyStrategyOperateMode.operateSelfPoweredOpen": ("Operating mode: self-powered", "", "setting"),
    "energyStrategyOperateMode.operateIntelligentScheduleModeOpen": ("Operating mode: AI mode", "", "setting"),
    "energyStrategyOperateMode.operateScheduledOpen": ("Operating mode: scheduled", "", "setting"),
    "energyStrategyOperateMode.operateTouModeOpen": ("Operating mode: time-of-use", "", "setting"),
    "feedGridMode": ("Feed-in control", "", "setting"), "feedGridModePowLimit": ("Feed-in power limit", "W", "setting"),
    "feedGridModePowMax": ("Feed-in power maximum", "W", "setting"),
    "backupReverseSoc": ("Backup reserve level", "%", "setting"), "backupSocOnOff": ("Backup reserve switch", "", "setting"),
    "backupSoc": ("Backup reserve level (STREAM 5000 series)", "%", "setting"),
    "cmsMaxChgSoc": ("Max charge level", "%", "live"), "cmsMinDsgSoc": ("Min discharge level", "%", "live"),
    "soc": ("Battery level", "%", "live"), "cmsBattSoc": ("Battery level (system)", "%", "live"), "f32ShowSoc": ("Battery level (shown in the app)", "%", "live"),
    "cmsBattFullEnergy": ("Full energy", "Wh", "live"),
    "accuChgEnergy": ("Cumulative energy charged", "Wh", "live"), "accuDsgEnergy": ("Cumulative energy discharged", "Wh", "live"),
    "accuChgCap": ("Cumulative charge capacity", "mAh", "live"), "accuDsgCap": ("Cumulative discharge capacity", "mAh", "live"),
    "inputWatts": ("In power", "W", "live"), "outputWatts": ("Out power", "W", "live"),
    "gridConnectionPower": ("Power AC", "W", "live"), "sysGridConnectionPower": ("Power AC (system)", "W", "live"),
    "gridConnectionVol": ("Grid voltage", "V", "live"), "gridConnectionFreq": ("Grid frequency", "Hz", "live"),
    "powGetBpCms": ("Power battery (+ charging, - discharging)", "W", "live"), "powGetPvSum": ("Power solar (all inputs; some firmware also counts grid charging here)", "W", "live"),
    "powGetPv": ("Power solar 1", "W", "live"), "powGetPv2": ("Power solar 2", "W", "live"),
    "powGetPv3": ("Power solar 3", "W", "live"), "powGetPv4": ("Power solar 4", "W", "live"),
    "plugInInfoPvVol": ("Solar 1 voltage", "V", "live"), "plugInInfoPv2Vol": ("Solar 2 voltage", "V", "live"),
    "plugInInfoPv3Vol": ("Solar 3 voltage", "V", "live"), "plugInInfoPv4Vol": ("Solar 4 voltage", "V", "live"),
    "plugInInfoPvAmp": ("Solar 1 current", "A", "live"), "plugInInfoPv2Amp": ("Solar 2 current", "A", "live"),
    "plugInInfoPv3Amp": ("Solar 3 current", "A", "live"), "plugInInfoPv4Amp": ("Solar 4 current", "A", "live"),
    "powGetSchuko1": ("AC 1 output power", "W", "live"), "powGetSchuko2": ("AC 2 output power", "W", "live"),
    "powGetSysGrid": ("Power grid", "W", "live"), "powGetSysLoad": ("Power house load", "W", "live"),
    "powGetSysLoadFromBp": ("House load from battery", "W", "live"), "powGetSysLoadFromGrid": ("House load from grid", "W", "live"),
    "powGetSysLoadFromPv": ("House load from solar", "W", "live"),
    "remainTime": ("Remaining time", "min", "live"), "bmsChgRemTime": ("Time to full", "min", "live"), "bmsDsgRemTime": ("Time to empty", "min", "live"),
    "soh": ("State of health", "%", "info"), "realSoh": ("State of health (measured)", "%", "info"), "cmsBattSoh": ("State of health (system)", "%", "info"),
    "temp": ("Battery temperature", "°C", "info"), "minCellTemp": ("Coldest cell", "°C", "info"), "maxCellTemp": ("Warmest cell", "°C", "info"),
    "vol": ("Battery voltage", "mV", "info"), "minCellVol": ("Lowest cell voltage", "mV", "info"), "maxCellVol": ("Highest cell voltage", "mV", "info"),
    "cycles": ("Cycles", "", "info"), "designCap": ("Design capacity", "mAh", "info"), "fullCap": ("Full capacity", "mAh", "info"),
    "remainCap": ("Remaining capacity", "mAh", "info"), "moduleWifiRssi": ("Wi-Fi signal", "dBm", "info"),
    # STREAM 5000 series and grid-meter keys (Ecoflow-Cloud's stream_ac_5000.py)
    "bpPower": ("Battery power", "W", "live"), "bmsBattSoc": ("Battery level (BMS)", "%", "live"), "cycleSoh": ("State of health (cycle)", "%", "info"),
    "chgPowLimit": ("Net power in limit", "W", "setting"), "maxChgPow": ("Charge power limit", "W", "live"), "maxDsgPow": ("Max discharge power", "W", "live"),
    "gridPortPower": ("Grid port power", "W", "live"), "upsEnabled": ("UPS mode", "", "setting"),
    "meterFreq": ("Grid frequency (meter)", "Hz", "live"), "meterTotalPower": ("Grid power, all phases (meter)", "W", "live"),
    "meterPhaseAPower": ("Grid power L1 (meter)", "W", "live"), "meterPhaseBPower": ("Grid power L2 (meter)", "W", "live"), "meterPhaseCPower": ("Grid power L3 (meter)", "W", "live"),
    "meterPhaseAVol": ("Grid voltage L1 (meter)", "V", "live"), "meterPhaseBVol": ("Grid voltage L2 (meter)", "V", "live"), "meterPhaseCVol": ("Grid voltage L3 (meter)", "V", "live"),
    # the private (app) API sends the solar strings under other names
    "inVolPv1": ("Solar 1 voltage", "V", "live"), "inVolPv2": ("Solar 2 voltage", "V", "live"), "inVolPv3": ("Solar 3 voltage", "V", "live"), "inVolPv4": ("Solar 4 voltage", "V", "live"),
    "inAmpPv1": ("Solar 1 current", "A", "live"), "inAmpPv2": ("Solar 2 current", "A", "live"), "inAmpPv3": ("Solar 3 current", "A", "live"), "inAmpPv4": ("Solar 4 current", "A", "live"),
    # the local (Modbus) EcoFlow driver's own readings
    "consumption_w": ("House load", "W", "live"), "grid_w": ("Grid", "W", "live"), "pv_w": ("Solar", "W", "live"), "battery_w": ("Battery power", "W", "live"),
}


def _num(v) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


def ecoflow_rows(quota: dict) -> list[dict]:
    """Rows for an EcoFlow battery's values, named as the Ecoflow-Cloud integration names them; anything it does not name
    is listed under its own key (kind "other") so nothing the battery sent is hidden."""
    rows = []
    for key in sorted(quota):
        v = quota[key]
        name, unit, kind = _EF.get(key, (key, "", "other"))
        if isinstance(v, bool):
            shown = "On" if v else "Off"
        elif isinstance(v, (int, float)):
            if unit == "min" and v >= 60:
                shown, unit = f"{int(v // 60)} h {int(v % 60)} min", ""
            else:
                shown = _num(v)
        elif isinstance(v, (list, dict)):
            import json
            shown = json.dumps(v)
        else:
            shown = str(v)
        rows.append({"addr": key, "name": name, "access": "", "kind": kind, "value": shown, "unit": unit})
    full, soc = quota.get("cmsBattFullEnergy"), quota.get("cmsBattSoc")
    if isinstance(full, (int, float)) and isinstance(soc, (int, float)) and not isinstance(full, bool):
        rows.append({"addr": "", "name": "Stored energy", "access": "", "kind": "live", "value": _num(round(full * soc / 100, 1)), "unit": "Wh"})
    return rows


def actual_fields(a) -> dict:
    """Everything the battery's last reading carries, in plain types (None for what it did not report)."""
    if a is None:
        return {}
    lim = a.limits
    return {
        "soc": a.soc, "power_w": a.power_w, "mode": a.mode.value if a.mode else None, "set_power_w": a.set_power_w,
        "target_soc": a.target_soc, "reserve_soc": a.reserve_soc, "grid_w": a.grid_w, "pv_w": a.pv_w, "load_w": a.load_w,
        "temp_c": a.temp_c, "firmware": a.firmware, "off_grid": a.off_grid, "protected": a.protected,
        "alarms": list(a.alarms), "pack_v": list(a.pack_v),
        "limits": ({"charge_to": lim[0], "discharge_to": lim[1], "backup_reserve": lim[2], "backup_on": lim[3]} if lim else None),
    }
