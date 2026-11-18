"""Randomised checks for the newer rules: batteries that pick their own rate, cold-weather boost, keep-for-dearer-hours,
efficiency-aware planning, and the learning/logging classes."""
import random
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone

from core.engine import compute
from core.goals import Goal
from core.insights import PlanLog
from core.models import Actual, Capabilities, Mode
from core.planner import Need, plan_charge
from core.rates import RateTable, Slot
from core.report import hold_for_dearer, window_drift
from core.sanity import ChargeWatch, CommandBudget, RateLearner, cycling_margin
from core.traces import CommandTrace
from core.weather import ColdModel, TempLog, boost_points, cold_extra_wh, pick_tomorrow
from test_engine_fuzz import rand_world

UTC = timezone.utc


def _mutate(w, rng):
    """Make some batteries pick their own charge rate and add the newer inputs."""
    for d in w.devices:
        if rng.random() < 0.5:
            c = d.config.caps
            d.config.caps = replace(c, power_control=False)
    w.cold_extra_wh = rng.choice([0.0, 0.0, 500.0, 2500.0, 12000.0])
    w.cold_note = "-3 C vs 5 C"
    w.price_now = rng.choice([None, 0.10, 0.25])
    w.price_later_max = rng.choice([None, 0.12, 0.40])
    w.settings.hold_for_dearer = rng.random() < 0.5
    w.efficiency = {d.config.device_id: rng.uniform(0.75, 0.99) for d in w.devices if rng.random() < 0.5}
    return w


def test_engine_invariants_with_the_newer_inputs():
    rng = random.Random(777)
    held_for_dearer = boosted = 0
    for n in range(4000):
        w = _mutate(rand_world(rng), rng)
        r = compute(w)
        by = {d.config.device_id: d for d in w.devices}
        for did, want in r.wants.items():
            caps, why = by[did].config.caps, r.reasons[did]
            if want.mode is Mode.CHARGE:
                assert want.power_w is None or 0 <= want.power_w <= caps.max_charge_w, (n, did, want)
                assert want.target_soc is None or 0 <= want.target_soc <= 100, (n, want)
                if not caps.power_control:
                    # it cannot follow a meter or a surplus: those must never become a Charge command
                    assert why != "zero-import" and why != "export limit" and "(solar only)" not in why, (n, did, why)
            if why == "saving charge for dearer hours later":
                held_for_dearer += 1
                assert w.settings.hold_for_dearer and hold_for_dearer(w.price_now, w.price_later_max), n
                assert (by[did].actual.pv_w or 0) <= 50 and want.mode is Mode.HOLD, n
        boosted += any("raised to" in note for note in r.notes)
    assert held_for_dearer > 20 and boosted > 20, (held_for_dearer, boosted)


def test_a_colder_day_never_lowers_a_goal_target_and_never_raises_it_by_more_than_the_cap():
    rng = random.Random(4242)
    compared = 0
    for n in range(3000):
        w = rand_world(rng)
        w.schedules, w.overrides, w.backup_active, w.settings.zero_import_device = [], {}, False, None
        w.goals = [Goal("g", frozenset(), rng.choice([40, 70, 100]), time(7), auto_target=rng.random() < 0.5)]
        w.rates = RateTable(peak=30.0, offpeak=7.5, offpeak_start=time(0, 0), offpeak_end=time(7, 0))
        w.now = w.now.replace(hour=1)
        base = compute(replace(w, cold_extra_wh=0.0))
        cold = compute(replace(w, cold_extra_wh=rng.choice([800.0, 3000.0, 20000.0]), cold_note="x"))
        for did, a in base.wants.items():
            b = cold.wants.get(did)
            if a.mode is Mode.CHARGE and a.target_soc is not None and b is not None and b.mode is Mode.CHARGE and b.target_soc is not None \
                    and base.reasons[did].startswith("goal") and cold.reasons[did].startswith("goal"):
                compared += 1
                assert b.target_soc >= a.target_soc - 0, (n, a, b)
                assert b.target_soc - a.target_soc <= 25, (n, a, b)
                assert b.target_soc <= 100
    assert compared > 200, compared


def _slots(rng, n):
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    return t0, [Slot(t0 + timedelta(minutes=30 * i), t0 + timedelta(minutes=30 * (i + 1)), round(rng.uniform(-0.05, 0.4), 3)) for i in range(n)]


