"""Describe any Modbus TCP battery with a small register map (no code needed).

Example (read-only, Victron GX system registers, unit 100):
  {"soc":   {"addr": 843, "type": "u16"},
   "power": {"addr": 842, "type": "i16", "positive_is": "charging"},
   "grid":  {"addrs": [820, 821, 822], "type": "i16"}}

Optional control (a signed setpoint plus a mode register):
  "setpoint": {"addr": 10071, "type": "i32", "positive_is": "discharge"},
  "mode":     {"addr": 10064, "control_value": 3, "auto_value": 0}
"""
from __future__ import annotations

import json

TYPES = {"u16": 1, "i16": 1, "u32": 2, "i32": 2}
READ_KEYS = ("soc", "power", "grid", "pv", "load")


class MapError(ValueError):
    pass


def _reg(name: str, raw: dict, positive: tuple[str, str] | None = None) -> dict:
    if not isinstance(raw, dict):
        raise MapError(f"'{name}' must be an object")
    addrs = raw.get("addrs") or ([raw["addr"]] if "addr" in raw else None)
    if not addrs or not all(isinstance(a, int) and 0 <= a <= 65535 for a in addrs):
        raise MapError(f"'{name}' needs a register address (addr or addrs)")
    typ = raw.get("type", "u16")
    if typ not in TYPES:
        raise MapError(f"'{name}' type must be one of {sorted(TYPES)}")
    try:
        scale = float(raw.get("scale", 1))
    except (TypeError, ValueError) as e:
        raise MapError(f"'{name}' scale must be a number") from e
    fc = int(raw.get("fc", 3))
    if fc not in (3, 4):
        raise MapError(f"'{name}' fc must be 3 (holding) or 4 (input)")
    out = {"addrs": list(addrs), "type": typ, "scale": scale, "fc": fc}
    if positive:
        sign = raw.get("positive_is", positive[0])
        if sign not in positive:
            raise MapError(f"'{name}' positive_is must be one of {list(positive)}")
        out["positive_is"] = sign
    return out


def parse_map(raw: dict | str | None) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else None
        except json.JSONDecodeError as e:
            raise MapError(f"register map is not valid JSON: {e.msg}") from e
    if not isinstance(raw, dict):
        raise MapError("a register map is required")
    out: dict = {"word_order": raw.get("word_order", "big")}
    if out["word_order"] not in ("big", "little"):
        raise MapError("word_order must be big or little")
    for k in ("soc", "power"):
        if k not in raw:
            raise MapError(f"the map needs '{k}'")
    out["soc"] = _reg("soc", raw["soc"])
    out["power"] = _reg("power", raw["power"], ("charging", "discharging"))
    out["grid"] = _reg("grid", raw["grid"], ("import", "export")) if "grid" in raw else None
    for k in ("pv", "load"):
        out[k] = _reg(k, raw[k]) if k in raw else None
    out["setpoint"] = _reg("setpoint", raw["setpoint"], ("discharge", "charge")) if "setpoint" in raw else None
    m = raw.get("mode")
    if m is not None:
        reg = _reg("mode", m)
        try:
            reg["control_value"], reg["auto_value"] = int(m["control_value"]), int(m["auto_value"])
        except (KeyError, TypeError, ValueError) as e:
            raise MapError("'mode' needs integer control_value and auto_value") from e
        out["mode"] = reg
    else:
        out["mode"] = None
    if bool(out["setpoint"]) != bool(out["mode"]):
        raise MapError("control needs both 'setpoint' and 'mode' (or neither, for read-only)")
    return out


def has_control(parsed: dict) -> bool:
    return bool(parsed.get("setpoint") and parsed.get("mode"))
