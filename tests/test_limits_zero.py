from core.limits import apply_totals
from core.models import Capabilities, Desired, Mode
from core.zero_import import desired_for, setpoint

X1 = Capabilities(10000, max_charge_w=6000, max_discharge_w=6000, min_soc=10)
E5K = Capabilities(5000, max_charge_w=2500, max_discharge_w=2500)


def test_total_charge_cap_trims_lower_priority_first():
    items = {
        "x1": (Desired(Mode.CHARGE, 4000), X1, 0),
        "e5k": (Desired(Mode.CHARGE, 2500), E5K, 1),
    }
    out = apply_totals(items, charge_cap_w=5000, discharge_cap_w=None)
    assert out["x1"].power_w == 4000 and out["e5k"].power_w == 1000


def test_device_cut_to_zero_goes_to_hold():
    items = {"x1": (Desired(Mode.CHARGE, 4000), X1, 0), "e5k": (Desired(Mode.CHARGE, 2500), E5K, 1)}
    out = apply_totals(items, charge_cap_w=4000, discharge_cap_w=None)
    assert out["e5k"].mode is Mode.HOLD


def test_unspecified_power_counts_as_device_max():
    items = {"x1": (Desired(Mode.DISCHARGE), X1, 0), "e5k": (Desired(Mode.DISCHARGE), E5K, 1)}
    out = apply_totals(items, charge_cap_w=None, discharge_cap_w=7000)
    assert out["x1"].power_w == 6000 and out["e5k"].power_w == 1000


def test_charge_and_discharge_caps_are_independent_and_none_means_no_cap():
    items = {"x1": (Desired(Mode.CHARGE, 6000), X1, 0), "e5k": (Desired(Mode.DISCHARGE, 2500), E5K, 1)}
    out = apply_totals(items, charge_cap_w=None, discharge_cap_w=1000)
    assert out["x1"].power_w == 6000 and out["e5k"].power_w == 1000


K = dict(max_discharge_w=6000, max_charge_w=4000)


def test_zero_import_covers_import():
    assert setpoint(800, 0, 0, **K) == 800
    assert setpoint(200, 800, 0, **K) == 1000  # discharging 800, still importing 200


def test_zero_import_exporting_reduces_then_charges_with_surplus():
    assert setpoint(-300, 800, 0, **K) == 500   # still discharging, less
    assert setpoint(-800, 800, 0, **K) == 0     # exactly balanced -> idle
    assert setpoint(-1500, 0, 0, **K) == -1500  # pure surplus -> charge
    assert setpoint(-500, -1500, 0, **K) == -2000  # already charging 1500, export grew by 500


def test_zero_import_charging_never_pulls_from_grid():
    # charging at 1500 but meter shows 400 import (cloud) -> wind charge back to 1100
    assert setpoint(400, -1500, 0, **K) == -1100
    # import exceeds own charge -> flips to discharge
    assert setpoint(600, -400, 0, **K) == 200


def test_zero_import_limits_and_dead_zone():
    assert setpoint(9000, 0, 0, **K) == 6000
    assert setpoint(-9000, 0, 0, **K) == -4000
    assert setpoint(20, 0, 0, **K) == 0 and setpoint(-20, 0, 0, **K) == 0


def test_zero_import_ignores_deliberate_charging():
    assert setpoint(2000, 0, 2000, **K) == 0


def test_zero_import_deadband_same_direction_only():
    assert setpoint(20, 1000, 0, current_set_w=1000, **K) == 1000
    assert setpoint(-1000, 0, 0, current_set_w=-1030, **K) == -1030


def test_desired_for_failsafe_and_soc_floor():
    assert desired_for(None, X1, 80).mode is Mode.HOLD       # no grid reading
    assert desired_for(500, X1, 10).mode is Mode.HOLD        # at min_soc
    d = desired_for(500, X1, 50)
    assert d.mode is Mode.DISCHARGE and d.power_w == 500


def test_desired_for_charge_and_full():
    d = desired_for(-1500, X1, 50)
    assert d.mode is Mode.CHARGE and d.power_w == 1500 and d.target_soc == 100
    assert desired_for(-1500, X1, 100).mode is Mode.HOLD
    assert desired_for(-1500, X1, 90, target_soc=90).mode is Mode.HOLD


# ---- soc_guard: software SOC limits for batteries with no target/reserve entity
from core.limits import soc_guard
from core.models import Capabilities, Desired, Mode

_CAPS = Capabilities(capacity_wh=5000, max_charge_w=3000, max_discharge_w=3000, min_soc=10)


def test_soc_guard_stops_charge_at_target():
    d = Desired(Mode.CHARGE, 2000, target_soc=80)
    assert soc_guard(d, 79.0, _CAPS) == d
    assert soc_guard(d, 80.0, _CAPS).mode is Mode.HOLD


def test_soc_guard_stops_discharge_at_reserve_or_min():
    assert soc_guard(Desired(Mode.DISCHARGE, 1000, reserve_soc=30), 30, _CAPS).mode is Mode.HOLD
    assert soc_guard(Desired(Mode.DISCHARGE, 1000), 10, _CAPS).mode is Mode.HOLD   # device min_soc
    assert soc_guard(Desired(Mode.DISCHARGE, 1000), 11, _CAPS).mode is Mode.DISCHARGE


def test_soc_guard_ignores_unknown_soc_and_other_modes():
    d = Desired(Mode.CHARGE, 2000, target_soc=80)
    assert soc_guard(d, None, _CAPS) == d
    assert soc_guard(Desired(Mode.AUTO), 100, _CAPS).mode is Mode.AUTO


def test_soc_guard_hysteresis():
    d = Desired(Mode.CHARGE, 2000, target_soc=80)
    assert soc_guard(d, 79.0, _CAPS, held=True).mode is Mode.HOLD   # not yet 2 points inside
    assert soc_guard(d, 77.5, _CAPS, held=True).mode is Mode.CHARGE
    dd = Desired(Mode.DISCHARGE, 1000)
    assert soc_guard(dd, 11.0, _CAPS, held=True).mode is Mode.HOLD
    assert soc_guard(dd, 12.5, _CAPS, held=True).mode is Mode.DISCHARGE


def test_charge_ceiling_import_headroom():
    from core.limits import charge_ceiling
    # limit 7000, house base import 2000 W (grid 2000 + 0 charging) -> 5000 W headroom
    assert charge_ceiling(None, 7000, 2000.0, 0.0) == 5000
    # already charging 3000: grid 5000 -> base 2000 -> still 5000
    assert charge_ceiling(None, 7000, 5000.0, 3000.0) == 5000
    # user cap is lower
    assert charge_ceiling(3000, 7000, 2000.0, 0.0) == 3000
    # solar export: negative grid gives more headroom, never negative cap
    assert charge_ceiling(None, 7000, 9000.0, 0.0) == 0
    # no grid sensor: use the limit itself; nothing configured: no ceiling
    assert charge_ceiling(None, 7000, None, 0.0) == 7000
    assert charge_ceiling(None, None, None, 0.0) is None
