"""Drivers against a simulated Anker Modbus TCP device (real sockets) and a fake EcoFlow API."""
import asyncio
import struct
from contextlib import asynccontextmanager

import pytest

from core.models import Capabilities, Desired, DeviceConfig, Mode
from hp.drivers import discovery
from hp.drivers.anker import AnkerModbusDriver
from hp.drivers.ecoflow import EcoFlowStreamDriver, flatten, sign
from hp.drivers.modbus import ModbusError, detect_word_order, from_i32, to_i32


class FakeAnker:
    """Tiny Modbus TCP server: FC03/04/06/16 over plain dict register spaces."""

    def __init__(self, order="big"):
        self.order = order                                    # real units send the HIGH word first
        self.inp = {a: 0 for a in range(10000, 10260)}
        self.hold = {a: 0 for a in range(10060, 10085)}
        self.hold[60001] = 10                                 # the battery's own discharge floor
        self.writes = []
        self.inp[10014] = 62                                  # SOC
        self.put(10008, -1500)                                # battery: charging 1500 W
        self.put(10012, 300)                                  # grid import 300 W
        self.put(10036, 3600)
        self.put(10038, 3600)
        self.put(10250, 100)                                  # rated energy 10.0 kWh (0.1 kWh units)
        self.model = struct.unpack(">8H", b"A17C0 E5\x00\x00\x00\x00\x00\x00\x00\x00")

    def put(self, addr, value):
        self.inp[addr], self.inp[addr + 1] = from_i32(value, self.order)

    async def handle(self, r, w):
        try:
            while True:
                tid, _p, ln, unit = struct.unpack(">HHHB", await r.readexactly(7))
                pdu = await r.readexactly(ln - 1)
                fc = pdu[0]
                if fc in (3, 4):
                    addr, n = struct.unpack(">HH", pdu[1:5])
                    space = self.hold if fc == 3 else self.inp
                    if fc == 4 and addr == 32768:
                        vals = list(self.model) + [0] * (n - 8)
                    elif any(a not in space for a in range(addr, addr + n)):
                        resp = bytes([fc | 0x80, 2])
                        w.write(struct.pack(">HHHB", tid, 0, 1 + len(resp), unit) + resp)
                        continue
                    else:
                        vals = [space[a] for a in range(addr, addr + n)]
                    resp = bytes([fc, 2 * n]) + struct.pack(f">{n}H", *vals)
                elif fc == 6:
                    addr, val = struct.unpack(">HH", pdu[1:5])
                    self.hold[addr] = val
                    self.writes.append((addr, [val]))
                    resp = pdu
                elif fc == 16:
                    addr, n, _bc = struct.unpack(">HHB", pdu[1:6])
                    vals = list(struct.unpack(f">{n}H", pdu[6:6 + 2 * n]))
                    for i, v in enumerate(vals):
                        self.hold[addr + i] = v
                    self.writes.append((addr, vals))
                    resp = pdu[:5]
                else:
                    resp = bytes([fc | 0x80, 1])
                w.write(struct.pack(">HHHB", tid, 0, 1 + len(resp), unit) + resp)
                await w.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass


@asynccontextmanager
async def fake_anker(order="big"):
    dev = FakeAnker(order)
    srv = await asyncio.start_server(dev.handle, "127.0.0.1", 0)
    dev.port = srv.sockets[0].getsockname()[1]
    try:
        yield dev
    finally:
        srv.close()
        await srv.wait_closed()


def cfg(port, **extra):
    caps = Capabilities(5000, 3600, 3600, 10)
    return DeviceConfig("a", "A", "anker_e5000_pro", caps,
                        extra={"connection": {"host": "127.0.0.1", "port": port, "unit_id": 1}, **extra})


def sp(dev):
    return to_i32(dev.hold[10071], dev.hold[10072], dev.order)


def test_read_decodes_and_normalises_sign():
    async def go():
        async with fake_anker() as dev:
            d = AnkerModbusDriver(cfg(dev.port))
            a = await d.read()
            assert a.online and a.soc == 62 and a.power_w == 1500      # register -1500 => +1500 charging
            assert a.mode is Mode.AUTO and a.grid_w == 300
            await d.close()
    asyncio.run(go())


