"""Advanced path: drive any battery through Home Assistant entities the user picks (no driver needed).

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
    if d.power_w is not None:   # defence in depth: never above the battery's own limit
        limit = caps.max_charge_w if d.mode is Mode.CHARGE else caps.max_discharge_w
        out += _number(ent, "set_power", int(max(0, min(d.power_w, limit))) if d.mode in (Mode.CHARGE, Mode.DISCHARGE) else d.power_w)
    return out + _select(ent, "mode", mm[d.mode.value])


_IMPL = {"generic": (_interpret_generic, _plan_generic)}


def interpret(extra: dict, caps: Capabilities, states: dict[str, str | None]) -> Actual:
    return _IMPL[extra.get("strategy", "generic")][0](extra, caps, states)


def plan(extra: dict, caps: Capabilities, desired: Desired) -> list[Command]:
    if desired.mode not in caps.supported_modes:
        raise ValueError(f"mode '{desired.mode.value}' is not supported by this battery")
    return _IMPL[extra.get("strategy", "generic")][1](extra, caps, desired)
