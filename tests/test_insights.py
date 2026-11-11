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
