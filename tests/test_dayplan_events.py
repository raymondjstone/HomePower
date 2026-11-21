"""The day plan (where stored energy is worth most), saving-session times and the goal that charges up before one."""
from datetime import datetime, time, timedelta, timezone

from core.dayplan import DEFAULT_LOAD_W, plan_day
from core.engine import EXPORT_REASON, DevIn, Settings, World, compute
from core.events import GOAL_ID, charge_goal, event_windows, next_event_start
from core.goals import next_deadline
from core.models import Actual, Capabilities, DeviceConfig, Mode
from core.rates import RateTable, Slot

UTC = timezone.utc
T0 = datetime(2026, 11, 18, 14, 0, tzinfo=UTC)


def slots(prices, start=T0):
    return [Slot(start + timedelta(minutes=30 * i), start + timedelta(minutes=30 * (i + 1)), p) for i, p in enumerate(prices)]


def flat_use(w=500.0):
    return lambda t, hours: w * hours


# 14:00 .. : 0.20, 0.20, 0.30, 0.30 (16:00-17:00), 0.35 x2 (17:00-18:00 peak), 0.25, 0.25, then 0.07 (19:00: cheapest)
PRICES = [0.20, 0.20, 0.30, 0.30, 0.35, 0.35, 0.25, 0.25, 0.07, 0.07, 0.10, 0.20]


def test_plan_covers_the_time_up_to_the_cheapest_price_and_uses_energy_where_it_is_worth_most():
    p = plan_day(T0, slots(PRICES), None, flat_use(), budget_wh=1000, export_max_w=0)
    assert p.until == T0 + timedelta(hours=4) and len(p.slots) == 8
    assert abs(p.threshold - 0.07 / 0.85) < 1e-9
    used = [round(s.house_wh) for s in p.slots]
    assert used == [0, 0, 250, 250, 250, 250, 0, 0]           # 1 kWh: the four dearest half-hours (0.30, 0.30, 0.35, 0.35)
    assert p.hold_now and p.export_now_w == 0                   # 14:00 at 0.20: keep it for the peak
    assert round(p.used_wh) == 1000


def test_plan_with_plenty_of_energy_runs_the_house_now_too():
    p = plan_day(T0, slots(PRICES), None, flat_use(), budget_wh=10000, export_max_w=0)
    assert all(round(s.house_wh) == 250 for s in p.slots) and not p.hold_now


