"""Regression: adding/removing batteries while a control cycle is mid-flight must not break the cycle."""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

import asyncio, sys, tempfile
from types import SimpleNamespace
import conftest  # noqa: F401
from test_drivers import FakeAnker
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util
from custom_components.homepower.manager import HomePowerManager
from custom_components.homepower.coordinator import HomePowerCoordinator
from custom_components.homepower.options_flow import HomePowerOptionsFlow


async def main():
    tmp = tempfile.mkdtemp(); hass = HomeAssistant(tmp); hass.config.config_dir = tmp
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/London"))
    dev = FakeAnker(); srv = await asyncio.start_server(dev.handle, "127.0.0.1", 0); port = srv.sockets[0].getsockname()[1]
    entry = ConfigEntry(data={}, disabled_by=None, discovery_keys={}, domain="homepower", entry_id="e1", minor_version=1, options={},
                        pref_disable_new_entities=False, pref_disable_polling=False, source="user", subentries_data=[],
                        title="HomePower", unique_id="homepower", version=1)
    mgr = HomePowerManager(hass); await mgr.async_load()
    coord = HomePowerCoordinator(hass, entry, mgr, *mgr.build()); mgr.coordinator = coord
    hass.data["homepower"] = {"manager": mgr}
    hass.config_entries = SimpleNamespace(async_get_known_entry=lambda i: entry)
    conn = {"host": "127.0.0.1", "port": port}
    await mgr.save_device({"device_id": "a", "name": "A", "kind": "anker_e5000_pro", "connection": conn})
    for i in range(6):
        # a cycle reading the batteries while the user adds one (and later removes it)
        cyc = asyncio.create_task(coord._async_update_data())
        await asyncio.sleep(0)
        await mgr.save_device({"device_id": f"n{i}", "name": f"N{i}", "kind": "anker_e5000_pro", "connection": conn})
        await cyc
        cyc = asyncio.create_task(coord._async_update_data())
        await asyncio.sleep(0)
        await mgr.delete_device(f"n{i}")
        await cyc
    await coord._async_update_data()
    assert list(mgr.devices) == ["a"], mgr.devices
    # Configure flow: the same name twice must not overwrite
    def flow():
        f = HomePowerOptionsFlow(); f.hass = hass; f.flow_id, f.handler = "x", "e1"; return f
    ids = []
    for _ in range(2):
        f = flow(); await f.async_step_add_battery({"name": "Twin", "kind": "anker_e5000_pro"})
        f._d["connection"] = conn; f._info = await mgr.probe({"kind": "anker_e5000_pro", "connection": conn})
        r = await f.async_step_battery_details(f._info and __import__("voluptuous").Schema({})({}) or {})
        assert r["type"] == "create_entry", r
    print("devices after two 'Twin' adds:", sorted(mgr.devices))
    assert {"twin", "twin_2"} <= set(mgr.devices)
    # revoking control (or deleting) a battery we are forcing hands it back first
    from custom_components.homepower.core.models import Desired, Mode
    from hp.drivers.modbus import to_i32
    await mgr.save_device({**mgr.devices["a"], "control_enabled": True})
    coord.set_override(["a"], Desired(Mode.CHARGE, 1500), 30)
    await coord._async_update_data()
    assert dev.hold[10064] == 3 and to_i32(dev.hold[10071], dev.hold[10072], "big") == -1500 and "a" in mgr.forced
    await mgr.save_device({**mgr.devices["a"], "control_enabled": False})
    print("after revoking control: mode reg", dev.hold[10064], "forced", sorted(mgr.forced))
    assert dev.hold[10064] == 0 and "a" not in mgr.forced
    await mgr.save_device({**mgr.devices["a"], "control_enabled": True})
    coord._snap.devices["a"].last_applied = None
    await coord._async_update_data()
    assert dev.hold[10064] == 3 and "a" in mgr.forced
    await mgr.delete_device("a")
    print("after deleting: mode reg", dev.hold[10064], "forced", sorted(mgr.forced))
    assert dev.hold[10064] == 0 and "a" not in mgr.forced
    # a bug in the decision engine must not raise, send commands, or take the integration down
    import custom_components.homepower.coordinator as C
    real = C.compute
    C.compute = lambda w: (_ for _ in ()).throw(RuntimeError("boom"))
    before = dev.writes[:]
    snap = await coord._async_update_data()
    C.compute = real
    assert dev.writes == before and any("internal error" in n for n in coord.view()["notes"])
    print("engine failure handled:", coord.view()["notes"][0][:60])
    await coord.async_close(); srv.close(); await srv.wait_closed()
    print("E2E3 OK")

asyncio.run(main())