def test_probe_reports_capabilities():
    async def go():
        async with fake_anker() as dev:
            d = AnkerModbusDriver(cfg(dev.port))
            info = await d.probe()
            # everything setup needs comes from the battery: nothing for the user to type
            assert info["capacity_wh"] == 10000 and info["max_charge_w"] == 3600 and info["max_discharge_w"] == 3600
            assert info["min_soc"] == 10 and info["soc"] == 62 and info["word_order"] == "big"
            assert info["model"] == "A17C0 E5"
            await d.close()
    asyncio.run(go())


def test_charge_takes_control_then_release_restores_mode():
    async def go():
        async with fake_anker() as dev:
            dev.hold[10064] = 20                                       # app-managed
            d = AnkerModbusDriver(cfg(dev.port, restore_mode=0, watchdog=True))
            await d.apply(Desired(Mode.CHARGE, 2000))
            assert dev.hold[10064] == 3 and sp(dev) == -2000 and dev.hold[10080] == 600
            a = await d.read()
            assert a.mode is Mode.CHARGE and a.set_power_w == 2000
            await d.apply(Desired(Mode.DISCHARGE, 900))
            assert sp(dev) == 900
            await d.apply(Desired(Mode.HOLD))
            assert sp(dev) == 0 and dev.hold[10064] == 3
            await d.apply(Desired(Mode.AUTO))
            assert sp(dev) == 0 and dev.hold[10064] == 20              # handed back to what it was
            assert (await d.read()).mode is Mode.AUTO
            await d.close()
    asyncio.run(go())


def test_auto_when_not_ours_writes_nothing():
    async def go():
        async with fake_anker() as dev:
            d = AnkerModbusDriver(cfg(dev.port))
            await d.apply(Desired(Mode.AUTO))
            assert dev.writes == []
            await d.close()
    asyncio.run(go())


def test_offline_device_is_reported_not_raised():
    c = cfg(1)
    assert asyncio.run(AnkerModbusDriver(c).read()).online is False


def test_scan_finds_the_battery_and_ignores_others():
    async def go():
        async with fake_anker() as dev:
            found = await discovery.scan(["127.0.0.1"], port=dev.port)
            assert found and found[0]["soc"] == 62 and found[0]["port"] == dev.port
        assert await discovery.scan(["127.0.0.1"], port=1) == []
    asyncio.run(go())


def test_subnet_hosts():
    h = discovery.subnet_hosts("192.168.1.20", 16)
    assert len(h) == 253 and "192.168.1.20" not in h and h[0] == "192.168.1.1"


# ------------------------------------------------------------------ EcoFlow
def test_flatten_and_sign_are_deterministic():
    assert flatten({"sn": "S", "params": {"cfgBackupReverseSoc": 95, "x": True}, "l": [1, 2]}) == {
        "sn": "S", "params.cfgBackupReverseSoc": "95", "params.x": "true", "l[0]": "1", "l[1]": "2"}
    a = sign({"sn": "S"}, "AK", "SK", "123456", "1700000000000")
    assert a == sign({"sn": "S"}, "AK", "SK", "123456", "1700000000000") and len(a) == 64
    assert a != sign({"sn": "T"}, "AK", "SK", "123456", "1700000000000")


class FakeApi:
    def __init__(self, quota):
        self._q, self.sent = quota, []

    async def quota(self, sn):
        return self._q

    async def command(self, sn, params):
        self.sent.append(params)


def eco(quota):
    c = DeviceConfig("e", "E", "ecoflow_stream_ultra_x", Capabilities(5000, 2300, 2300, 10),
                     extra={"connection": {"sn": "BK61X", "access_key": "a", "secret_key": "b", "region": "eu"}})
    d = EcoFlowStreamDriver(c, session=None)
    d.api = FakeApi(quota)
    return d


def test_ecoflow_read_and_commands():
    async def go():
        d = eco({"cmsBattSoc": 0.0, "soc": 71, "powGetBpCms": -450.0, "backupReverseSoc": 10, "powGetSysGrid": 120})
        a = await d.read()
        assert a.soc == 71 and a.power_w == -450 and a.mode is Mode.AUTO and a.grid_w == 120
        await d.apply(Desired(Mode.HOLD))
        assert d.api.sent == [{"cfgBackupReverseSoc": 95}]
        d.api.sent.clear()
        await d.apply(Desired(Mode.AUTO))
        assert d.api.sent == [{"cfgEnergyStrategyOperateMode": {"operateSelfPoweredOpen": True}},
                              {"cfgBackupReverseSoc": 10}]
        with pytest.raises(ValueError):
            await d.apply(Desired(Mode.CHARGE, 1000))
        d.api._q = {"backupReverseSoc": 95}
        assert (await d.read()).mode is Mode.HOLD
    asyncio.run(go())


