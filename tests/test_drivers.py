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
        self.conns = getattr(self, "conns", set()) | {w}
        try:
            while True:
                tid, _p, ln, unit = struct.unpack(">HHHB", await r.readexactly(7))
                pdu = await r.readexactly(ln - 1)
                fc = pdu[0]
                if fc in (3, 4):
                    addr, n = struct.unpack(">HH", pdu[1:5])
                    space = self.hold if fc == 3 else self.inp
                    if fc == 4 and addr == 32768:
                        vals = (list(self.model) + [0] * max(0, n - 8))[:n]       # exactly n registers, whatever n is
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
        for w in getattr(dev, "conns", ()):      # a test that failed before closing its driver must not leave the server waiting for ever
            w.close()
        await asyncio.wait_for(srv.wait_closed(), 5)


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


def test_x1_reports_grid_loss_and_other_models_do_not():
    async def go():
        async with fake_anker() as dev:
            x1 = AnkerModbusDriver(cfg(dev.port, house_meter=True))
            dev.inp[10000] = 1
            a = await x1.read()
            assert a.off_grid is False and not a.protected
            dev.inp[10000] = 2                                          # plant status 2 = off-grid
            a = await x1.read()
            assert a.off_grid is True and a.protected                    # engine leaves it alone
            dev.inp[10000] = 4                                          # fault etc.: not a grid statement
            assert (await x1.read()).off_grid is None
            dev.inp[10000] = 2
            other = AnkerModbusDriver(cfg(dev.port))                    # models without the flag never claim it
            assert (await other.read()).off_grid is None
            await x1.close(); await other.close()
    asyncio.run(go())


def test_register_scan_reads_without_writing_and_skips_unanswered_ranges():
    async def go():
        async with fake_anker() as dev:
            d = AnkerModbusDriver(cfg(dev.port))
            dev.inp[10014] = 62
            dev.hold[10064] = 3
            try:
                out = await d.scan(ranges=((10000, 10049), (10060, 10084), (59000, 59010)))   # the fake refuses the gaps
                assert out["input"]["10014"] == 62 and out["holding"]["10064"] == 3
                assert "10049" not in out["holding"] and not any(k.startswith("590") for k in {**out["input"], **out["holding"]})
                assert dev.writes == []                                      # never writes
            finally:
                await d.close()
    asyncio.run(go())


def test_x1_grid_limits_homepower_set_follow_the_setting_up_and_off_but_not_someone_elses():
    async def go():
        async with fake_anker() as dev:
            for a in range(10060, 10085):
                dev.hold.setdefault(a, 0)
            d = AnkerModbusDriver(cfg(dev.port, house_meter=True))
            d._order = "big"
            exp = lambda: (dev.hold[10074], dev.hold[10075] << 16 | dev.hold[10076])
            try:
                mine: dict = {}
                assert await d.set_grid_limits(None, 3000, mine) == ["export limit 3000 W"] and mine == {"export": 3000}
                assert await d.set_grid_limits(None, 5000, mine) == ["export limit 5000 W"] and exp() == (2, 5000)   # ours: raised
                assert await d.set_grid_limits(None, None, mine) == ["export limit off"] and exp()[0] == 0 and mine == {}
                assert await d.set_grid_limits(None, 3000, mine) == ["export limit 3000 W"]
                dev.hold[10076] = 2500                               # changed in the Anker app since: no longer ours
                assert await d.set_grid_limits(None, 5000, mine) == [] and exp() == (2, 2500) and mine == {}
                assert await d.set_grid_limits(None, None, mine) == [] and exp() == (2, 2500)                         # and never switched off
            finally:
                await d.close()
    asyncio.run(go())


def test_x1_grid_limits_only_ever_tighten_and_skip_what_is_already_right():
    async def go():
        async with fake_anker() as dev:
            for a in range(10060, 10085):
                dev.hold.setdefault(a, 0)
            d = AnkerModbusDriver(cfg(dev.port, house_meter=True))
            d._order = "big"
            try:
                assert await d.set_grid_limits(6000, 3680) == ["export limit 3680 W", "import limit 6000 W"]
                assert dev.hold[10074] == 2 and (dev.hold[10075] << 16 | dev.hold[10076]) == 3680
                assert dev.hold[10077] == 2 and (dev.hold[10078] << 16 | dev.hold[10079]) == 6000
                n = len(dev.writes)
                assert await d.set_grid_limits(6000, 3680) == [] and len(dev.writes) == n        # already right: no writes
                dev.hold[10076] = 2000                                                          # installer set a stricter export limit
                assert await d.set_grid_limits(6000, 3680) == []                                # never loosened
                assert (dev.hold[10075] << 16 | dev.hold[10076]) == 2000
                assert await d.set_grid_limits(None, None) == []                                # no limit set: never switches one off
            finally:
                await d.close()
    asyncio.run(go())


def test_x1_pv_strings_voltage_current_and_total_power():
    async def go():
        async with fake_anker() as dev:
            dev.inp[10167], dev.inp[10168] = 3652, 512            # PV1: 365.2 V, 5.12 A
            dev.inp[10169], dev.inp[10170] = 3010, 250            # PV2: 301.0 V, 2.50 A
            dev.inp[10183], dev.inp[10184] = 0, 2623              # total 2623 W (word order is learned from the real registers)
            d = AnkerModbusDriver(cfg(dev.port, house_meter=True))
            try:
                a = await d.read()
                assert a.pv_strings[0] == (365.2, 5.12) and a.pv_strings[1] == (301.0, 2.5) and a.pv_strings[2] == (0.0, 0.0)
                assert len(a.pv_strings) == 8 and a.pv_total_w == 2623.0
            finally:
                await d.close()
    asyncio.run(go())


def test_x1_extras_from_anker_register_document():
    from core.anker_x1_map import alarm_texts, describe_all
    async def go():
        async with fake_anker() as dev:
            for i, w in enumerate((11825, 11824, 14641, 12590, 54, 0)):      # a real X1's software version, characters low byte first
                dev.inp[10112 + i] = w
            for i, w in enumerate((12632, 18477, 19254, 21293, 0, 0, 0, 0, 0, 0)):   # its model name, "X1-H6K-S"
                dev.inp[10090 + i] = w
            dev.inp[10156] = 312                                            # internal temperature, tenths of a degree
            dev.inp[10042] = (1 << 4) | (1 << 5)                           # BMS and meter 1 communication failure (app: A5, A6)
            dev.inp[10043] = 1 << 5                                        # System Alarm 2: wrong meter address
            d = AnkerModbusDriver(cfg(dev.port, house_meter=True))
            try:
                a = await d.read()
                assert a.firmware == "1.0.19.16" and a.temp_c == 31.2
                assert a.alarms == ("battery (BMS) communication failure", "meter 1 communication failure",
                                    "incorrect meter communication address")
                info = await d.probe()
                assert info["model"] == "X1-H6K-S" and info["firmware"] == "1.0.19.16"
                dev.inp[10156] = 0xFFF6                                      # -1.0 degrees: signed
                d._slow_at = 0
                assert (await d.read()).temp_c == -1.0
            finally:
                await d.close()
    asyncio.run(go())
    assert alarm_texts(0) == [] and alarm_texts(1) == ["EEPROM read/write error"]
    assert alarm_texts(1 << 5) == ["meter 1 communication failure"]                 # the app's "A6" (it counts bits from 1)
    assert alarm_texts(1 << 3) == ["inverter (DC/AC) communication failure"] and alarm_texts(1 << 7) == []
    assert alarm_texts([0, 0, 0, 0, 1 << 1, 0, 1 << 4, 0]) == ["grid L2 CT connection fault", "Modbus communication timeout"]
    x1, sb = describe_all(True), describe_all(False)
    assert x1["10064"][2] == "setting" and x1["10156"][2] == "live" and x1["10075"][0].startswith("Export")
    assert sb["60000"][0].startswith("Charge limit") and "60000" not in x1


def test_solarbank_reports_its_own_limits_and_x1_extras_are_not_used_on_it():
    async def go():
        async with fake_anker() as dev:
            dev.hold.update({60000: 90, 60001: 10, 60002: 20, 60003: 1})
            d = AnkerModbusDriver(cfg(dev.port))                              # not an X1
            try:
                a = await d.read()
                assert a.limits == (90, 10, 20, True) and a.temp_c is None and a.alarms == () and a.off_grid is None
            finally:
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
            d = AnkerModbusDriver(cfg(dev.port, watchdog=True))
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


def test_anker_hand_back_with_an_unknown_original_mode_leaves_the_battery_alone():
    async def go():
        async with fake_anker() as dev:
            dev.hold[10064] = 3                                        # third-party control, but nothing saved about the mode before
            d = AnkerModbusDriver(cfg(dev.port, house_meter=True))
            await d.apply(Desired(Mode.AUTO))
            assert dev.writes == [] and dev.hold[10064] == 3           # no made-up mode (the X1 used to get 20)
            await d.close()
    asyncio.run(go())


def test_anker_hand_back_after_a_restart_restores_the_saved_mode():
    async def go():
        async with fake_anker() as dev:
            saved = []
            dev.hold[10064] = 1                                        # the user's time-of-use mode
            d = AnkerModbusDriver(cfg(dev.port), on_orig=saved.append)
            await d.apply(Desired(Mode.HOLD))
            assert saved[-1] == {"restore": 1} and dev.hold[10064] == 3
            await d.close()
            again = AnkerModbusDriver(cfg(dev.port), orig=saved[-1], on_orig=saved.append)   # Home Assistant restarted
            await again.apply(Desired(Mode.AUTO))
            assert dev.hold[10064] == 1 and saved[-1] is None
            await again.close()
    asyncio.run(go())


def test_offline_device_is_reported_not_raised():
    c = cfg(1)
    assert asyncio.run(AnkerModbusDriver(c).read()).online is False


