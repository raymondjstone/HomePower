"""The decision engine: given the state of the world, what should every battery do, and why?

Pure and HA-free so every policy can be tested. The coordinator only gathers inputs and applies results.

Priority, highest first:
  manual override > backup reserve > schedule > goal plan > zero-import battery
then, for batteries still without an instruction: peak shaving / export limiting, and finally
"hand back to the battery's own control" for ones we previously forced.
Safety clamps always apply: device limits, SOC limits (with hysteresis), load rules, overall caps.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Callable

from .goals import Goal, next_deadline
from .grid import apply_load_rules, backup_desired, export_guard, peak_shave, split_power
from .limits import apply_totals, charge_ceiling, soc_guard
from .models import Actual, Capabilities, Desired, DeviceConfig, Mode
from .planner import Alloc, Need, adjust_target_for_forecast, plan_charge, power_now
from .rates import RateTable
from .reconcile import clamp
from .schedule import ScheduleEntry, resolve_entry
from .zero_import import desired_for, setpoint

SOC_MARGIN = 2  # batteries this close to their floor are not asked to discharge more


@dataclass
class DevIn:
    config: DeviceConfig
    actual: Actual
    release_pending: bool = False
    guard_held: bool = False
    controllable: bool = True   # False = monitor-only: shown, never used to satisfy a guard


@dataclass
class Settings:
    charge_cap_w: int | None = None
    discharge_cap_w: int | None = None
    import_limit_w: int | None = None
    export_limit_w: int | None = None
    zero_import_device: str | None = None
    peak_shaving: bool = False
    backup_pct: int = 50
    forecast_share: float = 0.5


@dataclass
class World:
    now: datetime
    devices: list[DevIn]
    settings: Settings = field(default_factory=Settings)
    schedules: list[ScheduleEntry] = field(default_factory=list)
    goals: list[Goal] = field(default_factory=list)
    overrides: dict[str, Desired] = field(default_factory=dict)
    grid_w: float | None = None
    condition_ok: Callable[[str], bool] | None = None
    backup_active: bool = False
    away: bool = False
    load_blocks: set[str] = field(default_factory=set)
    rates: RateTable | None = None
    forecast_kwh: float | None = None


@dataclass
class Result:
    wants: dict[str, Desired] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    guard_held: dict[str, bool] = field(default_factory=dict)
    allocs: list[Alloc] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _floor(caps: Capabilities, w: World) -> int:
    return max(caps.min_soc, w.settings.backup_pct) if w.backup_active else caps.min_soc


def _zero_import(w: World, dev: DevIn) -> Desired:
    act, caps = dev.actual, dev.config.caps
    if w.grid_w is None or not act.online:
        return desired_for(None, caps, act.soc)  # fail safe: HOLD
    other_charge = sum(max(0.0, o.actual.power_w or 0.0) for o in w.devices if o is not dev and o.actual.online)
    cur = act.set_power_w or 0
    signed_cur = cur if act.mode is Mode.DISCHARGE else -cur if act.mode is Mode.CHARGE else 0
    sp = setpoint(
        w.grid_w, own_net_discharge_w=-(act.power_w or 0.0), other_charge_w=other_charge,
        max_discharge_w=caps.max_discharge_w, max_charge_w=caps.max_charge_w, current_set_w=signed_cur,
    )
    return desired_for(sp, caps, act.soc)


def _plan_goals(w: World, res: Result) -> dict[str, tuple[Desired, str]]:
    out: dict[str, tuple[Desired, str]] = {}
    if not w.goals:
        return out
    if w.rates is None:
        res.notes.append("goals need price data: set a rates entity or a fixed tariff")
        return out
    for g in w.goals:
        if not g.enabled:
            continue
        deadline = next_deadline(g, w.now)
        if deadline is None:
            continue
        devs = [d for d in w.devices if g.covers(d.config.device_id) and d.actual.online and d.actual.soc is not None
                and Mode.CHARGE in d.config.caps.supported_modes and d.config.caps.max_charge_w > 0]
        if not devs:
            continue
        target = g.target_soc
        if g.use_forecast:
            target = adjust_target_for_forecast(
                target, w.forecast_kwh, sum(d.config.caps.capacity_wh for d in devs) / 1000, w.settings.forecast_share)
        needs = [Need(d.config.device_id, d.actual.soc, d.config.caps.capacity_wh, d.config.caps.max_charge_w, target)
                 for d in devs]
        slots = w.rates.slots_between(w.now, deadline)
        if not slots:
            res.notes.append(f"goal '{g.id}': no price data before {deadline:%H:%M}")
            continue
        allocs, unmet = plan_charge(w.now, deadline, needs, slots, g.max_power_w)
        res.allocs += allocs
        for did in unmet:
            res.notes.append(f"goal '{g.id}': cannot fully reach {target}% by {deadline:%H:%M} on {did}")
        for d in devs:
            did = d.config.device_id
            p = power_now(allocs, did, w.now)
            if p > 0 and (did not in out or p > (out[did][0].power_w or 0)):
                out[did] = (Desired(Mode.CHARGE, p, target_soc=target), f"goal '{g.id}'")
    return out


def compute(w: World) -> Result:
    S, res = w.settings, Result()
    by_id = {d.config.device_id: d for d in w.devices}
    goal_plan = _plan_goals(w, res)
    entries: dict[str, ScheduleEntry] = {}

    # ---- 1. who wants what
    for d in w.devices:
        did, caps = d.config.device_id, d.config.caps
        want, reason = None, ""
        if did in w.overrides:
            want, reason = w.overrides[did], "manual override"
        elif w.backup_active and (b := backup_desired(d.actual.soc if d.actual.online else None, S.backup_pct, caps)):
            want, reason = b, "backup reserve"
        elif (e := resolve_entry(w.schedules, did, w.now, w.condition_ok, w.away)) is not None:
            want, reason, entries[did] = e.desired, f"schedule '{e.id}'", e
        elif did in goal_plan:
            want, reason = goal_plan[did]
        elif did == S.zero_import_device:
            want, reason = _zero_import(w, d), "zero-import"
        if want is None:
            continue
        if want.mode not in caps.supported_modes:
            res.notes.append(f"{did}: {want.mode.value} is not supported by this battery; left alone")
            continue
        res.wants[did], res.reasons[did] = want, reason

    # ---- 2. schedules that share one total across several batteries
    groups: dict[str, list[str]] = {}
    for did, e in entries.items():
        if did in res.wants and e.total_power_w and res.wants[did].mode in (Mode.CHARGE, Mode.DISCHARGE):
            groups.setdefault(e.id, []).append(did)
    for dids in groups.values():
        e, mode = entries[dids[0]], res.wants[dids[0]].mode
        items = []
        for did in dids:
            c, soc = by_id[did].config.caps, by_id[did].actual.soc
            soc = 50.0 if soc is None else soc
            weight = c.capacity_wh * ((100 - soc) if mode is Mode.CHARGE else soc) / 100
            items.append((did, max(weight, 1.0), c.max_charge_w if mode is Mode.CHARGE else c.max_discharge_w))
        for did, p in split_power(e.total_power_w, items).items():
            old = res.wants[did]
            res.wants[did] = replace(old, power_w=p) if p > 0 else Desired(Mode.HOLD)

    # ---- 3. clamps: device limits, SOC limits (with hysteresis), load rules
    for did in list(res.wants):
        d = by_id[did]
        caps = d.config.caps
        want = clamp(res.wants[did], max_charge_w=caps.max_charge_w, max_discharge_w=caps.max_discharge_w, min_soc=caps.min_soc)
        guarded = soc_guard(want, d.actual.soc if d.actual.online else None, replace(caps, min_soc=_floor(caps, w)),
                            held=d.guard_held)
        res.guard_held[did] = guarded.mode is not want.mode
        if res.reasons[did] not in ("manual override", "backup reserve"):
            guarded = apply_load_rules(guarded, w.load_blocks)
        res.wants[did] = guarded

    # ---- 4. grid guards for batteries nobody has instructed
    if w.grid_w is not None:
        free = [d for d in w.devices if d.config.device_id not in res.wants and d.actual.online
                and d.controllable and d.actual.soc is not None]
        if S.peak_shaving and S.import_limit_w and "discharge" not in w.load_blocks:
            cands = []
            for d in free:
                c = d.config.caps
                if Mode.DISCHARGE in c.supported_modes and c.max_discharge_w > 0 and d.actual.soc > _floor(c, w) + SOC_MARGIN:
                    cands.append((d.config.device_id, max(0.0, -(d.actual.power_w or 0.0)), c.max_discharge_w,
                                  max(c.capacity_wh * d.actual.soc / 100, 1.0)))
            for did, p in peak_shave(w.grid_w, S.import_limit_w, cands).items():
                res.wants[did] = Desired(Mode.DISCHARGE, p, reserve_soc=_floor(by_id[did].config.caps, w))
                res.reasons[did] = "peak shaving"
        if S.export_limit_w:
            discharging = {did: max(0.0, -(by_id[did].actual.power_w or 0.0)) for did, x in res.wants.items()
                           if x.mode is Mode.DISCHARGE and res.reasons[did] != "manual override" and by_id[did].actual.online}
            chargers = []
            for d in free:
                c = d.config.caps
                if (d.config.device_id not in res.wants and Mode.CHARGE in c.supported_modes and c.max_charge_w > 0
                        and d.actual.soc < 98 and "charge" not in w.load_blocks):
                    chargers.append((d.config.device_id, max(0.0, d.actual.power_w or 0.0), c.max_charge_w,
                                     max(c.capacity_wh * (100 - d.actual.soc) / 100, 1.0)))
            trims, charges = export_guard(w.grid_w, S.export_limit_w, {k: v for k, v in discharging.items() if v > 0}, chargers)
            for did, p in trims.items():
                old = res.wants[did]
                res.wants[did] = replace(old, power_w=p) if p > 0 else Desired(Mode.HOLD)
                res.reasons[did] += " (trimmed for export limit)"
            for did, p in charges.items():
                res.wants[did] = Desired(Mode.CHARGE, p, target_soc=100)
                res.reasons[did] = "export limit"

    # ---- 5. hand back batteries we forced earlier and nothing wants any more
    for d in w.devices:
        did = d.config.device_id
        if did not in res.wants and d.release_pending:
            res.wants[did], res.reasons[did] = Desired(Mode.AUTO), "handing back to the battery"

    # ---- 6. overall caps across everything combined
    online_net = sum(x.actual.power_w or 0.0 for x in w.devices if x.actual.online)
    res.wants.update(apply_totals(
        {k: (v, by_id[k].config.caps, by_id[k].config.priority) for k, v in res.wants.items()},
        charge_ceiling(S.charge_cap_w, S.import_limit_w, w.grid_w, online_net),
        S.discharge_cap_w,
    ))
    return res
