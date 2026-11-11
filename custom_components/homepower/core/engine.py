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
from .insights import auto_target_soc
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
    has_solar: bool | None = None   # panels attached to this battery? None = not known


@dataclass
class Settings:
    charge_cap_w: int | None = None
    discharge_cap_w: int | None = None
    import_limit_w: int | None = None
    export_limit_w: int | None = None
    zero_import_device: str | None = None
    peak_shaving: bool = False
    backup_pct: int = 50
    save_for_peak: bool = False   # during cheap hours, hold batteries instead of letting them discharge
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
    grid_fresh: bool = True      # False = the grid reading is too old to steer charging by
    condition_ok: Callable[[str], bool] | None = None
    backup_active: bool = False
    away: bool = False
    load_blocks: set[str] = field(default_factory=set)
    rates: RateTable | None = None
    forecast_kwh: float | None = None
    export_better: bool = False                    # selling surplus pays more than storing it and buying cheap later
    cheap_now: bool | None = None                  # is this a cheap-rate period? None = unknown / flat tariff
    usage_wh: Callable[[datetime, float], float | None] | None = None   # learned household use: (from, hours) -> Wh


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


def _auto_target(w: World, g: Goal, devs: list[DevIn], deadline: datetime, res: Result) -> int:
    """The goal's target follows the household's usual use after the deadline (up to the next cheap period),
    never above the target the user set. Falls back to that target until the usage has been learned."""
    hours = 16.0
    nxt = w.rates.minutes_to_cheap(deadline.time()) if w.rates is not None else None
    if nxt is not None:
        hours = max(4.0, min(24.0, (nxt[0] or 1440) / 60))
    use = w.usage_wh(deadline, hours) if w.usage_wh else None
    cap = sum(d.config.caps.capacity_wh for d in devs)
    floor = max((_floor(d.config.caps, w) for d in devs), default=0)
    auto = auto_target_soc(use, cap, floor)
    if auto is None:
        res.notes.append(f"goal '{g.id}': still learning your usual use, so it aims for {g.target_soc}% for now")
        return g.target_soc
    target = min(g.target_soc, auto)
    res.notes.append(f"goal '{g.id}': auto target {target}% (about {use / 1000:.1f} kWh of usual use over the {hours:.0f} h after the deadline)")
    return target


def _plan_goals(w: World, res: Result) -> dict[str, tuple[Desired, str]]:
    out: dict[str, tuple[Desired, str, str]] = {}      # device -> (instruction, reason, source: grid / solar / both)
    if not w.goals:
        return out
    if w.rates is None and any(g.enabled and g.source != "solar" for g in w.goals):
        res.notes.append("goals need price data: set a rates entity or a fixed tariff")
    for g in w.goals:
        if not g.enabled or (w.rates is None and g.source == "grid"):
            continue
        deadline = next_deadline(g, w.now)
        if deadline is None:
            continue
        devs = [d for d in w.devices if g.covers(d.config.device_id) and d.actual.online and d.actual.soc is not None
                and Mode.CHARGE in d.config.caps.supported_modes and d.config.caps.max_charge_w > 0]
        if not devs:
            if any(g.covers(d.config.device_id) for d in w.devices):
                res.notes.append(f"goal '{g.id}': none of its batteries can charge right now (offline, or charge control is not "
                                 "supported: EcoFlow STREAM only offers Auto and Hold)")
            continue
        target = g.target_soc
        if g.auto_target:
            target = _auto_target(w, g, devs, deadline, res)
        if g.use_forecast:
            target = adjust_target_for_forecast(
                target, w.forecast_kwh, sum(d.config.caps.capacity_wh for d in devs) / 1000, w.settings.forecast_share)
        allocs: list[Alloc] = []
        if g.source != "solar" and w.rates is not None:
            needs = [Need(d.config.device_id, d.actual.soc, d.config.caps.capacity_wh, d.config.caps.max_charge_w, target)
                     for d in devs]
            slots = w.rates.slots_between(w.now, deadline)
            if not slots:
                res.notes.append(f"goal '{g.id}': no price data before {deadline:%H:%M}")
                if g.source == "grid":
                    continue
            else:
                allocs, unmet = plan_charge(w.now, deadline, needs, slots, g.max_power_w)
                res.allocs += allocs
                for did in unmet:
                    res.notes.append(f"goal '{g.id}': cannot fully reach {target}% by {deadline:%H:%M} on {did}")
        if g.source != "grid" and w.export_better:
            res.notes.append(f"goal '{g.id}': exporting pays more than storing right now, so spare solar is left to export")
        for d in devs:
            did = d.config.device_id
            p = power_now(allocs, did, w.now)
            if p > 0 and (did not in out or p > (out[did][0].power_w or 0)):
                out[did] = (Desired(Mode.CHARGE, p, target_soc=target), f"goal '{g.id}'", "both" if g.source == "both" else "grid")
            elif (p <= 0 and g.source != "grid" and not w.export_better and did not in out and d.actual.soc < target):
                # no grid slot right now: take whatever solar would otherwise be exported (and nothing from the grid)
                out[did] = (Desired(Mode.CHARGE, g.max_power_w, target_soc=target), f"goal '{g.id}'", "solar")
    return out


