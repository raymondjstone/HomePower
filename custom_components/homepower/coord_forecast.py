"""Solar forecast, room left for solar, the dull-day reserve and the cold-weather target.

Part of HomePowerCoordinator (coordinator.py), kept in its own file so each file has one concern: the methods
run on the coordinator itself and use its state."""
from __future__ import annotations

import sys
import time as _time
from datetime import datetime, timedelta

from homeassistant.util import dt as dt_util

from .core.models import Actual
from .core.rates import RateTable
from .core.solar_forecast import compare_days, compare_hours, hours_by_day, remaining_kwh
from .core.weather import BASE_C, cold_extra_wh, parse_open_meteo, pick_tomorrow, recent_degree_days, to_celsius
from .core.solar_room import SolarDay, charge_ceiling_pct, split_by_capacity
from .coord_base import _LOGGER, FORECAST_PLATFORMS, _real


class ForecastMixin:
    def _forecast_entities(self) -> dict[str, list[str]]:
        """Forecast.Solar / Solcast sensors for today's and tomorrow's generation: all planes / sites of ONE of them, to be
        added up. With both installed, Forecast.Solar is used: adding the two would count the same panels twice."""
        if _time.monotonic() - self._fc_at < 60:
            return self._fc_cache
        by_platform: dict[str, dict[str, list[str]]] = {}
        try:
            from homeassistant.helpers import entity_registry as er
            for e in er.async_get(self.hass).entities.values():
                ends = FORECAST_PLATFORMS.get(e.platform)
                if not ends or e.domain != "sensor" or e.disabled_by:
                    continue
                uid = str(e.unique_id or "")
                got = by_platform.setdefault(e.platform, {"today": [], "tomorrow": []})
                if uid.endswith(ends[0]):
                    got["today"].append(e.entity_id)
                elif uid.endswith(ends[1]):
                    got["tomorrow"].append(e.entity_id)
        except Exception as err:  # noqa: BLE001 - registry unavailable: nothing found
            _LOGGER.debug("forecast lookup failed: %s", err)
        found: dict[str, list[str]] = {"today": [], "tomorrow": []}
        self.forecast_source = None                             # (a forecast integration removed since is not still named)
        for platform in FORECAST_PLATFORMS:                     # in order of preference
            got = by_platform.get(platform)
            if got and (got["today"] or got["tomorrow"]):
                found = got
                self.forecast_source = platform
                break
        self._fc_cache, self._fc_at = found, _time.monotonic()
        return found

    async def _refresh_solar_hours(self) -> None:
        """Every 15 minutes: the expected solar energy for each hour, from the forecast integration's energy platform (what
        Home Assistant's Energy dashboard draws its forecast from). The first integration in FORECAST_PLATFORMS that gives
        an answer is used, all its planes / sites added up."""
        if self._solar_busy or _time.monotonic() - self._solar_at < 900:
            return
        self._solar_busy, self._solar_at = True, _time.monotonic()
        try:
            from homeassistant.config_entries import ConfigEntryState
            from homeassistant.loader import async_get_integration

            tz = dt_util.now().tzinfo
            for domain in FORECAST_PLATFORMS:
                entries = [e for e in self.hass.config_entries.async_entries(domain) if e.state is ConfigEntryState.LOADED]
                if not entries:
                    continue
                try:
                    platform = await (await async_get_integration(self.hass, domain)).async_get_platform("energy")
                    fetch = platform.async_get_solar_forecast
                except Exception:  # noqa: BLE001 - an integration without an energy platform: its sensors still work
                    _LOGGER.debug("%s has no hourly solar forecast", domain, exc_info=True)
                    continue
                # Forecast.Solar keyed each hour by its END until Home Assistant added energy_by_period_start: on those
                # releases (and so in their own Energy dashboard) the forecast would land an hour late.
                by_end = domain == "forecast_solar" and not hasattr(
                    sys.modules.get("homeassistant.components.forecast_solar.coordinator"), "energy_by_period_start")
                total: dict[str, list[float]] = {}
                for e in entries:
                    try:
                        data = await fetch(self.hass, e.entry_id)
                    except Exception:  # noqa: BLE001 - one plane failing must not lose the others
                        _LOGGER.debug("solar forecast from %s failed", domain, exc_info=True)
                        continue
                    for day, hours in hours_by_day((data or {}).get("wh_hours") if isinstance(data, dict) else None, tz, by_end).items():
                        acc = total.setdefault(day, [0.0] * 24)
                        for i, v in enumerate(hours):
                            acc[i] += v
                if total:
                    self.solar_hours = total
                    return
            self.solar_hours = {}
        except Exception:  # noqa: BLE001 - a background task: an error must not escape as "Task exception was never retrieved"
            _LOGGER.debug("hourly solar forecast refresh failed", exc_info=True)
        finally:
            self._solar_busy = False

    def _note_solar(self, now: datetime) -> None:
        """Each cycle: today's forecast total (kept as first seen) and the latest hourly profiles, for forecast-vs-actual."""
        today = now.date().isoformat()
        fc = self._forecast_by_day(now)
        total = fc.get(today)
        if total is None and self.solar_hours.get(today):
            total = sum(self.solar_hours[today])
        self.solar_log.note_total(today, total)
        for day, hours in self.solar_hours.items():
            if day >= today:          # today's hours already started stay as last forecast before they began
                self.solar_log.note_hours(day, hours, now.hour if day == today else 0)
        if self._solar_pruned != today:
            self.solar_log.prune(today)
            self._solar_pruned = today

    def solar_view(self, now: datetime) -> dict | None:
        """Today's solar forecast for the Overview (None when no forecast is set up). Never raises: it is part of the live
        view, which must always be sent."""
        try:
            return self._solar_view(now)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("solar forecast view failed", exc_info=True)
            return None

    def _solar_view(self, now: datetime) -> dict | None:
        today, tomorrow = now.date().isoformat(), (now.date() + timedelta(days=1)).isoformat()
        fc, hours = self._forecast_by_day(now), self.solar_hours.get(today)
        total = fc.get(today)
        if total is None and hours:
            total = round(sum(hours), 2)
        if total is None and fc.get(tomorrow) is None:
            return None
        measured = bool(self.manager.pv_seen)
        return {"today_kwh": None if total is None else round(total, 2),
                "tomorrow_kwh": None if fc.get(tomorrow) is None else round(fc[tomorrow], 2),
                "remaining_kwh": remaining_kwh(hours, now),
                "so_far_kwh": round((self.daily.days.get(today) or {}).get("pv", 0.0), 2) if measured else None,
                "source": self.forecast_source}

    def solar_comparison(self, days: int = 30, day: str | None = None) -> dict:
        """Forecast against the solar HomePower measured: the last `days` days, and one day hour by hour (today by default)."""
        now = dt_util.now()
        today = now.date().isoformat()
        day = day if day and day <= today else today
        measured = bool(self.manager.pv_seen)
        actual = {d: v.get("pv") for d, v in self.daily.days.items() if isinstance(v, dict)} if measured else {}
        hours = (self.solar_log.days.get(day) or {}).get("hours") or (self.solar_hours.get(day) if day == today else None)
        half = (self.usage.days.get(day) or {}).get("pv") if measured else None
        rows = compare_hours(hours, half)
        if day == today:
            for r in rows[now.hour + 1:]:
                r["actual"] = None              # hours still to come have nothing measured yet (not "0 kWh")
        return {"days": compare_days(self.solar_log, actual, today, days), "day": day,
                "hours": rows, "measured": measured, "source": self.forecast_source}

    def _kwh(self, entity_id: str) -> float | None:
        st = self.hass.states.get(entity_id)
        try:
            v = _real(st.state)  # type: ignore[union-attr]
        except (AttributeError, TypeError, ValueError):
            return None
        return v / 1000 if str(st.attributes.get("unit_of_measurement", "")).lower() == "wh" else v

    def _forecast_by_day(self, now: datetime) -> dict[str, float]:
        """Solar forecast kWh for today and tomorrow. The sensor picked in Settings is "tomorrow"; what it said yesterday
        is remembered as today's. Otherwise Forecast.Solar / Solcast are found by themselves."""
        today, tomorrow = now.date().isoformat(), (now.date() + timedelta(days=1)).isoformat()
        out: dict[str, float] = {}
        if (v := self._forecast_kwh()) is not None:
            out[tomorrow] = v
        found = self._forecast_entities()
        for day, key in ((today, "today"), (tomorrow, "tomorrow")):
            vals = [x for x in (self._kwh(e) for e in found[key]) if x is not None]
            if vals and day not in out:
                out[day] = sum(vals)
        mem = self.manager.forecast_days
        if out.get(tomorrow) is not None and mem.get(tomorrow) != out[tomorrow]:
            mem[tomorrow] = out[tomorrow]
            for k in [k for k in mem if k < today]:
                del mem[k]
        if today not in out and today in mem:
            out[today] = mem[today]
        return out

    def _sun(self, day) -> tuple[datetime, datetime] | None:
        """Sunrise and sunset at the home location on a date (what the Sun integration shows, worked out the same way)."""
        try:
            from homeassistant.const import SUN_EVENT_SUNRISE, SUN_EVENT_SUNSET
            from homeassistant.helpers.sun import get_astral_event_date
            rise = get_astral_event_date(self.hass, SUN_EVENT_SUNRISE, day)
            sset = get_astral_event_date(self.hass, SUN_EVENT_SUNSET, day)
        except Exception:  # noqa: BLE001 - no location / polar day: no solar day
            return None
        if rise is None or sset is None:
            return None
        return dt_util.as_local(rise), dt_util.as_local(sset)

    def _solar_ceilings(self, now: datetime, actuals: dict[str, Actual], rates: RateTable | None) -> dict[str, int]:
        """For batteries with panels: the highest charge to reach before the cheap rate ends so that the coming day's solar,
        less the base load, never takes them over 100% (see core/solar_room.py). Without a forecast solar counts as zero
        and nothing is capped."""
        s = self.settings
        info: dict = {"enabled": s.solar_room, "base_load_w": s.base_load_w, "batteries": {}}
        self.solar_room = info
        cfgs = {a.config.device_id: a.config for a in self.adapters}
        solar = {did: cfgs[did] for did in cfgs if self._has_solar(cfgs[did])}
        info["solar_batteries"] = [cfgs[d].name for d in solar]
        if not s.solar_room or not solar:
            return {}
        windows = bool(rates and rates.windows)
        t0 = (rates.cheap_window_end(now) if windows else None) or now     # charging stops when the cheap rate ends
        sun = self._sun(t0.date())
        if sun is not None and t0 >= sun[1]:
            sun = self._sun(t0.date() + timedelta(days=1))
        fc = self._forecast_by_day(now)
        if sun is None:
            info["missing"] = "sun"
            return {}
        day = sun[0].date().isoformat()
        forecast = fc.get(day)
        info.update({"day": day, "sunrise": sun[0].strftime("%H:%M"), "sunset": sun[1].strftime("%H:%M"),
                     "charge_ends": t0.strftime("%H:%M"), "forecast_kwh": forecast})
        if forecast is None:
            info["missing"] = "forecast"
            return {}
        online = {did: act for did, act in actuals.items() if act.online and did in cfgs}
        solar_share = split_by_capacity(forecast, {d: cfgs[d].caps.capacity_wh for d in solar})
        base_share = split_by_capacity(s.base_load_w / 1000, {d: cfgs[d].caps.capacity_wh for d in online})

        def battery_pays(t: datetime) -> bool:
            return not (windows and rates.in_cheap_window(t.time()))

        out: dict[str, int] = {}
        for did, cfg in solar.items():
            cap_kwh = cfg.caps.capacity_wh / 1000
            pct = charge_ceiling_pct(cap_kwh, cfg.caps.min_soc, t0, SolarDay(sun[0], sun[1], solar_share[did]),
                                     base_share.get(did, 0.0), battery_pays)
            info["batteries"][did] = {"name": cfg.name, "solar_kwh": round(solar_share[did], 2), "ceiling": pct}
            if pct < 100:
                out[did] = pct
        return out

    def _forecast_floor(self, now: datetime) -> int:
        """Settings > Backup: on a day forecast to be dull, batteries are not drained below a set charge. Before midday it
        is today's forecast that counts, after it tomorrow's (the night and morning ahead). No forecast, no floor."""
        s = self.settings
        self.forecast_floor = {}
        if not s.forecast_reserve_kwh:
            return 0
        fc = self._forecast_by_day(now)
        day = (now.date() if now.hour < 12 else now.date() + timedelta(days=1)).isoformat()
        kwh = fc.get(day)
        low = kwh is not None and kwh < s.forecast_reserve_kwh
        self.forecast_floor = {"day": day, "forecast_kwh": None if kwh is None else round(kwh, 1), "below_kwh": s.forecast_reserve_kwh,
                               "pct": s.forecast_reserve_pct, "active": low}
        return s.forecast_reserve_pct if low else 0

    def _forecast_kwh(self) -> float | None:
        st = self.hass.states.get(self.settings.forecast_entity) if self.settings.forecast_entity else None
        try:
            v = _real(st.state)  # type: ignore[union-attr]
        except (AttributeError, TypeError, ValueError):
            return None
        return v / 1000 if str(st.attributes.get("unit_of_measurement", "")).lower() == "wh" else v

    def _state_temp(self, entity: str) -> float | None:
        """A temperature (°C) from a sensor, or from a weather entity's current temperature."""
        st = self.hass.states.get(entity) if entity else None
        if st is None:
            return None
        attrs = getattr(st, "attributes", {}) or {}
        try:
            if str(entity).startswith("weather."):
                return to_celsius(_real(attrs["temperature"]), attrs.get("temperature_unit"))
            return to_celsius(_real(st.state), attrs.get("unit_of_measurement"))
        except (KeyError, TypeError, ValueError):
            return None

    def _observe_temp(self, now: datetime) -> None:
        """Remember today's outdoor temperature (from the sensor, else the weather entity) to learn how cold affects use."""
        s = self.settings
        t = self._state_temp(s.temp_sensor_entity) if s.temp_sensor_entity else self._state_temp(s.weather_entity)
        if t is not None and -60 < t < 60:
            self.temp_log.add(now.strftime("%Y-%m-%d"), t)

    async def _fetch_json(self, url: str) -> dict | None:
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        from aiohttp import ClientTimeout

        async with async_get_clientsession(self.hass).get(url, timeout=ClientTimeout(total=15)) as resp:
            return await resp.json(content_type=None) if resp.status == 200 else None

    async def _tomorrow_temp(self, tomorrow) -> tuple[float | None, str]:
        """Tomorrow's mean temperature (°C) and where it came from: the weather entity, then a sensor, then (only if switched on) Open-Meteo."""
        s, tz = self.settings, dt_util.now().tzinfo
        if s.weather_entity:
            unit = getattr(self.hass.config.units, "temperature_unit", "°C")
            for kind in ("daily", "hourly"):
                try:
                    reply = await self.hass.services.async_call("weather", "get_forecasts", {"entity_id": s.weather_entity, "type": kind},
                                                                 blocking=True, return_response=True)
                    t = pick_tomorrow((reply or {}).get(s.weather_entity, {}).get("forecast", []), tomorrow, tz, unit)
                    if t is not None:
                        return t, f"weather entity ({kind})"
                except Exception:  # noqa: BLE001 - this entity may not support that forecast type: try the next source
                    _LOGGER.debug("weather forecast (%s) unavailable", kind, exc_info=True)
        if s.forecast_temp_entity:
            t = self._state_temp(s.forecast_temp_entity)
            if t is not None:
                return round(t, 1), "forecast sensor"
        if s.weather_online:
            try:   # the home's location is rounded to about 10 km before it leaves Home Assistant
                lat, lon = round(self.hass.config.latitude, 1), round(self.hass.config.longitude, 1)
                data = await self._fetch_json("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s&daily=temperature_2m_max,temperature_2m_min"
                                              "&forecast_days=3&timezone=auto" % (lat, lon))
                t = parse_open_meteo(data or {}, tomorrow)
                if t is not None:
                    return t, "Open-Meteo"
            except Exception:  # noqa: BLE001 - optional source
                _LOGGER.debug("Open-Meteo unavailable", exc_info=True)
        return None, ""

    async def _refresh_weather(self) -> None:
        """Hourly: tomorrow's temperature, how the house responds to cold, and the extra energy that implies."""
        s = self.settings
        if self._weather_busy or _time.monotonic() - self._weather_at < 3600 or not (
                s.weather_boost or s.weather_entity or s.temp_sensor_entity):
            return
        self._weather_busy, self._weather_at = True, _time.monotonic()
        try:
            now = dt_util.now()
            today = now.strftime("%Y-%m-%d")
            self.cold_model.fit(self.temp_log, self.usage.days, today)
            tomorrow_c, source = await self._tomorrow_temp((now + timedelta(days=1)).date()) if s.weather_boost else (None, "")
            recent = recent_degree_days(self.temp_log, today)
            extra = cold_extra_wh(self.cold_model.k, tomorrow_c, recent)
            if not s.weather_boost:
                status = "off"
            elif tomorrow_c is None:
                status = "no forecast temperature: pick a weather entity or sensor"
            elif self.cold_model.k is None:
                status = (f"still learning how cold affects your use ({self.cold_model.days} usable days; needs 7 with a spread of "
                          "temperatures and an outdoor temperature sensor or weather entity)")
            elif extra <= 0:
                status = "tomorrow is not colder than lately: no change"
            else:
                status = "raising the overnight target"
            lately = None if recent is None else round(BASE_C - recent, 1)
            self.weather = {"tomorrow_c": tomorrow_c, "source": source, "k_wh_per_degree": self.cold_model.k,
                            "days_learned": self.cold_model.days, "link": self.cold_model.r, "lately_c": lately,
                            "extra_wh": extra, "status": status,
                            "note": f"{tomorrow_c:g} °C forecast vs about {lately:g} °C lately" if tomorrow_c is not None and lately is not None else ""}
        except Exception:  # noqa: BLE001 - weather is an extra: it must never disturb control
            _LOGGER.debug("weather refresh failed", exc_info=True)
        finally:
            self._weather_busy = False