def test_scan_reads_an_x1s_model_and_serial_the_x1_way():
    async def go():
        async with fake_anker() as dev:
            for i, w in enumerate((12632, 18477, 19254, 21293, 0, 0, 0, 0, 0, 0)):   # "X1-H6K-S", characters low byte first
                dev.inp[10090 + i] = w
            for i, w in enumerate(struct.unpack("<6H", b"APC123456789")):          # its serial, stored the same way
                dev.inp[10100 + i] = w
            found = await discovery.scan(["127.0.0.1"], port=dev.port)
            assert found[0]["model"] == "X1-H6K-S" and found[0]["serial"] == "APC123456789", found[0]   # not "PA1C..."
    asyncio.run(go())


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

    async def command(self, sn, params, cmd_id=17):
        self.sent.append(params)
        self.ids = getattr(self, "ids", []) + [cmd_id]


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
        assert d.api.sent == [{"cfgBackupReverseSoc": 71}]        # held at its current level: no grid top-up
        d.api.sent.clear()
        await d.apply(Desired(Mode.AUTO))
        assert d.api.sent == [{"cfgBackupReverseSoc": 10}]        # only the reserve: the energy strategy is never touched
        with pytest.raises(ValueError):
            await d.apply(Desired(Mode.DISCHARGE, 1000))     # no discharge setpoint exists
        d.api._q = {"backupReverseSoc": 95}
        assert (await d.read()).mode is Mode.HOLD
        d._cache = None
        d.api._q = {"soc": 71, "backupReverseSoc": 71}
        assert (await d.read()).mode is Mode.HOLD            # reserve at the charge level = holding
        d._cache = None
        d.api._q = {"soc": 10.5, "backupReverseSoc": 10}
        assert (await d.read()).mode is Mode.AUTO            # just an empty battery resting on its normal floor
        e = eco({"backupReverseSoc": 10})
        await e.read()
        with pytest.raises(ValueError):
            await e.apply(Desired(Mode.HOLD))                # no charge level known: refuse rather than guess
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


def test_ecoflow_prefixed_and_string_readings_still_decode():
    from hp.drivers.ecoflow import EcoFlowStreamDriver, normalise
    q = normalise({"20_1.cmsBattSoc": "0", "20_1.f32ShowSoc": "64.5", "20_1.powGetBpCms": "-1200", "20_1.backupReverseSoc": 10})
    a = EcoFlowStreamDriver._decode(q)
    assert a.soc == 64.5 and a.power_w == -1200
    odd = normalise({"data": {"bpSoc": 70, "bpPowerWatts": 300, "socMax": 100}})
    b = EcoFlowStreamDriver._decode(odd)
    assert b.soc == 70 and b.power_w == 300


def test_ecoflow_ac5000_style_readings_decode_and_stay_read_only():
    import asyncio
    from hp.core.models import Desired, Mode
    from hp.drivers.ecoflow import EcoFlowStreamDriver, normalise
    q = normalise({"sysEnergyStreamReport-bpSoc": 54, "sysEnergyStreamReport-bpPwr": -789, "sysEnergyStreamReport-gridPwr": 11,
                   "sysEnergyStreamReport-loadPwr": 800, "sysEnergyStreamReport-pvPwr": 0, "workMode": "TIMER_TASK"})
    a = EcoFlowStreamDriver._decode(q)
    assert (a.soc, a.power_w, a.grid_w, a.load_w, a.pv_w) == (54, -789, 11, 800, 0)
    d = EcoFlowStreamDriver.__new__(EcoFlowStreamDriver)
    d.last_quota, d._cache = q, None
    asyncio.run(d.apply(Desired(Mode.AUTO)))                      # nothing to hand back: fine
    try:
        asyncio.run(d.apply(Desired(Mode.HOLD)))
        assert False
    except ValueError:
        pass


def test_ecoflow_live_messages_and_driver_merge():
    import asyncio
    from hp.core.models import Mode
    from hp.drivers.ecoflow import EcoFlowStreamDriver
    from hp.drivers.ecoflow_live import EcoFlowLive, parse_topic, push_params

    assert parse_topic("/open/acct/BK61ZK/quota") == ("BK61ZK", "quota") and parse_topic("/x/y") is None
    assert push_params({"cmdFunc": 254, "params": {"powGetBpCms": -500.0, "cmsBattSoc": 90}}) == {
        "powGetBpCms": -500.0, "cmsBattSoc": 90, "cmdFunc": 254}
    assert push_params({"param": {"20_1.soc": "88"}}) == {"soc": 88.0}
    assert push_params("junk") == {}

    got = []

    async def go():
        hub = EcoFlowLive(api=None, loop=asyncio.get_running_loop())
        hub.register = lambda sn, cb: hub._devs.__setitem__(sn, cb)       # no network in this test
        d = EcoFlowStreamDriver.__new__(EcoFlowStreamDriver)
        d.sn, d.hub, d.last_quota, d.last_error, d._cache = "SN1", hub, {}, None, None
        d._state, d._pushed, d._polled = {}, 0.0, 0.0
        d.config = type("C", (), {"device_id": "x", "caps": type("K", (), {"min_soc": 10})()})()
        hub.register("SN1", d.merge_push)
        hub._dispatch("/open/a/SN1/quota", b'{"params": {"soc": 70}}')     # before the first full reading: ignored
        assert d._state == {}
        import time
        d._state, d._polled = {"soc": 50.0, "powGetBpCms": 0.0}, time.monotonic()
        hub._dispatch("/open/a/SN1/quota", b'{"params": {"soc": 71.5, "powGetBpCms": -900}}')
        hub._dispatch("/open/a/OTHER/quota", b'{"params": {"soc": 1}}')     # not ours
        hub._dispatch("/open/a/SN1/quota", b"not json")                     # ignored quietly
        a = await d.read()
        got.append((a.soc, a.power_w, hub.messages))
        hub.unregister("SN1", lambda x: None)                               # someone else's callback: stays registered
        assert "SN1" in hub._devs
        hub.unregister("SN1", d.merge_push)
        assert "SN1" not in hub._devs

    asyncio.run(go())
    assert got == [(71.5, -900.0, 2)]


def test_register_names_cover_what_a_real_x1_changes_between_two_scans():
    from core.anker_x1_map import describe_all
    names = describe_all(True)
    # registers seen moving on a real X1 (live readings), all of which must be named and filed as live
    for a in (10010, 10012, 10024, 10026, 10028, 10030, 10156, 10199, 10205, 10208, 10210, 10212, 10213, 10224, 10230, 10233,
              10237, 10238, 10254, 10328, 10496, 10632, 10638, 10644, 10646, 10649, 10656):
        assert str(a) in names and names[str(a)][2] == "live", a
    assert names["10496"][0] == "Pack 4 maximum temperature" and names["10064"][2] == "setting"


def test_read_registers_reads_only_what_is_asked_and_never_writes():
    async def go():
        async with fake_anker() as dev:
            d = AnkerModbusDriver(cfg(dev.port))
            dev.inp[10014] = 62
            dev.hold[10064] = 3
            try:
                out = await d.read_registers([10064, 10014, 10015, 59999])
                assert out["10014"] == 62 and out["10064"] == 3 and out["10015"] == 0 and "59999" not in out
                assert dev.writes == []
            finally:
                await d.close()
    asyncio.run(go())


def test_x1_reports_pack_voltages_and_stops_at_the_first_missing_pack():
    async def go():
        async with fake_anker() as dev:
            dev.inp[10321], dev.inp[10377] = 500, 520          # packs 1 and 2; pack 3 is not answered
            d = AnkerModbusDriver(cfg(dev.port, house_meter=True))
            try:
                assert (await d.read()).pack_v == (500, 520)
            finally:
                await d.close()
    asyncio.run(go())


def test_ecoflow_charge_raises_reserve_to_target():
    async def go():
        d = eco({"soc": 40, "backupReverseSoc": 10, "powGetBpCms": 0})
        await d.read()
        await d.apply(Desired(Mode.CHARGE, 1000, target_soc=80))
        assert d.api.sent == [{"cfgBackupReverseSoc": 80}]
        d.api.sent.clear()
        await d.apply(Desired(Mode.CHARGE, 1000, target_soc=100))
        assert d.api.sent == [{"cfgBackupReverseSoc": 95}]          # the API tops out at 95
        d.api.sent.clear()
        await d.apply(Desired(Mode.CHARGE, 1000, target_soc=30))
        assert d.api.sent == [{"cfgBackupReverseSoc": 40}]          # already above target: hold, do not top up
        with pytest.raises(ValueError):
            await d.apply(Desired(Mode.DISCHARGE, 1000))
        d._cache = None
        d.api._q = {"soc": 40, "backupReverseSoc": 80}
        assert (await d.read()).mode is Mode.CHARGE
    asyncio.run(go())


# What a real ES5000 (STREAM 5000 series, developer API) reported: reserve switched off, in timer-task mode, discharging.
ES5000 = {"bpSoc": 40, "bpPwr": -790, "gridPwr": 10, "loadPwr": 800, "pvPwr": 0, "onGridPwr": 790, "backupSoc": 40,
          "backupSocOnOff": False, "workMode": "TIMER_TASK", "feedToGridLimitOnOff": True, "acSocketOnoff": False}


