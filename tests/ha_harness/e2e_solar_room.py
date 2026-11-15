"""Room for solar through the real coordinator: forecast found, sunrise/sunset from the home location, ceilings shown,
grid charging capped, and the setup check asking for a forecast when there is none."""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

import asyncio, tempfile, datetime as dtm
from types import SimpleNamespace
import conftest  # noqa: F401
from test_drivers import FakeAnker
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util
from custom_components.homepower.manager import HomePowerManager
from custom_components.homepower.coordinator import HomePowerCoordinator


async def main():
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Dublin"))
    tmp = tempfile.mkdtemp()
    dev = FakeAnker(); srv = await asyncio.start_server(dev.handle, "127.0.0.1", 0); port = srv.sockets[0].getsockname()[1]
    hass = HomeAssistant(tmp); hass.config.config_dir = tmp
    hass.config.latitude, hass.config.longitude, hass.config.elevation = 53.35, -6.26, 10     # Dublin
    entry = ConfigEntry(data={}, disabled_by=None, discovery_keys={}, domain="homepower", entry_id="e1", minor_version=1, options={},
                        pref_disable_new_entities=False, pref_disable_polling=False, source="user", subentries_data=[],
                        title="HomePower", unique_id="homepower", version=1)
    mgr = HomePowerManager(hass); await mgr.async_load()
    coord = HomePowerCoordinator(hass, entry, mgr, *mgr.build()); mgr.coordinator = coord
    hass.config_entries = SimpleNamespace(async_get_known_entry=lambda i: entry, async_update_entry=lambda e, **kw: None)
    hass.services.async_register("persistent_notification", "create", lambda call: None)
    hass.services.async_register("persistent_notification", "dismiss", lambda call: None)

    await mgr.save_device({"device_id": "a", "name": "A", "kind": "anker_e5000_pro", "grid_meter": "yes", "solar": "yes",
                           "connection": {"host": "127.0.0.1", "port": port}, "control_enabled": True})
    now = dt_util.now()
    hhmm = lambda d: d.strftime("%H:%M")
    coord.apply_settings({"price_peak": 30.0, "price_offpeak": 14.0, "base_load_w": 300,
                          "offpeak_start": hhmm(now - dtm.timedelta(hours=1)), "offpeak_end": hhmm(now + dtm.timedelta(hours=1))})
    await coord._async_update_data()

    # --- no forecast: solar counts as zero, nothing capped, and the setup check asks for one
    chk = {i["key"]: i for i in coord.setup_check()}
    print("no fcast :", chk["solar_forecast"]["ok"], "|", coord.view()["solar_room"].get("missing"))
    assert not chk["solar_forecast"]["ok"] and "Forecast.Solar" in chk["solar_forecast"]["detail"]
    assert coord.view()["solar_room"]["missing"] == "forecast" and not coord.view()["solar_room"]["batteries"]

    # --- a big forecast (the user's 'tomorrow' sensor, plus what it said yesterday for today): batteries get a ceiling
    hass.states.async_set("sensor.fc_tomorrow", "40", {"unit_of_measurement": "kWh"})
    mgr.forecast_days[now.date().isoformat()] = 40.0
    coord.apply_settings({"price_peak": 30.0, "price_offpeak": 14.0, "base_load_w": 300, "forecast_entity": "sensor.fc_tomorrow",
                          "offpeak_start": hhmm(now - dtm.timedelta(hours=1)), "offpeak_end": hhmm(now + dtm.timedelta(hours=1))})
    await coord._async_update_data()
    room = coord.view()["solar_room"]
    print("room     :", {k: room.get(k) for k in ("day", "sunrise", "sunset", "charge_ends", "forecast_kwh")}, room["batteries"])
    assert {i["key"]: i for i in coord.setup_check()}["solar_forecast"]["ok"]
    assert room["forecast_kwh"] == 40.0 and room["sunrise"] < room["sunset"]
    ceiling = room["batteries"]["a"]["ceiling"]
    assert ceiling < 100 and room["batteries"]["a"]["solar_kwh"] == 40.0                  # the only solar battery gets it all
    tomorrow = (now.date() + dtm.timedelta(days=1)).isoformat()
    assert mgr.forecast_days[tomorrow] == 40.0, "tomorrow's forecast is remembered for after midnight"

    # --- grid charging is capped at the ceiling and says why
    await mgr.save_schedule({"id": "night", "start": hhmm(now - dtm.timedelta(minutes=5)), "end": hhmm(now + dtm.timedelta(minutes=50)),
                             "mode": "charge", "power_w": 2000})
    coord._snap.devices["a"].last_applied = None
    await coord._async_update_data()
    st = coord._snap.devices["a"]
    print("schedule :", st.desired, "|", st.reason)
    assert "room left for solar" in st.reason and (st.desired.target_soc == ceiling or st.desired.mode.value == "hold")

    # --- switched off: nothing capped
    coord.apply_settings({"price_peak": 30.0, "price_offpeak": 14.0, "solar_room": False, "forecast_entity": "sensor.fc_tomorrow",
                          "offpeak_start": hhmm(now - dtm.timedelta(hours=1)), "offpeak_end": hhmm(now + dtm.timedelta(hours=1))})
    await coord._async_update_data()
    assert "room left" not in coord._snap.devices["a"].reason and coord.view()["solar_room"]["enabled"] is False
    await coord.async_close()
    srv.close(); await srv.wait_closed()
    print("E2E8 OK")

asyncio.run(main())
