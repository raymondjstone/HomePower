from datetime import date, datetime, time

from core.allocator import ChargeRequest, allocate
from core.models import Actual, Desired, Mode
from core.reconcile import Decision, clamp, decide
from core.schedule import WEEKDAYS, WEEKEND, ScheduleEntry, resolve

CHARGE = Desired(Mode.CHARGE, power_w=3000)
HOLD = Desired(Mode.HOLD)


def dt(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi)


# 2026-11-09 is a Monday
def test_overnight_window_crosses_midnight():
    e = ScheduleEntry("cheap", frozenset(), time(23, 30), time(7, 30), CHARGE)
    assert resolve([e], "x1", dt(2026, 11, 9, 23, 45)) == CHARGE
    assert resolve([e], "x1", dt(2026, 11, 10, 3)) == CHARGE
    assert resolve([e], "x1", dt(2026, 11, 10, 8)) is None


def test_morning_part_uses_start_day():
    # window starts on weekdays only; Saturday 02:00 belongs to Friday's window, Monday 02:00 to Sunday's (not active)
    e = ScheduleEntry("c", frozenset(), time(23, 30), time(7, 30), CHARGE, days=WEEKDAYS)
    assert resolve([e], "d", dt(2026, 11, 14, 2)) == CHARGE   # Sat 02:00 <- Fri
    assert resolve([e], "d", dt(2026, 11, 9, 2)) is None      # Mon 02:00 <- Sun


def test_priority_and_date_override():
    base = ScheduleEntry("a", frozenset(), time(0), time(23, 59), HOLD)
    over = ScheduleEntry("b", frozenset(), time(0), time(23, 59), CHARGE, on_date=date(2026, 11, 10))
    assert resolve([base, over], "d", dt(2026, 11, 10, 12)) == CHARGE
    assert resolve([base, over], "d", dt(2026, 11, 11, 12)) == HOLD


def test_device_filter_weekend_and_season():
    e = ScheduleEntry("w", frozenset({"e5000"}), time(0), time(23, 59), CHARGE,
                      days=WEEKEND, season_start=(11, 1), season_end=(2, 28))
    assert resolve([e], "e5000", dt(2026, 11, 14, 12)) == CHARGE   # Sat, in winter season
    assert resolve([e], "x1", dt(2026, 11, 14, 12)) is None
    assert resolve([e], "e5000", dt(2026, 11, 9, 12)) is None      # Monday
    assert resolve([e], "e5000", dt(2026, 7, 4, 12)) is None       # summer Saturday


def test_reconcile_decisions():
    want = Desired(Mode.CHARGE, power_w=3000, reserve_soc=20)
    ok = Actual(True, mode=Mode.CHARGE, set_power_w=3000, reserve_soc=20)
    assert decide(want, ok, now=0, last_apply=None)[0] is Decision.OK
    bad = Actual(True, mode=Mode.AUTO, set_power_w=3000, reserve_soc=20)
    assert decide(want, bad, now=0, last_apply=None)[0] is Decision.APPLY
    assert decide(want, bad, now=10, last_apply=0, cooldown_s=120)[0] is Decision.WAIT
    prot = Actual(True, mode=Mode.AUTO, protected=True)
    assert decide(want, prot, now=0, last_apply=None)[0] is Decision.SKIP_PROTECTED
    assert decide(want, Actual(False), now=0, last_apply=None)[0] is Decision.OFFLINE


def test_clamp_limits():
    d = clamp(Desired(Mode.CHARGE, 9000, reserve_soc=5), max_charge_w=4000, max_discharge_w=6000, min_soc=10)
    assert d.power_w == 4000 and d.reserve_soc == 10
    d = clamp(Desired(Mode.DISCHARGE, 9000), max_charge_w=4000, max_discharge_w=6000, min_soc=10)
    assert d.power_w == 6000


def test_allocator_just_in_time_and_import_limit():
    reqs = [
        ChargeRequest("x1", soc=20, target_soc=100, capacity_wh=10000, max_charge_w=6000, priority=0),
        ChargeRequest("e5k", soc=50, target_soc=100, capacity_wh=5000, max_charge_w=2500, priority=1),
    ]
    # 7h window: x1 needs 8000Wh -> 1143W; e5k needs 2500Wh -> 357W
    out = allocate(reqs, 7, import_limit_w=7000)
    assert out == {"x1": 1143, "e5k": 357}
    # tight limit: priority device first
    out = allocate(reqs, 1, import_limit_w=3000)
    assert out["x1"] == 3000 and out["e5k"] == 0
    # at target -> 0
    done = [ChargeRequest("a", 100, 100, 5000, 2500)]
    assert allocate(done, 5, 7000) == {"a": 0}


def test_condition_entity_and_all_day():
    from core.schedule import ScheduleEntry
    e = ScheduleEntry("tariff", frozenset(), time(0, 0), time(23, 59), CHARGE, all_day=True, condition_entity="binary_sensor.cheap")
    on = lambda ent: ent == "binary_sensor.cheap"
    off = lambda ent: False
    assert resolve([e], "d", dt(2026, 11, 10, 14), on) == CHARGE
    assert resolve([e], "d", dt(2026, 11, 10, 14), off) is None
    # windowed schedule + condition: both must hold
    w = ScheduleEntry("w", frozenset(), time(1, 0), time(2, 0), CHARGE, condition_entity="binary_sensor.cheap")
    assert resolve([w], "d", dt(2026, 11, 10, 3), on) is None
    assert resolve([w], "d", dt(2026, 11, 10, 1, 30), on) == CHARGE
    assert resolve([w], "d", dt(2026, 11, 10, 1, 30), off) is None


def test_materially_different_bypasses_cooldown_only_for_real_changes():
    from core.reconcile import materially_different as md
    a = Desired(Mode.CHARGE, 2000)
    assert md(None, a) and md(a, Desired(Mode.DISCHARGE, 2000)) and md(a, Desired(Mode.HOLD))
    assert not md(a, Desired(Mode.CHARGE, 2050))          # noise: wait out the cooldown
    assert md(a, Desired(Mode.CHARGE, 1000)) and md(a, Desired(Mode.CHARGE, 2500))
    assert not md(Desired(Mode.AUTO), Desired(Mode.AUTO))
    assert md(Desired(Mode.CHARGE, None), Desired(Mode.CHARGE, 500))
