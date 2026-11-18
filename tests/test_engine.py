from datetime import datetime, time, timedelta, timezone

import pytest

from core.alerts import AlertTracker
from core.energy import EnergyMeter, Ledger
from core.engine import DevIn, Settings, World, compute
from core.goals import Goal, next_deadline
from core.grid import apply_load_rules, backup_desired, export_guard, peak_shave, split_power
from core.models import Actual, Capabilities, Desired, DeviceConfig, Mode
from core.planner import Need, adjust_target_for_forecast, plan_charge, power_now
from core.preview import find_conflicts, preview
from core.rates import RateTable, Slot, parse_rates
from core.schedule import ScheduleEntry

UTC = timezone.utc
NOW = datetime(2026, 11, 10, 22, 0, tzinfo=UTC)   # a Tuesday


def dev(did="a", soc=50.0, power=0.0, cap=10000, mc=3000, md=3000, min_soc=10, modes=None, mode=Mode.AUTO,
        set_w=None, release=False, held=False, controllable=True, online=True, grid=None):
    caps = Capabilities(cap, mc, md, min_soc, frozenset(modes) if modes else frozenset(Mode))
    cfg = DeviceConfig(did, did, "x", caps, extra={"control_enabled": controllable})
    act = Actual(online=online, soc=soc if online else None, power_w=power, mode=mode, set_power_w=set_w, grid_w=grid)
    return DevIn(cfg, act, release_pending=release, guard_held=held, controllable=controllable)


def world(devs, **kw):
    st = kw.pop("settings", Settings(import_limit_w=None))
    return World(now=kw.pop("now", NOW), devices=devs, settings=st, **kw)


def sched(sid="s", mode=Mode.CHARGE, power=2000, start=time(21), end=time(23), prio=0, **kw):
    return ScheduleEntry(sid, frozenset(kw.pop("devs", ())), start, end, Desired(mode, power), priority=prio, **kw)


# ------------------------------------------------------------------ split_power
def test_split_power_proportional_and_capped():
    r = split_power(3000, [("a", 1, 5000), ("b", 2, 5000)])
    assert r == {"a": 1000, "b": 2000}
    r = split_power(5000, [("a", 1, 1000), ("b", 1, 5000)])      # a saturates, b takes the rest
    assert r == {"a": 1000, "b": 4000}
    assert split_power(9000, [("a", 1, 1000), ("b", 1, 2000)]) == {"a": 1000, "b": 2000}
    assert split_power(1000, []) == {} and split_power(0, [("a", 1, 100)]) == {"a": 0}
    assert split_power(1000, [("a", 0, 5000), ("b", 0, 5000)]) == {"a": 500, "b": 500}   # zero weights -> even


# ------------------------------------------------------------------ rates
def test_parse_rates_octopus_and_nordpool_shapes():
    octo = {"rates": [{"start": "2026-11-10T23:30:00+00:00", "end": "2026-11-11T00:00:00+00:00", "value_inc_vat": 7.5},
                      {"start": "2026-11-10T22:00:00Z", "end": "2026-11-10T22:30:00Z", "value_inc_vat": 24.1}]}
    s = parse_rates(octo)
    assert [x.price for x in s] == [24.1, 7.5] and s[0].start < s[1].start
    npool = {"raw_today": [{"start": "2026-11-10T00:00:00+00:00", "end": "2026-11-10T01:00:00+00:00", "value": 0.2}],
             "raw_tomorrow": [{"start": "2026-11-11T00:00:00+00:00", "end": "2026-11-11T01:00:00+00:00", "value": 0.1}]}
    assert [x.price for x in parse_rates(npool)] == [0.2, 0.1]
    assert parse_rates({"rates": [{"start": "garbage"}, "x", {"start": "2026-11-10T00:00:00Z", "value": "nan?"}]}) == []
    assert parse_rates(None) == []


def test_rate_table_fixed_tariff_window_crosses_midnight():
    rt = RateTable(peak=30.0, offpeak=7.5, offpeak_start=time(0, 30), offpeak_end=time(4, 30))
    assert rt.price_at(datetime(2026, 11, 11, 1, 0, tzinfo=UTC)) == 7.5
    assert rt.price_at(datetime(2026, 11, 11, 12, 0, tzinfo=UTC)) == 30.0
    rt2 = RateTable(peak=30.0, offpeak=7.5, offpeak_start=time(23, 30), offpeak_end=time(5, 30))
    assert rt2.price_at(datetime(2026, 11, 10, 23, 45, tzinfo=UTC)) == 7.5
    assert rt2.price_at(datetime(2026, 11, 11, 3, 0, tzinfo=UTC)) == 7.5
    sl = rt2.slots_between(NOW, NOW + timedelta(hours=4))
    assert sum((s.end - s.start).total_seconds() for s in sl) == 4 * 3600
    assert RateTable().price_at(NOW) is None and RateTable().slots_between(NOW, NOW + timedelta(hours=1)) == []


