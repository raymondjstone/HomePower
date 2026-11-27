"""Batteries that answer only some of what they are asked: the drivers must read what there is, never guess, and never
stop a command because something optional was refused."""
import asyncio
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest
from hp.drivers.anker import MODE_REG, MODE_THIRD_PARTY, WATCHDOG_REG, WATCHDOG_SECONDS, AnkerModbusDriver
from hp.drivers.ecoflow import EcoFlowApi, EcoFlowError, EcoFlowStreamDriver, friendly_error, normalise
from hp.drivers.ecoflow_modbus import ADDRESS_OFFSET, REG_CONTROL_POINT, REG_FLOW, REG_HEARTBEAT, EcoFlowModbusDriver
from hp.drivers.modbus import ModbusError, ModbusTcpClient
from hp.drivers.modbus_generic import GenericModbusDriver
from hp.drivers.portscan import check_host

from core.insights import DailyStats, UsageHistory, drop_impossible
from core.meter import MeterReader
from core.models import Capabilities, Desired, DeviceConfig, Mode


def config(kind="x", **extra):
    return DeviceConfig("d", "D", kind, Capabilities(10000, 3000, 3000, 10), extra=extra)


class Units:
    """A Modbus device: registers by address; `refuse` answers with an exception code, `silent` does not answer."""

    def __init__(self, regs=None):
        self.regs, self.refuse, self.silent, self.writes = dict(regs or {}), set(), set(), []

    def _check(self, addr, count=1):
        span = range(addr, addr + count)
        if self.silent.intersection(span):
            raise ModbusError("no reply")
        if self.refuse.intersection(span):
            raise ModbusError("refused", code=2)

    async def read_holding(self, addr, count):
        self._check(addr, count)
        return [self.regs.get(a, 0) for a in range(addr, addr + count)]

    read_input = read_holding

    async def write_register(self, addr, value):
        self._check(addr)
        self.writes.append((addr, value))
        self.regs[addr] = value

    async def write_multiple(self, addr, values):
        for i, v in enumerate(values):
            await self.write_register(addr + i, v)

    async def close(self):
        pass


def anker(client, **kw):
    return AnkerModbusDriver(config(connection={"host": "x"}, **kw.pop("extra", {})), client=client, **kw)


def run(coro):
    return asyncio.run(coro)


def cannot_save(value):
    raise OSError("disk full")


def test_anker_register_scan_and_reading_survive_refusals_and_silence():
    c = Units({a: a % 7 for a in range(10000, 10100)})
    drv = anker(c)
    c.refuse, c.silent = {10002}, set()
    got = run(drv.read_registers([10000, 10001, 10002, 10003, 10050]))
    assert got["10000"] == 10000 % 7 and "10002" not in got and "10050" in got
    c.refuse, c.silent = set(), set(range(0, 70000))
    assert run(drv.read_registers(list(range(100, 700, 50)))) == {}             # gives up after a few unanswered requests
    assert run(drv.scan()) is not None


def test_anker_hand_back_keeps_going_when_the_watchdog_cannot_be_read_or_written():
    c = Units({MODE_REG: 0, WATCHDOG_REG: 120})
    drv = anker(c, extra={"watchdog": True}, on_orig=cannot_save)
    drv._order = "big"
    c.refuse = {WATCHDOG_REG}
    run(drv.apply(Desired(Mode.HOLD)))                                             # neither read nor set: control still taken
    assert c.regs[MODE_REG] == MODE_THIRD_PARTY
    c.refuse, c.silent = set(), {WATCHDOG_REG}
    c.regs[MODE_REG] = 0
    with pytest.raises(ModbusError):                                               # no answer at all: the whole command is retried
        run(drv.apply(Desired(Mode.HOLD)))
    c.silent = set()
    drv._watchdog_orig, c.regs[WATCHDOG_REG] = 120, WATCHDOG_SECONDS
    c.refuse = {WATCHDOG_REG}
    run(drv._put_back_watchdog())                                                  # refused: said in the log, not raised
    c.refuse, c.silent = set(), {WATCHDOG_REG}
    with pytest.raises(ModbusError):
        run(drv._put_back_watchdog())
    assert run(anker(Units()).set_grid_limits(1000, 1000)) == []                   # word order unknown: nothing is written


