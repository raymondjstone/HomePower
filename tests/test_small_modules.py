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
from hp.drivers import ecoflow_live
from hp.drivers.ecoflow_live import RETRY_AFTER_S, EcoFlowLive
from hp.drivers.modbus import ModbusError

from core import strategies
from core.alerts import AlertTracker
from core.modbus_map import MapError, parse_map
from core.models import Actual, Capabilities, Desired, DeviceConfig, Mode


class MqttError(Exception):
    pass


class Client:
    """Stands in for aiomqtt's client: records what is asked of it; the test feeds it messages or drops the connection."""

    refuse = 0                                    # connections still to refuse (broker down, wrong password...)

    def __init__(self, hostname, port, **kw):
        self.hostname, self.port, self.kw = hostname, port, kw
        self.subs, self.calls = [], []
        self.inbox = asyncio.Queue()

    async def __aenter__(self):
        if Client.refuse:
            Client.refuse -= 1
            raise MqttError("refused")
        self.calls.append("connect")
        return self

    async def _exit(self, *exc):
        self.calls.append("disconnect")

    async def __aexit__(self, *exc):
        await self._exit(*exc)

    async def subscribe(self, topics):
        self.subs += [t for t, _ in topics]

    @property
    def messages(self):
        return self._messages()

    async def _messages(self):
        while True:
            item = await self.inbox.get()
            if item is None:                       # the broker dropped the connection
                raise MqttError("connection lost")
            yield types.SimpleNamespace(topic=item[0], payload=item[1])


def aiomqtt(made):
    def client(*a, **kw):
        made.append(Client(*a, **kw))
        return made[-1]
    return types.SimpleNamespace(Client=client, MqttError=MqttError)


class Api:
    def __init__(self, error=None):
        self.error = error
        self.gate = None                           # an Event: certification() waits for it

    async def certification(self):
        if self.gate is not None:
            await self.gate.wait()
        if self.error:
            raise self.error
        return {"certificateAccount": "acct", "certificatePassword": "pw", "url": "broker", "port": "8883"}


def install(monkeypatch, module):
    Client.refuse = 0
    monkeypatch.setitem(sys.modules, "aiomqtt", module)


async def settle(cond, tries=300):
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return cond()


def test_live_feed_connects_subscribes_delivers_and_stops(monkeypatch):
    made, got = [], []
    install(monkeypatch, aiomqtt(made))

    async def run():
        live = EcoFlowLive(Api(), asyncio.get_running_loop())
        live.register("SN1", got.append)
        assert live._task is None                                   # off until switched on
        live.enabled = True
        live.ensure()
        assert await settle(lambda: live.connected)
        c = made[0]
        assert (c.hostname, c.port, c.kw["username"], c.kw["password"]) == ("broker", 8883, "acct", "pw")
        assert c.kw["tls_context"] is not None and live.error is None and live._client is c
        assert await settle(lambda: {"/open/acct/SN1/quota", "/open/acct/SN1/status"} <= set(c.subs))
        live.register("SN2", got.append)                            # added while connected: subscribed at once
        assert await settle(lambda: "/open/acct/SN2/status" in c.subs)
        for item in (("/open/acct/SN1/quota", json.dumps({"params": {"soc": 50}})), ("/open/acct/SN1/quota", b"not json"),
                     ("/open/acct/SN1/status", b"{}"), ("/open/acct/OTHER/quota", b'{"params": {"soc": 1}}')):
            c.inbox.put_nowait(item)
        assert await settle(lambda: c.inbox.empty())
        await asyncio.sleep(0.02)
        assert live.messages == 1 and got == [{"soc": 50}]
        live.unregister("SN1", lambda f: None)                      # not its owner: ignored
        assert "SN1" in live._devs
        live.unregister("SN1")
        live.unregister("SN2")                                      # the last one: stops
        assert live._client is None and not live.connected and live._task is None
        assert await settle(lambda: "disconnect" in c.calls)        # it said goodbye to the broker
        c.inbox.put_nowait(("/open/acct/SN1/quota", b'{"params": {"soc": 9}}'))
        await asyncio.sleep(0.05)
        assert got == [{"soc": 50}]                                 # nothing reaches the drivers after a stop

    asyncio.run(run())


