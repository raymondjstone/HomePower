"""The control loop when things go wrong around it: failing reads, reports and saves, test runs, Test control and hand-back."""
import _home  # noqa: F401 - sets up the paths first
import asyncio
from datetime import timedelta
from types import SimpleNamespace
from homeassistant.util import dt as dt_util
from custom_components.homepower import coordinator as C
from custom_components.homepower.core.models import Actual, Desired, Mode
from custom_components.homepower.drivers.modbus import ModbusError


def boom(*a, **kw):
    raise RuntimeError("boom")


async def aboom(*a, **kw):
    raise RuntimeError("boom")


async def loop():
    h = await _home.Home().start()
    coord, mgr = h.coord, h.mgr
    adapter = coord.adapters[0]

    # a read that raises is only an offline reading; an override runs out
    real_read, adapter.read = adapter.read, aboom
    assert not (await coord._read(adapter)).online
    adapter.read = real_read
    coord.set_override(["a"], Desired(Mode.HOLD), 1)
    coord.overrides["a"] = (coord.overrides["a"][0], dt_util.now() - timedelta(minutes=1))
    assert coord._active_overrides(dt_util.now()) == {}

    # nothing secondary can stop the loop
    names = ("_account", "_check_alerts", "_learn_rates", "_record_plan", "_note_solar", "_note_decision", "_startup_check", "_day_plan")
    saved = {n: getattr(coord, n) for n in names if hasattr(coord, n)}
    for n in saved:
        setattr(coord, n, boom)
    real_save, mgr.save_runtime = mgr.save_runtime, aboom
    coord._started, coord._last_persist = False, -1e9
    coord.set_override(["a"], Desired(Mode.HOLD), 5)
    await coord.async_refresh()
    assert coord.last_update_success
    for n, fn in saved.items():
        setattr(coord, n, fn)
    mgr.save_runtime = real_save

    # a command that cannot be sent is recorded, and the keep-alive failing is ignored
    real_apply, adapter.apply = adapter.apply, aboom
    coord.clear_override(None)
    coord.set_override(["a"], Desired(Mode.CHARGE, 500), 5)
    await coord.async_refresh()
    assert coord._snap.devices["a"].last_error
    st = coord._snap.devices["a"]
    st.release_pending, st.last_keepalive = True, -1e9
    adapter.config.extra["watchdog"] = True
    adapter.keepalive = aboom
    await coord._keepalive(1e6, [adapter])

    # a test run sends nothing
    sent = []

    async def record(want):
        sent.append(want.mode)

    adapter.apply = record
    coord.apply_settings({"dry_run": True})
    coord.clear_override(None)
    coord.set_override(["a"], Desired(Mode.DISCHARGE, 500), 5)
    st.last_applied = None
    await coord.async_refresh()
    assert Mode.DISCHARGE not in sent and st.last_command["result"] == "dry run: not sent", (sent, st.last_command)
    coord.apply_settings({})

    # hand-back: one battery that will not answer stays marked, and is retried
    adapter.apply = aboom
    st.release_pending = True
    await coord.release_all(force=True)
    assert "a" in coord._retry_handback and st.release_pending
    lock = coord._send_lock()
    await lock.acquire()
    real_limit, C.HANDBACK_TIMEOUT_S = C.HANDBACK_TIMEOUT_S, 0.05
    adapter.apply = record
    await coord.release_all(force=True, closing=True)                # a command stuck going out: hand back anyway
    assert not st.release_pending
    C.HANDBACK_TIMEOUT_S = real_limit
    lock.release()
    await mgr.set_paused(True)
    await coord.release_all()                                         # paused: nothing is touched, totals are still saved
    await mgr.set_paused(False)
    adapter.apply = real_apply
    print("loop     : survives failing parts; test run and hand-back")
    await h.stop()