def cheap_night_rates():
    rt = RateTable(peak=30.0, offpeak=7.5, offpeak_start=time(23, 30), offpeak_end=time(5, 30))
    return rt


# ------------------------------------------------------------------ planner
def test_planner_picks_cheapest_slots_and_respects_power():
    now = datetime(2026, 11, 10, 18, 0, tzinfo=UTC)
    deadline = datetime(2026, 11, 11, 7, 0, tzinfo=UTC)
    slots = cheap_night_rates().slots_between(now, deadline)
    # 5 kWh needed (50% of 10 kWh) at up to 3 kW
    allocs, unmet = plan_charge(now, deadline, [Need("a", 50, 10000, 3000, 100)], slots)
    assert not unmet and allocs
    assert all(a.price == 7.5 for a in allocs)                    # only cheap slots used
    assert all(a.start >= datetime(2026, 11, 10, 23, 30, tzinfo=UTC) for a in allocs)
    got = sum(a.power_w * (a.end - a.start).total_seconds() / 3600 for a in allocs)
    assert got * 0.92 >= 5000 - 1                                  # enough energy after efficiency losses
    assert max(a.power_w for a in allocs) <= 3000


def test_planner_unmet_when_impossible_and_ceiling_shared():
    now = datetime(2026, 11, 10, 22, 0, tzinfo=UTC)
    deadline = now + timedelta(hours=1)
    slots = [Slot(now, deadline, 10.0)]
    allocs, unmet = plan_charge(now, deadline, [Need("a", 0, 10000, 3000, 100)], slots)
    assert "a" in unmet and unmet["a"] > 5000
    allocs, _ = plan_charge(now, deadline, [Need("a", 0, 10000, 3000, 100), Need("b", 0, 10000, 3000, 100)], slots, ceiling_w=4000)
    assert power_now(allocs, "a", now) + power_now(allocs, "b", now) <= 4000


def test_forecast_reduces_target_with_floor():
    assert adjust_target_for_forecast(100, None, 10) == 100
    assert adjust_target_for_forecast(100, 8.0, 10) == 60          # 8 kWh * 0.5 / 10 kWh = 40 points
    assert adjust_target_for_forecast(100, 100.0, 10) == 20        # never below the floor
    assert adjust_target_for_forecast(15, 50.0, 10) == 15          # floor never raises a low target


def test_next_deadline():
    g = Goal("g", frozenset(), 80, time(7), frozenset({1}))        # Tuesdays only
    assert next_deadline(g, datetime(2026, 11, 10, 22, 0, tzinfo=UTC)) is None   # next Tue is 6 days away
    g2 = Goal("g", frozenset(), 80, time(7))
    assert next_deadline(g2, datetime(2026, 11, 10, 22, 0, tzinfo=UTC)) == datetime(2026, 11, 11, 7, 0, tzinfo=UTC)
    assert next_deadline(g2, datetime(2026, 11, 10, 3, 0, tzinfo=UTC)) == datetime(2026, 11, 10, 7, 0, tzinfo=UTC)


# ------------------------------------------------------------------ grid policies
def test_peak_shave_discharges_only_the_excess_and_releases():
    cands = [("a", 0.0, 3000, 5000.0), ("b", 0.0, 3000, 5000.0)]
    r = peak_shave(8000, 7000, cands)                              # 8000 - 6650 target = 1350 over
    assert sum(r.values()) == 1350 and r["a"] == r["b"]
    assert peak_shave(6000, 7000, cands) == {}
    assert sum(peak_shave(9000, 7000, [("a", 0.0, 1000, 1.0)]).values()) == 1000   # capped by the battery


def test_export_guard_trims_discharge_then_charges():
    trims, charges = export_guard(-5000, 3000, {"a": 2000.0}, [("b", 0.0, 3000, 1.0)])
    # 5000 export vs 2850 target: excess 2150 -> trim a's 2000, then 150 charge on b
    assert trims == {"a": 0} and charges == {"b": 150}
    assert export_guard(-1000, 3000, {"a": 500.0}, []) == ({}, {})


def test_load_rules_and_backup():
    assert apply_load_rules(Desired(Mode.DISCHARGE, 1000), {"discharge"}).mode is Mode.HOLD
    assert apply_load_rules(Desired(Mode.CHARGE, 1000), {"discharge"}).mode is Mode.CHARGE
    caps = Capabilities(5000, 3000, 3000, 10)
    assert backup_desired(30, 50, caps).mode is Mode.CHARGE and backup_desired(60, 50, caps) is None
    assert backup_desired(30, 50, Capabilities(5000, 0, 0, 10, frozenset({Mode.AUTO, Mode.HOLD}))) is None