def test_es5000_hold_charge_and_auto_use_the_5000_series_commands():
    async def go():
        d = eco(dict(ES5000))
        a = await d.read()
        assert (a.soc, a.power_w, a.grid_w, a.mode) == (40, -790, 10, Mode.AUTO)      # reserve off = the battery runs itself
        await d.apply(Desired(Mode.HOLD))
        assert d.api.sent == [{"workMode": "SELF_CONSUMPTION"}, {"bpBackupConfig": {"backupSocOnOff": True, "backupSoc": 40}}]
        assert set(d.api.ids) == {38}
        d.api.sent.clear()
        await d.apply(Desired(Mode.CHARGE, 1000, target_soc=80))                    # already in self-consumption: no mode change needed
        d.api._q = {**ES5000, "workMode": "SELF_CONSUMPTION", "backupSocOnOff": True, "backupSoc": 80}
        d._cache, d._state = None, {}
        assert (await d.read()).mode is Mode.CHARGE                                  # reserve above the charge level
        d.api.sent.clear()
        await d.apply(Desired(Mode.AUTO))
        assert d.api.sent == [{"bpBackupConfig": {"backupSocOnOff": False, "backupSoc": 0}}, {"workMode": "TIMER_TASK"}]
        d.api.sent.clear()
        d.api._q, d._state, d.last_quota = dict(ES5000), {}, dict(ES5000)            # back to how it was
        await d.apply(Desired(Mode.AUTO))
        assert d.api.sent == []                                                      # nothing of ours to hand back
        with pytest.raises(ValueError):
            await d.apply(Desired(Mode.DISCHARGE, 1000))
    asyncio.run(go())


def test_setting_changes_ignore_live_figures():
    from hp.drivers.ecoflow import setting_changes
    old = {"bpPwr": -700, "bpSoc": 40, "workMode": "TIMER_TASK", "backupSocOnOff": False, "backupSoc": 40, "newKey": 1}
    new = {"bpPwr": -650, "bpSoc": 41, "workMode": "SELF_CONSUMPTION", "backupSocOnOff": True, "backupSoc": 80, "newKey": 2, "other": 3}
    got = {c["key"]: (c["from"], c["to"]) for c in setting_changes(old, new)}
    assert got == {"workMode": ("TIMER_TASK", "SELF_CONSUMPTION"), "backupSocOnOff": (False, True), "backupSoc": (40, 80)}


def test_es5000_originals_survive_a_restart():
    async def go():
        saved = []
        d = eco(dict(ES5000))
        d._on_orig = saved.append
        await d.read()
        await d.apply(Desired(Mode.HOLD))
        assert saved and saved[-1]["mode"] == "TIMER_TASK"
        # Home Assistant restarts while the battery is held: a new driver is built from what was saved
        c = eco({**ES5000, "workMode": "SELF_CONSUMPTION", "backupSocOnOff": True, "backupSoc": 40})
        again = EcoFlowStreamDriver(c.config, session=None, orig=saved[-1], on_orig=saved.append)
        again.api = c.api
        await again.read()
        await again.apply(Desired(Mode.AUTO))
        assert {"workMode": "TIMER_TASK"} in again.api.sent and {"bpBackupConfig": {"backupSocOnOff": False, "backupSoc": 0}} in again.api.sent
        assert saved[-1] is None                                  # handed back: nothing left to remember
    asyncio.run(go())


def test_ecoflow_refusal_and_rate_limit_back_off_and_say_so():
    from hp.drivers.ecoflow import EcoFlowError

    class Refusing(FakeApi):
        async def command(self, sn, params, cmd_id=17):
            err = EcoFlowError("EcoFlow error 1006: x")
            err.code = "1006"
            raise err

    async def go():
        d = eco({"soc": 40, "backupReverseSoc": 10})
        await d.read()
        d.api = Refusing({})
        with pytest.raises(ValueError, match="1006"):
            await d.apply(Desired(Mode.HOLD))
        with pytest.raises(ValueError):                           # backing off: no second attempt reaches EcoFlow
            await d.apply(Desired(Mode.HOLD))
        d.api = FakeApi({})
        await d.apply(Desired(Mode.AUTO))                         # handing back is still attempted while backing off
        assert d.api.sent
    asyncio.run(go())


def test_ultra_x_auto_puts_back_the_reserve_the_user_had_not_the_minimum():
    async def go():
        saved = []
        d = eco({"soc": 50, "backupReverseSoc": 30, "powGetBpCms": 0})            # the user keeps a 30 % reserve
        d._on_orig = saved.append
        await d.read()
        await d.apply(Desired(Mode.HOLD))
        assert d.api.sent == [{"cfgBackupReverseSoc": 50}] and saved[-1] == {"reserve": 30, "wrote": [50]}
        d.api.sent.clear()
        await d.apply(Desired(Mode.AUTO))
        assert d.api.sent[-1] == {"cfgBackupReverseSoc": 30} and saved[-1] is None
        d.api.sent.clear()
        await d.apply(Desired(Mode.AUTO))                                          # nothing of ours left to hand back
        assert d.api.sent == []
    asyncio.run(go())


def test_ultra_x_original_reserve_survives_a_restart_and_a_raised_reserve_is_not_trusted():
    async def go():
        d = eco({"soc": 50, "backupReverseSoc": 30})
        await d.read()
        await d.apply(Desired(Mode.CHARGE, 1000, target_soc=80))
        orig = d._orig
        c = eco({"soc": 60, "backupReverseSoc": 80})                               # restarted while charging
        again = EcoFlowStreamDriver(c.config, session=None, orig=orig)
        again.api = c.api
        await again.read()
        await again.apply(Desired(Mode.AUTO))
        assert again.api.sent[-1] == {"cfgBackupReverseSoc": 30}
        h = eco({"soc": 60, "backupReverseSoc": 95})                               # found at 95 (the user's, or left by something)
        await h.read()
        await h.apply(Desired(Mode.HOLD))
        assert h._orig == {"reserve": 95, "wrote": [60]}
        h.api.sent.clear()
        await h.apply(Desired(Mode.AUTO))
        assert h.api.sent == [{"cfgBackupReverseSoc": 95}]                         # put back as it was found, never a made-up minimum
        old = EcoFlowStreamDriver(c.config, session=None, orig={"reserve": None})  # saved by an older version: the original is unknown
        old.api = FakeApi({"soc": 60, "backupReverseSoc": 95})
        await old.read()
        await old.apply(Desired(Mode.AUTO))
        assert old.api.sent == [] and old._orig is None                            # left alone
    asyncio.run(go())


def test_a_resting_reserve_three_above_the_discharge_limit_is_not_mistaken_for_hold_or_charge():
    """EcoFlow raises a reserve to at least the discharge limit + 3, so a nearly empty battery rests at 13 when min is 10."""
    from hp.drivers.ecoflow import EcoFlowStreamDriver as D
    for soc in (5, 11, 13, 14, 20, 80):
        assert D._mode(13, soc, 10) is Mode.AUTO, soc
    assert D._mode(14, 40, 10) is Mode.AUTO and D._mode(45, 40, 10) is Mode.CHARGE      # a real raise is still seen
    assert D._mode(40, 40, 10) is Mode.HOLD and D._mode(95, 60, 10) is Mode.CHARGE


def test_a_held_battery_resting_at_its_own_floor_reads_as_held_so_hold_is_not_resent():
    async def go():
        d = eco({"soc": 12, "backupReverseSoc": 10, "powGetBpCms": 0})
        assert (await d.read()).mode is Mode.AUTO                      # nothing of ours: just a battery at its floor
        await d.apply(Desired(Mode.HOLD))                              # the device raises/keeps its floor; reads as Auto again
        d._cache = None
        d.api._q = {"soc": 12, "backupReverseSoc": 13, "powGetBpCms": 0}
        assert (await d.read()).mode is Mode.HOLD                      # held (it cannot discharge): the Hold counts as done
        d._cache = None
        d.api._q = {"soc": 40, "backupReverseSoc": 10, "powGetBpCms": -300}
        assert (await d.read()).mode is Mode.AUTO                      # a battery well above its floor is not claimed as held
        await d.apply(Desired(Mode.AUTO))
        d._cache = None
        d.api._q = {"soc": 12, "backupReverseSoc": 10}
        assert (await d.read()).mode is Mode.AUTO                      # handed back: Auto again
    asyncio.run(go())


def test_a_failed_ecoflow_hand_back_keeps_the_users_original_reserve_for_the_retry():
    from hp.drivers.ecoflow import EcoFlowError

    async def go():
        d = eco({"cmsBattSoc": 0.0, "soc": 60, "powGetBpCms": 0.0, "backupReverseSoc": 30, "powGetSysGrid": 0})
        await d.read()
        await d.apply(Desired(Mode.HOLD))                       # remembers the user's own reserve (30)
        assert d._orig == {"reserve": 30, "wrote": [60]}
        real = d.api.command

        async def failing(sn, params, cmd_id=17):
            raise EcoFlowError("EcoFlow error 1006: busy")
        d.api.command = failing
        with pytest.raises(ValueError):
            await d.apply(Desired(Mode.AUTO))
        assert d._orig == {"reserve": 30, "wrote": [60]}              # not forgotten by the failure
        d.api.command = real
        d.api.sent.clear()
        d._state = {**d._state, "backupReverseSoc": 60}
        await d.apply(Desired(Mode.AUTO))
        assert {"cfgBackupReverseSoc": 30} in d.api.sent and d._orig is None
    asyncio.run(go())


def test_live_connection_finishing_after_stop_is_closed_not_kept():
    import types
    from hp.drivers.ecoflow_live import EcoFlowLive

    class FakeClient:
        def __init__(self, *a, **k):
            self.calls = []

        def __getattr__(self, name):
            return lambda *a, **k: self.calls.append(name)

    made = []

    def client_factory(*a, **k):
        made.append(FakeClient())
        return made[-1]
    mqtt = types.SimpleNamespace(Client=client_factory, CallbackAPIVersion=types.SimpleNamespace(VERSION2=2))
    cert = {"certificateAccount": "acc", "certificatePassword": "pw", "url": "x", "port": 8883}
    live = EcoFlowLive(None, None)
    live._connect(mqtt, cert, live._gen)                     # a normal connection is kept
    assert live._client is made[0] and "loop_start" in made[0].calls
    live.stop()
    assert live._client is None and "disconnect" in made[0].calls
    gen_then = live._gen - 1                                 # a connection that started before the stop and finishes after it
    live._connect(mqtt, cert, gen_then)
    assert live._client is None and "disconnect" in made[1].calls and "loop_stop" in made[1].calls