def test_real_hardware_values_decode_correctly():
    """Regression: a real battery sent battery power as regs [0xFFFF, 0xFFBA] and grid [0xFFFF, 0xFFBB].
    Decoding them low-word-first gave -4,587,520 W; the right answer is -70 W / -69 W."""
    async def go():
        async with fake_anker() as dev:
            dev.inp[10008], dev.inp[10009] = 0xFFFF, 0xFFBA
            dev.inp[10012], dev.inp[10013] = 0xFFFF, 0xFFBB
            d = AnkerModbusDriver(cfg(dev.port))
            a = await d.read()
            assert a.power_w == 70.0          # register -70 (negative = charging) => +70 W charging
            assert a.grid_w == -69.0          # exporting 69 W
            await d.close()
    asyncio.run(go())


def test_little_word_order_devices_work_too():
    async def go():
        async with fake_anker("little") as dev:
            d = AnkerModbusDriver(cfg(dev.port))
            a = await d.read()
            assert a.power_w == 1500 and a.grid_w == 300
            await d.apply(Desired(Mode.CHARGE, 2000))
            assert sp(dev) == -2000            # the setpoint is written in the order the device uses
            info = await d.probe()
            assert info["capacity_wh"] == 10000 and info["word_order"] == "little"
            await d.close()
    asyncio.run(go())


def test_detect_word_order():
    assert detect_word_order([(0xFFFF, 0xFFBA)]) == "big"
    assert detect_word_order([(0xFFBA, 0xFFFF)]) == "little"
    assert detect_word_order([(0, 0), (0, 0)]) is None
    assert detect_word_order([(0, 1500), (0xFFFF, 0xFFBA)]) == "big"


# ------------------------------------------------------------ generic Modbus register map
from hp.drivers.modbus_generic import GenericModbusDriver
from core.modbus_map import MapError, has_control, parse_map
from core.serde import ValidationError, device_from_dict

ANKER_AS_MAP = {
    "soc": {"addr": 10014, "type": "u16", "fc": 4},
    "power": {"addr": 10008, "type": "i32", "fc": 4, "positive_is": "discharging"},
    "grid": {"addr": 10012, "type": "i32", "fc": 4, "positive_is": "import"},
    "setpoint": {"addr": 10071, "type": "i32", "fc": 3, "positive_is": "discharge"},
    "mode": {"addr": 10064, "type": "u16", "fc": 3, "control_value": 3, "auto_value": 0},
}


def gcfg(port, mp):
    caps = Capabilities(10000, 3600, 3600, 10)
    return DeviceConfig("g", "G", "modbus_map", caps, extra={"connection": {"host": "127.0.0.1", "port": port, "unit_id": 1, "map": mp}})


def test_map_validation():
    assert has_control(parse_map(ANKER_AS_MAP)) and not has_control(parse_map({k: ANKER_AS_MAP[k] for k in ("soc", "power")}))
    for bad in (None, {}, {"soc": {"addr": 1}}, {**ANKER_AS_MAP, "mode": None},
                {"soc": {"addr": 1, "type": "f64"}, "power": {"addr": 2}}):
        with pytest.raises(MapError):
            parse_map(bad)
    with pytest.raises(MapError):
        parse_map("{not json")
    assert parse_map(json_dump := __import__("json").dumps(ANKER_AS_MAP))["soc"]["addrs"] == [10014]


def test_generic_driver_reads_and_controls_through_the_map():
    async def go():
        async with fake_anker() as dev:
            d = GenericModbusDriver(gcfg(dev.port, ANKER_AS_MAP))
            a = await d.read()
            assert a.online and a.soc == 62 and a.power_w == 1500 and a.grid_w == 300 and a.mode is Mode.AUTO
            await d.apply(Desired(Mode.CHARGE, 2000))
            assert dev.hold[10064] == 3 and sp(dev) == -2000
            a = await d.read()
            assert a.mode is Mode.CHARGE and a.set_power_w == 2000
            await d.apply(Desired(Mode.AUTO))
            assert sp(dev) == 0 and dev.hold[10064] == 0
            await d.close()
    asyncio.run(go())