# ------------------------------------------------------------------ engine
def test_priority_override_beats_schedule_beats_goal():
    d = dev()
    g = Goal("g", frozenset(), 100, time(7))
    w = world([d], schedules=[sched(power=2000)], goals=[g], rates=cheap_night_rates(),
              overrides={"a": Desired(Mode.DISCHARGE, 500)})
    r = compute(w)
    assert r.wants["a"].mode is Mode.DISCHARGE and r.reasons["a"] == "manual override"
    w.overrides.clear()
    r = compute(w)
    assert r.wants["a"].power_w == 2000 and r.reasons["a"].startswith("schedule")
    w.schedules.clear()
    r = compute(world([d], goals=[g], rates=cheap_night_rates(), now=datetime(2026, 11, 11, 0, 0, tzinfo=UTC)))
    assert r.reasons["a"].startswith("goal") and r.wants["a"].mode is Mode.CHARGE


def test_goal_waits_for_cheap_slot_then_charges():
    g = Goal("g", frozenset(), 100, time(7))
    r = compute(world([dev()], goals=[g], rates=cheap_night_rates()))                       # 22:00, peak price
    assert "a" not in r.wants
    r = compute(world([dev()], goals=[g], rates=cheap_night_rates(), now=datetime(2026, 11, 11, 0, 30, tzinfo=UTC)))
    assert r.wants["a"].mode is Mode.CHARGE and r.wants["a"].power_w > 0


def test_goal_without_prices_explains_itself():
    r = compute(world([dev()], goals=[Goal("g", frozenset(), 100, time(7))]))
    assert not r.wants and any("price data" in n for n in r.notes)


def test_goal_forecast_lowers_the_target():
    g = Goal("g", frozenset(), 100, time(7), use_forecast=True)
    r = compute(world([dev(soc=50)], goals=[g], rates=cheap_night_rates(), forecast_kwh=8.0,
                      now=datetime(2026, 11, 11, 0, 30, tzinfo=UTC)))
    assert r.wants["a"].target_soc == 60


def test_ecoflow_style_battery_never_gets_charge_instructions():
    d = dev(modes={Mode.AUTO, Mode.HOLD})
    r = compute(world([d], schedules=[sched()]))
    assert "a" not in r.wants and any("not supported" in n for n in r.notes)


def test_soc_target_in_schedule_holds_with_hysteresis():
    s = [sched(power=2000)]
    s[0] = ScheduleEntry("s", frozenset(), time(21), time(23), Desired(Mode.CHARGE, 2000, target_soc=80))
    r = compute(world([dev(soc=80)], schedules=s))
    assert r.wants["a"].mode is Mode.HOLD and r.guard_held["a"]
    r = compute(world([dev(soc=79, held=True)], schedules=s))
    assert r.wants["a"].mode is Mode.HOLD
    r = compute(world([dev(soc=77, held=True)], schedules=s))
    assert r.wants["a"].mode is Mode.CHARGE and not r.guard_held["a"]


def test_total_power_is_balanced_by_capacity_and_headroom():
    e = ScheduleEntry("s", frozenset(), time(21), time(23), Desired(Mode.CHARGE, 3000), total_power_w=4000)
    a, b = dev("a", soc=50, cap=10000, mc=5000), dev("b", soc=50, cap=5000, mc=5000)
    r = compute(world([a, b], schedules=[e]))
    assert r.wants["a"].power_w + r.wants["b"].power_w in (3999, 4000, 4001)
    assert r.wants["a"].power_w == pytest.approx(2 * r.wants["b"].power_w, abs=2)   # a has twice the room


def test_presence_filters_schedules():
    away = sched("away", power=1000, presence="away")
    home = sched("home", power=3000, presence="home")
    assert compute(world([dev()], schedules=[away, home], away=True)).wants["a"].power_w == 1000
    assert compute(world([dev()], schedules=[away, home], away=False)).wants["a"].power_w == 3000


def test_load_rule_blocks_discharge_but_not_override():
    s = [sched(mode=Mode.DISCHARGE, power=1000)]
    r = compute(world([dev()], schedules=s, load_blocks={"discharge"}))
    assert r.wants["a"].mode is Mode.HOLD
    r = compute(world([dev()], overrides={"a": Desired(Mode.DISCHARGE, 1000)}, load_blocks={"discharge"}))
    assert r.wants["a"].mode is Mode.DISCHARGE


def test_backup_mode_charges_to_reserve_and_raises_discharge_floor():
    st = Settings(import_limit_w=None, backup_pct=50)
    r = compute(world([dev(soc=30)], settings=st, backup_active=True))
    assert r.reasons["a"] == "backup reserve" and r.wants["a"].mode is Mode.CHARGE
    r = compute(world([dev(soc=55)], settings=st, backup_active=True, schedules=[sched(mode=Mode.DISCHARGE, power=1000)]))
    assert r.wants["a"].mode is Mode.DISCHARGE            # 55 > 50 + hysteresis not held
    r = compute(world([dev(soc=50)], settings=st, backup_active=True, schedules=[sched(mode=Mode.DISCHARGE, power=1000)]))
    assert r.wants["a"].mode is Mode.HOLD                  # never below the backup reserve


