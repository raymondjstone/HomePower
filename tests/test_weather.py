"""Cold-weather planning: forecast parsing, learning how cold affects use, and the raised goal target."""
import asyncio
import types
from datetime import date, datetime, time, timedelta, timezone

import pytest

from hp.core.goals import Goal
from hp.core.weather import (ColdModel, TempLog, boost_points, cold_extra_wh, degree_days, parse_open_meteo, pick_tomorrow,
                             recent_degree_days, to_celsius)

TOMORROW = date(2026, 11, 11)
UTC = timezone.utc


def test_degree_days_and_units():
    assert degree_days(5) == 13 and degree_days(22) == 0
    assert round(to_celsius(32, "°F"), 1) == 0.0 and to_celsius(7, "°C") == 7


def test_tomorrow_from_a_daily_forecast_uses_the_mean_of_high_and_low():
    fc = [{"datetime": "2026-11-10T00:00:00+00:00", "temperature": 12, "templow": 6},
          {"datetime": "2026-11-11T00:00:00+00:00", "temperature": 4, "templow": -2},
          {"datetime": "2026-11-12T00:00:00+00:00", "temperature": 1, "templow": -5}]
    assert pick_tomorrow(fc, TOMORROW, UTC) == 1.0


def test_tomorrow_from_an_hourly_forecast_and_in_fahrenheit():
    fc = [{"datetime": f"2026-11-11T{h:02d}:00:00+00:00", "temperature": t} for h, t in ((0, 36), (6, 32), (12, 41), (18, 39))]
    fc.append({"datetime": "2026-11-12T00:00:00+00:00", "temperature": 90})
    assert round(pick_tomorrow(fc, TOMORROW, UTC, "°F"), 1) == 2.8             # (36+32+41+39)/4 = 37 F
    assert pick_tomorrow([], TOMORROW, UTC) is None and pick_tomorrow([{"datetime": "junk"}, "x"], TOMORROW, UTC) is None


def test_tomorrow_uses_local_dates_not_utc():
    tz = timezone(timedelta(hours=11))                       # 2026-11-10T20:00Z is already the 11th there
    fc = [{"datetime": "2026-11-10T20:00:00+00:00", "temperature": 8}, {"datetime": "2026-11-11T20:00:00+00:00", "temperature": 30}]
    assert pick_tomorrow(fc, TOMORROW, tz) == 8


def test_open_meteo_reply():
    data = {"daily": {"time": ["2026-11-10", "2026-11-11"], "temperature_2m_max": [10, 5], "temperature_2m_min": [4, -1]}}
    assert parse_open_meteo(data, TOMORROW) == 2.0
    assert parse_open_meteo({"daily": {"time": []}}, TOMORROW) is None and parse_open_meteo({}, TOMORROW) is None


def _history(slope_kwh=0.8, base=10.0, days=14, temps=None):
    log, usage = TempLog(), {}
    temps = temps or [2 + (i * 3) % 17 for i in range(days)]
    for i, t in enumerate(temps):
        day = (date(2026, 10, 1) + timedelta(days=i)).isoformat()
        for _ in range(400):
            log.add(day, t)
        usage[day] = {"load": [(base + slope_kwh * degree_days(t)) / 48] * 48}
    return log, usage


def test_the_model_learns_how_much_more_a_cold_day_uses():
    log, usage = _history()
    m = ColdModel()
    m.fit(log, usage, today="2026-12-31")
    assert m.days == 14 and m.k is not None and abs(m.k - 800) < 5                # 0.8 kWh per degree-day


def test_a_day_seen_only_in_part_is_not_used():
    log, usage = _history()
    first = sorted(usage)[0]
    usage[first] = {"load": [0.0] * 30 + [0.5] * 18}                  # HomePower started late that day
    m = ColdModel()
    m.fit(log, usage, "2026-12-31")
    assert m.days == 13
    short = TempLog()
    for _ in range(50):
        short.add("2026-10-01", 5.0)
    assert short.mean("2026-10-01") is None                           # minutes of readings are not a day's temperature


def test_the_model_refuses_to_guess_without_a_real_link():
    log, usage = _history(slope_kwh=0.0)
    m = ColdModel()
    m.fit(log, usage, "2026-12-31")
    assert m.k is None                                                            # use does not depend on temperature
    log, usage = _history(days=4)
    m.fit(log, usage, "2026-12-31")
    assert m.k is None and m.days == 4                                            # too few days
    log, usage = _history(temps=[10, 10.5, 10, 9.5, 10, 10, 10.2, 10])
    m.fit(log, usage, "2026-12-31")
    assert m.k is None                                                            # every day the same temperature: nothing to learn
    assert ColdModel(m.dump()).days == m.days


def test_extra_use_only_when_tomorrow_is_colder_than_lately_and_the_boost_is_capped():
    log, _ = _history(temps=[8] * 10)
    recent = recent_degree_days(log, "2026-12-31")
    assert recent == 10
    assert cold_extra_wh(800, 0, recent) == 800 * 8                               # 0 degrees vs 8 lately: 8 more degree-days
    assert cold_extra_wh(800, 12, recent) == 0                                    # warmer: never negative
    assert cold_extra_wh(None, 0, recent) == 0 and cold_extra_wh(800, None, recent) == 0 and cold_extra_wh(800, 0, None) == 0
    assert boost_points(6400, 10000) == 25 and boost_points(2000, 10000) == 20 and boost_points(0, 10000) == 0


