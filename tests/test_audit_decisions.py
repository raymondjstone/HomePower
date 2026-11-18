"""Second audit, decisions and money: found by simulating whole days against the planner, engine and savings ledger."""
from datetime import datetime, time, timedelta, timezone

from core.energy import Ledger
from core.engine import DevIn, Settings, World, compute
from core.grid import backup_desired
from core.models import Actual, Capabilities, Desired, DeviceConfig, Mode
from core.planner import Need, plan_charge
from core.rates import Slot

UTC = timezone.utc


def slots_from(t0, prices):
    return [Slot(t0 + timedelta(minutes=30 * i), t0 + timedelta(minutes=30 * (i + 1)), p) for i, p in enumerate(prices)]


def test_a_tiny_leftover_is_not_bought_at_peak_price_in_a_short_slot():
    # 10 kWh battery at 34.7 %, goal 90 %: 6010.9 Wh. Four cheap half-hours give 6000 Wh; the last 10.9 Wh must not become a
    # 1956 W burst at the peak price in the 20 seconds left of the current half-hour.
    now = datetime(2026, 11, 10, 17, 59, 40, tzinfo=UTC)
    t0 = datetime(2026, 11, 10, 17, 30, tzinfo=UTC)
    prices = [0.27, 0.27] + [0.27] * 14 + [0.07] * 4 + [0.27] * 20           # cheap from 01:30 for two hours
    slots = slots_from(t0, prices)
    allocs, unmet = plan_charge(now, datetime(2026, 11, 11, 7, 0, tzinfo=UTC),
                                [Need("a", 34.7, 10000, 3000, 90.0, efficiency=1.0)], slots, efficiency=1.0)
    assert [a for a in allocs if a.price > 0.07] == [], allocs
    assert unmet == {}


def test_batteries_sharing_a_limit_all_reach_a_target_that_is_possible():
    t0 = datetime(2026, 11, 10, 18, 0, tzinfo=UTC)
    slots = slots_from(t0, [0.20, 0.15, 0.10, 0.08, 0.09, 0.12, 0.18])         # 7 half-hours
    needs = [Need("b0", 27, 10000, 1500, 70.0, efficiency=0.92), Need("b1", 22, 10000, 3000, 70.0, efficiency=0.95)]
    allocs, unmet = plan_charge(t0, t0 + timedelta(hours=3.5), needs, slots, ceiling_w=3500)
    assert unmet == {}, unmet
    assert all(sum(a.power_w for a in allocs if a.start == s) <= 3500 for s in {a.start for a in allocs})


def test_the_efficient_battery_still_takes_the_cheapest_slot_when_both_can_finish():
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = slots_from(t0, [0.05, 0.30, 0.30, 0.30])
    needs = [Need("poor", 20, 5000, 3000, 60, efficiency=0.80), Need("good", 20, 5000, 3000, 60, efficiency=0.95)]
    allocs, unmet = plan_charge(t0, t0 + timedelta(hours=2), needs, slots, ceiling_w=3000)
    assert [a.device_id for a in allocs if a.price == 0.05] == ["good"] and unmet == {}


def _dev(did, soc, power, pv=None, mode=Mode.AUTO):
    caps = Capabilities(10000, 3000, 3000, 10)
    return DevIn(DeviceConfig(did, did, "x", caps, extra={"control_enabled": True}), Actual(online=True, soc=soc, power_w=power, mode=mode, pv_w=pv), controllable=True)


def test_zero_import_does_not_charge_because_another_battery_fills_from_its_own_panels():
    a, b = _dev("a", 60, 0.0), _dev("b", 50, 1500.0, pv=1500.0)                 # b's charging never reaches the house meter
    w = World(now=datetime(2026, 11, 10, 12, 0, tzinfo=UTC), devices=[a, b], settings=Settings(import_limit_w=None, zero_import_device="a"),
              grid_w=800.0, grid_fresh=True)
    want = compute(w).wants.get("a")
    assert want is not None and want.mode is not Mode.CHARGE, want
    assert want.mode is Mode.DISCHARGE and want.power_w > 300, want              # the house imports 800 W: a covers it


def test_backup_mode_holds_the_reserve_instead_of_using_and_rebuying_it():
    caps = Capabilities(10000, 3000, 3000, 10)
    assert backup_desired(40, 50, caps).mode is Mode.CHARGE
    held = backup_desired(51, 50, caps)
    assert held.mode is Mode.HOLD and held.reserve_soc == 50                      # just above the reserve: kept there
    assert backup_desired(60, 50, caps) is None                                   # well above: the battery runs as normal
    assert backup_desired(None, 50, caps) is None


def test_charging_while_paid_to_take_the_energy_counts_as_savings():
    lg = Ledger()
    lg.add("2026-11-10", "a", 10.0, 1.0, 0.0, 0.0, -0.05, 0.0, 10.0, -0.05)       # 10 kWh in at -5p: the battery is paid 50p
    lg.add("2026-11-10", "a", 0.0, 0.0, 9.0, 0.0, 0.25, 0.0, 1.0, -0.05)          # 9 kWh used when the import would cost 25p
    assert lg.saved("2026-11-10") >= 2.85, lg.saved("2026-11-10")            # the 9 kWh value plus the money earned storing it


def test_an_idle_battery_just_under_a_goal_target_is_not_topped_up_again_and_again():
    from datetime import time as dtime
    from core.goals import Goal
    from core.rates import RateTable
    now = datetime(2026, 11, 10, 5, 0, tzinfo=UTC)                              # after the cheap window, deadline 07:00
    rates = RateTable(peak=0.30, offpeak=0.075, offpeak_start=dtime(0, 30), offpeak_end=dtime(4, 30))
    goal = Goal("g", frozenset(), 90, dtime(7, 0))

    def want(soc, mode):
        d = _dev("a", soc, 0.0, mode=mode)
        return compute(World(now=now, devices=[d], settings=Settings(import_limit_w=None), goals=[goal], rates=rates, grid_w=400.0, grid_fresh=True)).wants.get("a")
    assert want(89.6, Mode.AUTO) is None                                        # within 1.5 points and idle: left alone
    assert want(87.0, Mode.AUTO).mode is Mode.CHARGE                            # a real shortfall is still charged
    assert want(89.6, Mode.CHARGE).mode is Mode.CHARGE                          # once charging it carries on to the target
