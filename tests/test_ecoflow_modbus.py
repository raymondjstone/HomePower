"""EcoFlow STREAM 5000 over local Modbus: a fake register space that follows EcoFlow's published map
(wire address = register offset + 40001, words high first, floats big-endian)."""
import asyncio
import struct

import pytest

from core.models import Capabilities, Desired, DeviceConfig, Mode
from hp.drivers.ecoflow_modbus import ADDRESS_OFFSET, EcoFlowModbusDriver
from hp.drivers.modbus import ModbusError

OFF = ADDRESS_OFFSET


def f32(x):
    return list(struct.unpack(">HH", struct.pack(">f", x)))


class FakeStream:
    def __init__(self):
        self.regs = {}
        self.writes = []
        self.dead = False
        self.put(0x206, f32(500.0) + f32(300.0) + f32(0.0) + f32(1500.0) + [62])      # load, grid, pv, battery (+ charging), SOC
        self.regs[0x1610], self.regs[0x1611], self.regs[0x1612], self.regs[0x1613], self.regs[0x1614] = 0, 0, 0, 0, 0
        self.refuse = set()                                 # offsets the device answers with a Modbus exception
        self.put(0x3, list(struct.unpack(">8H", b"ES21ZABC12345678"[:16])))
        self.put(0x227, [0, 5000]); self.put(0x221, [0, 2300]); self.put(0x223, [0, 3000]); self.regs[0x2200] = 10
        self.put(0xB, [0x0101, 0x071C])

    def put(self, off, words):
        for i, w in enumerate(words):
            self.regs[off + i] = w

    async def read_holding(self, addr, count):
        if self.dead:
            raise ModbusError("unreachable")
        off = addr - OFF
        if any(off + i not in self.regs for i in range(count)):
            raise ModbusError("device returned exception 2", code=2)
        return [self.regs[off + i] for i in range(count)]

    async def write_register(self, addr, value):
        if self.dead:
            raise ModbusError("unreachable")
        if addr - OFF in self.refuse:
            raise ModbusError("device returned exception 3", code=3)
        self.writes.append((addr - OFF, value & 0xFFFF))
        self.regs[addr - OFF] = value & 0xFFFF

    async def close(self):
        pass


def driver(**conn):
    caps = Capabilities(5000, 3000, 2300, 10)
    cfg = DeviceConfig("s", "S", "ecoflow_stream_5000_local", caps, extra={"connection": {"host": "x", **conn}})
    fake = FakeStream()
    return EcoFlowModbusDriver(cfg, client=fake), fake


def run(c):
    return asyncio.run(c)


def test_read_decodes_the_published_map():
    d, f = driver()
    a = run(d.read())
    assert a.online and a.soc == 62 and a.power_w == 1500 and a.grid_w == 300 and a.load_w == 500 and a.pv_w == 0
    assert a.mode is Mode.AUTO
    assert (0x25F, 1) in f.writes                       # the keep-alive is written on the first read


def test_keepalive_is_not_written_every_cycle():
    d, f = driver()
    run(d.read()); run(d.read()); run(d.read())
    assert [w for w in f.writes if w[0] == 0x25F] == [(0x25F, 1)]


def test_offline_when_unreachable():
    d, f = driver()
    f.dead = True
    assert run(d.read()).online is False and d.last_error


def test_charge_sets_target_before_taking_control():
    d, f = driver()
    run(d.apply(Desired(Mode.CHARGE, 2000)))
    order = [w for w in f.writes if w[0] in (0x1610, 0x1611)]
    assert order == [(0x1611, 2000), (0x1610, 1)]
    a = run(d.read())
    assert a.mode is Mode.CHARGE and a.set_power_w == 2000


def test_discharge_is_negative_and_read_back():
    d, f = driver()
    run(d.apply(Desired(Mode.DISCHARGE, 1200)))
    assert f.regs[0x1611] == (-1200) & 0xFFFF
    a = run(d.read())
    assert a.mode is Mode.DISCHARGE and a.set_power_w == 1200


def test_power_is_clamped_to_the_battery_limits():
    d, f = driver()
    run(d.apply(Desired(Mode.CHARGE, 99999)))
    assert f.regs[0x1611] == 3000
    run(d.apply(Desired(Mode.DISCHARGE, 99999)))
    assert f.regs[0x1611] == (-2300) & 0xFFFF
    run(d.apply(Desired(Mode.CHARGE, -50)))
    assert f.regs[0x1611] == 0


def test_hold_is_zero_target():
    d, f = driver()
    run(d.apply(Desired(Mode.HOLD)))
    assert f.regs[0x1610] == 1 and f.regs[0x1611] == 0
    assert run(d.read()).mode is Mode.HOLD


def test_auto_hands_back_to_the_device():
    d, f = driver()
    run(d.apply(Desired(Mode.CHARGE, 1000)))
    run(d.apply(Desired(Mode.AUTO)))
    assert f.regs[0x1610] == 0 and f.regs[0x1611] == 0
    assert run(d.read()).mode is Mode.AUTO


