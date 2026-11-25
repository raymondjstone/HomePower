"""Solar forecast against what came, through the real coordinator and Home Assistant's real Forecast.Solar energy platform:
the hourly forecast is fetched (as the Energy dashboard does), today's total and tomorrow's come from the Forecast.Solar
sensors, Solcast sensors installed alongside are NOT added on top (that would count the panels twice), the history is kept
and saved, and the Overview tile and the Insights comparison get what they need."""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

import asyncio, tempfile, datetime as dtm
from types import SimpleNamespace
import conftest  # noqa: F401
import _harness  # noqa: F401 - Python 3.12+ server shutdown (see _harness.py)
from test_drivers import FakeAnker
from homeassistant import loader
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.components.forecast_solar.coordinator import ForecastSolarDataUpdateCoordinator
from custom_components.homepower.manager import HomePowerManager
from custom_components.homepower.coordinator import HomePowerCoordinator


def entry_for(domain, entry_id):
    return ConfigEntry(data={}, disabled_by=None, discovery_keys={}, domain=domain, entry_id=entry_id, minor_version=1, options={},
                       pref_disable_new_entities=False, pref_disable_polling=False, source="user", subentries_data=[],
                       title=domain, unique_id=entry_id, version=1)


async def main():
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/London"))
    tmp = tempfile.mkdtemp()
    dev = FakeAnker(); srv = await asyncio.start_server(dev.handle, "127.0.0.1", 0); port = srv.sockets[0].getsockname()[1]
    hass = HomeAssistant(tmp); hass.config.config_dir = tmp
    if hasattr(loader, "async_setup"):
        loader.async_setup(hass)
    entry = entry_for("homepower", "e1")

    # Forecast.Solar, set up and loaded, with today's hourly estimate (the API keys each hour by its END, as the real one does)
    now = dt_util.now()
    today = now.date()
    tz = now.tzinfo
    ends = {dtm.datetime.combine(today, dtm.time(h), tz): (h - 9) * 400 if 10 <= h <= 13 else 0 for h in range(6, 18)}
    fs = entry_for("forecast_solar", "fs1")
    fs._async_set_state(hass, ConfigEntryState.LOADED, None)
    fs.runtime_data = ForecastSolarDataUpdateCoordinator.__new__(ForecastSolarDataUpdateCoordinator)
    fs.runtime_data.data = SimpleNamespace(wh_period=ends)
    entries = {"forecast_solar": [fs], "homepower": [entry]}
    hass.config_entries = SimpleNamespace(async_get_known_entry=lambda i: entry, async_update_entry=lambda e, **kw: None,
                                          async_entries=lambda d=None: entries.get(d, []),
                                          async_get_entry=lambda i: {"fs1": fs, "e1": entry}.get(i),
                                          async_wait_initialized=lambda: asyncio.sleep(0))
    from homeassistant.helpers import device_registry as dr
    for reg_mod in (dr, er):                       # newer releases set each registry up before it can load
        if hasattr(reg_mod, "async_setup"):
            reg_mod.async_setup(hass)
    await dr.async_load(hass)
    await er.async_load(hass)
    hass.services.async_register("persistent_notification", "create", lambda call: None)
    hass.services.async_register("persistent_notification", "dismiss", lambda call: None)

    # its sensors, and Solcast's too (installed alongside: must not be added on top)
    reg = er.async_get(hass)
    for platform, uid, eid, v in (("forecast_solar", "fs1_energy_production_today", "sensor.fs_today", "4.0"),
                                  ("forecast_solar", "fs1_energy_production_tomorrow", "sensor.fs_tomorrow", "11.5"),
                                  ("solcast_solar", "sc_forecast_today", "sensor.sc_today", "9.9"),
                                  ("solcast_solar", "sc_forecast_tomorrow", "sensor.sc_tomorrow", "9.9")):
        reg.async_get_or_create("sensor", platform, uid, suggested_object_id=eid.split(".")[1])
        hass.states.async_set(reg.async_get_entity_id("sensor", platform, uid), v, {"unit_of_measurement": "kWh"})

    mgr = HomePowerManager(hass); await mgr.async_load()
    coord = HomePowerCoordinator(hass, entry, mgr, *mgr.build()); mgr.coordinator = coord
    await mgr.save_device({"device_id": "a", "name": "A", "kind": "anker_e5000_pro", "grid_meter": "yes", "solar": "yes",
                           "connection": {"host": "127.0.0.1", "port": port}, "control_enabled": True})
    mgr.pv_seen.add("a")                                   # this battery has panels: its solar is measured
    await hass.async_block_till_done()                     # (saving the battery started a refresh, which fetches too)
    coord._solar_at, coord._solar_busy = -1e9, False
    await coord._refresh_solar_hours()
    hours = coord.solar_hours.get(today.isoformat())
    print("hourly   :", hours and [round(x, 2) for x in hours[8:15]], "| source", coord.forecast_source)
    assert hours and abs(sum(hours) - 4.0) < 1e-6 and hours[9] == 0.4 and hours[12] == 1.6   # 10:00-ended hour starts at 09:00

    coord.daily.days[today.isoformat()] = {"pv": 1.25, "export": 0, "import": 0, "import_cheap": 0, "import_known": 0}
    coord.usage.days[today.isoformat()] = {"imp": [0.0] * 48, "exp": [0.0] * 48, "load": [0.0] * 48, "pv": [0.0] * (2 * now.hour) + [0.3, 0.35] + [0.0] * (46 - 2 * now.hour)}   # this hour
    await coord._async_update_data()                       # the cycle notes the forecast history
    sv = coord.view()["solar_forecast"]
    print("overview :", sv)
    assert sv["today_kwh"] == 4.0 and sv["tomorrow_kwh"] == 11.5, "Forecast.Solar only: Solcast is not added on top"
    assert sv["so_far_kwh"] == 1.25 and sv["source"] == "forecast_solar" and sv["remaining_kwh"] is not None

    cmp = coord.solar_comparison(30)
    print("compare  :", cmp["days"][-1], "| hour 9:", cmp["hours"][9], "| this hour:", cmp["hours"][now.hour])
    assert cmp["days"][-1] == {"day": today.isoformat(), "forecast": 4.0, "actual": 1.25} and cmp["measured"]
    assert cmp["hours"][9]["forecast"] == 0.4 and cmp["hours"][now.hour]["actual"] == 0.65
    assert all(r["actual"] is None for r in cmp["hours"][now.hour + 1:])          # hours still to come: nothing measured yet

    # kept across a restart
    await mgr._save()
    mgr2 = HomePowerManager(hass); await mgr2.async_load()
    assert mgr2.solar_forecast_data[today.isoformat()]["first"] == 4.0, mgr2.solar_forecast_data
    print("saved    :", {d: v["first"] for d, v in mgr2.solar_forecast_data.items()})

    # nothing set up: no tile, and the comparison still answers (empty)
    for e in list(reg.entities.values()):
        reg.async_remove(e.entity_id)
    entries["forecast_solar"] = []
    coord._fc_at = coord._solar_at = -1e9
    coord.solar_hours = {}
    mgr.forecast_days.clear()
    await coord._refresh_solar_hours()
    assert coord.solar_view(dt_util.now()) is None and coord.forecast_source is None, coord.forecast_source
    print("none     :", coord.solar_view(dt_util.now()))
    await coord.async_close()
    srv.close(); await srv.wait_closed()
    print("E2E_SOLAR_FORECAST OK")

asyncio.run(main())
