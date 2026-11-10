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


def test_zero_import_covers_import():
    assert setpoint(800, own_discharge_w=0, other_charge_w=0, max_discharge_w=6000) == 800
    # already discharging 800 and still importing 200 -> raise to 1000
    assert setpoint(200, own_discharge_w=800, other_charge_w=0, max_discharge_w=6000) == 1000


def test_zero_import_exporting_reduces_and_stops():
    assert setpoint(-300, own_discharge_w=800, other_charge_w=0, max_discharge_w=6000) == 500
    assert setpoint(-900, own_discharge_w=800, other_charge_w=0, max_discharge_w=6000) == 0


def test_zero_import_ignores_deliberate_charging_and_caps_at_max():
    # 2000 W import is another battery's scheduled charge: don't discharge to feed it
    assert setpoint(2000, own_discharge_w=0, other_charge_w=2000, max_discharge_w=6000) == 0
    assert setpoint(9000, own_discharge_w=0, other_charge_w=0, max_discharge_w=6000) == 6000


def test_zero_import_deadband():
    assert setpoint(20, own_discharge_w=1000, other_charge_w=0, max_discharge_w=6000, current_set_w=1000) == 1000


def test_desired_for_failsafe_and_soc_floor():
    assert desired_for(None, X1, 80).mode is Mode.HOLD       # no grid reading
    assert desired_for(500, X1, 10).mode is Mode.HOLD        # at min_soc
    d = desired_for(500, X1, 50)
    assert d.mode is Mode.DISCHARGE and d.power_w == 500
