"""The newer features against real Home Assistant: safe mode, debug file, first-command confirmation, warnings on save,
notification buttons, weather/plan/report in the live view, and that everything new survives a save and a reload."""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

import asyncio, json, tempfile
from types import SimpleNamespace
import conftest  # noqa: F401
from test_drivers import FakeAnker
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util
from custom_components.homepower.manager import HomePowerManager
from custom_components.homepower.coordinator import HomePowerCoordinator, ACTION_HAND_BACK, ACTION_PAUSE
from custom_components.homepower import websocket_api as H


async def main():
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/London"))
    tmp = tempfile.mkdtemp(); hass = HomeAssistant(tmp); hass.config.config_dir = tmp
    dev = FakeAnker(); srv = await asyncio.start_server(dev.handle, "127.0.0.1", 0); port = srv.sockets[0].getsockname()[1]
    entry = ConfigEntry(data={}, disabled_by=None, discovery_keys={}, domain="homepower", entry_id="e1", minor_version=1, options={},
                        pref_disable_new_entities=False, pref_disable_polling=False, source="user", subentries_data=[],
                        title="HomePower", unique_id="homepower", version=1)
    mgr = HomePowerManager(hass); await mgr.async_load()
    coord = HomePowerCoordinator(hass, entry, mgr, *mgr.build()); mgr.coordinator = coord
    hass.data["homepower"] = {"manager": mgr}
    hass.config_entries = SimpleNamespace(async_get_known_entry=lambda i: entry, async_entries=lambda d: [entry],
                                          async_update_entry=lambda e, options=None, **kw: coord.apply_settings(options) or True)
    hass.data.setdefault("websocket_api", {})
    H.async_register(hass)
    cmds = hass.data["websocket_api"]
    out, n = {}, [0]

    class Conn:
        user = SimpleNamespace(is_admin=True)
        def __init__(self): self.subscriptions = {}
        def send_result(self, i, result=None): out["r"] = ("ok", json.loads(json.dumps(result)))     # must be JSON serialisable
        def send_error(self, i, code, msg): out["r"] = ("err", code, msg)
        def send_message(self, m): pass
        def async_handle_exception(self, msg, err): out["r"] = ("exc", repr(err))
    conn = Conn()

    async def call(type_, **kw):
        n[0] += 1
        msg = {"id": n[0], "type": type_, **kw}
        handler, schema = cmds[type_]
        out.pop("r", None)
        handler(hass, conn, msg if schema is False else schema(msg))
        for _ in range(300):
            if "r" in out: break
            await asyncio.sleep(0.05)
        return out.get("r")

    async def must(type_, **kw):
        r = await call(type_, **kw)
        assert r and r[0] == "ok", (type_, r)
        return r[1]

    cfg = {"host": "127.0.0.1", "port": port}
    await must("homepower/device/save", device={"device_id": "a", "name": "A", "kind": "anker_e5000_pro", "connection": cfg})
    await must("homepower/device/control", device_id="a", enabled=True)
    await coord.async_refresh()

    # warnings come back with a saved schedule / goal, and the schedule is saved either way
    s = await must("homepower/schedule/save", schedule={"id": "low", "start": "23:30", "end": "07:30", "mode": "charge", "power_w": 2000, "target_soc": 5})
    assert s["id"] == "low" and any("minimum charge" in w for w in s["warnings"]), s
    g = await must("homepower/goal/save", goal={"id": "G", "target_soc": 90, "by": "07:00", "days": [0, 1, 2, 3, 4, 5, 6]})
    assert g["warnings"] == [], g
    print("warnings :", s["warnings"])

    # the live view carries the new sections and is JSON-safe
    live = json.loads(json.dumps(coord.view()))
    for k in ("plan_actual", "weather", "startup", "health"):
        assert k in live, k
    assert "a" in live["plan_actual"] and len(live["plan_actual"]["a"]["rows"]) == 48
    assert "report" in live["devices"]["a"] and "why" in live["devices"]["a"] and "setting_changes" in live["devices"]["a"]
    assert coord.startup_note and "Started" in coord.startup_note
    print("view     :", coord.startup_note)

    # debug file: JSON-safe, no keys or addresses
    dbg = await must("homepower/debug")
    text = json.dumps(dbg)
    assert "127.0.0.1" not in text and "batteries" in dbg and "plan_against_actual" in dbg and "host" not in dbg["batteries"]["a"]["connection"]
    print("debug    : ok,", len(text), "bytes")

    # safe mode: hands back, pauses, and leaving it resumes
    dev.regs = getattr(dev, "regs", {})
    st = coord._snap.devices["a"]
    st.release_pending = True
    r = await must("homepower/safe", enabled=True)
    assert mgr.safe and mgr.paused, (mgr.safe, mgr.paused)
    assert (await must("homepower/get"))["safe"] is True if "homepower/get" in cmds else True
    await must("homepower/pause", paused=True)                       # pausing while safe keeps it safe
    assert mgr.safe
    await must("homepower/safe", enabled=False)
    assert not mgr.safe and not mgr.paused
    # already paused, then safe mode: must still hand back
    await mgr.set_paused(True)
    st.release_pending = True
    handed = []
    orig = coord.adapters[0].apply

    async def spy(want):
        handed.append(want.mode.value)
        return await orig(want)
    coord.adapters[0].apply = spy
    await must("homepower/safe", enabled=True)
    assert "auto" in handed, handed
    coord.adapters[0].apply = orig
    await must("homepower/safe", enabled=False)
    print("safe mode: ok (hand-back happened while already paused:", handed, ")")

    # notification buttons through the real event bus
    hass.bus.async_fire("mobile_app_notification_action", {"action": ACTION_PAUSE})
    for _ in range(40):
        await asyncio.sleep(0.05)
        if mgr.paused: break
    assert mgr.paused
    await mgr.set_paused(False)
    st.release_pending = True
    hass.bus.async_fire("mobile_app_notification_action", {"action": ACTION_HAND_BACK})
    for _ in range(60):
        await asyncio.sleep(0.05)
        if mgr.paused: break
    assert mgr.paused and mgr.safe is False
    await mgr.set_paused(False)
    print("buttons  : ok")

    # first-command confirmation endpoint
    await must("homepower/device/confirm", device_id="a")
    assert "a" in mgr.confirmed
    bad = await call("homepower/device/confirm", device_id="nope")
    assert bad[0] == "err", bad

    # the local port scan: works on a local address, refuses a public one, and is JSON-safe
    scan = await must("homepower/scan_ports", host="127.0.0.1")
    from custom_components.homepower.drivers.portscan import PORTS
    assert [r["port"] for r in scan["results"]] == sorted(PORTS) and all(r["state"] in ("open", "closed", "no answer") for r in scan["results"]), scan
    refused = await call("homepower/scan_ports", host="8.8.8.8")
    assert refused[0] == "err" and "own network" in refused[2], refused
    refused = await call("homepower/scan_ports", host="http://192.168.1.1/x")
    assert refused[0] == "err", refused
    import custom_components.homepower.drivers.portscan as PS
    PS.WIDE_PORTS = range(1, 300)                              # a small range keeps this check quick
    wide = await must("homepower/scan_ports", host="127.0.0.1", wide=True)
    assert wide["wide"] is True and wide["total"] == 299 and wide["complete"] and sum(wide["counts"].values()) == 299, wide
    assert all(r["state"] == "open" for r in wide["results"]) and len(wide["results"]) == wide["counts"]["open"]
    refused = await call("homepower/scan_ports", host="8.8.8.8", wide=True)
    assert refused[0] == "err" and "own network" in refused[2], refused
    assert PS._wide_running is False
    print("ports    : local scan ok,", sum(r["state"] == "open" for r in scan["results"]), "open here; public and malformed addresses refused")

    # new state is saved and comes back after a reload
    coord.traces.start("a", "charge", 0, "t", 40, 0); coord.traces.observe("a", 800, 40, 0, None)
    coord.rate_learner.rates["a"] = 2600
    coord.temp_log.add("2026-03-01", 4.0)
    coord.plan_log.add(dt_util.now(), "a", 1000.0, 900.0)
    await mgr.save_runtime()
    mgr2 = HomePowerManager(hass); await mgr2.async_load()
    assert mgr2.confirmed == {"a"} and mgr2.charge_rates == {"a": 2600}
    assert mgr2.trace_data["a"][0]["ok"] is False and mgr2.weather_data["temps"]["2026-03-01"] == [4.0, 1]
    assert mgr2.plan_log_data
    coord2 = HomePowerCoordinator(hass, entry, mgr2, *mgr2.build())
    assert coord2.traces.results["a"] and coord2.rate_learner.rates == {"a": 2600} and coord2.temp_log.days
    print("persist  : ok")

    # settings: the new keys are accepted and applied
    r = await must("homepower/settings/save", settings={"weather_boost": True, "weather_entity": "weather.home", "hold_for_dearer": True,
                                                         "weather_online": False, "temp_sensor_entity": "sensor.out"}) if "homepower/settings/save" in cmds else None
    if r is not None:
        assert coord.settings.weather_boost and coord.settings.weather_entity == "weather.home" and coord.settings.engine.hold_for_dearer
        print("settings : ok")
    await coord.async_close()
    print("E2E7 OK")


asyncio.run(main())
