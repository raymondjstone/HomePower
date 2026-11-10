import pytest

from core.models import Desired, Mode
from core.serde import ValidationError, device_from_dict
from core.strategies import interpret, plan

BASE = {"name": "B", "capacity_wh": 5000, "max_charge_w": 3000, "max_discharge_w": 2500, "min_soc": 10}


def anker():
    return device_from_dict({**BASE, "device_id": "a", "kind": "anker_e5000_pro",
        "entities": {"soc": "sensor.soc", "charge_power": "sensor.cp", "discharge_power": "sensor.dp",
                     "work_mode": "select.wm", "direction": "select.dir", "setpoint": "number.sp"},
        "mode_map": {"auto": "self_consumption", "control": "third_party_control", "charge": "charge", "discharge": "discharge"}})


def x1(engage=True):
    ents = {"soc": "sensor.soc", "power": "sensor.p", "setpoint": "number.sp"}
    ents.update({"engage": "switch.mc"} if engage else {"work_mode": "select.wm"})
    mm = {} if engage else {"auto": "App-managed", "control": "3rd party control"}
    return device_from_dict({**BASE, "device_id": "x", "kind": "anker_x1_afewyards", "entities": ents, "mode_map": mm})


def eco():
    return device_from_dict({**BASE, "device_id": "e", "kind": "ecoflow_stream_ultra_x",
        "entities": {"soc": "sensor.soc", "power": "sensor.p", "backup_reserve": "number.br", "self_powered": "switch.sp"}})


def calls(cfg, d):
    return [(dom, svc, {k: v for k, v in data.items() if k != "entity_id"} | {"e": data["entity_id"]})
            for dom, svc, data in plan(cfg.extra, cfg.caps, d)]


def test_anker_charge_sets_mode_direction_then_absolute_setpoint():
    c = anker()
    assert [(x[2]["e"]) for x in calls(c, Desired(Mode.CHARGE, 2000))] == ["select.wm", "select.dir", "number.sp"]
    last = plan(c.extra, c.caps, Desired(Mode.CHARGE, 2000))[-1]
    assert last[2]["value"] == 2000
    assert plan(c.extra, c.caps, Desired(Mode.DISCHARGE, 900))[1][2]["option"] == "discharge"


def test_anker_auto_and_hold():
    c = anker()
    assert plan(c.extra, c.caps, Desired(Mode.AUTO)) == [("select", "select_option", {"entity_id": "select.wm", "option": "self_consumption"})]
    hold = plan(c.extra, c.caps, Desired(Mode.HOLD))
    assert hold[-1] == ("number", "set_value", {"entity_id": "number.sp", "value": 0})


def test_anker_read_back():
    c = anker()
    st = {"soc": "50", "charge_power": "0", "discharge_power": "800", "work_mode": "third_party_control",
          "direction": "discharge", "setpoint": "800"}
    a = interpret(c.extra, c.caps, st)
    assert (a.mode, a.set_power_w, a.power_w) == (Mode.DISCHARGE, 800, -800)
    assert interpret(c.extra, c.caps, {**st, "work_mode": "self_consumption"}).mode is Mode.AUTO
    assert interpret(c.extra, c.caps, {**st, "setpoint": "0"}).mode is Mode.HOLD
    assert interpret(c.extra, c.caps, {**st, "work_mode": "unavailable"}).online is False


def test_x1_signed_setpoint_and_switch():
    c = x1()
    ch = plan(c.extra, c.caps, Desired(Mode.CHARGE, 1500))
    assert ch[0][:2] == ("switch", "turn_on") and ch[-1][2]["value"] == -1500
    assert plan(c.extra, c.caps, Desired(Mode.DISCHARGE, 700))[-1][2]["value"] == 700
    rel = plan(c.extra, c.caps, Desired(Mode.AUTO))
    assert rel[0][2]["value"] == 0 and rel[-1][:2] == ("switch", "turn_off")


def test_x1_read_back_and_power_sign():
    c = x1()
    st = {"soc": "40", "power": "600", "engage": "on", "setpoint": "-1500"}
    a = interpret(c.extra, c.caps, st)
    assert (a.mode, a.set_power_w) == (Mode.CHARGE, 1500)
    assert a.power_w == -600  # raw + discharging is normalised to - discharging
    assert interpret(c.extra, c.caps, {**st, "engage": "off"}).mode is Mode.AUTO


def test_x1_work_mode_variant_needs_option_names():
    c = x1(engage=False)
    assert plan(c.extra, c.caps, Desired(Mode.CHARGE, 1000))[0][2]["option"] == "3rd party control"
    with pytest.raises(ValidationError):
        device_from_dict({**BASE, "device_id": "x", "kind": "anker_x1_cbource",
                          "entities": {"soc": "s.a", "power": "s.b", "setpoint": "n.c", "work_mode": "select.w"}, "mode_map": {}})
    with pytest.raises(ValidationError):  # neither switch nor work mode
        device_from_dict({**BASE, "device_id": "x", "kind": "anker_x1_cbource",
                          "entities": {"soc": "s.a", "power": "s.b", "setpoint": "n.c"}, "mode_map": {}})


def test_ecoflow_only_auto_and_hold():
    c = eco()
    assert c.caps.supported_modes == {Mode.AUTO, Mode.HOLD}
    with pytest.raises(ValueError):
        plan(c.extra, c.caps, Desired(Mode.CHARGE, 1000))
    assert plan(c.extra, c.caps, Desired(Mode.HOLD))[0][2]["value"] == 95
    auto = plan(c.extra, c.caps, Desired(Mode.AUTO))
    assert auto[0][:2] == ("switch", "turn_on") and auto[-1][2]["value"] == 10
    assert interpret(c.extra, c.caps, {"backup_reserve": "95", "soc": "70", "power": "-300"}).mode is Mode.HOLD
    a = interpret(c.extra, c.caps, {"backup_reserve": "10", "soc": "70", "power": "-300"})
    assert a.mode is Mode.AUTO and a.power_w == -300  # EcoFlow already reports discharge as negative


def test_generic_unchanged():
    c = device_from_dict({**BASE, "device_id": "g", "kind": "custom",
        "entities": {"soc": "sensor.a", "power": "sensor.b", "mode": "select.c", "set_power": "number.d"},
        "mode_map": {"auto": "A", "charge": "C", "discharge": "D", "hold": "H"}})
    p = plan(c.extra, c.caps, Desired(Mode.CHARGE, 1000))
    assert [x[0] for x in p] == ["number", "select"] and p[-1][2]["option"] == "C"
