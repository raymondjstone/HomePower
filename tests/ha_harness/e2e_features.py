"""New features through the real coordinator: goal planner, backup, peak shaving, alerts, accounting, persistence."""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

import asyncio, sys, tempfile, datetime as dtm
from types import SimpleNamespace
import conftest  # noqa: F401
from test_drivers import FakeAnker
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util
import custom_components.homepower.coordinator as C
from custom_components.homepower.manager import HomePowerManager
from custom_components.homepower.coordinator import HomePowerCoordinator
from hp.drivers.modbus import to_i32


async def make(tmp, port, options=None):
    hass = HomeAssistant(tmp); hass.config.config_dir = tmp
    entry = ConfigEntry(data={}, disabled_by=None, discovery_keys={}, domain="homepower", entry_id="e1", minor_version=1,
                        options=options or {}, pref_disable_new_entities=False, pref_disable_polling=False, source="user",
                        subentries_data=[], title="HomePower", unique_id="homepower", version=1)
    mgr = HomePowerManager(hass); await mgr.async_load()
    coord = HomePowerCoordinator(hass, entry, mgr, *mgr.build()); mgr.coordinator = coord
    hass.config_entries = SimpleNamespace(async_get_known_entry=lambda i: entry)
    return hass, entry, mgr, coord


async def main():
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/London"))
    tmp = tempfile.mkdtemp()
    dev = FakeAnker(); srv = await asyncio.start_server(dev.handle, "127.0.0.1", 0); port = srv.sockets[0].getsockname()[1]
    hass, entry, mgr, coord = await make(tmp, port)
    sent = []
    hass.services.async_register("persistent_notification", "create", lambda call: sent.append(("create", call.data["notification_id"])))
    hass.services.async_register("persistent_notification", "dismiss", lambda call: sent.append(("dismiss", call.data["notification_id"])))
    sp = lambda: to_i32(dev.hold[10071], dev.hold[10072], "big")
    await mgr.save_device({"device_id": "a", "name": "A", "kind": "anker_e5000_pro", "connection": {"host": "127.0.0.1", "port": port}, "control_enabled": True})
    now = dt_util.now()
    hhmm = lambda d: d.strftime("%H:%M")

    # --- goal planner: cheap window covers now, deadline in 3 h, battery at 62% -> charge now
    coord.apply_settings({"price_peak": 30.0, "price_offpeak": 7.5, "offpeak_start": hhmm(now - dtm.timedelta(hours=1)), "offpeak_end": hhmm(now + dtm.timedelta(hours=2))})
    await mgr.save_goal({"id": "Morning", "target_soc": 90, "by": hhmm(now + dtm.timedelta(hours=3))})
    await coord._async_update_data()
    v = coord.view()["devices"]["a"]
    print("goal     :", v["desired_mode"], v["desired_power_w"], v["reason"], "| setpoint", sp(), "| plan slots", len(coord.last_result.allocs))
    assert v["reason"].startswith("goal") and v["desired_mode"] == "charge" and sp() < 0
    # same goal but the cheap window is later: it must wait, not charge now
    coord.apply_settings({"price_peak": 30.0, "price_offpeak": 7.5, "offpeak_start": hhmm(now + dtm.timedelta(hours=1)), "offpeak_end": hhmm(now + dtm.timedelta(hours=2, minutes=30))})
    coord._snap.devices["a"].last_applied = None
    await coord._async_update_data()
    v = coord.view()["devices"]["a"]
    print("goal wait:", v["desired_mode"], v["reason"], "| mode reg", dev.hold[10064])
    assert v["desired_mode"] != "charge" and dev.hold[10064] == 0
    await mgr.delete_goal("Morning")

    # --- backup mode: raise the reserve above the battery's 62% -> top up
    coord.apply_settings({"backup_reserve_pct": 80})
    await mgr.set_backup(True)
    coord._snap.devices["a"].last_applied = None
    await coord._async_update_data()
    v = coord.view()["devices"]["a"]
    print("backup   :", v["desired_mode"], v["reason"], "| setpoint", sp())
    assert v["reason"] == "backup reserve" and sp() < 0 and coord.view()["backup"] is True
    await mgr.set_backup(False)

    # --- peak shaving using the grid reading the battery itself reports (300 W import)
    coord.apply_settings({"import_limit_w": 250, "peak_shaving": True})
    coord._snap.devices["a"].last_applied = None
    await coord._async_update_data()
    v = coord.view()["devices"]["a"]
    print("peak     :", v["desired_mode"], v["desired_power_w"], v["reason"], "| setpoint", sp())
    assert v["reason"] == "peak shaving" and sp() > 0
    coord.apply_settings({"import_limit_w": 7000, "peak_shaving": False})
    coord._snap.devices["a"].last_applied = None
    await coord._async_update_data()
    assert dev.hold[10064] == 0 and not mgr.forced, "handed back after the peak"

    # --- accounting: a forced charge at a known price creates cost; energy counters move
    await mgr.save_schedule({"id": "N", "start": hhmm(now - dtm.timedelta(minutes=5)), "end": hhmm(now + dtm.timedelta(hours=1)), "mode": "charge", "power_w": 2000})
    coord.apply_settings({"price_peak": 30.0, "price_offpeak": None})
    for _ in range(3):
        coord.meter._last = {k: (t - 120, w) for k, (t, w) in coord.meter._last.items()}
        await coord._async_update_data()
    day = dt_util.now().strftime("%Y-%m-%d")
    print("ledger   :", coord.ledger.days.get(day), "| bat in kWh", round(coord.meter.get("bat:a_in"), 4))
    assert coord.ledger.days[day]["cost"] > 0 and coord.meter.get("bat:a_in") > 0 and coord.ledger.saved(day) < 0
    await mgr.delete_schedule("N")

    # --- persistence: energy + ledger + flags survive a restart
    await mgr.set_away(True)
    await coord.async_close()
    hass2, entry2, mgr2, coord2 = await make(tmp, port)
    print("restart  :", round(mgr2.energy.get("bat:a_in", 0), 4), "kWh in,", bool(mgr2.ledger_days), "ledger, away =", mgr2.away, "devices", list(mgr2.devices))
    assert mgr2.energy["bat:a_in"] > 0 and mgr2.ledger_days and mgr2.away and "a" in mgr2.devices and mgr2.goals == {}
    assert round(coord2.meter.get("bat:a_in"), 4) == round(coord.meter.get("bat:a_in"), 4)

    # --- alerts: the battery disappears -> raised (no debounce here) with a notification
    C.OFFLINE_ALERT_AFTER_S = 0
    coord2.adapters, coord2.schedules, coord2.goals = mgr2.build()
    coord2.set_config(coord2.adapters, [], [])
    hass2.services.async_register("persistent_notification", "create", lambda call: sent.append(("create", call.data["notification_id"])))
    srv.close(); await srv.wait_closed()
    await coord2._async_update_data()
    await asyncio.sleep(0.2)   # service calls are scheduled, not instant
    print("alerts   :", coord2.view()["alerts"], "| notifications", sent[-1:])
    assert "offline:a" in coord2.view()["alerts"] and ("create", "homepower_alert_offline:a") in sent
    await coord2.async_close()
    print("E2E4 OK")

asyncio.run(main())