async def test_control():
    h = await _home.Home().start()
    coord = h.coord
    coord._closing = False
    adapter = coord.adapters[0]

    async def no_wait(s):
        return None

    real_sleep, C.asyncio.sleep = C.asyncio.sleep, no_wait

    def results(r):
        return {s["name"]: s["ok"] for s in r["steps"]}

    assert (await coord.test_control("nope"))["steps"][0]["name"] == "find battery"
    coord.testing.add("a")
    assert (await coord.test_control("a"))["steps"][0]["name"] == "already testing"
    coord.testing.discard("a")
    await h.mgr.set_safe(True)
    assert (await coord.test_control("a"))["steps"][0]["name"] == "safe mode"
    await h.mgr.set_safe(False)

    battery = {"soc": 50.0, "mode": Mode.AUTO, "power": 0.0, "online": True, "follow": True, "step": 800.0}

    async def read():
        return Actual(online=battery["online"], soc=battery["soc"], mode=battery["mode"], power_w=battery["power"])

    async def apply(want):
        if battery["follow"]:
            battery["mode"] = want.mode
            sign = {Mode.CHARGE: 1, Mode.DISCHARGE: -1}.get(want.mode, 0)
            battery["power"] = sign * battery["step"]

    adapter.read, adapter.apply = read, apply
    battery["online"] = False
    assert results(await coord.test_control("a"))["battery readable"] is False
    battery["online"] = True
    adapter.config.extra["control_enabled"] = False
    assert results(await coord.test_control("a"))["control allowed"] is False
    adapter.config.extra["control_enabled"] = True
    battery["soc"] = 99.0                                             # full: tested by discharging
    assert any(n.startswith("sent discharge") for n in results(await coord.test_control("a")))
    battery["soc"] = 12.0
    caps = adapter.config.caps
    import dataclasses
    adapter.config.caps = dataclasses.replace(caps, supported_modes=frozenset({Mode.AUTO, Mode.HOLD}))
    assert "sent hold" in results(await coord.test_control("a"))
    adapter.config.caps, battery["soc"] = caps, 50.0

    # the local EcoFlow driver also checks which way the power moved
    adapter.config.extra["driver"] = "ecoflow_modbus"
    seen = []
    for step in (800.0, -800.0, 5.0, None):
        battery["power"], battery["mode"] = 0.0, Mode.AUTO
        battery["step"] = step if step is not None else 0.0

        async def read_step(step=step):
            a = await read()
            return dataclasses.replace(a, power_w=None) if step is None else a

        adapter.read = read_step
        r = await coord.test_control("a")
        seen.append(next(s for s in r["steps"] if s["name"] == "power moves the right way"))
    assert [s["ok"] for s in seen] == [True, False, True, True] and "reversed" in seen[1]["detail"], seen
    adapter.read = read

    async def fails(want):
        raise RuntimeError("refused")

    adapter.apply = fails
    assert results(await coord.test_control("a"))["error"] is False
    C.asyncio.sleep = real_sleep
    adapter.apply = apply
    print("test ctl : every refusal and outcome")
    await h.stop()


async def x1_limits_and_removal():
    h = await _home.Home().start()
    coord = h.coord
    adapter = coord.adapters[0]
    adapter.config.extra["house_meter"] = True
    calls = []

    async def limits(import_w, export_w, mine, orig):
        calls.append((import_w, export_w))
        if calls[-1] == (None, None) and len(calls) > 2:
            raise ModbusError("no answer")
        mine["export"] = 500
        return ["export limit 500 W"]

    adapter.driver.set_grid_limits = limits
    coord.apply_settings({"x1_grid_limits": True, "export_limit_w": 500, "dry_run": True})
    coord._x1_at = -1e9
    await coord._sync_x1_limits(1e9, [adapter])
    assert calls == []                                                # test run: nothing new is written
    coord.apply_settings({"x1_grid_limits": True, "export_limit_w": 500})
    coord._snap.devices["a"].actual = Actual(online=True, soc=50)
    coord._x1_at = -1e9
    await coord._sync_x1_limits(2e9, [adapter])
    assert calls and calls[-1][1] == 500, calls
    calls.extend([(0, 0), (0, 0)])
    await coord.release_x1_limits("a")                                # being deleted while the X1 does not answer: the user is told
    assert not [k for k in coord._event_state if k.startswith("x1lim:a:")]
    await coord.release_x1_limits("a")                                # nothing of HomePower's left in it
    coord.set_config([], coord.schedules, coord.goals)                # the battery removed: what was kept about it goes
    assert "a" not in coord._snap.devices
    print("x1       : limits written, and taken off on removal")
    await h.stop()


async def main():
    await loop()
    await test_control()
    await x1_limits_and_removal()
    print("E2E LOOP OK")


asyncio.run(main())