def test_export_goes_to_the_best_paid_half_hours_not_the_first_one_that_pays():
    exp = {T0 + timedelta(minutes=30 * i): v for i, v in enumerate([0.15, 0.15, 0.15, 0.15, 0.30, 0.30, 0.15, 0.15])}
    at = lambda t: exp.get(t.replace(minute=(t.minute // 30) * 30, second=0, microsecond=0))   # noqa: E731
    p = plan_day(T0, slots(PRICES), at, flat_use(), budget_wh=4000, export_max_w=3000)
    assert p.export_now_w == 0                                  # 0.15 now pays, but 0.30 at 17:00 pays more for the same energy
    peak = [s for s in p.slots if s.start.hour == 16]                         # 16:00-17:00 pays 0.30 to export
    assert len(peak) == 2 and all(s.export_wh > 0 and s.house_wh == 250 for s in peak)
    assert sum(s.export_wh for s in p.slots if s.start.hour != 16) == 0
    p2 = plan_day(T0 + timedelta(hours=2), slots(PRICES), at, flat_use(), budget_wh=4000, export_max_w=3000)
    assert p2.export_now_w == 3000                              # now it is the peak: full power (house + export)


def test_nothing_is_used_below_the_buy_back_cost_and_wear():
    p = plan_day(T0, slots([0.08] * 8 + [0.07] * 4), None, flat_use(), budget_wh=5000, export_max_w=0)
    assert p.used_wh == 0 and not p.hold_now                    # 0.08 < 0.07 / 0.85: run from the grid, keep the battery
    p = plan_day(T0, slots(PRICES), None, flat_use(), budget_wh=5000, export_max_w=0, wear=0.25)
    assert [round(s.house_wh) for s in p.slots] == [0, 0, 0, 0, 250, 250, 0, 0]   # only 0.35 beats 0.082 + 0.25


def test_plan_needs_prices_and_handles_cheap_now_and_unknown_use():
    assert plan_day(T0, slots([0.3]), None, None, 1000, 0) is None
    p = plan_day(T0, slots([0.07, 0.3, 0.3]), None, None, 1000, 0)
    assert p.slots == [] and not p.hold_now and p.export_now_w == 0          # cheapest is now: nothing to plan before it
    p = plan_day(T0, slots(PRICES), None, lambda t, h: None, 100000, 0)
    assert round(p.slots[0].load_wh) == round(DEFAULT_LOAD_W / 2)
    late = T0 + timedelta(minutes=10)                                           # part-way through a half-hour: only what is left
    p = plan_day(late, slots(PRICES), None, flat_use(), 100000, 0)
    assert p.slots[0].start == late and round(p.slots[0].load_wh) == round(500 * 20 / 60)


def _dev(did="a", soc=80.0, pc=True):
    caps = Capabilities(10000, 3000, 3000, 10, frozenset(Mode), pc)
    return DevIn(DeviceConfig(did, did, "x", caps, extra={"control_enabled": True}), Actual(online=True, soc=soc, power_w=0.0, mode=Mode.AUTO))


def test_engine_follows_the_plan_for_export_and_hold():
    exp = lambda t: 0.30 if t.hour == 17 else 0.15   # noqa: E731
    plan = plan_day(T0, slots(PRICES), exp, flat_use(), budget_wh=7000, export_max_w=6000)
    w = World(now=T0, devices=[_dev("a"), _dev("b")], settings=Settings(export_discharge=True, hold_for_dearer=True), day_plan=plan,
              export_price_now=0.15, buyback_price=0.07)
    r = compute(w)
    assert all(r.wants[d].mode is Mode.HOLD and "planned" in r.reasons[d] for d in "ab")   # energy kept for 17:00
    peak = T0 + timedelta(hours=3)
    plan = plan_day(peak, slots(PRICES), exp, flat_use(), budget_wh=7000, export_max_w=6000)
    r = compute(World(now=peak, devices=[_dev("a"), _dev("b", soc=40)], settings=Settings(export_discharge=True), day_plan=plan,
                      export_price_now=0.30, buyback_price=0.07))
    assert {d: r.wants[d].mode for d in "ab"} == {"a": Mode.DISCHARGE, "b": Mode.DISCHARGE}
    assert r.reasons["a"].startswith(EXPORT_REASON) and "planned" in r.reasons["a"]
    assert r.wants["a"].power_w + r.wants["b"].power_w <= plan.export_now_w + 1
    small = plan_day(peak, slots(PRICES), exp, flat_use(), budget_wh=1200, export_max_w=6000)     # less stored: a part power,
    r = compute(World(now=peak, devices=[_dev("a"), _dev("b", soc=40)], settings=Settings(export_discharge=True), day_plan=small))
    assert 0 < small.export_now_w < 6000 and r.wants["a"].power_w > r.wants["b"].power_w            # shared by stored energy
    off = compute(World(now=T0, devices=[_dev("a")], settings=Settings(), day_plan=plan))
    assert "a" not in off.wants                                  # neither feature on: the plan changes nothing


# ------------------------------------------------------------------ saving sessions
def test_event_windows_from_a_calendar_and_from_octopus_joined_sessions():
    cal = {"message": "Saving session", "start_time": "2026-11-18 17:30:00", "end_time": "2026-11-18 18:30:00"}
    w = event_windows(cal, UTC)
    assert w == [(datetime(2026, 11, 18, 17, 30, tzinfo=UTC), datetime(2026, 11, 18, 18, 30, tzinfo=UTC))]
    octo = {"joined_events": [{"id": 1, "start": "2026-11-19T17:00:00+00:00", "end": "2026-11-19T18:00:00+00:00", "octopoints": 800},
                              {"start": "junk"}, "x", {"start": "2026-11-19T19:00:00Z", "end": "2026-11-19T18:00:00Z"}]}
    assert event_windows(octo) == [(datetime(2026, 11, 19, 17, 0, tzinfo=UTC), datetime(2026, 11, 19, 18, 0, tzinfo=UTC))]
    assert event_windows(None) == [] and event_windows({"start_time": "soon"}) == []


def test_charge_goal_before_the_next_event():
    wins = [(T0 + timedelta(hours=3), T0 + timedelta(hours=4)), (T0 + timedelta(days=3), T0 + timedelta(days=3, hours=1))]
    start = next_event_start(wins, T0)
    assert start == T0 + timedelta(hours=3)
    assert next_event_start(wins, T0 + timedelta(hours=3, minutes=30)) is None      # under way: nothing to charge for
    g = charge_goal(start, 95, T0)
    assert g.id == GOAL_ID and g.target_soc == 95 and g.by == time(17, 0) and g.source == "grid"
    assert next_deadline(g, T0) == start
    assert charge_goal(start, 0, T0) is None and charge_goal(None, 90, T0) is None


def test_the_charge_goal_charges_in_the_cheapest_slots_before_the_event():
    g = charge_goal(T0 + timedelta(hours=3), 90, T0)
    rates = RateTable(slots([0.30, 0.10, 0.30, 0.30, 0.30, 0.30, 0.35]))
    w = World(now=T0 + timedelta(minutes=30), devices=[_dev(soc=60)], settings=Settings(), goals=[g], rates=rates)
    r = compute(w)
    assert r.wants["a"].mode is Mode.CHARGE and r.reasons["a"] == f"goal '{GOAL_ID}'"


def test_house_use_is_capped_by_battery_power():
    p = plan_day(T0, slots(PRICES), None, flat_use(8000.0), budget_wh=100000, export_max_w=0, discharge_max_w=3000)
    assert all(round(s.house_wh) == 1500 for s in p.slots)                      # 3 kW for half an hour, not the 4 kWh the house uses
    assert not p.hold_now                                                        # running flat out is not "holding"


def test_the_last_seconds_of_a_half_hour_decide_like_the_rest_of_it():
    exp = lambda t: 0.30   # noqa: E731
    for left_s in (1, 5, 30, 600, 1790):
        t = T0 + timedelta(minutes=30) - timedelta(seconds=left_s)
        p = plan_day(t, slots(PRICES), exp, flat_use(), budget_wh=50000, export_max_w=3000)
        assert p.export_now_w == 3000, (left_s, p.export_now_w)
        held = plan_day(t, slots(PRICES), None, flat_use(), budget_wh=1000, export_max_w=0)
        assert held.hold_now, left_s                                             # still keeping the energy for the peak


def test_the_charge_before_an_event_is_not_capped_by_room_for_solar():
    g = charge_goal(T0 + timedelta(hours=3), 95, T0)
    rates = RateTable(slots([0.30, 0.10, 0.30, 0.30, 0.30, 0.30, 0.35]))
    w = World(now=T0 + timedelta(minutes=30), devices=[_dev(soc=60)], settings=Settings(), goals=[g], rates=rates, solar_ceiling={"a": 70})
    r = compute(w)
    assert r.wants["a"].mode is Mode.CHARGE and r.wants["a"].target_soc == 95


def test_all_day_calendar_entries_are_not_sessions_and_the_octopus_binary_sensor_gives_the_next_session():
    note = {"message": "Saving sessions this week", "all_day": True, "start_time": "2026-11-18 00:00:00", "end_time": "2026-11-19 00:00:00"}
    assert event_windows(note, UTC) == []
    octo = {"current_joined_event_start": None, "next_joined_event_start": "2026-11-18T17:30:00+00:00",
            "next_joined_event_end": "2026-11-18T18:30:00+00:00"}
    assert event_windows(octo) == [(datetime(2026, 11, 18, 17, 30, tzinfo=UTC), datetime(2026, 11, 18, 18, 30, tzinfo=UTC))]
