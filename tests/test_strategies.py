from core.models import Desired, Mode
from core.serde import ValidationError, device_from_dict
from core.strategies import interpret, plan
import pytest

BASE = {"name": "B", "capacity_wh": 5000, "max_charge_w": 3000, "max_discharge_w": 2500, "min_soc": 10}


def generic():
    return device_from_dict({**BASE, "device_id": "g", "kind": "custom",
        "entities": {"soc": "sensor.a", "power": "sensor.b", "mode": "select.c", "set_power": "number.d"},
        "mode_map": {"auto": "A", "charge": "C", "discharge": "D", "hold": "H"}})


def test_generic_plan_orders_limits_before_mode():
    c = generic()
    p = plan(c.extra, c.caps, Desired(Mode.CHARGE, 1000))
    assert [x[0] for x in p] == ["number", "select"] and p[-1][2]["option"] == "C"


def test_generic_read_back_and_sign():
    c = generic()
    a = interpret(c.extra, c.caps, {"soc": "50", "power": "800", "mode": "D", "set_power": "800"})
    assert (a.mode, a.set_power_w, a.power_w, a.online) == (Mode.DISCHARGE, 800, 800.0, True)
    assert interpret(c.extra, c.caps, {"mode": "unavailable"}).online is False


def test_generic_requires_entities_and_modes():
    with pytest.raises(ValidationError):
        device_from_dict({**BASE, "device_id": "g", "kind": "custom", "entities": {"soc": "sensor.a"}, "mode_map": {}})
    with pytest.raises(ValidationError):
        device_from_dict({**BASE, "device_id": "g", "kind": "custom",
                          "entities": {"soc": "sensor.a", "power": "sensor.b", "mode": "select.c", "set_power": "number.d"},
                          "mode_map": {"auto": "A"}})
