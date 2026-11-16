"""Stuck-charge watch, grid sign check, cycling economics, fixed-rate planning."""
from datetime import datetime, timedelta, timezone

from hp.core.planner import Need, plan_charge
from hp.core.rates import Slot
from hp.core.sanity import ChargeWatch, cycling_margin, grid_sign_mismatch


def test_charge_watch_flags_a_battery_that_never_starts():
    w = ChargeWatch(after_s=600)
    assert not w.stuck("a", True, 40, 0, 80, 0)
    assert not w.stuck("a", True, 40, 5, 80, 300)
    assert w.stuck("a", True, 40, 5, 80, 700)                  # 11 minutes, nothing happened


def test_charge_watch_is_quiet_when_charging_or_done_or_not_asked():
    w = ChargeWatch(after_s=600)
    w.stuck("a", True, 40, 0, 80, 0)
    assert not w.stuck("a", True, 41, 0, 80, 700)               # the level rose: charging, whatever the power says
    assert not w.stuck("a", True, 41, 900, 80, 1800)            # drawing power
    assert not w.stuck("a", True, 79.5, 0, 80, 3000)            # at its target
    assert not w.stuck("a", False, 41, 0, 80, 5000)             # not being asked to charge


def test_grid_sign_mismatch():
    assert grid_sign_mismatch(800, -700)
    assert grid_sign_mismatch(-900, 600)
    assert not grid_sign_mismatch(800, 700)
    assert not grid_sign_mismatch(100, -90)                     # near zero: signs mean nothing
    assert not grid_sign_mismatch(None, -700) and not grid_sign_mismatch(800, None)


def test_cycling_margin_uses_the_batterys_own_efficiency():
    good = cycling_margin(0.10, 0.30, 0.90)
    assert good["worth_it"] and good["net_per_kwh"] == 0.17 and good["break_even_price"] == 0.1111
    bad = cycling_margin(0.25, 0.28, 0.85)
    assert not bad["worth_it"] and bad["net_per_kwh"] < 0          # a small price gap does not cover the loss


def _slots(n, price):
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    return [Slot(t0 + timedelta(minutes=30 * i), t0 + timedelta(minutes=30 * (i + 1)), price) for i in range(n)]


def test_fixed_rate_battery_gets_whole_slots_at_its_own_rate():
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    needs = [Need("anker", 20, 5000, 3000, 60), Need("eco", 20, 5000, 3000, 60, fixed_rate=True)]
    allocs, unmet = plan_charge(t0, t0 + timedelta(hours=6), needs, _slots(12, 0.1))
    eco = [a for a in allocs if a.device_id == "eco"]
    assert eco and {a.power_w for a in eco} == {3000}           # never a made-up 600 W it cannot do
    assert not unmet


def test_fixed_rate_battery_skipped_when_the_house_limit_has_no_room():
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    allocs, unmet = plan_charge(t0, t0 + timedelta(hours=2), [Need("eco", 20, 5000, 3000, 60, fixed_rate=True)], _slots(4, 0.1), ceiling_w=1500)
    assert allocs == [] and "eco" in unmet