def test_generic_read_only_map_never_writes():
    async def go():
        async with fake_anker() as dev:
            d = GenericModbusDriver(gcfg(dev.port, {k: ANKER_AS_MAP[k] for k in ("soc", "power")}))
            assert (await d.read()).soc == 62
            await d.apply(Desired(Mode.AUTO))
            with pytest.raises(ValueError):
                await d.apply(Desired(Mode.CHARGE, 1000))
            assert dev.writes == []
            await d.close()
    asyncio.run(go())


def test_serde_for_generic_and_victron_presets():
    base = {"device_id": "v", "name": "V", "capacity_wh": 10000, "max_charge_w": 0, "max_discharge_w": 0}
    v = device_from_dict({**base, "kind": "victron_gx", "connection": {"host": "10.0.0.9"}})
    assert v.extra["connection"]["unit_id"] == 100 and v.caps.supported_modes == {Mode.AUTO}   # read-only
    sized = {**base, "max_charge_w": 3000, "max_discharge_w": 3000}
    with pytest.raises(ValidationError):   # a controllable map with no power limits would silently never charge
        device_from_dict({**base, "kind": "modbus_map", "connection": {"host": "10.0.0.9", "map": ANKER_AS_MAP}})
    g = device_from_dict({**sized, "kind": "modbus_map", "connection": {"host": "10.0.0.9", "map": ANKER_AS_MAP}})
    assert Mode.CHARGE in g.caps.supported_modes
    g2 = device_from_dict({**sized, "kind": "modbus_map", "connection": {"host": "10.0.0.9", "map": __import__("json").dumps(ANKER_AS_MAP)}})
    assert g2.extra["connection"]["map"] == ANKER_AS_MAP                                         # JSON text is normalised
    with pytest.raises(ValidationError):
        device_from_dict({**base, "kind": "modbus_map", "connection": {"host": "10.0.0.9", "map": "{oops"}})


def test_anker_keepalive_rewrites_the_setpoint_unchanged():
    async def go():
        async with fake_anker() as dev:
            d = AnkerModbusDriver(cfg(dev.port))
            await d.keepalive()
            assert dev.writes == []                       # not under our control: nothing written
            await d.apply(Desired(Mode.CHARGE, 1200))
            dev.writes.clear()
            await d.keepalive()
            assert dev.writes == [(10071, [dev.hold[10071], dev.hold[10072]])] and sp(dev) == -1200
            await d.close()
    asyncio.run(go())


def test_anker_driver_clamps_to_the_batterys_limits():
    async def go():
        async with fake_anker() as dev:
            d = AnkerModbusDriver(cfg(dev.port))          # limits 3600 W
            await d.apply(Desired(Mode.CHARGE, 99999))
            assert sp(dev) == -3600
            await d.apply(Desired(Mode.DISCHARGE, 500000))
            assert sp(dev) == 3600
            await d.apply(Desired(Mode.CHARGE, -50))
            assert sp(dev) == 0
            await d.close()
    asyncio.run(go())


def test_anker_driver_refuses_to_guess_the_word_order():
    async def go():
        async with fake_anker() as dev:
            for a in list(dev.inp):                        # an idle, all-zero battery: nothing tells the order
                dev.inp[a] = 0
            dev.inp[10014] = 50
            d = AnkerModbusDriver(cfg(dev.port))
            with pytest.raises(ModbusError):
                await d.apply(Desired(Mode.CHARGE, 1000))
            assert dev.writes == []                        # nothing at all was written
            await d.apply(Desired(Mode.AUTO))              # handing back needs no order (zeros)
            dev.put(10008, -700)                           # the battery starts moving: now it is learnable
            await d.apply(Desired(Mode.CHARGE, 1000))
            assert sp(dev) == -1000
            await d.close()
    asyncio.run(go())


def test_generic_driver_clamps_to_limits():
    async def go():
        async with fake_anker() as dev:
            d = GenericModbusDriver(gcfg(dev.port, ANKER_AS_MAP))   # limits 3600 W
            await d.apply(Desired(Mode.CHARGE, 99999))
            assert sp(dev) == -3600
            await d.close()
    asyncio.run(go())