def test_live_feed_reconnects_with_a_growing_wait(monkeypatch):
    made, waits = [], []
    install(monkeypatch, aiomqtt(made))
    real_sleep = asyncio.sleep

    async def fake_sleep(s, *a):
        waits.append(s)
        await real_sleep(0)
    monkeypatch.setattr(ecoflow_live.asyncio, "sleep", fake_sleep)

    async def run():
        live = EcoFlowLive(Api(), asyncio.get_running_loop())
        live.enabled = True
        Client.refuse = 6                                            # the broker refuses six times in a row
        live.register("SN1", print)
        for _ in range(500):
            if live.connected:
                break
            await real_sleep(0.005)
        assert live.connected
        assert waits[:6] == [5.0, 10.0, 20.0, 40.0, 80.0, 120.0]       # doubling, up to two minutes
        assert len(made) == 7 and live.error is None
        made[-1].inbox.put_nowait(None)                              # the connection drops
        for _ in range(500):
            if len(made) == 8 and live.connected:
                break
            await real_sleep(0.005)
        assert len(made) == 8 and live.connected
        assert waits[-1] == 5.0                                      # a connection that worked starts the wait again from 5 s
        for _ in range(500):
            if "/open/acct/SN1/quota" in made[-1].subs:
                break
            await real_sleep(0.005)
        assert "/open/acct/SN1/quota" in made[-1].subs               # subscribed again after the reconnect
        live.stop()

    asyncio.run(run())


def test_live_feed_failure_is_reported_and_retried_later(monkeypatch):
    install(monkeypatch, aiomqtt([]))

    async def run():
        live = EcoFlowLive(Api(RuntimeError("refused")), asyncio.get_running_loop())
        live.enabled = True
        live.register("SN1", print)
        await live._task
        assert "refused" in live.error and live._next_try > time.monotonic() + RETRY_AFTER_S - 5
        first = live._task
        live.ensure()
        assert live._task is first                                  # not again until the wait is over
        monkeypatch.setitem(sys.modules, "aiomqtt", None)           # the import fails: polling only
        live._next_try, live._task = 0.0, None
        await live._run()
        assert "not installed" in live.error

    asyncio.run(run())


def test_stopping_while_connecting_keeps_nothing_and_never_raises(monkeypatch):
    made = []
    install(monkeypatch, aiomqtt(made))

    async def run():
        api = Api()
        api.gate = asyncio.Event()
        live = EcoFlowLive(api, asyncio.get_running_loop())
        live.enabled = True
        live.register("SN1", print)
        await asyncio.sleep(0.02)                                   # waiting for EcoFlow's certificate
        task = live._task
        live.stop()                                                 # unload, or live updates switched off, meanwhile
        api.gate.set()
        await asyncio.sleep(0.05)
        assert task.cancelled() and made == [] and live._client is None

    asyncio.run(run())

    class Task:
        def done(self):
            return False

        def cancel(self):
            raise RuntimeError("Event loop is closed")

        def add_done_callback(self, fn):
            pass

    class ClosedLoop:                                               # Home Assistant stopping: the loop is gone
        def create_task(self, coro):
            coro.close()
            return Task()

    live = EcoFlowLive(Api(), ClosedLoop())
    live.enabled = True
    live.register("SN1", print)
    live.stop()                                                     # quietly does nothing
    assert live._task is None


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


def test_an_old_connection_closing_late_never_hides_the_new_one(monkeypatch):
    made = []
    install(monkeypatch, aiomqtt(made))

    async def run():
        live = EcoFlowLive(Api(), asyncio.get_running_loop())
        live.enabled = True
        live.register("SN1", print)
        assert await settle(lambda: live.connected)
        old = made[0]
        slow = asyncio.Event()

        async def slow_exit(*exc):                   # saying goodbye to a broker that is slow to answer
            await slow.wait()
            old.calls.append("disconnect")
        old._exit = slow_exit
        closing = live._task
        live.stop()
        await asyncio.sleep(0.02)
        assert closing in live._extra and not closing.done()  # held while it closes, so asyncio cannot drop it half way
        live.ensure()                                 # switched back on while the old one is still closing
        assert await settle(lambda: live.connected and len(made) == 2)
        slow.set()
        assert await settle(lambda: "disconnect" in old.calls)
        await asyncio.sleep(0.02)
        assert closing.done() and closing not in live._extra
        assert live.connected and live._client is made[1]       # the new connection is still the one in use
        live.stop()

    asyncio.run(run())


def test_a_stopped_connection_never_comes_back_even_if_closing_fails(monkeypatch):
    made = []
    install(monkeypatch, aiomqtt(made))

    async def run():
        live = EcoFlowLive(Api(), asyncio.get_running_loop())
        live.enabled = True
        live.register("SN1", print)
        assert await settle(lambda: live.connected)

        async def failing_exit(*exc):                 # the broker drops us while we say goodbye: aiomqtt raises instead
            made[0].calls.append("disconnect")
            raise MqttError("lost while disconnecting")
        made[0]._exit = failing_exit
        closing = live._task
        live.stop()
        assert await settle(lambda: closing.done())
        await asyncio.sleep(0.05)
        assert len(made) == 1 and live._task is None and not live.connected      # stays stopped: no second connection
        assert live.error is None                                                 # and no made-up problem shown

    asyncio.run(run())