def test_stopping_live_updates_never_blocks_the_event_loop():
    import threading
    import types
    from hp.drivers.ecoflow_live import EcoFlowLive

    release = threading.Event()

    class SlowClient:                                         # loop_stop() waits for the network thread, which may be stuck
        def __init__(self, *a, **k):
            self.calls = []

        def loop_stop(self):
            release.wait(5)
            self.calls.append("loop_stop")

        def __getattr__(self, name):
            return lambda *a, **k: self.calls.append(name)

    async def go():
        made = []
        mqtt = types.SimpleNamespace(Client=lambda *a, **k: made.append(SlowClient()) or made[-1],
                                     CallbackAPIVersion=types.SimpleNamespace(VERSION2=2))
        live = EcoFlowLive(None, asyncio.get_running_loop())
        live._connect(mqtt, {"certificateAccount": "a", "certificatePassword": "p", "url": "x", "port": 8883}, live._gen)
        live.stop()                                           # returns at once, with the slow part handed off
        assert live._client is None and "loop_stop" not in made[0].calls
        release.set()
        for _ in range(100):
            if "loop_stop" in made[0].calls:
                break
            await asyncio.sleep(0.02)
        c = made[0].calls
        assert c.index("disconnect") < c.index("loop_stop")      # disconnect while the loop can still send it
    asyncio.run(go())


def test_ecoflow_retry_never_overwrites_the_users_own_reserve():
    async def go():
        d = eco({"cmsBattSoc": 0.0, "soc": 31, "powGetBpCms": 0.0, "backupReverseSoc": 30, "powGetSysGrid": 0})
        await d.read()
        # the user's own 30 % reserve with the battery at 31 % already reads like a Hold: with nothing of ours saved, Auto sends nothing
        await d.apply(Desired(Mode.AUTO))
        assert d.api.sent == []
        # a real hold, then the hand-back puts the user's 30 back ...
        await d.apply(Desired(Mode.HOLD))
        d.api.sent.clear()
        await d.apply(Desired(Mode.AUTO))
        assert {"cfgBackupReverseSoc": 30} in d.api.sent
        # ... and a second Auto straight after (the cloud still shows the old reserve) does not write a made-up value
        d.api.sent.clear()
        d._state = {**d._state, "backupReverseSoc": 95}
        await d.apply(Desired(Mode.AUTO))
        assert d.api.sent == []
    asyncio.run(go())


def test_ecoflow_hand_back_leaves_a_raised_reserve_alone_when_nothing_was_saved():
    async def go():
        d = eco({"cmsBattSoc": 0.0, "soc": 60, "powGetBpCms": 0.0, "backupReverseSoc": 95, "powGetSysGrid": 0})
        await d.read()
        await d.apply(Desired(Mode.AUTO))                        # no saved original: it may be the user's own, so never a made-up value
        assert d.api.sent == [] and d._warned_unknown            # (and it is logged, once)
        e = eco({**ES5000, "workMode": "SELF_CONSUMPTION", "backupSocOnOff": True, "backupSoc": 95})
        await e.read()
        await e.apply(Desired(Mode.AUTO))
        assert e.api.sent == []                                  # the 5000 series too: not switched off
    asyncio.run(go())


def test_ecoflow_takes_the_originals_from_a_fresh_reading_not_the_cached_one():
    async def go():
        d = eco({"soc": 50, "backupReverseSoc": 30})
        await d.read()
        d.api._q = {"soc": 50, "backupReverseSoc": 40}           # changed in the app since the cached reading
        await d.apply(Desired(Mode.HOLD))
        assert d._orig == {"reserve": 40, "wrote": [50]}
        d.api.sent.clear()
        await d.apply(Desired(Mode.AUTO))
        assert d.api.sent == [{"cfgBackupReverseSoc": 40}]
        e = eco(dict(ES5000))
        await e.read()
        e.api._q = {**ES5000, "workMode": "AI_MODE", "backupSocOnOff": True, "backupSoc": 25}
        await e.apply(Desired(Mode.HOLD))
        assert e._orig == {"on": True, "level": 25, "mode": "AI_MODE", "wrote": [40]}
        e.api.sent.clear()
        await e.apply(Desired(Mode.AUTO))
        assert e.api.sent == [{"bpBackupConfig": {"backupSocOnOff": True, "backupSoc": 25}}, {"workMode": "AI_MODE"}]
    asyncio.run(go())


def test_es5000_reserve_on_at_an_unknown_level_is_not_switched_off_on_hand_back():
    async def go():
        d = EcoFlowStreamDriver(eco(dict(ES5000)).config, session=None, orig={"on": True, "level": None, "mode": "TIMER_TASK"})
        d.api = FakeApi({**ES5000, "workMode": "SELF_CONSUMPTION", "backupSocOnOff": True, "backupSoc": 40})
        await d.read()
        await d.apply(Desired(Mode.AUTO))
        assert d.api.sent == [{"workMode": "TIMER_TASK"}] and d._orig is None   # the mode goes back; the level is left as it is
    asyncio.run(go())


def test_anker_and_local_ecoflow_remember_the_original_across_a_rebuild():
    from hp.drivers.ecoflow_modbus import EcoFlowModbusDriver
    saved = []
    caps = Capabilities(10000, 3000, 3000, 10)
    cfg = DeviceConfig("a", "A", "anker_e5000_pro", caps, extra={"connection": {"host": "127.0.0.1", "port": 1, "unit_id": 1}})
    a = AnkerModbusDriver(cfg, orig={"restore": 5}, on_orig=saved.append)
    assert a._restore == 5                                   # restored from the saved copy after a restart or an edit
    a._restore = 7
    a._restore = None
    assert saved == [{"restore": 7}, None]
    cfg2 = DeviceConfig("e", "E", "ecoflow_stream_5000_local", caps, extra={"connection": {"host": "x"}})
    e = EcoFlowModbusDriver(cfg2, client=object(), orig={"prev": [2, 50]}, on_orig=saved.append)
    assert e._prev == (2, 50)
    e._prev = (3, 10)
    assert saved[-1] == {"prev": [3, 10]}


def test_generic_mode_register_with_a_scale_reads_back_the_value_it_wrote():
    async def go():
        async with fake_anker() as dev:
            m = {**ANKER_AS_MAP, "mode": {**ANKER_AS_MAP["mode"], "scale": 0.1}}     # control_value / auto_value are raw numbers
            d = GenericModbusDriver(gcfg(dev.port, m))
            await d.apply(Desired(Mode.CHARGE, 2000))
            assert dev.hold[10064] == 3
            assert (await d.read()).mode is Mode.CHARGE                                # not mistaken for Auto (and re-sent forever)
            await d.close()
    asyncio.run(go())


def test_a_register_scale_of_zero_is_refused():
    from hp.core.modbus_map import MapError, parse_map
    with pytest.raises(MapError):
        parse_map({**ANKER_AS_MAP, "soc": {**ANKER_AS_MAP["soc"], "scale": 0}})


def test_ecoflow_before_its_first_reading_never_guesses_the_model():
    async def go():
        c = eco({})
        # a STREAM 5000's originals, remembered across a restart: handed back the 5000 way although nothing has been read yet
        d = EcoFlowStreamDriver(c.config, session=None, orig={"on": True, "level": 30, "mode": "WORKMODE_TIMER"})
        d.api = FakeApi({})
        await d.apply(Desired(Mode.AUTO))
        assert d.api.sent == [{"bpBackupConfig": {"backupSocOnOff": True, "backupSoc": 30}}, {"workMode": "WORKMODE_TIMER"}]
        # nothing remembered and nothing read: no command at all, and Hold is refused rather than sent in a guessed format
        d2 = EcoFlowStreamDriver(c.config, session=None)
        d2.api = FakeApi({})
        await d2.apply(Desired(Mode.AUTO))
        with pytest.raises(ValueError):
            await d2.apply(Desired(Mode.HOLD))
        assert d2.api.sent == []
    asyncio.run(go())


def test_details_sheet_names_and_decodes_the_registers():
    """The battery's sheet: Anker's named registers, read for real, with enums spelled out, units only where known and text decoded."""
    from core.device_details import addresses, anker_rows, wanted

    async def go():
        async with fake_anker() as dev:
            dev.hold[10064] = 3                                # work mode: third-party control
            dev.inp[10000] = 1                                 # plant status: on grid
            d = AnkerModbusDriver(cfg(dev.port, house_meter=True))
            await d.read()                                     # learns the word order
            entries = wanted(True, 0)
            values = {}
            addrs = addresses(entries)
            for i in range(0, len(addrs), 64):
                values.update(await d.read_registers(addrs[i:i + 64]))
            rows = {r["name"]: r for r in anker_rows(entries, values, d._order, True)}
            await d.close()
            return rows
    rows = asyncio.run(go())
    assert rows["Work mode"]["value"] == "third-party control" and rows["Work mode"]["kind"] == "setting"
    assert rows["Plant status"]["value"] == "on-grid"
    assert rows["SOC"]["value"] == "62" and rows["SOC"]["unit"] == "%"
    assert rows["Battery power"]["value"] == "-1500" and rows["Battery power"]["unit"] == "W"      # the register's own sign
    assert rows["Grid power"]["value"] == "300"
    assert rows["Battery rated capacity"]["value"] == "10.0" and rows["Battery rated capacity"]["unit"] == "kWh"
    assert not any(n.startswith("Pack ") for n in rows)                                         # no packs reported: none listed
    assert "Switch on/off (write only)" not in rows


