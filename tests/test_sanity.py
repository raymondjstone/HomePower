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


def test_rate_learner_uses_the_median_and_ignores_tapering():
    from hp.core.sanity import RateLearner
    r = RateLearner()
    for w in (2900, 3000, 3100, 2950, 3050):
        assert not r.observe("e", True, w, 50)            # not enough samples yet
    assert r.observe("e", True, 3000, 50) and r.rates["e"] == 3000
    assert not r.observe("e", True, 200, 50)              # a battery tapering off is not its rate
    assert not r.observe("e", False, 3000, 50)            # not asked to charge
    assert not r.observe("e", True, 3000, 99)             # nearly full: it tapers
    assert RateLearner({"e": 2800}).rates == {"e": 2800}  # kept across restarts


def test_slow_battery_gets_one_cycle_of_grace():
    from hp.core.sanity import next_unconfirmed
    assert next_unconfirmed(0, 0, True, True) == (0, 1)           # cloud lag: wait
    assert next_unconfirmed(0, 1, True, True) == (1, 1)           # still wrong a cycle later: count it
    assert next_unconfirmed(0, 0, True, False) == (1, 0)          # a local battery is counted at once
    assert next_unconfirmed(2, 1, False, True) == (0, 0)


def test_command_budget_caps_commands_but_never_the_hand_back():
    from hp.core.sanity import CommandBudget
    b = CommandBudget(limit=3)
    assert [b.allow("e", "d1", False) for _ in range(4)] == [True, True, True, False]
    assert b.allow("e", "d1", True)                               # Auto always goes through
    assert b.allow("e", "d2", False)                              # a new day starts again


def test_explain_idle_names_the_main_blocker():
    from hp.core.sanity import explain_idle
    ok = dict(control_enabled=True, paused=False, dry_run=False, online=True, off_grid=False, protected=False,
              last_error=None, guard_held=False, unconfirmed=0)
    assert explain_idle(**ok) is None
    assert "Monitor only" in explain_idle(**{**ok, "control_enabled": False})
    assert "Offline" in explain_idle(**{**ok, "online": False, "control_enabled": False})
    assert explain_idle(**{**ok, "last_error": "rate limit"}) == "rate limit"
    assert "not confirmed" in explain_idle(**{**ok, "unconfirmed": 1})