def test_peak_shaving_only_when_enabled_and_over_the_limit():
    d = dev(soc=80)
    st = Settings(import_limit_w=7000, peak_shaving=True)
    r = compute(world([d], settings=st, grid_w=9000))
    assert r.wants["a"].mode is Mode.DISCHARGE and r.reasons["a"] == "peak shaving"
    assert r.wants["a"].power_w == pytest.approx(9000 - 6650, abs=2)
    assert "a" not in compute(world([d], settings=st, grid_w=3000)).wants
    assert "a" not in compute(world([d], settings=Settings(import_limit_w=7000), grid_w=9000)).wants
    assert "a" not in compute(world([dev(soc=11)], settings=st, grid_w=9000)).wants        # at its floor already
    assert "a" not in compute(world([dev(soc=80, controllable=False)], settings=st, grid_w=9000)).wants


def test_peak_shaving_released_after_the_peak():
    d = dev(soc=80, mode=Mode.DISCHARGE, set_w=2000, power=-2000, release=True)
    st = Settings(import_limit_w=7000, peak_shaving=True)
    r = compute(world([d], settings=st, grid_w=6500))        # 8500 W without the battery: still needs help
    assert r.wants["a"].mode is Mode.DISCHARGE and r.wants["a"].power_w == pytest.approx(2000 + (6500 - 6650), abs=2)
    r = compute(world([d], settings=st, grid_w=3000))        # 5000 W without the battery: peak is over
    assert r.wants["a"].mode is Mode.AUTO and r.reasons["a"].startswith("handing back")


def test_export_limit_charges_idle_battery_and_trims_scheduled_discharge():
    st = Settings(import_limit_w=None, export_limit_w=3000)
    r = compute(world([dev(soc=40)], settings=st, grid_w=-5000))
    assert r.wants["a"].mode is Mode.CHARGE and r.reasons["a"] == "export limit"
    assert r.wants["a"].power_w == pytest.approx(5000 - 2850, abs=2)
    s = [sched(mode=Mode.DISCHARGE, power=2500)]
    r = compute(world([dev(soc=80, power=-2500)], settings=st, schedules=s, grid_w=-4000))
    assert r.wants["a"].power_w == pytest.approx(2500 - (4000 - 2850), abs=2) and "trimmed" in r.reasons["a"]


def test_zero_import_battery_follows_grid_when_nothing_scheduled():
    st = Settings(import_limit_w=None, zero_import_device="a")
    r = compute(world([dev(soc=60)], settings=st, grid_w=800))
    assert r.wants["a"].mode is Mode.DISCHARGE and r.wants["a"].power_w == 800
    r = compute(world([dev(soc=60)], settings=st, grid_w=None))                  # fail safe
    assert r.wants["a"].mode is Mode.HOLD


def test_caps_and_import_limit_ceiling_apply_to_everything():
    a, b = dev("a", mc=3000), dev("b", mc=3000)
    r = compute(world([a, b], schedules=[sched(power=3000)], settings=Settings(charge_cap_w=4000, import_limit_w=None)))
    assert r.wants["a"].power_w + r.wants["b"].power_w == 4000
    r = compute(world([a, b], schedules=[sched(power=3000)], grid_w=5500, settings=Settings(import_limit_w=7000)))
    assert sum((x.power_w or 0) for x in r.wants.values()) <= 1500                # only 1500 W of headroom left


def test_offline_battery_is_not_used_by_goals_or_guards():
    st = Settings(import_limit_w=7000, peak_shaving=True)
    assert compute(world([dev(online=False)], settings=st, grid_w=9000)).wants == {}


# ------------------------------------------------------------------ energy, ledger, alerts
def test_energy_meter_integrates_and_ignores_gaps():
    m = EnergyMeter()
    m.add("bat:a", 0, 1000)
    d_in, d_out = m.add("bat:a", 120, 1000)
    assert d_in == pytest.approx(1000 * 120 / 3.6e6) and d_out == 0               # two minutes at 1 kW
    for t in range(240, 3601, 120):
        m.add("bat:a", t, 1000)
    assert m.get("bat:a_in") == pytest.approx(1.0, rel=1e-6)                       # an hour at 1 kW = 1 kWh
    m2 = EnergyMeter()
    m2.add("x", 0, -2000)
    assert m2.add("x", 1800, -2000)[1] == pytest.approx(0.0)                      # gap > 5 min: not integrated
    assert m2.add("x", 1860, -2000)[1] == pytest.approx(2000 * 60 / 3.6e6)
    m3 = EnergyMeter({"k_in": 5})
    m3.add("k", 0, None)
    assert m3.get("k_in") == 5