def test_details_text_and_enum_helpers():
    from core.device_details import _enum, _text, anker_rows
    assert _enum("Export power limit mode (0 off, 1 percent, 2 fixed W)") == ("Export power limit mode", {0: "off", 1: "percent", 2: "fixed W"})
    assert _enum("Charge limit SOC (%, 80-100)")[1] == {}                        # a range is not a list of meanings
    assert _text([0x3141, 0x4243], low_first=True) == "A1CB" and _text([0x4131, 0x4342], low_first=False) == "A1CB"
    rows = anker_rows([(10100, 2, "Serial number", "RO", "info"), (10042, 8, "System alarms 1-8", "RO", "alarm")],
                      {"10100": 0x3141, "10101": 0x4243, **{str(10042 + i): 0 for i in range(8)}}, "big", True)
    assert rows[0]["value"] == "A1CB" and rows[1]["value"] == "none"
    assert anker_rows([(10014, 1, "SOC", "RO", "live")], {}, "big", True) == []     # an unanswered register is left out, not shown as 0


def test_details_x1_values_read_like_the_vendors_own_page():
    """An X1 on firmware 1.0.19.16: energies in 1/100 kWh, pack voltage in 0.1 V, limits in watts, the app's own work mode."""
    from core.device_details import anker_rows, wanted

    def put(vals, addr, v, order="big"):
        hi, lo = from_i32(v, order)
        vals[str(addr)], vals[str(addr + 1)] = hi & 0xFFFF, lo & 0xFFFF

    fw = b"1.0.19.16".ljust(12, b"\x00")
    vals = {str(10112 + i): fw[2 * i] | fw[2 * i + 1] << 8 for i in range(6)}        # the X1 holds text low byte first
    vals.update({"10064": 20, "10074": 2, "10077": 0, "10132": 0, "10631": 1, "10253": 527, "10249": 5, "10080": 1800, "10156": 179})
    put(vals, 10075, 3600); put(vals, 10078, 0); put(vals, 10262, 2783); put(vals, 10250, 250); put(vals, 10016, 0)
    rows = {r["name"]: r for r in anker_rows(wanted(True, 5), vals, "big", True)}
    assert rows["Battery total charge energy"]["value"] == "27.83" and rows["Battery total charge energy"]["unit"] == "kWh"
    assert rows["Battery rated capacity"]["value"] == "25.0" and rows["Battery voltage"]["value"] == "52.7" and rows["Battery voltage"]["unit"] == "V"
    assert rows["Export power limit value"]["value"] == "3600" and rows["Export power limit value"]["unit"] == "W"
    assert rows["Export power limit mode"]["value"] == "fixed power" and rows["Import power limit mode"]["value"] == "disabled"
    assert rows["Work mode"]["value"] == "app-managed"                      # 20: not in Anker's document, seen on real units
    assert rows["Output mode"]["value"] == "L/N" and rows["Meter status"]["value"] == "offline"
    assert rows["Comms-disconnect time with VPP (s, default 1800)"]["unit"] == "s" or any(r["unit"] == "s" for r in rows.values())
    assert rows["Internal temperature"]["value"] == "17.9"
    # firmware 1.0.18.x counts in 1/10 kWh; an unknown firmware is not guessed
    vals18 = dict(vals, **{str(10112 + i): (b"1.0.18.2".ljust(12, b"\x00")[2 * i] | b"1.0.18.2".ljust(12, b"\x00")[2 * i + 1] << 8) for i in range(6)})
    assert {r["name"]: r for r in anker_rows(wanted(True, 5), vals18, "big", True)}["Battery total charge energy"]["value"] == "278.30"
    other = dict(vals, **{str(10112 + i): (b"9.9.9.9".ljust(12, b"\x00")[2 * i] | b"9.9.9.9".ljust(12, b"\x00")[2 * i + 1] << 8) for i in range(6)})
    assert "raw" in {r["name"]: r for r in anker_rows(wanted(True, 5), other, "big", True)}["Battery total charge energy"]["value"]


def test_details_unlisted_enum_values_are_shown_not_hidden():
    from core.device_details import anker_rows
    r = anker_rows([(10000, 1, "Plant status (1 on-grid, 2 off-grid, 3 standby, 4 fault)", "RO", "info")], {"10000": 9}, "big", True)
    assert r[0]["value"] == "9 (not in Anker's list)"


def test_details_ecoflow_values_are_named_like_the_vendors_integration():
    from core.device_details import ecoflow_rows
    q = {"relay2Onoff": True, "relay3Onoff": False, "energyStrategyOperateMode.operateSelfPoweredOpen": True, "cmsMaxChgSoc": 100, "cmsMinDsgSoc": 0,
         "backupReverseSoc": 10, "cmsBattFullEnergy": 3840, "cmsBattSoc": 100.0, "powGetSysGrid": 1269.4321, "temp": 26, "cycles": 101,
         "remainTime": 0, "bmsDsgRemTime": 5939, "mysteryKey": "x"}
    rows = {r["name"]: r for r in ecoflow_rows(q)}
    assert rows["AC 1 output"]["value"] == "On" and rows["AC 2 output"]["value"] == "Off" and rows["AC 1 output"]["kind"] == "setting"
    assert rows["Backup reserve level"]["value"] == "10" and rows["Backup reserve level"]["unit"] == "%"
    assert rows["Stored energy"]["value"] == "3840" and rows["Stored energy"]["unit"] == "Wh"
    assert rows["Power grid"]["value"] == "1269.43" and rows["Battery temperature"]["unit"] == "°C" and rows["Cycles"]["value"] == "101"
    assert rows["Time to empty"]["value"] == "98 h 59 min"
    assert rows["mysteryKey"]["kind"] == "other" and rows["mysteryKey"]["value"] == "x"           # what is not named is still listed


def test_details_solarbank_follows_the_published_modbus_document():
    """Solarbank family (Anker's 'Modbus Protocol' V1.0.0): totals in 1/10 kWh, an on/off export limit in watts, SOH, AI mode; temperature as users report it."""
    from core.device_details import anker_rows, wanted
    vals = {"10064": 6, "10074": 1, "10014": 80, "10015": 98, "10156": 335, "10040": 1}
    for addr, v in ((10075, 800), (10022, 1234), (10024, 1000), (10018, 55)):
        hi, lo = from_i32(v, "big")
        vals[str(addr)], vals[str(addr + 1)] = hi, lo
    rows = {r["name"]: r for r in anker_rows(wanted(False), vals, "big", False)}
    assert rows["Work mode"]["value"] == "smart / AI mode"
    assert rows["Export power limit"]["value"] == "on" and rows["Export power limit value"]["value"] == "800" and rows["Export power limit value"]["unit"] == "W"
    assert rows["Total battery charge energy"]["value"] == "123.40" and rows["Total battery discharge energy"]["value"] == "100.00"
    assert rows["Total PV generation"]["value"] == "5.50" and rows["SOH"]["value"] == "98" and rows["SOH"]["unit"] == "%"
    assert [r for r in rows if r.startswith("Internal temperature")] and rows[[r for r in rows if r.startswith("Internal temperature")][0]]["value"] == "33.5"


def test_details_x1_electrical_readings_and_firmware_rules():
    from core.device_details import _x1_energy_gain, anker_rows, wanted
    # Only 1.0.18.x counts energy in 0.1 kWh; every other firmware reported so far (16, 17, 19, including 19.17) in 0.01 kWh.
    assert [_x1_energy_gain(f) for f in ("1.0.18.8", "1.0.18.16", "1.0.19.17", "1.0.16.1", "1.0.17.8", "2.0.0.1", None)] == [10, 10, 100, 100, 100, None, None]
    vals = {"10199": 2312, "10200": 0, "10201": 0, "10202": 2312, "10203": 0, "10204": 0, "10205": 1250, "10206": 0, "10207": 0,
            "10212": 65526, "10213": 4998, "10632": 242, "10633": 0, "10634": 0, "10635": 130, "10636": 0, "10637": 0, "10649": 5001}
    rows = {r["name"]: r for r in anker_rows(wanted(True, 0), vals, "big", True)}
    assert rows["Grid voltage (Uab / Ubc / Uca / phase A, B, C)"]["value"].startswith("231.2 / 0.0 / 0.0 / 231.2")
    assert rows["Grid current (phase A, B, C)"]["value"].startswith("12.50 /")
    assert rows["Grid power factor"]["value"] == "-0.010" and rows["Grid frequency"]["value"] == "49.98" and rows["Grid frequency"]["unit"] == "Hz"
    assert rows["Meter voltage (phase A, B, C)"]["value"].startswith("242.0 /")          # firmware 1.0.19.16 sends whole volts, older units 0.1 V
    assert rows["Meter current (phase A, B, C)"]["value"].startswith("1.30 /") and rows["Meter grid frequency"]["value"] == "50.01"
    older = {**vals, "10632": 2420}
    assert {r["name"]: r for r in anker_rows(wanted(True, 0), older, "big", True)}["Meter voltage (phase A, B, C)"]["value"].startswith("242.0 /")


def test_details_solarbank_capability_masks_and_ecoflow_5000_keys():
    from core.device_details import anker_rows, ecoflow_rows, wanted
    vals = {"32774": 0b0101011, "32775": 0b1011}
    rows = {r["name"]: r for r in anker_rows(wanted(False), vals, "big", False)}
    assert rows["Work modes this battery supports"]["value"] == "self-consumption, time-of-use, smart / AI mode, third-party control"
    assert rows["Settings this battery supports"]["value"] == "charge limit, discharge limit, backup reserve switch"
    r = {x["name"]: x for x in ecoflow_rows({"chgPowLimit": 1200, "feedGridModePowLimit": 800, "upsEnabled": True, "meterPhaseAVol": 231.5, "cycleSoh": 99})}
    assert r["Net power in limit"]["kind"] == "setting" and r["UPS mode"]["value"] == "On" and r["Grid voltage L1 (meter)"]["unit"] == "V"


def _eco_screen_driver():
    from hp.drivers.ecoflow import EcoFlowStreamDriver
    d = EcoFlowStreamDriver.__new__(EcoFlowStreamDriver)
    return d


def _eco_reading(pv, grid, bat=3800.0):
    from core.models import Actual
    return Actual(online=True, soc=50.0, power_w=bat, pv_w=pv, grid_w=grid)


