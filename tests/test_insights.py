from datetime import datetime, time, timedelta, timezone

from core.engine import Settings, World, compute
from core.goals import Goal
from core.insights import (BatteryFigures, DailyStats, Health, LoadProfile, SceneError, auto_target_soc, clean_scene, combine)
from core.models import Mode
from core.rates import RateTable, Slot
from test_engine import dev

UTC = timezone.utc
TUE_22 = datetime(2026, 11, 10, 22, 0, tzinfo=UTC)


def test_load_profile_learns_and_refuses_until_known():
    p = LoadProfile()
    assert p.expected_wh(TUE_22, 2) is None
    t0 = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
    for day in range(3):
        for i in range(48):
            for s in range(25):
                p.record(t0 + timedelta(days=day, minutes=30 * i, seconds=s * 60 // 25), 500 + 100 * (i % 2))
    wh = p.expected_wh(datetime(2026, 11, 10, 7, 30, tzinfo=UTC), 2)
    assert wh is not None and 1000 < wh < 1300          # ~ 500+600+500+600 over two hours, in Wh/2
    assert LoadProfile(p.dump()).expected_wh(datetime(2026, 11, 10, 7, 30, tzinfo=UTC), 2) == wh
    assert p.expected_wh(datetime(2026, 11, 14, 7, 30, tzinfo=UTC), 2) is None   # weekend not learned yet


def test_load_profile_ignores_nonsense():
    p = LoadProfile()
    for bad in (None, -5, 1e9):
        p.record(TUE_22, bad)
    assert sum(p.n["wd"]) == 0


def test_auto_target_soc():
    assert auto_target_soc(None, 10000, 10) is None
    assert auto_target_soc(3000, 10000, 10) == 45                      # 10 + 30 * 1.15
    assert auto_target_soc(50000, 10000, 10) == 100                    # capped
    assert auto_target_soc(0, 10000, 5) == 20                          # never below the lower bound


def _goal_world(use):
    rates = RateTable([Slot(TUE_22 + timedelta(minutes=30 * i), TUE_22 + timedelta(minutes=30 * (i + 1)), 0.1) for i in range(20)],
                      offpeak_start=time(23, 30), offpeak_end=time(7, 30))
    return World(now=TUE_22, devices=[dev("a", soc=20, cap=10000)], settings=Settings(import_limit_w=None),
                 goals=[Goal("g", frozenset(), 90, time(7, 30), auto_target=True)], rates=rates,
                 usage_wh=(lambda start, hours: use))


def test_goal_auto_target_follows_usage_and_is_capped_by_the_set_target():
    r = compute(_goal_world(2000))
    assert any("auto target" in n and "33%" in n for n in r.notes), r.notes       # 10 + 20 * 1.15 = 33
    assert r.wants["a"].mode is Mode.CHARGE and r.wants["a"].target_soc == 33
    r = compute(_goal_world(40000))
    assert r.wants["a"].target_soc == 90                                          # never above what the user set
    r = compute(_goal_world(None))
    assert any("still learning" in n for n in r.notes) and r.wants["a"].target_soc == 90


def test_daily_stats():
    d = DailyStats()
    d.add("2026-11-10", pv_kwh=10, export_kwh=2)
    d.add("2026-11-10", import_kwh=3, cheap=True)
    d.add("2026-11-10", import_kwh=1, cheap=False)
    assert d.self_consumption("2026-11-10") == 80.0
    assert d.cheap_share("2026-11-10") == 75.0
    assert d.self_consumption("2026-11-11") is None and DailyStats(d.dump()).cheap_share("2026-11-10") == 75.0


def test_health_efficiency_and_degradation():
    h = Health()
    totals = {"a": (0.0, 0.0)}
    h.update("2026-W01", totals)
    c = o = 0.0
    for i, eff in enumerate((0.9, 0.9, 0.9, 0.9, 0.7), start=2):
        c += 10
        o += 10 * eff
        h.update(f"2026-W{i:02d}", {"a": (c, o)})
    assert [e for _, e in h.efficiency("a")] == [90.0, 90.0, 90.0, 90.0, 70.0]
    assert "fell to 70%" in (h.degraded("a") or "")
    assert Health(h.dump()).efficiency("a") == h.efficiency("a")
    assert Health().degraded("a") is None


def test_combine_totals_runtime_and_offline():
    a = BatteryFigures(10000, 50, True, -1000, 10)
    b = BatteryFigures(5000, 80, True, -500, 10)
    off = BatteryFigures(5000, None, False, None, 10)
    c = combine([a, b, off], load_w=2000)
    assert c["online"] == 2 and c["total"] == 3 and c["capacity_kwh"] == 15 and c["soc"] == 60.0
    assert c["stored_kwh"] == 9.0 and c["usable_kwh"] == 7.5 and c["flow_w"] == -1500
    assert c["runtime_h"] == round(7500 / 1500, 1)
    idle = combine([BatteryFigures(10000, 50, True, 0, 10)], load_w=800)
    assert idle["runtime_h"] == 5.0                                  # 4 kWh usable / 800 W load
    assert combine([off])["soc"] is None
    chg = combine([BatteryFigures(10000, 50, True, 2000, 10)])
    assert chg["full_in_h"] == 2.5 and chg["runtime_h"] is None


def test_scenes_validate():
    s = clean_scene({"name": " Holiday ", "away": 1, "settings": {"import_limit_w": "6000", "peak_shaving": True, "junk": 1}})
    assert s == {"name": "Holiday", "backup": False, "away": True, "paused": False, "settings": {"import_limit_w": 6000, "peak_shaving": True}}
    for bad in ({}, {"name": "x", "settings": {"import_limit_w": "abc"}}):
        try:
            clean_scene(bad)
            assert False
        except SceneError:
            pass


# ---------------------------------------------------------------- hold back for peak, reserve batteries, windows, diverter
def _hold_world(devs, **kw):
    return World(now=TUE_22, devices=devs, settings=Settings(import_limit_w=None, save_for_peak=kw.pop("save", False)), **kw)


def test_save_for_peak_holds_only_in_cheap_hours_and_never_blocks_solar():
    r = compute(_hold_world([dev("a", soc=80)], save=True, cheap_now=True))
    assert r.wants["a"].mode is Mode.HOLD and "peak" in r.reasons["a"]
    assert "a" not in compute(_hold_world([dev("a", soc=80)], save=True, cheap_now=False)).wants
    assert "a" not in compute(_hold_world([dev("a", soc=80)], save=True, cheap_now=None)).wants      # flat tariff: unknown
    assert "a" not in compute(_hold_world([dev("a", soc=80)], save=False, cheap_now=True)).wants
    import dataclasses
    sunny = dev("a", soc=80)
    sunny.actual = dataclasses.replace(sunny.actual, pv_w=900)
    assert "a" not in compute(_hold_world([sunny], save=True, cheap_now=True)).wants
    mon = dev("a", soc=80, controllable=False)
    assert "a" not in compute(_hold_world([mon], save=True, cheap_now=True)).wants                    # monitor-only is never touched


def test_save_for_peak_does_not_override_a_schedule_or_goal():
    from test_engine import sched
    w = _hold_world([dev("a", soc=50)], save=True, cheap_now=True, schedules=[sched()])
    assert compute(w).wants["a"].mode is Mode.CHARGE


def test_reserve_battery_is_held_until_the_others_run_down():
    res = dev("r", soc=90)
    res.config.extra["role"] = "reserve"
    r = compute(_hold_world([dev("a", soc=60), res]))
    assert r.wants["r"].mode is Mode.HOLD and "reserve" in r.reasons["r"] and "a" not in r.wants
    r = compute(_hold_world([dev("a", soc=12), res]))                 # the daily battery is at its floor (10%): reserve released
    assert "r" not in r.wants
    r = compute(_hold_world([dev("a", soc=60, online=False), res]))   # nothing else available: reserve is free to work
    assert "r" not in r.wants
    held = dev("r", soc=90, release=True)
    held.config.extra["role"] = "reserve"
    r = compute(_hold_world([dev("a", soc=12), held]))
    assert r.wants["r"].mode is Mode.AUTO                             # and it is handed back, not left on hold


def test_rate_table_with_several_cheap_windows():
    rt = RateTable(peak=30, offpeak=10, offpeak_start=time(4), offpeak_end=time(7), extra_windows=[(time(13), time(16)), (time(22), time(0))])
    at = lambda h, m=0: datetime(2026, 11, 10, h, m, tzinfo=UTC)
    assert [rt.price_at(at(h)) for h in (3, 4, 6, 7, 13, 15, 16, 22, 23)] == [30, 10, 10, 30, 10, 10, 30, 10, 10]
    assert rt.price_at(datetime(2026, 11, 11, 0, 0, tzinfo=UTC)) == 30
    assert rt.minutes_to_cheap(time(12, 30)) == (30, time(13)) and rt.minutes_to_cheap(time(23)) == (300, time(4))
    assert RateTable(peak=30, offpeak=10).minutes_to_cheap(time(1)) is None


def test_settings_cheap_windows_parse_and_reject():
    from core.settings import SettingsError, build, clean, parse_windows
    assert parse_windows("13:00-16:00, 22:00 - 00:00") == [(time(13), time(16)), (time(22), time(0))]
    assert parse_windows("") == []
    for bad in ("13:00", "13:00-13:00", "x-y", "25:00-26:00"):
        try:
            clean({"offpeak_extra": bad})
            assert False, bad
        except SettingsError:
            pass
    s = build(clean({"offpeak_extra": "13:00-16:00", "save_for_peak": True, "daily_summary_time": "07:45", "divert_entity": "switch.immersion"}))
    assert s.offpeak_extra == [(time(13), time(16))] and s.engine.save_for_peak and s.daily_summary_time == time(7, 45)


def test_diverter_waits_then_switches_and_releases():
    from core.insights import Diverter
    d = Diverter()
    assert d.decide(0, 97, -800, 500, 95) is None            # surplus seen: start timing
    assert d.decide(60, 97, -800, 500, 95) is None
    assert d.decide(130, 97, -900, 500, 95) == "on"
    assert d.decide(140, 97, -50, 500, 95) is None           # the load now uses the surplus: stays on
    assert d.decide(150, 97, 300, 500, 95) is None           # importing: start timing
    assert d.decide(200, 97, -100, 500, 95) is None          # recovered: timer cancelled
    assert d.decide(210, 97, 300, 500, 95) is None
    assert d.decide(340, 97, 300, 500, 95) == "off"
    assert d.decide(350, 60, -900, 500, 95) is None          # not full: never on
    d2 = Diverter()
    d2.on = True
    assert d2.decide(0, None, None, 500, 95) is None and d2.decide(130, None, None, 500, 95) == "off"   # no data: release the load


# ---------------------------------------------------------------- charge source: grid / solar only / grid + solar
def _src_world(devs, source, grid, power=2000, **kw):
    from test_engine import sched
    return World(now=TUE_22, devices=devs, settings=Settings(import_limit_w=None), grid_w=grid,
                 schedules=[sched(power=power, source=source)], **kw)


def test_grid_source_is_the_fixed_power_whatever_the_meter_says():
    for grid in (None, -3000, 0, 4000):
        r = compute(_src_world([dev("a")], "grid", grid))
        assert r.wants["a"].mode is Mode.CHARGE and r.wants["a"].power_w == 2000 and r.reasons["a"] == "schedule 's'"


def test_solar_only_follows_the_surplus_and_never_imports():
    r = compute(_src_world([dev("a", power=0)], "solar", -1500))
    assert r.wants["a"].mode is Mode.CHARGE and r.wants["a"].power_w == 1500 and "(solar only)" in r.reasons["a"]
    r = compute(_src_world([dev("a", power=1500)], "solar", 0))               # already taking the surplus, meter balanced: keep going
    assert r.wants["a"].power_w == 1500
    r = compute(_src_world([dev("a", power=1500)], "solar", 600))             # a cloud: charging now imports 600 W -> back off
    assert r.wants["a"].power_w == 900
    r = compute(_src_world([dev("a", power=0)], "solar", 800))                # importing, no surplus: wait, do not discharge either
    assert r.wants["a"].mode is Mode.HOLD
    r = compute(_src_world([dev("a", power=0)], "solar", -5000))              # the schedule's power is the ceiling
    assert r.wants["a"].power_w == 2000
    r = compute(_src_world([dev("a", power=0)], "solar", None))               # no meter: cannot know the surplus -> wait
    assert r.wants["a"].mode is Mode.HOLD and any("grid meter" in n for n in r.notes)


def test_grid_plus_solar_takes_the_fixed_power_and_any_extra_surplus():
    r = compute(_src_world([dev("a", power=2000)], "both", 0))
    assert r.wants["a"].power_w == 2000 and "(grid + solar)" in r.reasons["a"]
    r = compute(_src_world([dev("a", power=2000)], "both", -800))             # exporting 800 W while charging: soak it up too
    assert r.wants["a"].power_w == 2800
    r = compute(_src_world([dev("a", power=2000)], "both", -9000))            # never above the battery's own limit
    assert r.wants["a"].power_w == 3000
    r = compute(_src_world([dev("a", power=0)], "both", None))                # no meter: plain fixed power
    assert r.wants["a"].power_w == 2000


def test_solar_surplus_is_shared_and_ignores_deliberate_charging_elsewhere():
    r = compute(_src_world([dev("a", power=0), dev("b", power=0)], "solar", -2000))
    assert r.wants["a"].power_w == 1000 and r.wants["b"].power_w == 1000      # two batteries split the surplus, no overshoot
    from test_engine import sched
    w = World(now=TUE_22, devices=[dev("a", power=0), dev("g", power=2000)], settings=Settings(import_limit_w=None), grid_w=1500,
              schedules=[sched("sol", devs=("a",), power=3000, source="solar"), sched("grd", devs=("g",), power=2000)])
    r = compute(w)                                                             # 'g' charges 2000 W on purpose; import is only 1500 W
    assert r.wants["a"].power_w == 500 and r.wants["g"].power_w == 2000       # so 500 W of real surplus is left for 'a'


def test_solar_only_on_a_hold_only_battery_becomes_hold():
    eco = dev("e", soc=50, modes=[Mode.AUTO, Mode.HOLD], mc=0, md=0)
    r = compute(_src_world([eco], "solar", -1000))
    assert r.wants["e"].mode is Mode.HOLD and "(solar only)" in r.reasons["e"]
    r = compute(_src_world([eco], "grid", -1000))                             # a grid charge it cannot do: left alone, with a note
    assert "e" not in r.wants and any("not supported" in n for n in r.notes)


def test_schedule_source_round_trips_and_validates():
    from core.serde import ValidationError, schedule_from_dict, schedule_to_dict
    base = {"id": "s", "start": "10:00", "end": "15:00", "mode": "charge", "power_w": 2000}
    assert schedule_to_dict(schedule_from_dict(base))["source"] == "grid"                      # old schedules behave as before
    assert schedule_to_dict(schedule_from_dict({**base, "source": "solar"}))["source"] == "solar"
    assert schedule_to_dict(schedule_from_dict({**base, "mode": "discharge", "source": "solar"}))["source"] == "grid"
    try:
        schedule_from_dict({**base, "source": "wind"})
        assert False
    except ValidationError:
        pass


# ---------------------------------------------------------------- goals: solar only / solar first then grid; panels; export price
def _goal_src_world(source, grid, soc=40, cheap_now_slot=False, **kw):
    # prices: a cheap slot either right now or only later, so the grid plan is or is not charging at this moment
    def slot(i, p):
        return Slot(TUE_22 + timedelta(minutes=30 * i), TUE_22 + timedelta(minutes=30 * (i + 1)), p)
    prices = [slot(i, (5 if (i == 0) == cheap_now_slot and (cheap_now_slot or i >= 12) else 30)) for i in range(18)]
    return World(now=TUE_22, devices=[dev("a", soc=soc, power=0)], settings=Settings(import_limit_w=None), grid_w=grid,
                 goals=[Goal("g", frozenset(), 90, time(7, 0), source=source)], rates=RateTable(prices), **kw)


def test_solar_goal_takes_only_surplus_and_leaves_the_battery_alone_otherwise():
    r = compute(_goal_src_world("solar", -1200))
    assert r.wants["a"].mode is Mode.CHARGE and r.wants["a"].power_w == 1200 and "(solar only)" in r.reasons["a"]
    assert not r.allocs                                                         # a solar goal never plans grid slots
    for grid in (500, None):                                                    # no surplus / no meter: no instruction at all (not Hold)
        assert "a" not in compute(_goal_src_world("solar", grid)).wants
    assert "a" not in compute(_goal_src_world("solar", -1200, soc=95)).wants    # already at the target
    w = World(now=TUE_22, devices=[dev("a", soc=40, power=0)], settings=Settings(import_limit_w=None), grid_w=-900,
              goals=[Goal("g", frozenset(), 90, time(7, 0), source="solar")], rates=None)
    r = compute(w)
    assert r.wants["a"].power_w == 900 and not any("price data" in n for n in r.notes)   # needs no prices at all


def test_solar_first_goal_uses_solar_now_and_the_cheap_slots_later():
    later = compute(_goal_src_world("both", -1000))                             # cheap slot is hours away: only solar for now
    assert later.wants["a"].power_w == 1000 and "(solar only)" in later.reasons["a"] and later.allocs
    assert "a" not in compute(_goal_src_world("both", 400)).wants               # no surplus, not a cheap slot: leave it be
    now = compute(_goal_src_world("both", 0, cheap_now_slot=True))              # in the cheap slot: grid charge
    assert now.wants["a"].mode is Mode.CHARGE and now.wants["a"].power_w >= 1000 and "(grid + solar)" in now.reasons["a"]
    plain = compute(_goal_src_world("grid", -1000))                             # the old behaviour is untouched
    assert "a" not in plain.wants and plain.allocs


def test_stale_grid_reading_is_not_used_to_steer_solar_charging():
    assert "a" not in compute(_goal_src_world("solar", -1200, grid_fresh=False)).wants
    r = compute(_src_world([dev("a", power=0)], "solar", -1500, grid_fresh=False))
    assert r.wants["a"].mode is Mode.HOLD and any("live grid" in n for n in r.notes)


def test_export_paying_more_than_storing_leaves_surplus_to_export():
    r = compute(_goal_src_world("solar", -1200, export_better=True))
    assert "a" not in r.wants and any("exporting pays more" in n for n in r.notes)
    r = compute(_src_world([dev("a", power=0)], "solar", -1500, export_better=True))
    assert r.wants["a"].power_w == 1500                                         # an explicit schedule is still obeyed


def test_hold_only_battery_without_panels_is_not_held_for_solar_only():
    no_panels = dev("e", soc=50, modes=[Mode.AUTO, Mode.HOLD], mc=0, md=0)
    no_panels.has_solar = False
    r = compute(_src_world([no_panels], "solar", -1000))
    assert "e" not in r.wants and any("without panels" in n for n in r.notes)
    for known in (True, None):                                                  # panels, or not known: Hold so its panels charge it
        d = dev("e", soc=50, modes=[Mode.AUTO, Mode.HOLD], mc=0, md=0)
        d.has_solar = known
        assert compute(_src_world([d], "solar", -1000)).wants["e"].mode is Mode.HOLD


def test_goal_and_battery_fields_round_trip():
    from core.serde import ValidationError, device_from_dict, device_to_dict, goal_from_dict, goal_to_dict
    g = {"id": "g", "target_soc": 90, "by": "07:00"}
    assert goal_to_dict(goal_from_dict(g))["source"] == "grid" and goal_to_dict(goal_from_dict({**g, "source": "both"}))["source"] == "both"
    d = {"device_id": "a", "kind": "anker_e5000_pro", "connection": {"host": "10.0.0.2"}, "capacity_wh": 10000, "max_charge_w": 3000, "max_discharge_w": 3000}
    assert device_to_dict(device_from_dict(d))["solar"] == "auto" and device_to_dict(device_from_dict({**d, "solar": "yes"}))["solar"] == "yes"
    for bad in (lambda: goal_from_dict({**g, "source": "x"}), lambda: device_from_dict({**d, "solar": "maybe"})):
        try:
            bad()
            assert False
        except ValidationError:
            pass


def test_grid_order_setting_and_battery_meter_flag():
    from core.serde import ValidationError, device_from_dict, device_to_dict
    from core.settings import SettingsError, build, clean
    assert build({}).grid_order == ["sensor", "battery", "glow", "cloud"]
    assert build(clean({"grid_order": " glow , sensor,glow"})).grid_order == ["glow", "sensor"]       # order kept, duplicates dropped
    for bad in ("", "wind", "sensor,x"):
        try:
            clean({"grid_order": bad})
            assert False, bad
        except SettingsError:
            pass
    base = {"device_id": "a", "connection": {"host": "10.0.0.2"}, "capacity_wh": 10000, "max_charge_w": 3000, "max_discharge_w": 3000}
    plug = device_from_dict({**base, "kind": "anker_e5000_pro"})
    x1 = device_from_dict({**base, "kind": "anker_x1"})
    assert plug.extra["house_meter"] is False and x1.extra["house_meter"] is True                     # what 'auto' means per model
    assert device_to_dict(device_from_dict({**base, "kind": "anker_e5000_pro", "grid_meter": "yes"}))["grid_meter"] == "yes"
    try:
        device_from_dict({**base, "kind": "anker_x1", "grid_meter": "maybe"})
        assert False
    except ValidationError:
        pass
