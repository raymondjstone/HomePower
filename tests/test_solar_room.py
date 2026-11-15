"""Leave room for the solar forecast: the calculation, and how the engine uses it."""
from datetime import datetime, time, timedelta, timezone

import pytest

from core.engine import World, compute
from core.goals import Goal
from core.models import Desired, Mode
from core.rates import RateTable
from core.solar_room import SolarDay, charge_ceiling_pct, max_start_kwh, peak_energy, split_by_capacity
from test_engine import dev, sched, world

UTC = timezone.utc
D = datetime(2026, 11, 13, tzinfo=UTC)
RISE, SET = D.replace(hour=8), D.replace(hour=16, minute=30)
T0 = D.replace(hour=7, minute=30)                     # cheap rate (23:30 - 07:30) ends
ALWAYS = lambda t: True                               # noqa: E731 - battery covers the base load all day
CHEAP = RateTable(peak=0.29, offpeak=0.14, offpeak_start=time(23, 30), offpeak_end=time(7, 30))


def test_solar_profile_adds_up_to_the_forecast():
    day = SolarDay(RISE, SET, 6.0)
    assert day.energy(RISE, SET) == pytest.approx(6.0)
    assert day.energy(D, RISE) == 0 and day.energy(SET, SET + timedelta(hours=2)) == 0
    mid = RISE + (SET - RISE) / 2
    assert day.energy(RISE, mid) == pytest.approx(3.0)                       # symmetrical about midday
    assert day.energy(mid - timedelta(hours=1), mid + timedelta(hours=1)) > day.energy(RISE, RISE + timedelta(hours=2))


def test_no_forecast_or_tiny_forecast_charges_fully():
    assert charge_ceiling_pct(10, 10, T0, SolarDay(RISE, SET, 0.0), 0.3, ALWAYS) == 100
    assert charge_ceiling_pct(10, 10, T0, SolarDay(RISE, SET, 2.0), 0.3, ALWAYS) == 100   # base load uses it all


def test_ceiling_is_the_highest_start_that_never_overflows():
    day = SolarDay(RISE, SET, 8.0)
    pct = charge_ceiling_pct(10, 10, T0, day, 0.2, ALWAYS)
    assert 10 < pct < 100
    assert peak_energy(pct / 10, T0, day, 0.2, ALWAYS, 1.0) <= 10 + 1e-6
    assert peak_energy((pct + 1) / 10, T0, day, 0.2, ALWAYS, 1.0) > 10        # one percent more would overflow


def test_base_load_makes_room_and_cheap_hours_do_not():
    day = SolarDay(RISE, SET, 8.0)
    none = charge_ceiling_pct(10, 10, T0, day, 0.0, ALWAYS)
    some = charge_ceiling_pct(10, 10, T0, day, 0.3, ALWAYS)
    assert some > none                                                        # the battery runs the house before the sun is strong
    assert none == 20                                                         # no base load: all 8 kWh must fit
    # charging ending earlier means more base-load hours from the battery before sunrise: more room
    early = charge_ceiling_pct(10, 10, D.replace(hour=5), day, 0.3, ALWAYS)
    assert early > some
    # ... unless those hours are cheap-rate, when the grid runs the house
    cheap = charge_ceiling_pct(10, 10, D.replace(hour=5), day, 0.3, lambda t: not CHEAP.in_cheap_window(t.time()))
    assert cheap == some


def test_battery_at_its_minimum_stops_paying_and_only_rises():
    # a huge base load empties the battery before the sun; the solar then lifts it from the minimum
    day = SolarDay(RISE, SET, 3.0)
    assert peak_energy(1.0, T0, day, 5.0, ALWAYS, 1.0) >= 1.0
    assert max_start_kwh(10, 1.0, T0, day, 5.0, ALWAYS) == 10


