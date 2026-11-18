"""Summaries and checks in core/report.py, plus the engine and planner rules that use them."""
from datetime import datetime, time, timedelta, timezone

from hp.core.engine import DevIn, Settings, World, compute
from hp.core.models import Actual, Capabilities, DeviceConfig, Mode
from hp.core.planner import Need, plan_charge
from hp.core.rates import Slot
from hp.core.report import (charge_efficiency, ecoflow_report, goal_warnings, hold_for_dearer, schedule_warnings,
                            startup_summary, window_drift)
from hp.core.traces import CommandTrace

NOW = datetime(2026, 11, 10, 14, 0, tzinfo=timezone.utc)


def test_ecoflow_report_gives_a_plain_verdict():
    ok = [{"ok": True, "reaction_s": 40}] * 6
    r = ecoflow_report(ok, 90, 2800, "works")
    assert (r["sent"], r["worked"], r["avg_reaction_s"]) == (6, 6, 40) and "reliable" in r["advice"]
    assert "overriding" in ecoflow_report(ok, 30, 2800, "works")["advice"]
    assert "not working" in ecoflow_report([{"ok": False, "reaction_s": None}] * 4, None, None, "unreliable")["advice"]
    assert "Not enough" in ecoflow_report(ok[:2], None, None, "works")["advice"]


DEVS = [{"device_id": "a", "name": "Anker", "supported_modes": ["auto", "charge", "discharge", "hold"], "min_soc": 10, "power_control": True},
        {"device_id": "e", "name": "Eco", "supported_modes": ["auto", "charge", "hold"], "min_soc": 10, "power_control": False}]


def test_schedule_warnings_name_what_will_not_work():
    w = schedule_warnings({"device_ids": ["e"], "mode": "discharge", "target_soc": 50}, DEVS)
    assert len(w) == 1 and "cannot discharge" in w[0]
    w = schedule_warnings({"device_ids": ["e"], "mode": "charge", "power_w": 1000, "target_soc": 5}, DEVS)
    assert any("own charge rate" in x for x in w) and any("minimum charge" in x for x in w)
    assert schedule_warnings({"device_ids": ["a"], "mode": "charge", "power_w": 1000, "target_soc": 80}, DEVS) == []


def test_goal_warnings():
    assert goal_warnings({"device_ids": ["a"], "target_soc": 80}, DEVS) == []
    assert "minimum charge" in goal_warnings({"device_ids": ["a"], "target_soc": 8}, DEVS)[0]
    assert goal_warnings({"device_ids": ["a"], "target_soc": 8, "auto_target": True}, DEVS) == []
    only_hold = [{"device_id": "h", "name": "H", "supported_modes": ["auto", "hold"], "min_soc": 10}]
    assert "cannot be met" in goal_warnings({"device_ids": ["h"], "target_soc": 80}, only_hold)[0]


def test_window_drift_ignores_small_differences_and_wrap_around_midnight():
    t = lambda h, m=0: time(h, m)  # noqa: E731
    assert window_drift(t(23, 30), t(7, 30), t(23, 0), t(8, 0)) is None
    assert window_drift(t(23, 30), t(7, 30), t(0, 30), t(5, 30)) is not None          # clearly moved
    assert window_drift(t(23, 55), t(7, 0), t(0, 5), t(7, 10)) is None                # 10 minutes apart across midnight
    assert window_drift(None, None, t(1), t(5)) is None


def test_startup_summary():
    assert startup_summary({"a": 0}, []) == "Started. Restored nothing to restore (a fresh start)."
    s = startup_summary({"learned charge rates": 2, "x": 0}, ["Eco"])
    assert "2 learned charge rates" in s and "Could not reach: Eco" in s


def test_hold_for_dearer_needs_a_clear_gap():
    assert hold_for_dearer(0.15, 0.35)
    assert not hold_for_dearer(0.30, 0.32)          # nearly equal: not worth it
    assert not hold_for_dearer(None, 0.35) and not hold_for_dearer(0.15, None)


def test_charge_efficiency_is_the_square_root_of_the_round_trip_and_bounded():
    assert charge_efficiency(81) == 0.9
    assert charge_efficiency(None) == 0.92 and charge_efficiency(30) == 0.75


def test_more_efficient_battery_gets_the_cheapest_slots_when_the_house_limit_is_tight():
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    slots = [Slot(t0 + timedelta(minutes=30 * i), t0 + timedelta(minutes=30 * (i + 1)), 0.05 if i == 0 else 0.30) for i in range(4)]
    needs = [Need("poor", 20, 5000, 3000, 60, efficiency=0.80), Need("good", 20, 5000, 3000, 60, efficiency=0.95)]
    allocs, _ = plan_charge(t0, t0 + timedelta(hours=2), needs, slots, ceiling_w=3000)
    cheapest = [a.device_id for a in allocs if a.price == 0.05]
    assert cheapest == ["good"]                      # only room for one: the efficient one


def test_a_battery_is_held_for_a_dearer_price_later_but_not_while_solar_charges_it():
    caps = Capabilities(10000, 3000, 3000, 10)
    cfg = DeviceConfig("a", "A", "x", caps, extra={"control_enabled": True})
    on = Settings(import_limit_w=None, hold_for_dearer=True)

    def run(pv, settings=on, now_p=0.15, later=0.40):
        d = DevIn(cfg, Actual(online=True, soc=60, power_w=-300, mode=Mode.AUTO, pv_w=pv))
        return compute(World(now=NOW, devices=[d], settings=settings, price_now=now_p, price_later_max=later))
    r = run(0)
    assert r.wants["a"].mode is Mode.HOLD and "dearer" in r.reasons["a"]
    assert "a" not in run(500).wants                                   # solar is charging it: leave it alone
    assert "a" not in run(0, Settings(import_limit_w=None)).wants        # off by default
    assert "a" not in run(0, later=0.16).wants                           # no clearly dearer price coming


def test_grace_for_a_slow_battery_follows_how_long_it_has_really_taken():
    t = CommandTrace()
    assert t.grace("e", 120) == 1                                        # no evidence yet
    for s in (30, 40, 50):
        t.start("e", "hold", 0, "t", 40, 0)
        t.observe("e", s, 40, 0, "hold")
    assert t.grace("e", 120) == 1
    for s in (300, 310, 330):
        t.start("e", "hold", 0, "t", 40, 0)
        t.observe("e", s, 40, 0, "hold")
    assert t.grace("e", 120) == 3                                        # up to 5.5 min to react: wait three retries
    assert t.grace("e", 0) == 1