def test_ecoflow_grid_charge_reported_as_solar_is_held_back_but_real_solar_is_not():
    """EcoFlow's cloud alternates real readings with ones that call the grid charge 'solar' (tolwi/hassio-ecoflow-cloud #786)."""
    d = _eco_screen_driver()
    seen = [d._screen_pv(_eco_reading(*r)).pv_w for r in (
        (350.0, 3837.0),      # a good reading: grid charging, a little real solar
        (3792.8, 0),          # the bad one from the report: charge power called solar, grid at exactly 0
        (349.0, 3835.0),      # good again
        (3794.1, 0),          # bad again
    )]
    assert seen == [350.0, 350.0, 349.0, 349.0] and d.pv_ignored == 2
    # sun really did come out: charge power equals solar, but the grid is a real (non-zero) reading, so it is believed at once
    d = _eco_screen_driver()
    assert [d._screen_pv(_eco_reading(*r)).pv_w for r in ((300.0, 40.5), (3500.0, -12.3))] == [300.0, 3500.0] and d.pv_ignored == 0
    # even with the grid at exactly 0, solar that stays high is believed from the third reading on
    d = _eco_screen_driver()
    d._screen_pv(_eco_reading(300.0, 20.0))
    assert [d._screen_pv(_eco_reading(3600.0, 0)).pv_w for _ in range(4)] == [300.0, 300.0, 3600.0, 3600.0]


def test_ecoflow_solar_screen_needs_every_mark_and_never_invents_a_reading():
    d = _eco_screen_driver()
    # the very first reading has nothing to compare with: taken as it is
    assert d._screen_pv(_eco_reading(3800.0, 0)).pv_w == 3800.0
    for pv, grid, bat in ((3800.0, 0, -3800.0),        # discharging: solar cannot be the charge power
                          (2000.0, 0, 3800.0),         # not equal to the charge power
                          (400.0, 0, 380.0),           # small readings are never second-guessed
                          (3800.0, None, 3800.0)):     # no grid reading: no evidence
        d = _eco_screen_driver()
        d._screen_pv(_eco_reading(100.0, 50.0, bat))
        assert d._screen_pv(_eco_reading(pv, grid, bat)).pv_w == pv, (pv, grid, bat)
    d = _eco_screen_driver()
    assert d._screen_pv(_eco_reading(None, 0)).pv_w is None                       # no solar figure stays none


def _run_settings(coro):
    return asyncio.run(coro)


def test_settings_solarbank_are_changed_within_the_vendors_ranges_and_rules():
    from core.device_settings import apply_change, read_settings

    async def go():
        async with fake_anker() as dev:
            dev.hold.update({10064: 0, 60000: 100, 60001: 10, 60002: 20, 60003: 1})
            dev.inp[32774], dev.inp[32775] = 0b1111011, 0b1111           # no custom mode; all four settings supported
            d = AnkerModbusDriver(cfg(dev.port))
            await d.read()
            fam, st, _ = await read_settings(d, False)
            by = {s["key"]: s for s in st}
            assert fam == "anker" and [o["label"] for o in by["work_mode"]["options"]] == ["Self-consumption", "Time-of-use", "Smart / AI mode", "Socket overlay mode", "Dynamic pricing", ]
            assert (by["charge_limit"]["value"], by["discharge_limit"]["value"], by["backup_reserve"]["value"]) == (100, 10, 20) and by["backup_enabled"]["kind"] == "info"
            await apply_change(d, "charge_limit", 90, False)
            await apply_change(d, "work_mode", 1, False)
            assert dev.hold[60000] == 90 and dev.hold[10064] == 1
            for key, val, why in (("charge_limit", 70, "between 80 and 100"), ("discharge_limit", 25, "between 0 and 20"), ("charge_limit", 85, None),
                                  ("backup_reserve", 5, "at least the discharge limit"), ("backup_reserve", 12, None), ("discharge_limit", 15, "not be above the backup reserve"), ("backup_reserve", 20, None),
                                  ("work_mode", 4, "not one of the choices"), ("work_mode", 3, "not one of the choices"), ("work_mode", "abc", "whole number"),
                                  ("backup_enabled", 0, "does not have that setting"), ("charge_limit", 20.5, "whole number")):
                if why is None:
                    await apply_change(d, key, val, False)
                    continue
                with pytest.raises(ValueError, match=why):
                    await apply_change(d, key, val, False)
            assert dev.hold[60000] == 85 and dev.hold[60001] == 10 and dev.hold[60002] == 20       # nothing refused was written
            with pytest.raises(ValueError, match="managing this battery"):       # HomePower drives the mode: locked while it controls the battery
                await apply_change(d, "work_mode", 0, True)
            await apply_change(d, "charge_limit", 100, True)                       # the limits are not HomePower's: still allowed
            dev.hold[60003] = 0
            with pytest.raises(ValueError, match="backup reserve on in the Anker app"):
                await apply_change(d, "backup_reserve", 30, False)
            await d.close()
    _run_settings(go())


def test_settings_x1_limits_are_written_in_the_batterys_own_word_order():
    from core.device_settings import apply_change, read_settings

    async def go():
        for order in ("big", "little"):
            async with fake_anker(order) as dev:
                dev.hold.update({10064: 20, 10074: 2, 10077: 0, 10080: 1800})
                dev.hold[10075], dev.hold[10076] = from_i32(3600, order)
                d = AnkerModbusDriver(cfg(dev.port, house_meter=True))
                await d.read()
                _, st, _ = await read_settings(d, False)
                by = {s["key"]: s for s in st}
                assert by["export_limit"]["value"] == 3600 and by["export_limit"]["unit"] == "W" and by["import_limit_mode"]["value"] == 0 and by["watchdog"]["value"] == 1800
                await apply_change(d, "export_limit", 2500, False)
                assert [dev.hold[10075], dev.hold[10076]] == from_i32(2500, order)
                await apply_change(d, "import_limit_mode", 1, False)
                await apply_change(d, "import_limit", 50, False)
                await apply_change(d, "watchdog", 600, False)
                await apply_change(d, "work_mode", 0, False)
                assert (dev.hold[10077], dev.hold[10080], dev.hold[10064]) == (1, 600, 0) and [dev.hold[10078], dev.hold[10079]] == from_i32(50, order)
                for key, val, why in (("import_limit", 101, "between 0 and 100"), ("watchdog", 25, "between 30 and 3600"), ("work_mode", 3, "not one of the choices"),
                                      ("export_limit_mode", 5, "not one of the choices"), ("export_limit", 40000, None)):
                    if why is None:                                                 # 40000 W is over the X1 integration's own 30000 W range only in fixed mode
                        with pytest.raises(ValueError, match="between 0 and 30000"):
                            await apply_change(d, key, val, False)
                    else:
                        with pytest.raises(ValueError, match=why):
                            await apply_change(d, key, val, False)
                await d.close()
    _run_settings(go())


def test_settings_ecoflow_stream_commands_match_the_vendors_integration():
    from core.device_settings import apply_change, read_settings

    async def go():
        q = {"soc": 71, "powGetBpCms": 0.0, "backupReverseSoc": 10, "cmsMinDsgSoc": 5, "cmsMaxChgSoc": 100, "relay2Onoff": True, "relay3Onoff": False,
             "feedGridMode": 1, "operateSelfPoweredOpen": True, "operateIntelligentScheduleModeOpen": False}
        d = eco(q)
        await d.read()
        fam, st, _ = await read_settings(d, False)
        by = {s["key"]: s for s in st}
        assert fam == "ecoflow" and by["ac1"]["value"] is True and by["feed_in"]["value"] is False and (by["backup_reserve"]["min"], by["backup_reserve"]["max"]) == (8, 95)
        for key, val in (("ac1", False), ("ac2", "on"), ("feed_in", True), ("self_powered", False), ("ai_mode", True), ("backup_reserve", 50)):
            await apply_change(d, key, val, False)
        assert d.api.sent == [{"cfgRelay2Onoff": False}, {"cfgRelay3Onoff": True}, {"cfgFeedGridMode": 2},
                              {"cfgEnergyStrategyOperateMode": {"operateSelfPoweredOpen": False}},
                              {"cfgEnergyStrategyOperateMode": {"operateIntelligentScheduleModeOpen": True}}, {"cfgBackupReverseSoc": 50}]
        d.api.sent.clear()
        for key, val, why in (("backup_reserve", 7, "between 8 and 95"), ("backup_reserve", 96, "between 8 and 95"), ("ac1", "maybe", "on or off"), ("nope", 1, "does not have")):
            with pytest.raises(ValueError, match=why):
                await apply_change(d, key, val, False)
        for key in ("backup_reserve", "self_powered", "ai_mode"):                  # what HomePower drives is locked while it controls the battery
            with pytest.raises(ValueError, match="managing this battery"):
                await apply_change(d, key, 30 if key == "backup_reserve" else True, True)
        await apply_change(d, "ac2", False, True)                                  # the AC outputs are not HomePower's
        assert d.api.sent == [{"cfgRelay3Onoff": False}]
        e = eco({"backupSocOnOff": True, "backupSoc": 20, "soc": 50})              # STREAM 5000 series: a different command set, not offered
        await e.read()
        assert (await read_settings(e, False))[1] == []
    _run_settings(go())