# ------------------------------------------------------------------ the goal target
def _run(goal, extra_wh, note="-2 C forecast vs about 8 C lately"):
    from hp.core.engine import DevIn, Settings, World, compute
    from hp.core.models import Actual, Capabilities, DeviceConfig, Mode
    from hp.core.rates import RateTable
    cfg = DeviceConfig("a", "a", "x", Capabilities(10000, 3000, 3000, 10), extra={"control_enabled": True})
    rates = RateTable([], 0.30, 0.07, time(0), time(5))
    w = World(now=datetime(2026, 11, 11, 0, 30, tzinfo=UTC), devices=[DevIn(cfg, Actual(online=True, soc=50, power_w=0, mode=Mode.AUTO))],
              settings=Settings(import_limit_w=None), goals=[goal], rates=rates, cold_extra_wh=extra_wh, cold_note=note)
    return compute(w)


def test_a_cold_day_raises_a_fixed_goal_target_and_says_why():
    r = _run(Goal("g", frozenset(), 70, time(7)), 2000)                           # 2 kWh of 10 kWh = 20 points
    assert r.wants["a"].target_soc == 90 and any("raised to 90%" in n and "-2 C" in n for n in r.notes)
    assert _run(Goal("g", frozenset(), 70, time(7)), 0).wants["a"].target_soc == 70
    assert _run(Goal("g", frozenset(), 70, time(7)), 9000).wants["a"].target_soc == 95        # at most 25 points


def test_an_auto_target_is_never_pushed_above_the_target_the_user_set():
    r = _run(Goal("g", frozenset(), 80, time(7), auto_target=True), 6000)
    assert r.wants["a"].target_soc <= 80


# ------------------------------------------------------------------ the coordinator's sources, in order
from test_coordinator_logic import bare  # noqa: E402  (also puts a stand-in for Home Assistant in place)


def _weather_coord(**settings):
    c = bare()
    base = dict(weather_boost=True, weather_entity="", temp_sensor_entity="", forecast_temp_entity="", weather_online=False)
    c.settings = types.SimpleNamespace(**{**base, **settings})
    c.temp_log, c.cold_model, c.weather = TempLog(), ColdModel(), {}
    c.usage = types.SimpleNamespace(days={})
    c._weather_at, c._weather_busy = -1e9, False
    states = {}
    c.hass = types.SimpleNamespace(
        states=types.SimpleNamespace(get=lambda e: states.get(e)),
        config=types.SimpleNamespace(latitude=51.4567, longitude=-0.1234, units=types.SimpleNamespace(temperature_unit="°C")),
        services=types.SimpleNamespace())
    return c, states


def test_the_weather_entity_comes_first_then_the_sensor_then_open_meteo_only_if_switched_on():
    async def go():
        tomorrow = date(2026, 3, 2)                                   # the stubbed "now" is 2026-03-01
        c, states = _weather_coord(weather_entity="weather.home", forecast_temp_entity="sensor.tmrw")

        async def svc(domain, name, data, blocking=False, return_response=False):
            assert (domain, name) == ("weather", "get_forecasts")
            return {"weather.home": {"forecast": [{"datetime": "2026-03-02T00:00:00+00:00", "temperature": 2, "templow": -4}]}}
        c.hass.services.async_call = svc
        assert await c._tomorrow_temp(tomorrow) == (-1.0, "weather entity (daily)")

        async def broken(*a, **k):
            raise RuntimeError("no such forecast")
        c.hass.services.async_call = broken
        states["sensor.tmrw"] = types.SimpleNamespace(state="3.5", attributes={"unit_of_measurement": "°C"})
        assert await c._tomorrow_temp(tomorrow) == (3.5, "forecast sensor")

        c2, _ = _weather_coord()
        urls = []

        async def fetch(url):
            urls.append(url)
            return {"daily": {"time": ["2026-03-02"], "temperature_2m_max": [6], "temperature_2m_min": [0]}}
        c2._fetch_json = fetch
        assert await c2._tomorrow_temp(tomorrow) == (None, "") and urls == []        # off by default: nothing is sent anywhere
        c3, _ = _weather_coord(weather_online=True)
        c3._fetch_json = fetch
        assert await c3._tomorrow_temp(tomorrow) == (3.0, "Open-Meteo")
        assert "latitude=51.5&longitude=-0.1" in urls[0]                              # location rounded to about 10 km
    asyncio.run(go())


def test_the_status_says_what_is_missing():
    async def go():
        c, states = _weather_coord(weather_entity="weather.home")

        async def svc(*a, **k):
            return {"weather.home": {"forecast": [{"datetime": "2026-03-02T00:00:00+00:00", "temperature": -3, "templow": -3}]}}
        c.hass.services.async_call = svc
        await c._refresh_weather()
        assert c.weather["tomorrow_c"] == -3.0 and "still learning" in c.weather["status"] and c.weather["extra_wh"] == 0
        c2, _ = _weather_coord(weather_boost=False, weather_entity="weather.home")
        await c2._refresh_weather()
        assert c2.weather["status"] == "off"
    asyncio.run(go())


def test_outdoor_temperature_is_logged_in_celsius_from_a_sensor_or_the_weather_entity():
    c, states = _weather_coord(temp_sensor_entity="sensor.out")
    states["sensor.out"] = types.SimpleNamespace(state="41", attributes={"unit_of_measurement": "°F"})
    c._observe_temp(datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
    assert round(c.temp_log.days["2026-03-01"][0], 1) == 5.0
    c2, st2 = _weather_coord(weather_entity="weather.home")
    st2["weather.home"] = types.SimpleNamespace(state="sunny", attributes={"temperature": 7, "temperature_unit": "°C"})
    c2._observe_temp(datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
    assert c2.temp_log.days["2026-03-01"] == [7.0, 1.0]