def test_anker_probe_reads_what_the_battery_answers():
    c = Units({10014: 50, 10250: 100})
    drv = anker(c, extra={"house_meter": True})
    c.refuse = set(range(10090, 10124)) | set(range(10250, 10252)) | {60001}
    info = run(drv.probe())
    assert info["model"] is None and info["serial"] is None and info["capacity_wh"] is None and info["min_soc"] is None


def ecoflow_local(c, **kw):
    return EcoFlowModbusDriver(config(connection={"host": "x"}), client=c, **kw)


def test_ecoflow_local_reads_around_what_is_refused():
    c = Units()
    drv = ecoflow_local(c, on_orig=cannot_save)
    drv._prev = (1, 100)                                      # saving it fails: never stops anything
    c.refuse = {REG_HEARTBEAT + ADDRESS_OFFSET, REG_CONTROL_POINT + ADDRESS_OFFSET}
    a = run(drv.read())
    assert a.online and a.mode is Mode.AUTO                   # keep-alive and control block refused: still readable
    c.refuse, c.silent = set(), {REG_CONTROL_POINT + ADDRESS_OFFSET}
    assert not run(drv.read()).online                          # the link dropped half way
    c.silent = {REG_FLOW + ADDRESS_OFFSET}
    assert not run(drv.read()).online
    c.silent = set()
    c.refuse = set(range(ADDRESS_OFFSET, ADDRESS_OFFSET + 70000)) - set(range(REG_FLOW + ADDRESS_OFFSET, REG_FLOW + ADDRESS_OFFSET + 9))
    assert run(drv.probe())["serial"] is None
    c.refuse = set()
    run(drv.keepalive())
    with pytest.raises(ValueError):
        run(drv.apply(Desired(SimpleNamespace(value="sideways"))))
    c.silent = {REG_CONTROL_POINT + ADDRESS_OFFSET}
    with pytest.raises(ModbusError):
        run(drv.apply(Desired(Mode.HOLD)))


def test_generic_map_with_signs_the_other_way_round():
    mp = {"soc": {"addr": 1}, "power": {"addr": 2, "type": "i16"}, "grid": {"addr": 3, "type": "i16", "positive_is": "export"},
          "setpoint": {"addr": 4, "type": "i16", "positive_is": "charge"}, "mode": {"addr": 5, "control_value": 3, "auto_value": 0}}
    c = Units({1: 40, 2: 0xFFFF, 3: 100, 4: 200, 5: 3})
    drv = GenericModbusDriver(config(connection={"host": "x", "map": mp}), client=c)
    a = run(drv.read())
    assert a.power_w == -1 and a.grid_w == -100 and (a.mode, a.set_power_w) == (Mode.CHARGE, 200)
    c.regs[4] = 0
    assert run(drv.read()).mode is Mode.HOLD
    c.silent = {1}
    assert not run(drv.read()).online


def test_ecoflow_cloud_helpers_and_the_http_call(monkeypatch):
    # the plain unit-test run has no aiohttp installed: only its ClientTimeout is used here
    monkeypatch.setitem(sys.modules, "aiohttp", SimpleNamespace(ClientTimeout=lambda total=None: total))
    assert "rate limit" in friendly_error(EcoFlowError("too many requests, rate limit"))
    assert normalise({"quotaMap": {"20_1.soc": "5", "x": {"y": "text"}}}) == {"soc": 5.0, "y": "text"}
    sent = []

    class Reply:
        def __init__(self, status, data):
            self.status, self.data = status, data

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self, content_type=None):
            return self.data

    class Session:
        reply = Reply(200, {"code": "0", "data": []})

        def request(self, method, url, **kw):
            sent.append((method, url.rsplit("/", 1)[-1], sorted(kw["headers"])))
            if self.reply is None:
                raise OSError("down")
            return self.reply

    s = Session()
    api = EcoFlowApi(s, "eu", "ak", "sk")

    async def go():
        assert await api.devices() == [] and sent[-1][2] == ["accessKey", "nonce", "sign", "timestamp"]
        s.reply = Reply(200, {"code": "0", "data": {"url": "u"}})
        assert (await api.certification())["url"] == "u"
        assert await api.quota("SN1") == {"url": "u"}
        await api.command("SN1", {"a": 1})
        for reply, words in ((Reply(429, {}), "429"), (Reply(200, "text"), "unexpected"), (None, "cannot reach"),
                             (Reply(200, {"code": "1006", "message": "no"}), "1006")):
            s.reply = reply
            with pytest.raises(EcoFlowError, match=words):
                await api.quota("SN1")

    run(go())


