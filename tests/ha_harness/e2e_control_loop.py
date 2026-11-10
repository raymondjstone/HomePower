"""End-to-end: real HomeAssistant core object + real coordinator/manager + simulated Anker battery."""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

import asyncio, sys, tempfile, pathlib
from unittest.mock import patch
import conftest  # noqa: F401
from test_drivers import FakeAnker
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util
from custom_components.homepower.manager import HomePowerManager
from custom_components.homepower.coordinator import HomePowerCoordinator
from custom_components.homepower.core.models import Mode
from hp.drivers.modbus import to_i32


async def main():
    tmp = tempfile.mkdtemp()
    hass = HomeAssistant(tmp)
    hass.config.config_dir = tmp
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/London"))
    from homeassistant.helpers import storage
    dev = FakeAnker()
    srv = await asyncio.start_server(dev.handle, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    entry = ConfigEntry(data={}, disabled_by=None, discovery_keys={}, domain="homepower", entry_id="e1", minor_version=1,
                        options={"import_limit_w": 7000}, pref_disable_new_entities=False, pref_disable_polling=False,
                        source="user", subentries_data=[], title="HomePower", unique_id="homepower", version=1)
    mgr = HomePowerManager(hass)
    await mgr.async_load()
    adapters, schedules, goals = mgr.build()
    coord = HomePowerCoordinator(hass, entry, mgr, adapters, schedules, goals)
    mgr.coordinator = coord

    # add the battery through the real manager (autofills capacity & limits from the simulated device)
    d = await mgr.save_device({"device_id": "garage", "name": "Garage", "kind": "anker_e5000_pro",
                               "connection": {"host": "127.0.0.1", "port": port}})
    print("saved    :", {k: d[k] for k in ("capacity_wh", "max_charge_w", "max_discharge_w", "min_soc", "control_enabled")})
    assert d["capacity_wh"] == 10000 and d["control_enabled"] is False

    # monitor-only: a schedule exists but nothing may be written
    now = dt_util.now()
    start, end = (now - __import__("datetime").timedelta(minutes=5)).strftime("%H:%M"), (now + __import__("datetime").timedelta(hours=1)).strftime("%H:%M")
    await mgr.save_schedule({"id": "Night", "start": start, "end": end, "mode": "charge", "power_w": 2000, "target_soc": 100})
    await coord._async_update_data()
    v = coord.view()["devices"]["garage"]
    print("monitor  :", v["desired_mode"], v["reason"], "| decision", v["decision"], "| writes", dev.writes)
    assert dev.writes == [] and v["desired_mode"] == "charge" and v["power_w"] == 1500

    # allow control: the schedule is applied and verified
    await mgr.save_device({**mgr.devices["garage"], "control_enabled": True})
    await coord._async_update_data()
    sp = to_i32(dev.hold[10071], dev.hold[10072], "big")
    print("control  :", "mode reg", dev.hold[10064], "setpoint", sp, "| forced", sorted(mgr.forced))
    assert dev.hold[10064] == 3 and sp == -2000 and "garage" in mgr.forced

    # schedule removed -> the engine hands the battery back
    await mgr.delete_schedule("Night")
    coord._snap.devices["garage"].last_apply = None
    await coord._async_update_data()
    print("handback :", "mode reg", dev.hold[10064], "setpoint", to_i32(dev.hold[10071], dev.hold[10072], "big"), "| forced", sorted(mgr.forced))
    assert dev.hold[10064] == 0 and not mgr.forced

    # manual override, then pause
    coord.set_override(["garage"], __import__("hp.core.models", fromlist=["Desired"]).Desired(Mode.DISCHARGE, 900), 30)
    await coord._async_update_data()
    assert to_i32(dev.hold[10071], dev.hold[10072], "big") == 900
    await mgr.set_paused(True)
    coord.clear_override()
    coord._snap.devices["garage"].last_apply = None
    before = len(dev.writes)
    await coord._async_update_data()
    assert len(dev.writes) == before, "paused: nothing may be written"
    await mgr.set_paused(False)
    print("override/pause OK")

    # diagnostics, preview, test_control, energy, savings views
    diag = coord.diagnostics()["garage"]
    print("diag     :", diag["driver"], diag["online"], diag["word_order"], diag["connection"])
    pv = coord.preview_view(24)
    print("preview  :", list(pv), len(pv["conflicts"]))
    res = await coord.test_control("garage")
    print("test     :", res["ok"], [(s["name"], s["ok"]) for s in res["steps"]])
    assert res["ok"], res
    print("view keys:", sorted(coord.view())[:8], "...")
    await coord.async_close()
    srv.close(); await srv.wait_closed()
    print("E2E OK")

asyncio.run(main())