def test_ledger_counts_savings_when_the_energy_is_used():
    L = Ledger()
    # night: an empty battery charges 10 kWh from the grid at 0.13
    L.add("2026-11-11", "a", 0.0, 1.0, 0.0, 0.0, 0.13, 0.0, 0.0, 0.13)
    L.add("2026-11-12", "a", 10.0, 1.0, 0.0, 0.0, 0.13, 0.0, 10.0)
    assert L.saved("2026-11-12") == pytest.approx(0.0)                      # charging alone is not a loss
    assert L.days["2026-11-12"]["bought"] == pytest.approx(1.3)
    assert L.stored_cost("a") == pytest.approx(0.13)
    # day: 2 kWh used in the house at 0.29 -> saves 2 x (0.29 - 0.13)
    L.add("2026-11-12", "a", 0.0, 0.0, 2.0, 0.0, 0.29, 0.0, 8.0)
    assert L.saved("2026-11-12") == pytest.approx(2 * (0.29 - 0.13))
    # discharging at the cheap rate it was bought at saves nothing
    L.add("2026-11-12", "a", 0.0, 0.0, 1.0, 0.0, 0.13, 0.0, 7.0)
    assert L.saved("2026-11-12") == pytest.approx(2 * 0.16)
    assert L.saved("2026-11") == pytest.approx(0.32)


def test_ledger_solar_losses_export_and_unknown_price():
    L = Ledger()
    L.add("d", "s", 0.0, 0.0, 0.0, 0.0, 0.29, 0.0, 0.0, 0.13)
    L.add("d", "s", 5.0, 0.0, 0.0, 0.0, 0.29, 0.0, 5.0)                   # 5 kWh of solar: costs nothing
    assert L.stored_cost("s") == pytest.approx(0.0)
    L.add("d", "s", 0.0, 0.0, 1.0, 0.0, 0.29, 0.0, 4.0)
    assert L.saved("d") == pytest.approx(0.29)                              # all of it is saving
    # half the discharge went out to the grid: that half earns the export price, not the import price
    L.add("d", "s", 0.0, 0.0, 2.0, 0.5, 0.29, 0.15, 2.0)
    assert L.saved("d") == pytest.approx(0.29 + 1.0 * 0.29 + 1.0 * 0.15)
    # losses: 10 kWh bought at 0.10 but only 9 kWh stored -> each stored kWh cost more
    M = Ledger()
    M.add("d", "b", 0.0, 1.0, 0.0, 0.0, 0.10, 0.0, 0.0, 0.10)
    M.add("d", "b", 10.0, 1.0, 0.0, 0.0, 0.10, 0.0, 9.0)
    assert M.stored_cost("b") == pytest.approx(1.0 / 9.0)
    # no price known: nothing booked, but the stored level is followed
    M.add("d", "b", 0.0, 0.0, 1.0, 0.0, None, 0.0, 8.0)
    assert M.days["d"]["discharged"] == 0.0 and M.stores["b"][0] == 8.0
    # a battery first seen part full: its energy is assumed to have cost the cheapest rate around
    N = Ledger()
    N.add("d", "c", 0.0, 0.0, 1.0, 0.0, 0.30, 0.0, 6.0, 0.12)
    assert N.saved("d") == pytest.approx(0.30 - 0.12)
    N.forget(set())
    assert N.stores == {}


def test_alert_tracker_debounces_and_clears():
    t = AlertTracker()
    assert t.observe("offline:a", True, 0, after_s=60) is None
    assert t.observe("offline:a", True, 30, after_s=60) is None
    assert t.observe("offline:a", True, 61, after_s=60) == "raised"
    assert t.observe("offline:a", True, 90, after_s=60) is None                   # raised once only
    assert t.observe("offline:a", False, 100) == "cleared"
    assert t.observe("offline:a", False, 110) is None
    assert t.observe("blip", True, 0, after_s=60) is None and t.observe("blip", False, 10) is None


# ------------------------------------------------------------------ preview
def test_preview_merges_segments_and_flags_conflicts():
    s1 = sched("s1", power=2000, start=time(23), end=time(2), prio=0)
    s2 = sched("s2", mode=Mode.DISCHARGE, power=1000, start=time(1), end=time(3), prio=0)
    pv = preview([s1], ["a"], NOW, hours=6)
    segs = pv["a"]
    assert len(segs) == 1 and segs[0].start == datetime(2026, 11, 10, 23, tzinfo=UTC) and segs[0].end == datetime(2026, 11, 11, 2, tzinfo=UTC)
    c = find_conflicts([s1, s2], ["a"], NOW, hours=6)
    assert len(c) == 1 and c[0]["schedules"] == ["s1", "s2"]
    assert c[0]["start"] == datetime(2026, 11, 11, 1, tzinfo=UTC) and c[0]["end"] == datetime(2026, 11, 11, 2, tzinfo=UTC)
    s2b = sched("s2", mode=Mode.DISCHARGE, power=1000, start=time(1), end=time(3), prio=5)
    assert find_conflicts([s1, s2b], ["a"], NOW, hours=6) == []                   # higher priority resolves it