def test_ecoflow_originals_that_cannot_be_saved_do_not_stop_a_command_and_unknown_charge_is_refused():
    drv = EcoFlowStreamDriver(config(connection={"sn": "SN", "access_key": "a", "secret_key": "b"}), session=None, on_orig=cannot_save)
    drv._orig = {"reserve": 20}
    assert drv._orig == {"reserve": 20} and drv._ours(None) is False


def test_only_a_name_on_the_local_network_can_be_scanned():
    for host in ("no-such-host.invalid", "bad host name \x00"):
        with pytest.raises(ValueError):
            run(check_host(host))


def test_a_device_that_only_takes_function_16_is_written_with_it():
    sent = []

    class Client(ModbusTcpClient):
        code = 1

        async def write_single(self, addr, value):
            raise ModbusError("illegal function", code=self.code)

        async def write_multiple(self, addr, values):
            sent.append((addr, values))

    client = Client("x")
    run(client.write_register(5, 7))
    assert sent == [(5, [7])]
    client.code = 2
    with pytest.raises(ModbusError):                                   # any other refusal is passed on
        run(client.write_register(5, 7))


def test_a_counter_read_after_a_very_long_gap_gives_no_power():
    meter = MeterReader()
    assert meter.read("imp", "10", "kWh", 0) is None
    assert meter.read("imp", "11", "kWh", 100000) is None              # hours later: no meaningful average
    assert meter.read("imp", "11", "kWh", 100010) is None


def test_x1_solar_is_the_total_of_its_pv_inputs_and_an_old_reading_is_not_counted(monkeypatch):
    # as seen on a real X1: "PV power" (10002) 290 W while its strings gave 388 W (10183)
    c = Units({10003: 290, 10184: 388})
    x1, plug = anker(c, extra={"house_meter": True}), anker(c)
    x1._order = plug._order = "big"
    inp = [0, 0, 0, 290] + [0] * 60
    x1._order = None
    run(x1._refresh_pv())
    assert x1._pv_total_now() is None                                 # word order not known yet: not read at all
    x1._order = "big"
    c.regs.update({10183: 388, 10184: 0})                             # the wrong way round for this order: 25 million W
    run(x1._refresh_pv())
    assert x1._pv_total_now() is None
    c.regs.update({10183: 0, 10184: 388})
    x1._pv_at = 0
    run(x1._refresh_pv())
    assert x1._solar_w(inp) == 388 and plug._solar_w(inp) == 290
    c.silent = {10167}
    x1._pv_at = 0
    run(x1._refresh_pv())
    assert x1._solar_w(inp) == 388                                    # one missed read: the last total still stands
    x1._pv_total_ok -= 120
    assert x1._solar_w(inp) == 290 and x1._pv_total_now() is None      # long gone: not counted any more


def test_impossible_half_hours_are_taken_out_of_the_records():
    usage, daily = UsageHistory(), DailyStats()
    noon = datetime(2026, 11, 27, 12, 10)
    usage.add(noon, 0.1, 0.0, 188.0, 188.2)
    usage.add(noon.replace(hour=9), 0.0, 0.0, 0.4, 0.2)
    daily.add("2026-11-27", pv_kwh=188.4)
    assert drop_impossible(usage, daily) == pytest.approx(188.2)
    day = usage.days["2026-11-27"]
    assert day["pv"][24] == 0 and day["load"][24] == 0 and day["pv"][18] == pytest.approx(0.2)
    assert daily.days["2026-11-27"]["pv"] == pytest.approx(0.2) and drop_impossible(usage, daily) == 0
