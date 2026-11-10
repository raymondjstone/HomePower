"""Translate between HomePower's neutral model and each integration family's entities.

Pure functions, no Home Assistant imports:
  interpret(extra, caps, states) -> Actual     states: {key: state-string | None} per mapped entity
  plan(extra, caps, desired)     -> [Command]  ordered service calls (domain, service, data)
"""
from __future__ import annotations

from typing import Any

from .models import Actual, Capabilities, Desired, Mode

Command = tuple[str, str, dict[str, Any]]
BAD = (None, "unavailable", "unknown", "")


def num(s: str | None) -> float | None:
    if s in BAD:
        return None
    try:
        return float(s)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _int(s: str | None) -> int | None:
    v = num(s)
    return None if v is None else int(v)


def _is_on(s: str | None) -> bool:
    return s == "on"


def _number(ent: dict, key: str, value: int) -> list[Command]:
    return [("number", "set_value", {"entity_id": ent[key], "value": value})] if ent.get(key) else []


def _select(ent: dict, key: str, option: str | None) -> list[Command]:
    return [("select", "select_option", {"entity_id": ent[key], "option": option})] if ent.get(key) and option else []


def _switch(ent: dict, key: str, on: bool) -> list[Command]:
    return [("switch", "turn_on" if on else "turn_off", {"entity_id": ent[key]})] if ent.get(key) else []


def _power(extra: dict, st: dict, key: str = "power") -> float | None:
    p = num(st.get(key))
    return -p if p is not None and extra.get("invert_power") else p  # normalise: + charging


# ---------------------------------------------------------------- generic
def _interpret_generic(extra, caps, st) -> Actual:
    if st.get("mode") in BAD:
        return Actual(online=False)
    rev = {v: Mode(k) for k, v in extra["mode_map"].items()}
    return Actual(
        online=True, soc=num(st.get("soc")), power_w=_power(extra, st), mode=rev.get(st["mode"]),
        set_power_w=_int(st.get("set_power")), target_soc=_int(st.get("target_soc")),
        reserve_soc=_int(st.get("reserve_soc")), protected=_is_on(st.get("protected")),
    )


def _plan_generic(extra, caps, d) -> list[Command]:
    ent, mm = extra["entities"], extra["mode_map"]
    out: list[Command] = []  # limits first, mode last, so a mode flip never runs on stale setpoints
    if d.reserve_soc is not None:
        out += _number(ent, "reserve_soc", d.reserve_soc)
    if d.target_soc is not None:
        out += _number(ent, "target_soc", d.target_soc)
    if d.power_w is not None:
        out += _number(ent, "set_power", d.power_w)
    return out + _select(ent, "mode", mm[d.mode.value])


# ------------------------------------------------- official Anker (direction)
def _interpret_direction(extra, caps, st) -> Actual:
    if st.get("work_mode") in BAD:
        return Actual(online=False)
    mm = extra["mode_map"]
    ch, dis = num(st.get("charge_power")), num(st.get("discharge_power"))
    power = None if ch is None and dis is None else (ch or 0.0) - (dis or 0.0)
    sp = _int(st.get("setpoint"))
    if st["work_mode"] != mm.get("control"):
        mode, sp_out = Mode.AUTO, None
    elif not sp:
        mode, sp_out = Mode.HOLD, 0
    elif st.get("direction") == mm.get("charge"):
        mode, sp_out = Mode.CHARGE, abs(sp)
    else:
        mode, sp_out = Mode.DISCHARGE, abs(sp)
    return Actual(
        online=True, soc=num(st.get("soc")), power_w=power, mode=mode, set_power_w=sp_out,
        target_soc=_int(st.get("target_soc")), reserve_soc=_int(st.get("reserve_soc")),
    )