def compute(w: World) -> Result:
    S, res = w.settings, Result()
    by_id = {d.config.device_id: d for d in w.devices}
    goal_plan = _plan_goals(w, res)
    entries: dict[str, ScheduleEntry] = {}
    goal_src: dict[str, str] = {}      # batteries whose instruction comes from a goal -> its charge source

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
            want, reason, goal_src[did] = goal_plan[did]
        elif did == S.zero_import_device:
            want, reason = _zero_import(w, d), "zero-import"
        if want is None:
            continue
        if (did in entries and want.mode is Mode.CHARGE and entries[did].source == "solar" and Mode.CHARGE not in caps.supported_modes
                and Mode.HOLD in caps.supported_modes):
            if d.has_solar is False:
                res.notes.append(f"{did}: solar-only charging does nothing for a battery without panels that cannot be told to charge; left alone")
                continue
            # No charge command on this battery, but Hold stops it discharging while its own panels keep charging it.
            want, reason = Desired(Mode.HOLD, reserve_soc=want.reserve_soc), reason + " (solar only)"
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

    # ---- 2b. where the charging energy may come from: the grid (fixed power), solar surplus only, or both
    src = {did: e.source for did, e in entries.items()}
    src.update({did: s for did, s in goal_src.items() if did not in entries})
    trackers = [did for did, s in src.items() if did in res.wants and res.wants[did].mode is Mode.CHARGE and s in ("solar", "both")]
    if trackers:
        fixed = sum(max(0.0, o.actual.power_w or 0.0) for o in w.devices
                    if o.actual.online and o.config.device_id not in trackers)   # deliberate charging elsewhere: not surplus
        for did in trackers:
            source, d, old = src[did], by_id[did], res.wants[did]
            caps = d.config.caps
            from_goal = did not in entries
            label = " (solar only)" if source == "solar" else " (grid + solar)"

            def idle() -> None:
                # A schedule's window is explicit, so the battery waits on Hold; a goal can run all day, so the battery is
                # simply left to its own control (it must still be able to power the house).
                if from_goal:
                    del res.wants[did], res.reasons[did]
                else:
                    res.wants[did] = Desired(Mode.HOLD, reserve_soc=old.reserve_soc)
                    res.reasons[did] += label

            if w.grid_w is None or not w.grid_fresh or not d.actual.online:
                # Without a live grid reading the surplus is unknown: solar-only waits (never imports), grid+solar uses its fixed power.
                if source == "solar":
                    res.notes.append(f"{did}: solar-only charging needs a live grid meter reading; waiting until there is one")
                    idle()
                else:
                    res.reasons[did] += label
                continue
            own = max(0.0, d.actual.power_w or 0.0)
            export = -(w.grid_w - fixed)                       # what the house is sending out, ignoring deliberate charging
            surplus = max(0.0, min(own + export / len(trackers), float(caps.max_charge_w)))
            cur = d.actual.set_power_w if d.actual.mode is Mode.CHARGE and d.actual.set_power_w else 0
            if surplus < 30:
                surplus = 0.0
            elif cur and abs(surplus - cur) < 50:
                surplus = float(cur)                           # small changes do not re-send the command
            if source == "solar":
                cap = old.power_w if old.power_w is not None else caps.max_charge_w
                p = int(min(surplus, cap))
                if p <= 0:
                    idle()
                    continue
                res.wants[did] = replace(old, power_w=p)
            else:
                base = old.power_w if old.power_w is not None else caps.max_charge_w
                res.wants[did] = replace(old, power_w=int(max(base, surplus)))
            res.reasons[did] += label

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

    # ---- 4b. hold charge back: a battery kept as reserve, and everything during cheap-rate hours
    def others_have_charge(margin: int) -> bool:
        return any(o.config.extra.get("role") != "reserve" and o.actual.online and o.actual.soc is not None
                   and o.actual.soc > _floor(o.config.caps, w) + SOC_MARGIN + margin for o in w.devices)

    # Never get in the way of solar charging: not this battery's own panels, nor surplus from anywhere else in the house.
    solar_about = any((o.actual.pv_w or 0) > 50 for o in w.devices if o.actual.online) or (w.grid_w is not None and w.grid_w < -50)
    for d in w.devices:
        did, caps = d.config.device_id, d.config.caps
        if (did in res.wants or not d.controllable or not d.actual.online or Mode.HOLD not in caps.supported_modes or solar_about):
            continue
        # A reserve battery already being held is released a little later than it is taken (no flapping at the edge).
        if d.config.extra.get("role") == "reserve" and others_have_charge(1 if d.release_pending else 3):
            res.wants[did], res.reasons[did] = Desired(Mode.HOLD), "kept in reserve while other batteries have charge"
        elif S.save_for_peak and w.cheap_now:
            res.wants[did], res.reasons[did] = Desired(Mode.HOLD), "saving charge for peak-rate hours"

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