def test_settings_odd_values_from_the_battery_do_not_break_the_sheet():
    from core.device_settings import anker_settings, ecoflow_settings, ecoflow_write_params
    st = {s["key"]: s for s in ecoflow_settings({"relay2Onoff": "false", "relay3Onoff": "true", "feedGridMode": "2", "operateSelfPoweredOpen": 0,
                                                 "backupReverseSoc": 10, "cmsMinDsgSoc": "n/a", "cmsMaxChgSoc": None}, False)}
    assert st["ac1"]["value"] is False and st["ac2"]["value"] is True and st["feed_in"]["value"] is True and st["self_powered"]["value"] is False
    assert (st["backup_reserve"]["min"], st["backup_reserve"]["max"]) == (3, 95)
    with pytest.raises(ValueError, match="whole number"):
        ecoflow_write_params("backup_reserve", "abc", {"backupReverseSoc": 10}, False)
    # a Solarbank that does not say whether its backup reserve is on: the reserve is left alone, with the right reason
    by = {s["key"]: s for s in anker_settings(False, {10064: 0, 32774: 1, 32775: 0b0111, 60000: 100, 60001: 10, 60002: 20}, "big", False)}
    assert "did not say" in by["backup_reserve"]["locked"] and "backup_enabled" not in by
    # HomePower is driving an X1: its work mode is locked, its limits and watchdog are not
    x = {s["key"]: s for s in anker_settings(True, {10064: 20, 10074: 2, 10075: 0, 10076: 3600, 10077: 0, 10078: 0, 10079: 0, 10080: 600}, "big", True)}
    assert x["work_mode"]["locked"] and not x["export_limit"]["locked"] and not x["watchdog"]["locked"] and x["export_limit"]["value"] == 3600


def test_details_and_settings_survive_garbage_from_a_battery():
    """Whatever a battery (or a cloud) sends, the sheet is built and a change is either refused with a reason or checked: never an exception."""
    import random
    from core.device_details import anker_rows, ecoflow_rows, wanted
    from core.device_settings import anker_settings, anker_write_plan, ecoflow_settings, ecoflow_write_params
    rnd = random.Random(7)
    junk = [0, 1, 2, 3, 255, 1000, 32767, 32768, 65535, None, "x", 1.5, True, [], {}]
    for _ in range(300):
        x1 = rnd.random() < .5
        order = rnd.choice(["big", "little", None])
        vals = {str(a + i): rnd.choice([rnd.randrange(65536), rnd.randrange(65536), 0, 65535]) for a, n, *_ in wanted(x1) for i in range(n) if rnd.random() < .9}
        for r in anker_rows(wanted(x1), vals, order, x1):
            assert isinstance(r["value"], str) and isinstance(r["name"], str)
        regs = {a: rnd.randrange(65536) for a in (10064, 10074, 10075, 10076, 10077, 10078, 10079, 10080, 32774, 32775, 60000, 60001, 60002, 60003) if rnd.random() < .85}
        controlled = rnd.random() < .5
        anker_settings(x1, regs, order, controlled)
        for key in ("work_mode", "export_limit_mode", "export_limit", "import_limit", "watchdog", "charge_limit", "discharge_limit", "backup_reserve", "nope"):
            try:
                plan = anker_write_plan(x1, key, rnd.choice(junk + [rnd.randrange(-5, 40000)]), regs, order, controlled)
                assert plan and all(0 <= w <= 0xFFFF for _, words in plan for w in words)
            except ValueError:
                pass
        q = {k: rnd.choice(junk + [rnd.randrange(0, 3000)]) for k in ("relay2Onoff", "relay3Onoff", "feedGridMode", "operateSelfPoweredOpen", "operateIntelligentScheduleModeOpen",
                                                                       "backupReverseSoc", "cmsMinDsgSoc", "cmsMaxChgSoc", "soc", "temp") if rnd.random() < .8}
        ecoflow_rows(q)
        ecoflow_settings(q, controlled)
        for key in ("ac1", "ac2", "feed_in", "self_powered", "ai_mode", "backup_reserve", "nope"):
            try:
                ecoflow_write_params(key, rnd.choice(junk + [rnd.randrange(-5, 200)]), q, controlled)
            except ValueError:
                pass


def test_settings_the_remaining_refusals_and_empty_cases():
    from core.device_settings import anker_write_plan, apply_change, read_settings
    x1regs = {10064: 20, 10074: 2, 10075: 0, 10076: 3600, 10077: 0, 10078: 0, 10079: 0, 10080: 600}
    from core.device_settings import anker_settings
    assert "export_limit" not in {s["key"] for s in anker_settings(True, x1regs, None, False)}          # the value cannot even be shown without the word order
    with pytest.raises(ValueError, match="does not have that setting"):               # so a 32-bit limit is never written in a guessed word order
        anker_write_plan(True, "export_limit", 2000, x1regs, None, False)
    assert anker_write_plan(True, "watchdog", 600, x1regs, None, False) == [(10080, [600])]          # a 16-bit register needs no word order
    sb = {10064: 0, 32774: 0b1111011, 32775: 0b1111, 60000: 100, 60001: 10, 60002: 90, 60003: 1}
    with pytest.raises(ValueError, match="charging limit must be above"):
        anker_write_plan(False, "charge_limit", 85, sb, "big", False)              # the reserve (90 %) would end up above the charging limit
    assert anker_write_plan(False, "charge_limit", 95, sb, "big", False) == [(60000, [95])]
    sb[60003] = 0                                                                  # the reserve is off: its limits do not apply
    assert anker_write_plan(False, "charge_limit", 85, sb, "big", False) == [(60000, [85])]

    class Bare:                                                                    # a battery driven through Home Assistant entities, or a generic Modbus map
        config = type("C", (), {"extra": {}})()
    async def go():
        assert await read_settings(Bare(), False) == ("", [], {})
        with pytest.raises(ValueError, match="no settings that can be changed"):
            await apply_change(Bare(), "work_mode", 0, False)
    asyncio.run(go())


def test_details_actual_fields():
    from core.device_details import actual_fields
    from core.models import Actual
    assert actual_fields(None) == {}
    f = actual_fields(Actual(online=True, soc=62.0, power_w=-300.0, mode=Mode.HOLD, set_power_w=0, grid_w=120.0, pv_w=40.0, load_w=420.0, temp_c=21.5,
                             firmware="1.0.19.16", alarms=("meter 1 communication failure",), pack_v=(527, 526), limits=(100, 10, 20, True), off_grid=False))
    assert f["mode"] == "hold" and f["limits"] == {"charge_to": 100, "discharge_to": 10, "backup_reserve": 20, "backup_on": True}
    assert f["alarms"] == ["meter 1 communication failure"] and f["pack_v"] == [527, 526] and f["load_w"] == 420.0 and f["off_grid"] is False
    assert actual_fields(Actual(online=False))["limits"] is None and actual_fields(Actual(online=False))["mode"] is None


def test_ecoflow_read_applies_the_solar_screen_on_both_paths():
    """The guard must be wired into read(): the cloud poll and the live push both."""
    from core.models import Actual  # noqa: F401
    good = {"soc": 50, "powGetBpCms": 3800.0, "powGetPvSum": 350.0, "powGetSysGrid": 3837.0, "backupReverseSoc": 10}
    bad = {**good, "powGetPvSum": 3792.8, "powGetSysGrid": 0}

    async def go():
        d = eco(good)
        assert (await d.read()).pv_w == 350.0
        d._cache = None
        d.api._q = bad
        a = await d.read()                                  # the cloud poll path
        assert a.pv_w == 350.0 and d.pv_ignored == 1
        d._cache = None
        d.api._q = good
        assert (await d.read()).pv_w == 350.0
        d.merge_push({"powGetPvSum": 3794.1, "powGetSysGrid": 0})                  # the live push path: same mark, same answer
        assert (await d.read()).pv_w == 350.0 and d.pv_ignored == 2
    asyncio.run(go())


def test_offline_explanation_for_the_solarbanks_whose_modbus_depends_on_anker_cloud():
    from core.sanity import SOLARBANK_CLOUD_HINT, explain_idle, offline_hint
    assert offline_hint("anker_e5000_pro") == SOLARBANK_CLOUD_HINT and offline_hint("anker_solarbank_max_ac") == SOLARBANK_CLOUD_HINT
    assert offline_hint("anker_x1") is None and offline_hint("ecoflow_stream_ultra_x") is None and offline_hint(None) is None      # only where Anker has confirmed it
    base = dict(control_enabled=True, paused=False, dry_run=False, off_grid=False, protected=False, last_error=None, guard_held=False, unconfirmed=0)
    off = explain_idle(online=False, hint=SOLARBANK_CLOUD_HINT, **base)
    assert off.startswith("Offline: HomePower cannot read it.") and "Anker's cloud" in off and "Anker app" in off
    assert explain_idle(online=False, **base) == "Offline: HomePower cannot read it."                          # no hint for other batteries
    assert explain_idle(online=True, hint=SOLARBANK_CLOUD_HINT, **base) is None                                # the hint only ever explains being offline


def test_anker_driver_records_why_it_went_quiet_and_how_long_for():
    async def go():
        dev = FakeAnker()
        srv = await asyncio.start_server(dev.handle, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        d = AnkerModbusDriver(cfg(port))
        d.client.timeout, d.client.patience = 0.3, 0.6
        assert (await d.read()).online and d.last_error is None and d.outages == []
        for w in dev.conns:
            w.close()
        srv.close()
        a = await d.read()
        assert not a.online and any(t in d.last_error for t in ("refused the connection", "no reply when connecting"))
        await asyncio.sleep(0.2)
        srv = await asyncio.start_server(dev.handle, "127.0.0.1", port)
        assert (await d.read()).online
        assert d.last_error is None and len(d.outages) == 1 and d.outages[0]["seconds"] >= 0 and d.outages[0]["at"]
        await d.close(); srv.close()
    asyncio.run(go())


def test_settings_read_the_capability_mask_0x8007_on_its_own():
    from core.device_settings import read_settings

    async def go():
        async with fake_anker() as dev:
            dev.hold.update({10064: 0, 60000: 100, 60001: 10, 60002: 20, 60003: 1})
            dev.inp[32774] = 0b1111011                          # the battery has the mode mask but not 0x8007
            d = AnkerModbusDriver(cfg(dev.port))
            await d.read()
            asked = []
            real = d.read_registers

            async def spy(addrs):
                asked.append(list(addrs))
                return await real(addrs)
            d.read_registers = spy
            _, st, _ = await read_settings(d, False)
            assert not any(32774 in a and 32775 in a for a in asked) and [32775] in asked
            assert {"charge_limit", "discharge_limit", "backup_reserve"} <= {s["key"] for s in st}      # no 0x8007: every limit is still offered
            await d.close()
    asyncio.run(go())


def test_infinite_or_huge_values_are_refused_not_crashed():
    from core.device_settings import anker_write_plan, ecoflow_write_params, ecoflow_settings
    regs = {10064: 0, 32774: 0xFF, 32775: 0xFF, 60000: 100, 60001: 10, 60002: 20, 60003: 0}
    for bad in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ValueError):
            anker_write_plan(False, "charge_limit", bad, regs, "big", False)
        with pytest.raises(ValueError):
            ecoflow_write_params("backup_reserve", bad, {"backupReverseSoc": 20}, False)
    # a cloud value that is infinity must not break reading the settings
    assert ecoflow_settings({"backupReverseSoc": 20, "cmsMinDsgSoc": "1e999"}, False)


