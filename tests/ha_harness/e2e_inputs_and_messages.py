"""Where the figures come from (prices, forecast, grid, loads, weather) when they are missing or odd, and the messages sent."""
import _home  # noqa: F401 - sets up the paths first
import asyncio
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from homeassistant.util import dt as dt_util
from custom_components.homepower.core.models import Actual, Capabilities, DeviceConfig
from custom_components.homepower.core.rates import InferredTariff


def put(h, entity, state, **attrs):
    h.hass.states.async_set(entity, state, attrs)


async def prices(h):
    coord = h.coord
    now = dt_util.now()
    coord.apply_settings({"rates_entity": "sensor.price"})
    put(h, "sensor.price", "0.25")                                  # a plain price sensor: its state is the price now
    assert coord._rates().price_at(now) == 0.25
    put(h, "sensor.price", "unknown")
    assert coord._rates() is None
    coord.apply_settings({"offpeak_extra": "13:00-15:00, 22:00-23:00", "price_peak": 0.3, "price_offpeak": 0.1})
    assert coord._rates().windows[0] == (time(13), time(15)) and len(coord._rates().windows) == 2
    coord.apply_settings({"rates_entity": "sensor.price", "offpeak_auto": True})
    coord._tariff = InferredTariff(time(0, 30), time(5, 30), 0.07, 0.28, 3.0)
    t = coord._rates()
    assert (t.offpeak, t.peak) == (0.07, 0.28) and t.windows == [(time(0, 30), time(5))], t.windows

    # export prices: a plain sensor, a nonsense one, and none
    coord.apply_settings({"export_rates_entity": "sensor.export"})
    put(h, "sensor.export", "0.15")
    assert coord._export_table().price_at(now) == 0.15
    put(h, "sensor.export", "n/a")
    assert coord._export_table() is None
    assert coord._export_better(None, now) is False and coord._export_keep_pct(None, now) == 0 and coord._cycling("a", None, now) is None

    # the cheap hours learned from the recorder's history
    from homeassistant.components import recorder
    from homeassistant.components.recorder import history

    class Instance:
        async def async_add_executor_job(self, fn):
            return fn()

    base = dt_util.utcnow() - timedelta(days=4)
    samples = []
    for d in range(4):
        for hour, price in ((0, 0.28), (1, 0.07), (5, 0.28)):
            samples.append(SimpleNamespace(state=str(price), last_changed=base.replace(hour=hour, minute=0) + timedelta(days=d)))
    samples.append(SimpleNamespace(state="unavailable", last_changed=base))
    real = recorder.get_instance, history.get_significant_states
    recorder.get_instance = lambda hass: Instance()
    history.get_significant_states = lambda *a, **kw: {"sensor.price": sorted(samples, key=lambda s: s.last_changed)}
    coord.apply_settings({"rates_entity": "sensor.price", "offpeak_auto": True})
    coord._tariff, coord._tariff_key = None, None
    await coord._refresh_tariff()
    assert coord._tariff is not None and coord.history_window() is not None, coord._tariff
    await coord._refresh_tariff()                                   # not again for a few hours

    def broken(*a, **kw):
        raise OSError("recorder")

    history.get_significant_states = broken
    kept, coord._tariff_key = coord._tariff, None
    await coord._refresh_tariff()
    assert coord._tariff == kept                                    # the last answer stands
    recorder.get_instance, history.get_significant_states = real
    coord.apply_settings({})
    await coord._refresh_tariff()
    assert coord._tariff is None
    print("prices   : plain sensors, extra windows, history")


