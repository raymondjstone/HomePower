"""Drivers against a simulated Anker Modbus TCP device (real sockets) and a fake EcoFlow API."""
import asyncio
import struct
from contextlib import asynccontextmanager

import pytest

from core.models import Capabilities, Desired, DeviceConfig, Mode
from hp.drivers import discovery
from hp.drivers.anker import AnkerModbusDriver
from hp.drivers.ecoflow import EcoFlowStreamDriver, flatten, sign
from hp.drivers.modbus import ModbusError, from_i32, to_i32


class FakeAnker:
    """Tiny Modbus TCP server: FC03/04/06/16 over plain dict register spaces."""

    def __init__(self):
        self.inp = {a: 0 for a in range(10000, 10260)}
        self.hold = {a: 0 for a in range(10060, 10085)}
        self.writes = []
        self.inp[10014] = 62                                  # SOC
        self.inp[10008], self.inp[10009] = from_i32(-1500)    # battery: charging 1500 W
        self.inp[10012], self.inp[10013] = from_i32(300)      # grid import 300 W
        self.inp[10036], self.inp[10037] = from_i32(3600)
        self.inp[10038], self.inp[10039] = from_i32(3600)
        self.inp[10250] = 50                                  # 5.0 kWh
        self.model = struct.unpack(">8H", b"A17C0 E5\x00\x00\x00\x00\x00\x00\x00\x00")

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
async def fake_anker():
    dev = FakeAnker()
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
    return to_i32(dev.hold[10071], dev.hold[10072])


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
            assert info["capacity_wh"] == 5000 and info["max_charge_w"] == 3600 and info["soc"] == 62
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