def test_rate_units_auto_detect():
    from core.rates import to_main_units as u
    assert u(0.285) == 0.285 and u(28.5) == pytest.approx(0.285) and u(7.5) == pytest.approx(0.075)   # pounds vs pence
    assert u(0.285, "pence") == pytest.approx(0.00285) and u(28.5, "main") == 28.5


def test_settings_parse_and_validate():
    from core.settings import SettingsError, build, clean
    c = clean({"import_limit_w": "6000", "peak_shaving": 1, "price_peak": "28.5", "price_offpeak": "", "offpeak_start": "23:30",
               "load_rules": [{"entity": "sensor.ev", "above_w": "1000", "block": "discharge"}, {"entity": ""}], "bogus": 1})
    assert c["import_limit_w"] == 6000 and c["peak_shaving"] is True and c["price_offpeak"] is None and "bogus" not in c
    assert c["load_rules"] == [{"entity": "sensor.ev", "block": "discharge", "above_w": 1000.0}]
    a = build(c)
    assert a.engine.import_limit_w == 6000 and a.engine.peak_shaving and a.offpeak_start.hour == 23
    for bad in ({"offpeak_start": "25:99"}, {"backup_reserve_pct": 200}, {"forecast_share": 3}, {"notify_service": "nodot"}, {"rates_unit": "x"},
                {"load_rules": [{"entity": "sensor.x", "block": "sideways"}]}):
        with pytest.raises(SettingsError):
            clean(bad)
    assert build({}).engine.import_limit_w == 7000 and build({}).engine.backup_pct == 50


def test_naive_rate_timestamps_do_not_break_comparisons():
    naive = {"rates": [{"start": "2026-11-10T22:00:00", "end": "2026-11-10T22:30:00", "value_inc_vat": 7.5}]}
    s = parse_rates(naive)[0]
    assert s.start.tzinfo is not None and s.start <= NOW < s.end                    # read as UTC by default
    london = __import__("zoneinfo").ZoneInfo("Europe/London")
    assert parse_rates(naive, london)[0].start.utcoffset() == london.utcoffset(datetime(2026, 11, 10, 22))
    RateTable(parse_rates(naive)).slots_between(NOW, NOW + timedelta(hours=1))      # must not raise


def test_setpoint_rounding_is_not_a_mismatch():
    from core.reconcile import diff
    d = Desired(Mode.CHARGE, 1999)
    assert diff(d, Actual(True, mode=Mode.CHARGE, set_power_w=2000)) == []
    assert diff(Desired(Mode.CHARGE, 2000), Actual(True, mode=Mode.CHARGE, set_power_w=1500))   # a real difference is still one
    assert diff(Desired(Mode.CHARGE, 60), Actual(True, mode=Mode.CHARGE, set_power_w=100))


def test_goal_on_batteries_that_cannot_charge_explains_itself():
    g = Goal("g", frozenset(), 100, time(7))
    r = compute(world([dev(modes={Mode.AUTO, Mode.HOLD})], goals=[g], rates=cheap_night_rates(), now=datetime(2026, 11, 11, 0, 30, tzinfo=UTC)))
    assert not r.wants and any("can charge right now" in n for n in r.notes)


def test_dry_run_setting_round_trips_and_defaults_off():
    from core.settings import build, clean
    assert build({}).dry_run is False
    assert clean({"dry_run": True}) == {"dry_run": True} and build(clean({"dry_run": True})).dry_run is True


def test_tariff_comparison_parses_prices_and_ranks_tariffs():
    from core.tariffs import compare, parse_tariffs
    import pytest
    ts = parse_tariffs("Flat; peak=0.30; standing=0.50\nGo; peak=0.28; cheap=0.07; window=00:30-05:30; export=0.15")
    imp, exp = [0.0] * 48, [0.0] * 48
    imp[2] = 4.0          # 01:00, in the cheap window
    imp[20] = 2.0         # 10:00, outside it
    exp[24] = 3.0
    r = compare({"2026-11-10": {"imp": imp, "exp": exp}}, ts)
    by = {x["name"]: x for x in r["tariffs"]}
    assert by["Flat"]["total"] == round(6 * 0.30 + 0.50, 2)
    assert by["Go"]["import_cost"] == round(4 * 0.07 + 2 * 0.28, 2) and by["Go"]["export_earned"] == 0.45
    assert r["tariffs"][0]["name"] == "Go" and r["days"] == 1
    for bad in ("x; peak=abc", "x; cheap=0.1; peak=0.3", "x; speed=3; peak=1", "peak=1"):
        with pytest.raises(ValueError):
            parse_tariffs(bad)
    wrap = parse_tariffs("n; peak=0.3; cheap=0.1; window=23:30-07:30")
    assert compare({"d": {"imp": [1.0] * 48, "exp": [0.0] * 48}}, wrap)["tariffs"][0]["import_cost"] == round(16 * 0.1 + 32 * 0.3, 2)


