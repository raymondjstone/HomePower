"""JSON <-> model conversion for persisted batteries and schedules (HA-free)."""
from __future__ import annotations

from datetime import date, time
from typing import Any

from .models import Capabilities, Desired, DeviceConfig, Mode
from .profiles import DRIVERS, PROFILE_BY_ID
from .schedule import ALL_DAYS, ScheduleEntry


class ValidationError(ValueError):
    pass


def _int(v: Any, name: str, lo: int, hi: int, default: int | None = None) -> int:
    if v in (None, ""):
        if default is None:
            raise ValidationError(f"{name} is required")
        return default
    try:
        n = int(float(v))
    except (TypeError, ValueError) as e:
        raise ValidationError(f"{name} must be a number") from e
    if not lo <= n <= hi:
        raise ValidationError(f"{name} must be between {lo} and {hi}")
    return n


def _opt_int(v: Any, name: str, lo: int, hi: int) -> int | None:
    return None if v in (None, "") else _int(v, name, lo, hi)


def _time(s: Any, name: str) -> time:
    try:
        h, m = str(s).split(":")[:2]
        return time(int(h), int(m))
    except (ValueError, TypeError) as e:
        raise ValidationError(f"{name} must be HH:MM") from e


def _connection(driver: str, raw: dict) -> dict:
    raw = raw or {}
    if driver == "anker_modbus":
        host = str(raw.get("host") or "").strip()
        if not host:
            raise ValidationError("the battery's IP address is required")
        return {"host": host, "port": _int(raw.get("port"), "port", 1, 65535, 502),
                "unit_id": _int(raw.get("unit_id"), "unit id", 0, 255, 1)}
    if driver == "ecoflow_stream":
        region = str(raw.get("region") or "eu")
        if region not in ("eu", "global"):
            raise ValidationError("region must be eu or global")
        out = {"region": region}
        for k, label in (("access_key", "Access Key"), ("secret_key", "Secret Key"), ("sn", "device")):
            v = str(raw.get(k) or "").strip()
            if not v:
                raise ValidationError(f"{label} is required")
            out[k] = v
        return out
    return {}


def fill_from_probe(raw: dict, info: dict) -> dict:
    """Use what the battery itself reports for anything the user didn't enter (capacity, limits, min SOC)."""
    out = dict(raw)
    for key in ("capacity_wh", "max_charge_w", "max_discharge_w", "min_soc"):
        if out.get(key) in (None, "") and info.get(key) is not None:
            out[key] = info[key]
    return out


def device_from_dict(d: dict) -> DeviceConfig:
    did = str(d.get("device_id") or "").strip()
    if not did:
        raise ValidationError("device_id is required")
    name = str(d.get("name") or "").strip() or did
    kind = str(d.get("kind") or "custom")
    profile = PROFILE_BY_ID.get(kind) or PROFILE_BY_ID["custom"]
    driver = profile["driver"]
    spec = DRIVERS[driver]
    modes = frozenset(Mode(m) for m in spec["modes"])
    caps = Capabilities(
        capacity_wh=_int(d.get("capacity_wh"), "capacity", 100, 200000),
        max_charge_w=_int(d.get("max_charge_w"), "max charge", 0, 50000),
        max_discharge_w=_int(d.get("max_discharge_w"), "max discharge", 0, 50000),
        min_soc=_int(d.get("min_soc"), "minimum SOC", 0, 100, 0),
        supported_modes=modes,
    )
    extra: dict = {"driver": driver}
    if driver == "entities":
        valid_keys = {f["k"] for f in spec["entities"]}
        ents = {k: str(v).strip() for k, v in (d.get("entities") or {}).items() if k in valid_keys and v}
        for f in spec["entities"]:
            if f["req"] and f["k"] not in ents:
                raise ValidationError(f"entity '{f['label']}' is required")
        valid_mm = {m["k"] for m in spec["mode_map"]}
        mode_map = {k: v for k, v in (d.get("mode_map") or {}).items() if k in valid_mm and v}
        for m in spec["mode_map"]:
            if not m["optional"] and m["k"] not in mode_map:
                raise ValidationError(f"select option for '{m['label']}' is required")
        extra.update(strategy="generic", entities=ents, mode_map=mode_map,
                     invert_power=bool(d.get("invert_power", False)), control_enabled=bool(d.get("control_enabled", True)))
    else:
        extra.update(connection=_connection(driver, d.get("connection")),
                     restore_mode=int(profile.get("restore_mode", 0)), watchdog=bool(profile.get("watchdog", False)),
                     control_enabled=bool(d.get("control_enabled", False)))  # monitor-only until the user opts in
    return DeviceConfig(
        device_id=did, name=name, kind=kind, caps=caps,
        priority=_int(d.get("priority"), "priority", -100, 100, 0), extra=extra,
    )