def test_auto_when_not_controlling_writes_nothing_but_the_keepalive():
    d, f = driver()
    run(d.apply(Desired(Mode.AUTO)))
    assert [w for w in f.writes if w[0] != 0x25F] == []


def test_reverse_sign_flips_target_and_reading():
    d, f = driver(reverse_sign=True)
    run(d.apply(Desired(Mode.CHARGE, 1000)))
    assert f.regs[0x1611] == (-1000) & 0xFFFF
    a = run(d.read())
    assert a.mode is Mode.CHARGE and a.power_w == -1500       # the battery's own reading is flipped the same way


def test_probe_reports_the_unit():
    d, f = driver()
    info = run(d.probe())
    assert info["serial"] == "ES21ZABC12345678" and info["model"] == "STREAM 5000"
    assert info["capacity_wh"] == 5000 and info["max_charge_w"] == 3000 and info["max_discharge_w"] == 2300 and info["min_soc"] == 10
    assert info["firmware"] == "1.1.7.28" and info["control"] is True


def test_probe_explains_a_switched_off_modbus():
    d, f = driver()
    f.dead = True
    with pytest.raises(ModbusError, match="Third-party control"):
        run(d.probe())


def test_nan_and_junk_readings_become_unknown():
    d, f = driver()
    f.put(0x20C, [0x7FC0, 0])                              # NaN
    f.regs[0x20E] = 0xFFFF
    a = run(d.read())
    assert a.power_w is None and a.soc is None and a.online


def test_missing_control_block_still_reads():
    d, f = driver()
    del f.regs[0x1610]
    a = run(d.read())
    assert a.online and a.mode is Mode.AUTO


def test_profile_builds_a_controllable_device():
    from hp.core.serde import device_from_dict
    cfg = device_from_dict({"device_id": "es", "name": "ES5000", "kind": "ecoflow_stream_5000_local", "capacity_wh": 5000,
                            "max_charge_w": 3000, "max_discharge_w": 2300, "min_soc": 10, "control_enabled": True,
                            "connection": {"host": "10.0.0.213", "reverse_sign": True}})
    assert cfg.extra["driver"] == "ecoflow_modbus" and cfg.caps.power_control is True
    assert cfg.extra["connection"] == {"host": "10.0.0.213", "port": 502, "unit_id": 1, "reverse_sign": True}
    assert Mode.DISCHARGE in cfg.caps.supported_modes


def test_refused_keepalive_does_not_make_a_readable_battery_offline():
    d, f = driver()
    f.refuse.add(0x25F)                                    # e.g. read-only access in the EcoFlow app
    a = run(d.read())
    assert a.online and a.soc == 62


def test_refused_keepalive_stops_a_command_with_a_clear_reason():
    d, f = driver()
    f.refuse.add(0x25F)
    with pytest.raises(ModbusError, match="read/write"):
        run(d.apply(Desired(Mode.CHARGE, 1000)))
    assert [w for w in f.writes if w[0] in (0x1610, 0x1611)] == []


def test_another_tools_control_point_is_handed_back_on_auto():
    d, f = driver()
    f.regs[0x1610], f.regs[0x1612] = 2, 50                 # someone had the meter point set to 50 W
    run(d.apply(Desired(Mode.CHARGE, 1500)))
    assert f.regs[0x1610] == 1
    run(d.apply(Desired(Mode.AUTO)))
    assert f.regs[0x1610] == 2 and f.regs[0x1612] == 50


def test_auto_never_touches_a_point_it_did_not_set():
    d, f = driver()
    f.regs[0x1610] = 3
    run(d.apply(Desired(Mode.AUTO)))
    assert f.regs[0x1610] == 3 and f.writes == []           # not ours: never touched, not even a keep-alive


def test_limits_are_required_when_the_battery_did_not_report_them():
    from hp.core.serde import ValidationError, device_from_dict
    base = {"device_id": "es", "name": "ES", "kind": "ecoflow_stream_5000_local", "capacity_wh": 5000, "connection": {"host": "10.0.0.213"}}
    with pytest.raises(ValidationError, match="max charge and max discharge"):
        device_from_dict(base)
    with pytest.raises(ValidationError):
        device_from_dict({**base, "max_charge_w": 3000})


def test_link_dropping_between_the_two_reads_is_offline_not_auto():
    d, f = driver()
    real = f.read_holding
    calls = []

    async def flaky(addr, count):
        calls.append(addr)
        if len(calls) == 2:                                 # the second read of the cycle (the control block) loses the link
            raise ModbusError("unreachable")
        return await real(addr, count)
    f.read_holding = flaky
    import time
    d._beat = time.monotonic()                              # keep-alive just sent: none in this cycle
    a = run(d.read())
    assert a.online is False