def test_plan_charge_invariants_with_fixed_rate_and_efficiency():
    rng = random.Random(31337)
    for n in range(3000):
        t0, slots = _slots(rng, rng.randint(1, 24))
        needs = [Need(f"d{i}", rng.uniform(0, 100), rng.choice([2000, 5000, 10000]), rng.choice([0, 40, 1000, 3000]), rng.uniform(0, 100),
                      fixed_rate=rng.random() < 0.4, efficiency=rng.choice([None, 0.8, 0.95]))
                 for i in range(rng.randint(1, 4))]
        ceiling = rng.choice([None, 1500, 4000])
        allocs, unmet = plan_charge(t0, t0 + timedelta(hours=rng.randint(1, 12)), needs, slots, ceiling)
        by = {x.device_id: x for x in needs}
        per_slot = {}
        for a in allocs:
            nd = by[a.device_id]
            assert a.power_w >= 50 and a.power_w <= nd.max_charge_w, (n, a, nd)
            if nd.fixed_rate:
                assert a.power_w == nd.max_charge_w, (n, a, nd)          # its own rate, never a made-up fraction
            per_slot[a.start] = per_slot.get(a.start, 0) + a.power_w
        if ceiling is not None:
            assert all(v <= ceiling for v in per_slot.values()), (n, per_slot, ceiling)
        assert all(v >= 0 for v in unmet.values())
        for k, v in unmet.items():
            assert v == v                                               # never NaN


def test_the_learning_and_logging_classes_never_raise_and_stay_bounded():
    rng = random.Random(5)
    rl, cw, cb, tr, pl = RateLearner(), ChargeWatch(), CommandBudget(5), CommandTrace(), PlanLog()
    t = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    for i in range(6000):
        did = rng.choice(["a", "b"])
        soc = rng.choice([None, 5.0, 50.0, 99.0])
        pw = rng.choice([None, -300.0, 0.0, 600.0, 3000.0])
        rl.observe(did, rng.random() < 0.5, pw, soc)
        cw.stuck(did, rng.random() < 0.5, soc, pw, rng.choice([None, 80.0]), i * 10.0)
        cb.allow(did, str(i // 500), rng.random() < 0.3)
        if rng.random() < 0.05:
            tr.start(did, rng.choice(["charge", "hold"]), i * 10.0, "t", soc, pw)
        tr.observe(did, i * 10.0, soc, pw, rng.choice([None, "hold", "auto"]))
        pl.add(t + timedelta(minutes=10 * i), did, rng.choice([None, 0.0, 1500.0]), rng.choice([None, -200.0, 1400.0]))
    assert all(len(v) <= 30 for v in tr.results.values()) and len(pl.days) <= 8
    assert all(500 <= v < 10000 for v in rl.rates.values())
    v = pl.view("a", t + timedelta(minutes=10 * 6000))
    assert len(v["rows"]) == 48 and (v["match_pct"] is None or -100 <= v["match_pct"] <= 100)
    assert CommandTrace(tr.dump()).dump() == tr.dump() and PlanLog(pl.dump()).dump() == pl.dump()


def test_weather_functions_never_raise_on_odd_input():
    rng = random.Random(8)
    junk = [None, "x", 3, [], {}, {"datetime": None}, {"datetime": "2026-11-11"}, {"datetime": "2026-11-11T00:00:00Z", "temperature": "hot"},
            {"datetime": "2026-11-11T00:00:00+00:00", "temperature": float("nan")}, {"temperature": 4}]
    for _ in range(500):
        fc = [rng.choice(junk) for _ in range(rng.randint(0, 6))]
        pick_tomorrow(fc, datetime(2026, 11, 11).date(), rng.choice([None, UTC]), rng.choice([None, "C", "°F", "K"]))
    m = ColdModel({"k": "bad", "days": None})
    assert m.k is None and m.days == 0
    log, usage = TempLog({"a": [1, 2, 3], "b": "x", "c": [3.0, 5]}), {"2026-10-01": "junk", "2026-10-02": {"load": [1] * 5}}
    m.fit(log, usage, "2026-11-01")
    assert m.k is None
    assert cold_extra_wh(800, -5, 3) >= 0 and boost_points(float("inf"), 10000) == 25 and boost_points(float("nan"), 10000) == 0 and boost_points(5, 0) == 0
    assert cold_extra_wh(float("nan"), -5, 3) == 0 and ColdModel({"k": float("nan")}).k is None
    assert pick_tomorrow([{"datetime": "2026-11-11T00:00:00+00:00", "temperature": float("nan")}], datetime(2026, 11, 11).date(), UTC) is None
    assert window_drift(time(0), time(7), time(0, 30), time(7, 30)) is None and cycling_margin(0.1, 0.0, 0.0)["efficiency_pct"] == 5.0


def test_every_newer_saved_structure_tolerates_damaged_data():
    for junk in (None, "x", 5, [], [1, 2], {"a": "junk"}, {"a": [1, "x"]}, {"a": {"b": 1}}, {"k": float("nan")}):
        TempLog(junk), ColdModel(junk), CommandTrace(junk), RateLearner(junk), PlanLog(junk)
    assert RateLearner({"a": float("inf"), "b": 3000, "c": -5, "d": "x"}).rates == {"b": 3000}
    assert CommandTrace({"a": [{"mode": 5, "ok": True}, {"mode": "charge", "ok": True}]}).results["a"] == [{"mode": "charge", "ok": True}]
    assert TempLog({"a": [float("nan"), 3]}).days == {}
