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


ANKER = {"device_id": "x1", "name": "X1", "kind": "anker_x1", "capacity_wh": 5000, "max_charge_w": 3000,
         "max_discharge_w": 3000, "min_soc": 10, "connection": {"host": "192.168.1.50"}}
ECO = {"device_id": "e", "name": "E", "kind": "ecoflow_stream_ultra_x", "capacity_wh": 5000, "max_charge_w": 2300,
       "max_discharge_w": 2300, "connection": {"region": "eu", "access_key": "a", "secret_key": "s", "sn": "BK61"}}


def test_anker_direct_defaults_to_monitor_only_and_roundtrips():
    c = device_from_dict(ANKER)
    assert c.extra["connection"] == {"host": "192.168.1.50", "port": 502, "unit_id": 1}
    assert c.extra["control_enabled"] is False and c.extra["watchdog"] is True and c.extra["restore_mode"] == 20
    assert Mode.CHARGE in c.caps.supported_modes
    assert device_from_dict(device_to_dict(c)) == c
    assert device_from_dict({**ANKER, "control_enabled": True}).extra["control_enabled"] is True


def test_direct_connection_validation():
    with pytest.raises(ValidationError):
        device_from_dict({**ANKER, "connection": {}})
    with pytest.raises(ValidationError):
        device_from_dict({**ECO, "connection": {"region": "eu", "access_key": "a"}})
    with pytest.raises(ValidationError):
        device_from_dict({**ECO, "connection": {**ECO["connection"], "region": "mars"}})


def test_ecoflow_only_auto_and_hold():
    assert device_from_dict(ECO).caps.supported_modes == {Mode.AUTO, Mode.HOLD}