def test_forecast_and_base_load_shared_by_capacity():
    assert split_by_capacity(9.0, {"big": 10, "small": 5}) == pytest.approx({"big": 6.0, "small": 3.0})
    assert split_by_capacity(1.0, {"a": 0}) == {"a": 0.0}


def test_cheap_window_end():
    late = datetime(2026, 11, 12, 23, 45, tzinfo=UTC)
    assert CHEAP.cheap_window_end(late) == datetime(2026, 11, 13, 7, 30, tzinfo=UTC)
    assert CHEAP.cheap_window_end(D.replace(hour=2)) == T0
    assert CHEAP.cheap_window_end(D.replace(hour=12)) is None
    two = RateTable(peak=1, offpeak=0.5, offpeak_start=time(0), offpeak_end=time(4), extra_windows=[(time(4), time(7))])
    assert two.cheap_window_end(D.replace(hour=1)) == D.replace(hour=7)      # back-to-back windows are one


# ------------------------------------------------------------------ the engine
NIGHT = datetime(2026, 11, 13, 2, 0, tzinfo=UTC)


def test_schedule_grid_charging_stops_at_the_ceiling():
    s = sched(start=time(0), end=time(7), power=2000)
    r = compute(world([dev(soc=40)], schedules=[s], now=NIGHT, solar_ceiling={"a": 70}))
    assert r.wants["a"].mode is Mode.CHARGE and r.wants["a"].target_soc == 70 and "room left for solar" in r.reasons["a"]
    r = compute(world([dev(soc=71)], schedules=[s], now=NIGHT, solar_ceiling={"a": 70}))
    assert r.wants["a"].mode is Mode.HOLD                                     # reached: no more grid charging
    # a lower target of the schedule's own is kept; solar-only schedules are not limited
    low = sched(start=time(0), end=time(7), power=2000)
    low = low.__class__(**{**{f: getattr(low, f) for f in low.__dataclass_fields__}, "desired": Desired(Mode.CHARGE, 2000, target_soc=50)})
    assert compute(world([dev(soc=40)], schedules=[low], now=NIGHT, solar_ceiling={"a": 70})).wants["a"].target_soc == 50
    sol = sched(start=time(0), end=time(7), power=2000, source="solar")
    r = compute(world([dev(soc=40)], schedules=[sol], now=NIGHT, solar_ceiling={"a": 70}, grid_w=None))
    assert "room left" not in r.reasons.get("a", "")


def test_goal_targets_follow_each_battery_ceiling():
    g = Goal("g", frozenset(), 100, time(7))
    devs = [dev("a", soc=30), dev("b", soc=30)]
    r = compute(world(devs, goals=[g], now=NIGHT, rates=CHEAP, solar_ceiling={"a": 60}))
    assert r.wants["a"].target_soc == 60 and r.wants["b"].target_soc == 100
    assert any("leaving room for the solar forecast" in n for n in r.notes)


def test_battery_charging_itself_overnight_is_held_at_the_ceiling():
    charging = dev(soc=72, power=1500)
    r = compute(world([charging], now=NIGHT, cheap_now=True, solar_ceiling={"a": 70}))
    assert r.wants["a"].mode is Mode.HOLD and "room for solar" in r.reasons["a"]
    # below it, or outside the cheap rate, or with no limit: left alone
    assert "a" not in compute(world([dev(soc=60, power=1500)], now=NIGHT, cheap_now=True, solar_ceiling={"a": 70})).wants
    assert "a" not in compute(world([dev(soc=72, power=1500)], now=NIGHT, cheap_now=False, solar_ceiling={"a": 70})).wants
    assert "a" not in compute(world([dev(soc=72, power=1500)], now=NIGHT, cheap_now=True)).wants
    # once held (power 0) it stays held a little below the line, not flapping
    held = dev(soc=69, power=0, release=True)
    assert compute(world([held], now=NIGHT, cheap_now=True, solar_ceiling={"a": 70})).wants["a"].mode is Mode.HOLD