def _plan_direction(extra, caps, d) -> list[Command]:
    ent, mm = extra["entities"], extra["mode_map"]
    if d.mode is Mode.AUTO:
        return _select(ent, "work_mode", mm.get("auto"))
    out: list[Command] = []
    if d.reserve_soc is not None:
        out += _number(ent, "reserve_soc", d.reserve_soc)
    if d.target_soc is not None:
        out += _number(ent, "target_soc", d.target_soc)
    out += _select(ent, "work_mode", mm.get("control"))
    if d.mode is Mode.HOLD:
        return out + _number(ent, "setpoint", 0)
    limit = caps.max_charge_w if d.mode is Mode.CHARGE else caps.max_discharge_w
    out += _select(ent, "direction", mm["charge" if d.mode is Mode.CHARGE else "discharge"])
    return out + _number(ent, "setpoint", d.power_w if d.power_w is not None else limit)


# ----------------------------------------------------- Anker X1 (signed)
def _x1_engaged(extra, st) -> bool | None:
    if extra["entities"].get("engage"):
        s = st.get("engage")
        return None if s in BAD else _is_on(s)
    s = st.get("work_mode")
    return None if s in BAD else s == extra["mode_map"].get("control")


def _interpret_signed(extra, caps, st) -> Actual:
    engaged = _x1_engaged(extra, st)
    if engaged is None:
        return Actual(online=False)
    base = dict(online=True, soc=num(st.get("soc")), power_w=_power(extra, st))
    if not engaged:
        return Actual(**base, mode=Mode.AUTO)
    sp = num(st.get("setpoint")) or 0.0
    discharge = sp if extra.get("discharge_positive", True) else -sp
    mode = Mode.HOLD if abs(discharge) < 1 else Mode.DISCHARGE if discharge > 0 else Mode.CHARGE
    return Actual(**base, mode=mode, set_power_w=int(abs(discharge)))


def _plan_signed(extra, caps, d) -> list[Command]:
    ent, mm = extra["entities"], extra["mode_map"]
    if d.mode is Mode.AUTO:  # release: zero the setpoint, then hand control back
        out = _number(ent, "setpoint", 0)
        if ent.get("engage"):
            return out + _switch(ent, "engage", False)
        return out + _select(ent, "work_mode", mm.get("auto"))
    out = _switch(ent, "engage", True) if ent.get("engage") else _select(ent, "work_mode", mm.get("control"))
    if d.mode is Mode.HOLD:
        return out + _number(ent, "setpoint", 0)
    watts = d.power_w if d.power_w is not None else (caps.max_charge_w if d.mode is Mode.CHARGE else caps.max_discharge_w)
    positive = (d.mode is Mode.DISCHARGE) == bool(extra.get("discharge_positive", True))
    return out + _number(ent, "setpoint", watts if positive else -watts)


# ---------------------------------------------------- EcoFlow STREAM
def _interpret_ecoflow(extra, caps, st) -> Actual:
    reserve = _int(st.get("backup_reserve"))
    if reserve is None:
        return Actual(online=False)
    hold = reserve >= extra.get("hold_reserve", 95)
    return Actual(online=True, soc=num(st.get("soc")), power_w=_power(extra, st), mode=Mode.HOLD if hold else Mode.AUTO)


def _plan_ecoflow(extra, caps, d) -> list[Command]:
    ent, mm = extra["entities"], extra["mode_map"]
    if d.mode is Mode.HOLD:
        return _number(ent, "backup_reserve", extra.get("hold_reserve", 95))
    # AUTO: back to self-powered with the user's normal reserve (their configured minimum SOC, >= 3).
    return (_switch(ent, "self_powered", True) + _select(ent, "operating_mode", mm.get("auto"))
            + _number(ent, "backup_reserve", max(3, caps.min_soc)))


_IMPL = {
    "generic": (_interpret_generic, _plan_generic),
    "direction_setpoint": (_interpret_direction, _plan_direction),
    "signed_setpoint": (_interpret_signed, _plan_signed),
    "ecoflow_stream": (_interpret_ecoflow, _plan_ecoflow),
}


def interpret(extra: dict, caps: Capabilities, states: dict[str, str | None]) -> Actual:
    return _IMPL[extra.get("strategy", "generic")][0](extra, caps, states)


def plan(extra: dict, caps: Capabilities, desired: Desired) -> list[Command]:
    if desired.mode not in caps.supported_modes:
        raise ValueError(f"mode '{desired.mode.value}' is not supported by this battery")
    return _IMPL[extra.get("strategy", "generic")][1](extra, caps, desired)
