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


def test_x1_extras_from_anker_register_document():
    from core.anker_x1_map import alarm_texts, describe_all
    async def go():
        async with fake_anker() as dev:
            for i, w in enumerate((11825, 11824, 14641, 12590, 54, 0)):      # a real X1's software version, characters low byte first
                dev.inp[10112 + i] = w
            for i, w in enumerate((12632, 18477, 19254, 21293, 0, 0, 0, 0, 0, 0)):   # its model name, "X1-H6K-S"
                dev.inp[10090 + i] = w
            dev.inp[10156] = 312                                            # internal temperature, tenths of a degree
            dev.inp[10042] = (1 << 5) | (1 << 6)                           # BMS and meter 1 communication failure
            d = AnkerModbusDriver(cfg(dev.port, house_meter=True))
            try:
                a = await d.read()
                assert a.firmware == "1.0.19.16" and a.temp_c == 31.2
                assert a.alarms == ("battery (BMS) communication failure", "meter 1 communication failure")
                info = await d.probe()
                assert info["model"] == "X1-H6K-S" and info["firmware"] == "1.0.19.16"
                dev.inp[10156] = 0xFFF6                                      # -1.0 degrees: signed
                d._slow_at = 0
                assert (await d.read()).temp_c == -1.0
            finally:
                await d.close()
    asyncio.run(go())
    assert alarm_texts(0) == [] and alarm_texts(1) == ["EEPROM read/write error"]
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
        assert d.api.sent == [{"cfgEnergyStrategyOperateMode": {"operateSelfPoweredOpen": True}},
                              {"cfgBackupReverseSoc": 10}]
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
        assert d.api.sent == [{"cfgBackupReverseSoc": 50}] and saved[-1] == {"reserve": 30}
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
        h = eco({"soc": 60, "backupReverseSoc": 95})                               # left at 95 by something earlier
        await h.read()
        await h.apply(Desired(Mode.HOLD))
        assert h._orig == {"reserve": None}
        h.api.sent.clear()
        await h.apply(Desired(Mode.AUTO))
        assert h.api.sent[-1] == {"cfgBackupReverseSoc": 10}                       # unknown original: the battery's minimum
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
        assert d._orig == {"reserve": 30}
        real = d.api.command

        async def failing(sn, params, cmd_id=17):
            raise EcoFlowError("EcoFlow error 1006: busy")
        d.api.command = failing
        with pytest.raises(ValueError):
            await d.apply(Desired(Mode.AUTO))
        assert d._orig == {"reserve": 30}                       # not forgotten by the failure
        d.api.command = real
        d.api.sent.clear()
        d._state = {**d._state, "backupReverseSoc": 60}
        await d.apply(Desired(Mode.AUTO))
        assert {"cfgBackupReverseSoc": 30} in d.api.sent and d._orig is None
    asyncio.run(go())