async def figures(h):
    coord = h.coord
    now = dt_util.now()
    today, tomorrow = now.date().isoformat(), (now.date() + timedelta(days=1)).isoformat()

    # solar forecast: from a sensor in Wh, from hourly profiles, and broken
    put(h, "sensor.fc", "12000", unit_of_measurement="Wh")
    coord.apply_settings({"forecast_entity": "sensor.fc"})
    assert coord._forecast_kwh() == 12.0 and coord._kwh("sensor.fc") == 12.0 and coord._kwh("sensor.none") is None
    coord.apply_settings({})
    coord.manager.forecast_days.clear()
    coord.solar_hours = {today: [0.0] * 8 + [0.5] * 8 + [0.0] * 8}
    coord._note_solar(now)
    assert coord.solar_view(now)["today_kwh"] == 4.0
    coord.solar_hours = {}
    coord.manager.forecast_days.clear()
    assert coord.solar_view(now) is None
    real = coord._solar_view
    coord._solar_view = lambda n: 1 / 0
    assert coord.solar_view(now) is None                            # never breaks the live view
    coord._solar_view = real
    coord.apply_settings({"forecast_reserve_kwh": 5, "forecast_reserve_pct": 40})
    coord.manager.forecast_days.update({today: 1.0, tomorrow: 1.0})
    assert coord._forecast_floor(now) == 40 and coord.forecast_floor["active"]
    assert coord._coming_solar_day(now.replace(hour=1)) == today and coord._coming_solar_day(now.replace(hour=20)) == tomorrow

    # temperature: weather entity, then sensor, then (only if on) the online service
    put(h, "weather.home", "cloudy", temperature="not a number")
    assert coord._state_temp("weather.home") is None
    put(h, "sensor.tomorrow_temp", "3.25", unit_of_measurement="°C")
    coord.apply_settings({"weather_boost": True, "weather_entity": "weather.home", "forecast_temp_entity": "sensor.tomorrow_temp"})
    day = (now + timedelta(days=1)).date()
    assert await coord._tomorrow_temp(day) == (3.2, "forecast sensor")      # the weather service does not exist here
    coord.apply_settings({"weather_boost": True, "weather_online": True})

    async def fetch(url):
        return {"daily": {"time": [day.isoformat()], "temperature_2m_max": [6.0], "temperature_2m_min": [2.0]}}

    coord._fetch_json = fetch
    assert await coord._tomorrow_temp(day) == (4.0, "Open-Meteo")

    async def offline(url):
        raise OSError("no internet")

    coord._fetch_json = offline
    assert await coord._tomorrow_temp(day) == (None, "")
    statuses = []
    for k, tomorrow_c in ((None, None), (None, 2.0), (800.0, 25.0), (800.0, -5.0)):
        async def temp(d, value=tomorrow_c):
            return value, "x"

        coord._tomorrow_temp = temp
        coord.cold_model.fit = lambda *a, k=k: setattr(coord.cold_model, "k", k)
        coord.temp_log.add((now - timedelta(days=1)).strftime("%Y-%m-%d"), 8.0)
        coord._weather_at = -1e9
        await coord._refresh_weather()
        statuses.append(coord.weather["status"].split(":")[0])
    assert statuses[0] == "no forecast temperature" and statuses[1].startswith("still learning"), statuses
    coord.apply_settings({"temp_sensor_entity": "sensor.tomorrow_temp"})
    coord._weather_at = -1e9
    await coord._refresh_weather()
    assert coord.weather["status"] == "off"

    async def explode(d):
        raise RuntimeError("x")

    coord._tomorrow_temp = explode
    coord.apply_settings({"weather_boost": True})
    coord._weather_at = -1e9
    await coord._refresh_weather()                                  # weather is an extra: nothing is raised
    assert not coord._weather_busy

    # load rules and the grid meter
    put(h, "sensor.ev", "unavailable", unit_of_measurement="kW")
    put(h, "binary_sensor.heat", "on")
    coord.apply_settings({"load_rules": [{"entity": "sensor.ev", "above_w": 1000, "block": "charge"},
                                         {"entity": "binary_sensor.heat", "block": "both"},
                                         {"entity": "sensor.gone", "block": "charge"}]})
    assert coord._load_blocks() == {"charge", "discharge"}
    put(h, "sensor.exp", "500", unit_of_measurement="W")
    coord.apply_settings({"grid_export_entity": "sensor.exp"})
    assert coord._meter_w("") == -500 and coord._meter_w("sensor.gone") is None and coord._entity_age("sensor.gone") is None
    assert coord._measures_house(DeviceConfig("x", "X", "k", Capabilities(1, 1, 1), extra={"grid_meter": "no", "house_meter": True})) is False

    # the Hildebrand Glow display, found by itself
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    ents = [SimpleNamespace(platform=None, domain="sensor", disabled_by=None,
                            unique_id=uid, device_id="d", device_class=cls, original_device_class=None, entity_id="sensor." + name)
            for uid, cls, name in (("m-power", "power", "glow_power"), ("m-rate", None, "glow_rate"), ("m_import_today", "energy", "glow_usage"))]
    from custom_components.homepower import coord_inputs as CI
    for e in ents:
        e.platform = CI.GLOW_DOMAIN
    real_er, real_dr = er.async_get, dr.async_get
    er.async_get = lambda hass: SimpleNamespace(entities=SimpleNamespace(values=lambda: ents))
    dr.async_get = lambda hass: SimpleNamespace(async_get=lambda i: SimpleNamespace(name_by_user=None, name="Electricity meter"))
    coord._glow_at = -1e9
    assert coord._glow() == {"power": "sensor.glow_power", "rate": "sensor.glow_rate", "usage": "sensor.glow_usage"}, coord._glow_cache
    er.async_get = lambda hass: 1 / 0
    coord._glow_at = -1e9
    assert coord._glow() == {}
    er.async_get, dr.async_get = real_er, real_dr
    print("figures  : forecast, temperature, loads, meter")