def test_diverter_runs_in_cheap_hours_and_releases_afterwards():
    from core.insights import Diverter
    d = Diverter()
    assert d.decide(0, 50, 2000, 500, 95, cheap=True) is None          # must hold for HOLD_S first
    assert d.decide(Diverter.HOLD_S + 1, 50, 2000, 500, 95, cheap=True) == "on"      # importing, battery low: still on, it is cheap
    assert d.decide(500, 50, 2000, 500, 95, cheap=True) is None          # stays on while cheap
    assert d.decide(600, 50, 2000, 500, 95, cheap=False) is None         # cheap ended: the normal rule needs HOLD_S
    assert d.decide(600 + Diverter.HOLD_S + 1, 50, 2000, 500, 95, cheap=False) == "off"


def test_usage_history_records_half_hours_and_survives_a_round_trip():
    from datetime import datetime
    from core.insights import UsageHistory
    u = UsageHistory()
    u.add(datetime(2026, 11, 10, 1, 40), 0.2, 0.0)
    u.add(datetime(2026, 11, 10, 1, 50), 0.1, 0.05)
    u2 = UsageHistory(u.dump())
    assert round(u2.days["2026-11-10"]["imp"][3], 3) == 0.3 and round(u2.days["2026-11-10"]["exp"][3], 3) == 0.05


def test_forecast_reserve_floor_stops_discharge_below_it_but_not_above():
    sch = [sched(mode=Mode.DISCHARGE, power=2000)]
    assert compute(world([dev("a", soc=45)], schedules=sch)).wants["a"].mode is Mode.DISCHARGE
    assert compute(world([dev("a", soc=45)], schedules=sch, reserve_floor_pct=50)).wants["a"].mode is not Mode.DISCHARGE
    assert compute(world([dev("a", soc=80)], schedules=sch, reserve_floor_pct=50)).wants["a"].mode is Mode.DISCHARGE


def test_tariff_months_flag_a_tariff_cheaper_for_three_complete_months():
    from core.tariffs import compare, parse_tariffs
    ts = parse_tariffs("Now; peak=0.30\nGo; peak=0.28; cheap=0.07; window=00:30-05:30")
    usage = {}
    for m in ("2026-07", "2026-08", "2026-09", "2026-10", "2026-11"):
        imp = [0.0] * 48
        imp[2] = 2.0
        usage[m + "-01"] = {"imp": imp, "exp": [0.0] * 48}
    mo = compare(usage, ts)["monthly"]
    assert mo["months"][0] == "2026-07" and mo["totals"]["Now"][0] == 0.6 and mo["totals"]["Go"][0] == 0.14
    f = mo["cheaper_than_current"]
    assert f["tariff"] == "Go" and f["current"] == "Now" and f["months"] == 4 and f["saved"] == 1.84
    short = {k: v for k, v in usage.items() if k >= "2026-09"}
    assert compare(short, ts)["monthly"]["cheaper_than_current"] is None      # only two complete months: not enough
    assert compare(usage, parse_tariffs("Only; peak=0.3"))["monthly"]["cheaper_than_current"] is None
    # recording began on 30 August: August is a partial month and must not count as a complete one
    late = {("2026-08-30" if k == "2026-08-01" else k): v for k, v in usage.items() if k >= "2026-08"}
    assert compare(late, ts)["monthly"]["cheaper_than_current"] is None        # Sep and Oct only (Nov is in progress)


def test_forecast_reserve_settings_are_validated():
    from core.settings import SettingsError, build, clean
    assert build({}).forecast_reserve_kwh == 0
    assert build(clean({"forecast_reserve_kwh": 8, "forecast_reserve_pct": 60})).forecast_reserve_pct == 60
    with pytest.raises(SettingsError):
        clean({"forecast_reserve_pct": 150})


def test_usage_history_keeps_house_load_and_solar_only_for_days_that_have_them():
    from core.insights import UsageHistory
    u = UsageHistory({"2026-11-09": {"imp": [0.1] * 48, "exp": [0.0] * 48}})        # recorded before load was kept
    t = datetime(2026, 11, 9, 10, 10, tzinfo=UTC)
    u.add(t, 0.1, 0.0, 0.3, 0.2)
    assert "load" not in u.days["2026-11-09"]
    t2 = datetime(2026, 11, 10, 10, 10, tzinfo=UTC)
    u.add(t2, 0.1, 0.0, 0.3, 0.2)
    u.add(t2, 0.0, 0.0, 0.1, 0.0)
    again = UsageHistory(u.dump())
    assert "load" not in again.days["2026-11-09"] and round(again.days["2026-11-10"]["load"][20], 3) == 0.4 and again.days["2026-11-10"]["pv"][20] == 0.2


