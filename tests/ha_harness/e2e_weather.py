"""The cold-weather forecast through Home Assistant's real service machinery (weather.get_forecasts with a response)."""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

import asyncio, tempfile
from datetime import timedelta
from types import SimpleNamespace
import conftest  # noqa: F401
import _harness  # noqa: F401 - Python 3.12+ server shutdown (see _harness.py)
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, State
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util
from custom_components.homepower.manager import HomePowerManager
from custom_components.homepower.coordinator import HomePowerCoordinator


async def main():
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/London"))
    tmp = tempfile.mkdtemp(); hass = HomeAssistant(tmp); hass.config.config_dir = tmp
    hass.config.latitude, hass.config.longitude = 51.4567, -0.1234
    entry = ConfigEntry(data={}, disabled_by=None, discovery_keys={}, domain="homepower", entry_id="e1", minor_version=1, options={},
                        pref_disable_new_entities=False, pref_disable_polling=False, source="user", subentries_data=[],
                        title="HomePower", unique_id="homepower", version=1)
    mgr = HomePowerManager(hass); await mgr.async_load()
    coord = HomePowerCoordinator(hass, entry, mgr, *mgr.build()); mgr.coordinator = coord
    tomorrow = (dt_util.now() + timedelta(days=1)).date()
    seen = []

    async def forecasts(call: ServiceCall):
        seen.append(dict(call.data))
        if call.data["type"] == "hourly":
            return {"weather.home": {"forecast": []}}
        return {"weather.home": {"forecast": [{"datetime": dt_util.start_of_local_day(tomorrow).isoformat(), "temperature": 1.0, "templow": -5.0}]}}
    hass.services.async_register("weather", "get_forecasts", forecasts, supports_response=SupportsResponse.ONLY)
    hass.states.async_set("weather.home", "cloudy", {"temperature": 3.0, "temperature_unit": "°C"})
    hass.states.async_set("sensor.out", "37.4", {"unit_of_measurement": "°F"})

    base = dict(weather_boost=True, weather_entity="weather.home", temp_sensor_entity="sensor.out", forecast_temp_entity="", weather_online=False)
    coord.apply_settings(base)

    t, src = await coord._tomorrow_temp(tomorrow)
    assert (t, src) == (-2.0, "weather entity (daily)"), (t, src)
    assert seen and seen[0] == {"entity_id": "weather.home", "type": "daily"}, seen
    coord._observe_temp(dt_util.now())
    assert round(coord.temp_log.days[dt_util.now().strftime("%Y-%m-%d")][0], 1) == 3.0      # the sensor (37.4 F = 3 C)
    await coord._refresh_weather()
    assert coord.weather["tomorrow_c"] == -2.0 and "still learning" in coord.weather["status"], coord.weather
    # an entity that cannot give a forecast must not break anything
    coord.apply_settings({**base, "weather_entity": "weather.missing", "forecast_temp_entity": "sensor.out"})
    t, src = await coord._tomorrow_temp(tomorrow)
    assert (round(t, 1), src) == (3.0, "forecast sensor"), (t, src)
    print("weather  : entity service, unit conversion, sensor fallback OK")
    print("E2E8 OK")


asyncio.run(main())
