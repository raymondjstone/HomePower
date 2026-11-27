"""The corners of the EcoFlow live feed, the Anker network scan and the direct adapter that nothing else reaches."""
import asyncio
import json
import sys
import time
import types

import pytest
from hp.adapters.direct import DirectAdapter
from hp.drivers import discovery
from hp.drivers.ecoflow import EcoFlowError, EcoFlowStreamDriver
from hp.drivers.ecoflow_live import RETRY_AFTER_S, EcoFlowLive
from hp.drivers.modbus import ModbusError

from core import strategies
from core.alerts import AlertTracker
from core.modbus_map import MapError, parse_map
from core.models import Actual, Capabilities, Desired, DeviceConfig, Mode


class Client:
    """Stands in for paho's client: records what is asked of it."""

    def __init__(self, **kw):
        self.subs, self.calls = [], []

    def __getattr__(self, name):                  # username_pw_set, tls_set, connect_async, loop_start, disconnect...
        return lambda *a, **kw: self.calls.append(name)

    def subscribe(self, topics):
        self.subs += [t for t, _ in topics]


def mqtt(made):
    def client(**kw):
        made.append(Client())
        return made[-1]
    return types.SimpleNamespace(Client=client, CallbackAPIVersion=types.SimpleNamespace(VERSION2=2))


class Api:
    def __init__(self, error=None):
        self.error = error

    async def certification(self):
        if self.error:
            raise self.error
        return {"certificateAccount": "acct", "certificatePassword": "pw", "url": "broker", "port": "8883"}


def install(monkeypatch, module):
    paho, sub = types.ModuleType("paho"), types.ModuleType("paho.mqtt")
    paho.mqtt, sub.client = sub, module
    for name, mod in (("paho", paho), ("paho.mqtt", sub), ("paho.mqtt.client", module)):
        monkeypatch.setitem(sys.modules, name, mod)


def test_live_feed_connects_subscribes_delivers_and_stops(monkeypatch):
    made, got = [], []
    install(monkeypatch, mqtt(made))

    async def run():
        loop = asyncio.get_running_loop()
        live = EcoFlowLive(Api(), loop)
        live.register("SN1", got.append)
        assert live._task is None                                   # off until switched on
        live.enabled = True
        live.ensure()
        await live._task
        c = made[0]
        assert live.error is None and live._client is c and "loop_start" in c.calls
        c.on_connect(c, None, None, 0)
        assert live.connected and "/open/acct/SN1/quota" in c.subs
        live.register("SN2", got.append)                            # added while connected: subscribed at once
        assert "/open/acct/SN2/status" in c.subs
        c.on_message(None, None, types.SimpleNamespace(topic="/open/acct/SN1/quota", payload=json.dumps({"params": {"soc": 50}})))
        c.on_message(None, None, types.SimpleNamespace(topic="/open/acct/SN1/quota", payload=b"not json"))
        c.on_message(None, None, types.SimpleNamespace(topic="/open/acct/SN1/status", payload=b"{}"))
        await asyncio.sleep(0.05)
        assert live.messages == 1 and got[0]["soc"] == 50
        c.on_disconnect()
        assert not live.connected
        live.unregister("SN1", lambda f: None)                      # not its owner: ignored
        assert "SN1" in live._devs
        live.unregister("SN1")
        live.unregister("SN2")                                      # the last one: stops
        assert live._client is None
        await asyncio.sleep(0.05)
        assert "disconnect" in c.calls and "loop_stop" in c.calls

    asyncio.run(run())


def test_live_feed_failure_is_reported_and_retried_later(monkeypatch):
    install(monkeypatch, mqtt([]))

    async def run():
        live = EcoFlowLive(Api(RuntimeError("refused")), asyncio.get_running_loop())
        live.enabled = True
        live.register("SN1", print)
        await live._task
        assert "refused" in live.error and live._next_try > time.monotonic() + RETRY_AFTER_S - 5
        first = live._task
        live.ensure()
        assert live._task is first                                  # not again until the wait is over
        monkeypatch.setitem(sys.modules, "paho.mqtt.client", None)  # the import fails: polling only
        monkeypatch.setitem(sys.modules, "paho", None)
        live._next_try, live._task = 0.0, None
        await live._start()
        assert "not installed" in live.error

    asyncio.run(run())


def test_a_connection_finishing_after_a_stop_is_closed(monkeypatch):
    made = []

    async def run():
        live = EcoFlowLive(Api(), asyncio.get_running_loop())
        live._connect(mqtt(made), await Api().certification(), gen=live._gen - 1)
        assert live._client is None and "disconnect" in made[0].calls

    asyncio.run(run())
    loopless = EcoFlowLive(Api(), None)
    loopless._client = Client()
    kept = loopless._client
    loopless.stop()                                                 # at shutdown, with no loop left: closed where it is
    assert "disconnect" in kept.calls


