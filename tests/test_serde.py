import pytest

from core.models import Mode
from core.serde import (ValidationError, device_from_dict, device_to_dict,
                        schedule_from_dict, schedule_to_dict)

DEV = {
    "device_id": "x1", "name": "X1", "kind": "custom", "capacity_wh": 5000,
    "max_charge_w": 3000, "max_discharge_w": 3000, "min_soc": 10,
    "entities": {"soc": "sensor.a", "power": "sensor.b", "mode": "select.c", "set_power": "number.d"},
    "mode_map": {"auto": "A", "charge": "C", "discharge": "D", "hold": "H"},
}
SCH = {"id": "s1", "device_ids": ["x1"], "start": "23:30", "end": "07:30", "mode": "charge",
       "power_w": 3000, "target_soc": 100, "days": [0, 1, 2], "season_start": "10-01", "season_end": "03-31"}


def test_device_roundtrip():
    c = device_from_dict(DEV)
    assert device_from_dict(device_to_dict(c)) == c


def test_device_requires_entities_and_modes():
    with pytest.raises(ValidationError):
        device_from_dict({**DEV, "entities": {"soc": "sensor.a"}})
    with pytest.raises(ValidationError):
        device_from_dict({**DEV, "mode_map": {"auto": "A"}})


def test_schedule_roundtrip():
    e = schedule_from_dict(SCH)
    assert e.desired.mode is Mode.CHARGE and e.season_start == (10, 1)
    assert schedule_from_dict(schedule_to_dict(e)) == e


def test_schedule_validation():
    with pytest.raises(ValidationError):
        schedule_from_dict({**SCH, "start": "07:00", "end": "07:00"})
    with pytest.raises(ValidationError):
        schedule_from_dict({**SCH, "days": []})
    with pytest.raises(ValidationError):
        schedule_from_dict({**SCH, "season_end": None})


def test_all_day_needs_condition():
    with pytest.raises(ValidationError):
        schedule_from_dict({**SCH, "all_day": True})
    e = schedule_from_dict({**SCH, "all_day": True, "condition_entity": "binary_sensor.cheap_rate"})
    assert schedule_from_dict(schedule_to_dict(e)) == e