async def messages(h):
    coord = h.coord
    sent = []

    async def notify(key, message, raised):
        sent.append((key, message, raised))

    real_notify, coord._notify = coord._notify, notify
    monday = dt_util.now().replace(hour=9, minute=0)
    monday -= timedelta(days=monday.weekday())
    y = (monday - timedelta(days=1)).strftime("%Y-%m-%d")
    coord.ledger.days[y] = {"charged": 5.0, "discharged": 4.0, "value": 1.2, "cost": 0.4}
    coord.usage.days[y] = {"imp": [0.5] * 48, "exp": [0.25] * 48}
    flag = {"tariff": "Other", "current": "Mine", "months": 3, "saved": 4.5}
    coord.tariff_comparison = lambda days: {"monthly": {"cheaper_than_current": flag}}
    coord.health.efficiency = lambda did: [("w", 88.0)]
    coord.trend.warning = lambda did: "capacity is falling"
    coord.grid_events.outages_since = lambda *a: (2, 15.0)
    text = coord._weekly_digest(monday)
    for words in ("Imported 24 kWh", "2 grid outages", "88%", "capacity is falling", "Other has been cheaper"):
        assert words in text, (words, text)
    coord.daily.self_consumption = lambda d: 70
    coord.daily.cheap_share = lambda d: 60
    day = coord._daily_summary(monday, {"soc": 50, "stored_kwh": 5, "runtime_h": 4})
    for words in ("Yesterday: charged 5.0", "70% of your solar", "60% of grid energy", "lasts about 4 h"):
        assert words in day, (words, day)

    coord.apply_settings({"weekly_digest": True, "daily_summary_time": "09:00", "notify_cheap_soon": True, "cheap_soon_below_pct": 90,
                          "price_peak": 0.3, "price_offpeak": 0.1, "offpeak_start": "09:20", "offpeak_end": "10:00"})
    coord._event_state.clear()
    coord._event_state["tariff_m200001"] = True
    await coord._check_events(monday, 0.0, {}, coord._rates())
    keys = {k.split("_")[0] for k, _, _ in sent}
    assert {"tariff", "event"} <= keys and "tariff_m200001" not in coord._event_state, (keys, sent)
    assert any("Cheap rate starts" in m for _, m, _ in sent), sent
    coord.tariff_comparison = lambda days: (_ for _ in ()).throw(ValueError("none listed"))
    coord._event_state.clear()
    await coord._check_events(monday, 0.0, {}, None)
    assert "Last 7 days" in coord._weekly_digest(monday)

    # a battery that reports new software
    st = coord._snap.devices["a"]
    sent.clear()
    for version in ("1.0", "1.1"):
        st.actual, st.link = Actual(online=True, soc=50, firmware=version), "online"
        await coord._check_alerts(0.0, coord.adapters)
    assert any("changed from 1.0 to 1.1" in m for _, m, _ in sent), sent
    coord._notify = real_notify

    # delivery that fails is swallowed: alerts never stop the control loop
    async def refuse(*a, **kw):
        raise RuntimeError("no service")

    h.hass.services = SimpleNamespace(async_call=refuse)
    coord.apply_settings({"notify_service": "notify.mobile_app_phone"})
    await coord._notify("k", "m", True)

    async def fail(*a, **kw):
        raise RuntimeError("x")

    real_pause, h.mgr.set_paused = h.mgr.set_paused, fail
    from custom_components.homepower.coord_base import ACTION_PAUSE
    await coord._on_action(SimpleNamespace(data={"action": ACTION_PAUSE}))
    h.mgr.set_paused = real_pause

    # the cover left, said with a grid-outage alert
    coord.readiness = lambda: {"hours": 72.0, "capped": True, "basis": "usual use"}
    assert "more than 72 h" in coord._cover_text()
    coord.readiness = lambda: None
    assert coord._cover_text() == ""
    try:
        coord.whatif(30, {}, {})
    except ValueError as err:
        assert str(err), err
    else:
        raise AssertionError("what-if with no history")
    print("messages : digests, heads-ups, failed delivery")


async def main():
    for part in (prices, figures, messages):
        h = await _home.Home().start()
        await part(h)
        await h.stop()
    print("E2E INPUTS OK")


asyncio.run(main())