def device_to_dict(c: DeviceConfig) -> dict:
    e = c.extra
    return {
        "device_id": c.device_id, "name": c.name, "kind": c.kind, "priority": c.priority,
        "capacity_wh": c.caps.capacity_wh, "max_charge_w": c.caps.max_charge_w,
        "max_discharge_w": c.caps.max_discharge_w, "min_soc": c.caps.min_soc,
        "supported_modes": sorted(m.value for m in c.caps.supported_modes),
        "driver": e.get("driver", "entities"), "connection": e.get("connection", {}),
        "control_enabled": bool(e.get("control_enabled", True)),
        "entities": e.get("entities", {}), "mode_map": e.get("mode_map", {}),
        "invert_power": bool(e.get("invert_power", False)),
    }


def schedule_from_dict(d: dict) -> ScheduleEntry:
    sid = str(d.get("id") or "").strip()
    if not sid:
        raise ValidationError("id is required")
    try:
        mode = Mode(d.get("mode"))
    except ValueError as e:
        raise ValidationError("invalid mode") from e
    try:
        days = frozenset(int(x) for x in d.get("days", ALL_DAYS))
    except (TypeError, ValueError) as e:
        raise ValidationError("invalid weekdays") from e
    if not days or not days <= ALL_DAYS:
        raise ValidationError("pick at least one weekday")

    def md(v):
        if not v:
            return None
        m, dd = str(v).split("-")[-2:]
        return int(m), int(dd)

    try:
        season = (md(d.get("season_start")), md(d.get("season_end")))
        on_date = date.fromisoformat(d["on_date"]) if d.get("on_date") else None
    except (ValueError, TypeError) as e:
        raise ValidationError("invalid date") from e
    if (season[0] is None) != (season[1] is None):
        raise ValidationError("season needs both start and end")
    all_day = bool(d.get("all_day", False))
    cond = str(d.get("condition_entity") or "").strip() or None
    if all_day and not cond:
        raise ValidationError("an all-day schedule needs a condition entity (otherwise it never stops)")
    start, end = _time(d.get("start") or "00:00", "start"), _time(d.get("end") or "23:59", "end")
    if start == end and not all_day:
        raise ValidationError("start and end must differ")
    return ScheduleEntry(
        id=sid, device_ids=frozenset(d.get("device_ids") or []),
        start=start, end=end,
        desired=Desired(
            mode=mode,
            power_w=_opt_int(d.get("power_w"), "power", 0, 50000),
            target_soc=_opt_int(d.get("target_soc"), "target SOC", 0, 100),
            reserve_soc=_opt_int(d.get("reserve_soc"), "reserve SOC", 0, 100),
        ),
        days=days, priority=_int(d.get("priority"), "priority", -100, 100, 0),
        season_start=season[0], season_end=season[1], on_date=on_date,
        enabled=bool(d.get("enabled", True)), all_day=all_day, condition_entity=cond,
    )


def schedule_to_dict(e: ScheduleEntry) -> dict:
    ds = e.desired
    return {
        "id": e.id, "device_ids": sorted(e.device_ids),
        "start": e.start.strftime("%H:%M"), "end": e.end.strftime("%H:%M"),
        "mode": ds.mode.value, "power_w": ds.power_w, "target_soc": ds.target_soc,
        "reserve_soc": ds.reserve_soc, "days": sorted(e.days), "priority": e.priority,
        "season_start": f"{e.season_start[0]:02d}-{e.season_start[1]:02d}" if e.season_start else None,
        "season_end": f"{e.season_end[0]:02d}-{e.season_end[1]:02d}" if e.season_end else None,
        "on_date": e.on_date.isoformat() if e.on_date else None, "enabled": e.enabled,
        "all_day": e.all_day, "condition_entity": e.condition_entity,
    }