def test_scan_never_goes_outside_a_private_network_and_survives_anything(monkeypatch):
    assert discovery.subnet_hosts("8.8.8.8", 24) == []
    assert len(discovery.subnet_hosts("192.168.1.10", 24)) == 253

    class Modbus:
        soc = 50

        def __init__(self, *a, **kw):
            pass

        async def read_input(self, addr, count):
            if addr == 10000:
                regs = [0] * count
                regs[14] = self.soc
                return regs
            raise ModbusError("no such register")

        async def close(self):
            pass

    monkeypatch.setattr(discovery, "ModbusTcpClient", Modbus)
    found = asyncio.run(discovery.probe_host("10.0.0.1"))
    assert found["soc"] == 50 and found["model"] is None and found["serial"] is None
    Modbus.soc = 999                                                # not a charge level: some other device
    assert asyncio.run(discovery.probe_host("10.0.0.1")) is None


def test_direct_adapter_passes_everything_to_its_driver():
    done = []

    class Driver:
        async def read(self):
            return Actual(online=True)

        async def apply(self, desired):
            done.append(desired.mode)

        async def close(self):
            done.append("closed")

    class KeptAlive(Driver):
        async def keepalive(self):
            done.append("alive")

    cfg = DeviceConfig("a", "A", "x", Capabilities(1000, 100, 100, 10))

    async def run():
        for driver in (Driver(), KeptAlive()):
            a = DirectAdapter(cfg, driver)
            assert (await a.read()).online
            await a.apply(Desired(Mode.HOLD))
            await a.keepalive()
            await a.close()

    asyncio.run(run())
    assert done == [Mode.HOLD, "closed", Mode.HOLD, "alive", "closed"]


def test_alert_forgetting_one_key_or_a_prefix():
    t = AlertTracker()
    for key in ("offline:garage", "offline:garage_2", "stuck:garage"):
        assert t.observe(key, True, 0.0) == "raised"
    assert t.forget("offline:garage") == ["offline:garage"]
    assert t.forget("offline:") == ["offline:garage_2"]
    assert t.active() == {"stuck:garage"}


@pytest.mark.parametrize("change", [
    {"word_order": "middle"},
    {"soc": {"addr": 1, "fc": "x"}},
    {"power": {"addr": 2, "positive_is": "sideways"}},
    {"setpoint": {"addr": 10}, "mode": {"addr": 11, "control_value": "on", "auto_value": 0}},
    {"setpoint": {"addr": 10}},
])
def test_a_register_map_that_makes_no_sense_is_refused(change):
    with pytest.raises(MapError):
        parse_map({"soc": {"addr": 1}, "power": {"addr": 2}, **change})


def test_an_entity_driven_battery_ignores_nonsense_and_sends_its_limits_first():
    assert [strategies.num(x) for x in ("5", "nan", "inf", "abc", "unknown", None)] == [5.0, None, None, None, None, None]
    extra = {"entities": {"reserve_soc": "input_number.r", "target_soc": "number.t", "set_power": "number.p", "mode": "select.m"},
             "mode_map": {m.value: m.value.title() for m in Mode}}
    caps = Capabilities(1000, 800, 800, 10)
    sent = strategies.plan(extra, caps, Desired(Mode.CHARGE, 5000, target_soc=90, reserve_soc=20))
    assert [(d, data.get("value", data.get("option"))) for d, _, data in sent] == [
        ("input_number", 20), ("number", 90), ("number", 800), ("select", "Charge")]
    with pytest.raises(ValueError):
        strategies.plan(extra, Capabilities(1000, 800, 800, 10, frozenset({Mode.AUTO})), Desired(Mode.CHARGE, 1))


def test_ecoflow_refusing_the_keys_is_noticed_and_forgotten_once_they_work():
    class Api:
        code = "8513"

        async def quota(self, sn):
            if self.code:
                err = EcoFlowError(f"EcoFlow error {self.code}: no")
                err.code = self.code
                raise err
            return {"soc": 50}

    cfg = DeviceConfig("e", "E", "ecoflow_stream_ultra_x", Capabilities(1920, 600, 800, 10), extra={"connection": {"sn": "SN1", "access_key": "a", "secret_key": "s"}})
    drv = EcoFlowStreamDriver(cfg, session=None)
    drv.api = Api()
    assert not asyncio.run(drv.read()).online and drv.auth_failed
    drv.api.code = None
    assert asyncio.run(drv.read()).online and not drv.auth_failed
    drv.api.code, drv._cache = "500", None
    assert not asyncio.run(drv.read()).online and not drv.auth_failed      # an ordinary failure is not about the keys