def test_whatif_battery_model_saves_with_cheap_top_up_and_never_goes_below_reserve():
    from core.tariffs import parse_tariffs
    from core.whatif import simulate
    t = parse_tariffs("Go; peak=0.30; cheap=0.10; window=00:30-05:30; export=0.05")[0]
    day = {"imp": [0.0] * 48, "exp": [0.0] * 48, "load": [0.4] * 48, "pv": [0.0] * 48}          # 19.2 kWh a day, no solar
    days = {f"2026-11-0{i}": day for i in range(1, 6)}
    none = simulate(days, t, 10, 5, reserve_pct=10, cheap_pct=0)
    topup = simulate(days, t, 10, 5, reserve_pct=10, cheap_pct=100)
    assert topup["cost"] < none["cost"]                                   # buying cheap and using it in the evening beats buying peak
    assert none["import_kwh"] >= 5 * 19.2 - 5 * 9 * 0.95 - 0.5            # at most the 9 usable kWh could ever come out of the battery per day
    sunny = {"imp": [0.0] * 48, "exp": [0.0] * 48, "load": [0.1] * 48, "pv": [0.5] * 48}
    s = simulate({"2026-11-01": sunny}, t, 10, 5, reserve_pct=10, cheap_pct=0)
    assert s["export_kwh"] > 0 and s["import_kwh"] == 0
    assert simulate(days, t, 10, 5, reserve_pct=100, cheap_pct=0)["import_kwh"] == round(19.2 * 5, 1)   # a full reserve: the battery is never used


def test_whatif_battery_never_moves_more_than_its_power_in_a_half_hour():
    from core.tariffs import parse_tariffs
    from core.whatif import simulate
    t = parse_tariffs("Go; peak=0.30; cheap=0.10; window=00:00-23:30")[0]       # cheap nearly all day
    day = {"imp": [0.0] * 48, "exp": [0.0] * 48, "load": [1.0] * 48, "pv": [0.0] * 48}
    r = simulate({"2026-11-01": day}, t, 10, 2, reserve_pct=0, cheap_pct=100)          # 2 kW = at most 1 kWh per half-hour, house wants 1 kWh
    assert r["import_kwh"] >= 48 * 1.0 - 1e-6 + 0.0 - 10 / 0.95          # imports ~ the whole load; the battery cannot beat its power limit by discharging and recharging at once


def test_charge_without_power_control_never_follows_solar_or_meter():
    caps = Capabilities(5000, 2300, 2300, 10, frozenset({Mode.AUTO, Mode.CHARGE, Mode.HOLD}), power_control=False)
    d = DevIn(DeviceConfig("a", "a", "x", caps, extra={"control_enabled": True}), Actual(online=True, soc=50.0, power_w=0.0, mode=Mode.AUTO),
              release_pending=False, guard_held=False, controllable=True)
    s = ScheduleEntry("s", frozenset(), time(21), time(23), Desired(Mode.CHARGE, 2000), source="solar")
    r = compute(world([d], schedules=[s]))
    assert r.wants["a"].mode is Mode.HOLD
    g = ScheduleEntry("g", frozenset(), time(21), time(23), Desired(Mode.CHARGE, 2000))
    assert compute(world([d], schedules=[g])).wants["a"].mode is Mode.CHARGE



def test_solar_only_charging_does_not_count_another_batterys_own_panel_charging_as_surplus():
    """Battery b charges from its own panels (never seen by the house meter) while the house imports 500 W: a solar-only
    schedule on battery a has no surplus and must not import to charge."""
    a = dev("a")
    b = dev("b", soc=50, power=1500)
    b = DevIn(b.config, Actual(online=True, soc=50, power_w=1500, mode=Mode.AUTO, pv_w=1500), controllable=True)
    sc = sched(devs=("a",), power=3000, source="solar", start=time(0), end=time(23, 59))
    r = compute(world([a, b], schedules=[sc], grid_w=500, grid_fresh=True, now=datetime(2026, 11, 10, 12, 0, tzinfo=UTC)))
    assert r.wants["a"].mode is not Mode.CHARGE, r.wants["a"]
    # the same charging seen at the meter (grid-fed, no panels) is deliberate and is not surplus either way, so a real surplus still works
    b2 = DevIn(b.config, Actual(online=True, soc=50, power_w=1500, mode=Mode.AUTO, pv_w=0), controllable=True)
    r2 = compute(world([a, b2], schedules=[sc], grid_w=-1000, grid_fresh=True, now=datetime(2026, 11, 10, 12, 0, tzinfo=UTC)))
    assert r2.wants["a"].mode is Mode.CHARGE and r2.wants["a"].power_w >= 2000, r2.wants["a"]