def test_a_battery_that_answers_nothing_is_not_told_it_lacks_the_setting():
    import asyncio
    from core.device_settings import apply_change

    class Mute:
        config = type("C", (), {"extra": {}})()
        _order = "big"
        async def read_registers(self, addrs): return {}
        async def scan(self): return {}

    with pytest.raises(ValueError, match="did not answer"):
        asyncio.run(apply_change(Mute(), "charge_limit", 90, False))


def test_ultra_x_hand_back_keeps_a_reserve_changed_in_the_app_while_held():
    async def go():
        saved = []
        d = eco({"soc": 50, "backupReverseSoc": 30})
        d._on_orig = saved.append
        await d.read()
        await d.apply(Desired(Mode.HOLD))                          # HomePower raises the reserve to 50
        assert saved[-1] == {"reserve": 30, "wrote": [50]}         # what it wrote is saved with the original (survives a restart)
        d.api._q = {"soc": 50, "backupReverseSoc": 40}             # the user sets 40 in the EcoFlow app meanwhile
        d.api.sent.clear()
        await d.apply(Desired(Mode.AUTO))
        assert d.api.sent == [] and d._orig is None                # their 40 stays: the older 30 is not put back over it
        # HomePower's own raise, still showing (or rounded up to the battery's floor), is handed back as usual
        e = eco({"soc": 50, "backupReverseSoc": 30})
        await e.read()
        await e.apply(Desired(Mode.CHARGE, 1000, target_soc=80))
        e.api._q = {"soc": 60, "backupReverseSoc": 80}
        e.api.sent.clear()
        await e.apply(Desired(Mode.AUTO))
        assert e.api.sent == [{"cfgBackupReverseSoc": 30}]
        f = eco({"soc": 5, "backupReverseSoc": 30})
        await f.read()
        await f.apply(Desired(Mode.HOLD))                          # writes 5; the battery keeps it at its floor (min 10 + 3)
        f.api._q = {"soc": 5, "backupReverseSoc": 13}
        f.api.sent.clear()
        await f.apply(Desired(Mode.AUTO))
        assert f.api.sent == [{"cfgBackupReverseSoc": 30}]
        # an entry saved by the previous version (no record of what was written) still hands back the saved reserve
        g = EcoFlowStreamDriver(eco({}).config, session=None, orig={"reserve": 30})
        g.api = FakeApi({"soc": 50, "backupReverseSoc": 40})
        await g.read()
        await g.apply(Desired(Mode.AUTO))
        assert g.api.sent == [{"cfgBackupReverseSoc": 30}]
    asyncio.run(go())


def test_es5000_hand_back_keeps_a_reserve_or_mode_changed_in_the_app_while_held():
    async def go():
        d = eco(dict(ES5000))                                       # reserve off, timer-task mode
        await d.read()
        await d.apply(Desired(Mode.HOLD))                           # self-consumption, reserve on at 40
        d.api._q = {**ES5000, "workMode": "AI_MODE", "backupSocOnOff": True, "backupSoc": 20}   # both changed in the app
        d.api.sent.clear()
        await d.apply(Desired(Mode.AUTO))
        assert d.api.sent == [] and d._orig is None                 # neither is overwritten
        e = eco(dict(ES5000))
        await e.read()
        await e.apply(Desired(Mode.HOLD))
        e.api._q = {**ES5000, "workMode": "SELF_CONSUMPTION", "backupSocOnOff": True, "backupSoc": 40}   # still as HomePower left it
        e.api.sent.clear()
        await e.apply(Desired(Mode.AUTO))
        assert e.api.sent == [{"bpBackupConfig": {"backupSocOnOff": False, "backupSoc": 0}}, {"workMode": "TIMER_TASK"}]
    asyncio.run(go())


def test_anker_forgets_the_saved_mode_when_someone_else_leaves_third_party_control():
    async def go():
        async with fake_anker() as dev:
            saved = []
            dev.hold[10064] = 1
            d = AnkerModbusDriver(cfg(dev.port), on_orig=saved.append)
            await d.apply(Desired(Mode.HOLD))
            dev.hold[10064] = 4                                        # the user picks another mode in the Anker app meanwhile
            n = len(dev.writes)
            await d.apply(Desired(Mode.AUTO))
            assert dev.hold[10064] == 4 and len(dev.writes) == n and saved[-1] is None
            await d.close()
    asyncio.run(go())


def test_a_reserve_the_battery_clamped_to_its_charge_limit_still_counts_as_homepowers_own():
    async def go():
        d = eco({"soc": 50, "backupReverseSoc": 30, "cmsMaxChgSoc": 80, "cmsMinDsgSoc": 10})
        await d.read()
        await d.apply(Desired(Mode.CHARGE, 1000, target_soc=95))   # writes 95; the battery keeps it at its 80 % charge limit
        d.api._q = {"soc": 60, "backupReverseSoc": 80, "cmsMaxChgSoc": 80, "cmsMinDsgSoc": 10}
        d.api.sent.clear()
        await d.apply(Desired(Mode.AUTO))
        assert d.api.sent == [{"cfgBackupReverseSoc": 30}]          # not mistaken for the user's change (it would stay stuck at 80)
    asyncio.run(go())


def test_x1_clearing_homepowers_limit_puts_back_an_installers_looser_limit_not_off():
    async def go():
        async with fake_anker() as dev:
            for a in range(10060, 10085):
                dev.hold.setdefault(a, 0)
            dev.hold[10074], dev.hold[10075], dev.hold[10076] = 2, 0, 6000      # an installer's 6000 W export limit
            d = AnkerModbusDriver(cfg(dev.port, house_meter=True))
            d._order = "big"
            exp = lambda: (dev.hold[10074], dev.hold[10075] << 16 | dev.hold[10076])
            try:
                mine, orig = {}, {}
                assert await d.set_grid_limits(None, 3000, mine, orig) == ["export limit 3000 W"]   # tightened
                assert orig == {"export": [2, 6000]} and mine == {"export": 3000}
                assert await d.set_grid_limits(None, 4000, mine, orig) == ["export limit 4000 W"] and orig == {"export": [2, 6000]}
                assert await d.set_grid_limits(None, None, mine, orig) == ["export limit back to 6000 W"]
                assert exp() == (2, 6000) and mine == {} and orig == {}             # the installer's limit, not off
                dev.hold[10074] = 0                                                # was off: HomePower's limit goes off again
                assert await d.set_grid_limits(None, 3000, mine, orig) == ["export limit 3000 W"] and orig == {"export": [0, 6000]}
                assert await d.set_grid_limits(None, None, mine, orig) == ["export limit off"] and exp()[0] == 0
            finally:
                await d.close()
    asyncio.run(go())


def test_x1_watchdog_is_saved_on_takeover_and_put_back_on_hand_back():
    async def go():
        async with fake_anker() as dev:
            saved = []
            dev.hold[10064], dev.hold[10080] = 0, 1800                        # the user's own watchdog: 1800 s
            d = AnkerModbusDriver(cfg(dev.port, watchdog=True), on_orig=saved.append)
            await d.apply(Desired(Mode.HOLD))
            assert dev.hold[10080] == 600 and saved[-1] == {"restore": 0, "watchdog": 1800}
            await d.close()
            again = AnkerModbusDriver(cfg(dev.port, watchdog=True), orig=saved[-1], on_orig=saved.append)   # after a restart
            await again.apply(Desired(Mode.AUTO))
            assert dev.hold[10080] == 1800 and dev.hold[10064] == 0 and saved[-1] is None
            await again.apply(Desired(Mode.HOLD))
            dev.hold[10080] = 900                                              # changed in the Anker app while held
            await again.apply(Desired(Mode.AUTO))
            assert dev.hold[10080] == 900                                      # theirs is kept
            await again.close()
    asyncio.run(go())


def test_x1_watchdog_is_put_back_even_when_someone_else_left_third_party_control():
    async def go():
        async with fake_anker() as dev:
            dev.hold[10064], dev.hold[10080] = 0, 1800
            d = AnkerModbusDriver(cfg(dev.port, watchdog=True))
            await d.apply(Desired(Mode.HOLD))
            dev.hold[10064] = 1                                                # the user picked time-of-use in the app
            await d.apply(Desired(Mode.AUTO))
            assert dev.hold[10064] == 1 and dev.hold[10080] == 1800            # their mode kept, HomePower's watchdog undone
            await d.close()
    asyncio.run(go())


def test_es5000_reserve_switch_missing_from_the_reading_is_unknown_not_off():
    async def go():
        d = EcoFlowStreamDriver(eco(dict(ES5000)).config, session=None, orig={"on": None, "level": 40, "mode": "TIMER_TASK", "wrote": [40]})
        d.api = FakeApi({**ES5000, "workMode": "SELF_CONSUMPTION", "backupSocOnOff": True, "backupSoc": 40})
        await d.read()
        await d.apply(Desired(Mode.AUTO))
        assert d.api.sent == [{"workMode": "TIMER_TASK"}]          # the mode goes back; the reserve is not switched off
    asyncio.run(go())
