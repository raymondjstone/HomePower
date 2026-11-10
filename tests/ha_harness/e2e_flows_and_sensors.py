"""Configure flow steps (goal, settings, Modbus map) + new sensors, against real HA classes."""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

import asyncio, sys, tempfile, json
from types import SimpleNamespace
import conftest  # noqa: F401
from test_drivers import FakeAnker, ANKER_AS_MAP
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util
from custom_components.homepower.manager import HomePowerManager
from custom_components.homepower.coordinator import HomePowerCoordinator
from custom_components.homepower.options_flow import HomePowerOptionsFlow
from custom_components.homepower import sensor as S, switch as W


async def main():
    tmp = tempfile.mkdtemp()
    hass = HomeAssistant(tmp)
    hass.config.config_dir = tmp
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/London"))
    dev = FakeAnker()
    srv = await asyncio.start_server(dev.handle, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    entry = ConfigEntry(data={}, disabled_by=None, discovery_keys={}, domain="homepower", entry_id="e1", minor_version=1,
                        options={}, pref_disable_new_entities=False, pref_disable_polling=False, source="user",
                        subentries_data=[], title="HomePower", unique_id="homepower", version=1)
    mgr = HomePowerManager(hass)
    await mgr.async_load()
    coord = HomePowerCoordinator(hass, entry, mgr, *mgr.build())
    mgr.coordinator = coord
    hass.data["homepower"] = {"manager": mgr}
    hass.config_entries = SimpleNamespace(async_get_known_entry=lambda i: entry)
    await mgr.save_device({"device_id": "a", "name": "A", "kind": "anker_e5000_pro", "connection": {"host": "127.0.0.1", "port": port}, "control_enabled": True})

    def flow():
        f = HomePowerOptionsFlow(); f.hass = hass; f.flow_id, f.handler = "x", "e1"; return f

    # menu shows goal + settings entries
    r = await flow().async_step_init()
    print("menu     :", r["menu_options"])
    assert {"add_goal", "settings", "remove_battery"} <= set(r["menu_options"])

    # goal
    f = flow(); r = await f.async_step_add_goal()
    v = r["data_schema"]({"name": "Morning", "target_soc": 90, "by": "07:00:00"})
    r = await f.async_step_add_goal(v)
    print("goal     :", r["type"], list(mgr.goals), mgr.goals["Morning"]["by"], mgr.goals["Morning"]["days"])
    assert r["type"] == "create_entry" and mgr.goals["Morning"]["target_soc"] == 90

    # settings
    f = flow(); r = await f.async_step_settings()
    v = r["data_schema"]({"import_limit_w": 6000, "peak_shaving": True, "price_peak": 28.5, "price_offpeak": 7.5,
                          "offpeak_start": "23:30:00", "offpeak_end": "05:30:00", "backup_reserve_pct": 40})
    r = await f.async_step_settings(v)
    print("settings :", r["type"], {k: r["data"][k] for k in ("import_limit_w", "peak_shaving", "offpeak_start", "backup_reserve_pct")})
    assert r["type"] == "create_entry" and r["data"]["offpeak_start"] == "23:30" and r["data"]["peak_shaving"] is True
    coord.apply_settings({**entry.data, **r["data"]})
    assert coord.settings.engine.import_limit_w == 6000 and coord.settings.engine.peak_shaving

    # generic Modbus map through the flow
    f = flow(); r = await f.async_step_add_battery({"name": "Mapped", "kind": "modbus_map"})
    assert r["step_id"] == "modbus_map", r
    v = r["data_schema"]({"host": "127.0.0.1", "port": port, "map": json.dumps(ANKER_AS_MAP)})
    r = await f.async_step_modbus_map(v)
    assert r["step_id"] == "battery_details", r
    print("map flow :", r["description_placeholders"]["readings"])
    r = await f.async_step_battery_details(r["data_schema"]({"capacity_wh": 8000, "max_charge_w": 3000, "max_discharge_w": 3000}))
    print("map saved:", r["type"], mgr.devices["mapped"]["supported_modes"], mgr.devices["mapped"]["max_charge_w"])
    assert r["type"] == "create_entry"

    # a few control cycles so energy/savings accumulate
    await coord._async_update_data()
    await asyncio.sleep(0.2)
    coord.meter._last = {k: (t - 60, w) for k, (t, w) in coord.meter._last.items()}   # pretend a minute passed
    await coord._async_update_data()

    # sensors read live data
    vals = {
        "battery_power": S.BatteryFlow(coord, entry).native_value,
        "stored_kwh": S.StoredEnergy(coord, entry).native_value,
        "grid": S.GridPower(coord, entry).native_value,
        "bat_in_kwh": S.EnergyTotal(coord, entry, "bat:a_in", "x").native_value,
        "savings": S.Savings(coord, entry, "today").native_value,
        "price": S.PriceNow(coord, entry).native_value,
        "soc": S.BatterySoc(coord, entry, "a", "A").native_value,
        "eff": S.Efficiency(coord, entry, "a", "A").native_value,
        "cycles": S.Cycles(coord, entry, "a", "A", 10000).native_value,
        "target": S.BatteryTarget(coord, entry, "a", "A").native_value,
    }
    print("sensors  :", vals)
    assert vals["soc"] == 62 and vals["bat_in_kwh"] > 0 and vals["stored_kwh"] > 0 and vals["target"] in ("unmanaged", "auto", "charge", "discharge", "hold")
    sw = {s[0]: W.HomePowerSwitch(coord, entry, *s).is_on for s in W.SWITCHES}
    await W.HomePowerSwitch(coord, entry, *W.SWITCHES[1]).async_turn_on()
    print("switches :", sw, "-> backup now", mgr.backup)
    assert mgr.backup is True
    v = coord.view()
    print("view     :", {k: v[k] for k in ("backup", "away", "paused", "price_now", "savings", "alerts")})
    await coord.async_close()
    srv.close(); await srv.wait_closed()
    print("E2E2 OK")

asyncio.run(main())
