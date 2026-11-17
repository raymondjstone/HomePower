"""Control loop: read batteries -> decide (core.engine) -> apply -> verify -> account -> alert."""
from __future__ import annotations

import re
import asyncio
import logging
import time as _time
from collections import deque
from dataclasses import dataclass, field, replace as _replace
from datetime import datetime, time, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .adapters.base import DeviceAdapter
from .const import DOMAIN, UPDATE_INTERVAL_S
from .core.alerts import AlertTracker
from .core.energy import EnergyMeter, Ledger
from .core.engine import DevIn, Result, World, compute
from .core.goals import Goal
from .core.models import Actual, Desired, Mode
from .core.preview import find_conflicts, preview as build_preview
from .core.meter import MeterReader
from .core.anker_x1_map import X1_ALARMS
from .core.insights import BatteryFigures, DailyStats, Diverter, GridEvents, Health, HealthTrend, LoadProfile, PlanLog, UsageHistory, combine
from .core.rates import SLOT, RateTable, Slot, infer_offpeak, parse_rates, to_main_units
from .core.report import charge_efficiency, ecoflow_report, startup_summary, window_drift
from .core.traces import CommandTrace
from .core.weather import (BASE_C, ColdModel, TempLog, cold_extra_wh, degree_days, parse_open_meteo, pick_tomorrow,
                           recent_degree_days, to_celsius)
from .core.sanity import ChargeWatch, CommandBudget, RateLearner, cycling_margin, explain_idle, grid_sign_mismatch, next_unconfirmed
from .core.reconcile import Decision, decide, materially_different
from .core.schedule import ScheduleEntry
from .core.settings import AppSettings, build as build_settings
from .core.solar_room import SolarDay, charge_ceiling_pct, split_by_capacity

_LOGGER = logging.getLogger(__name__)
READ_TIMEOUT_S = 20
PERSIST_EVERY_S = 300
TARIFF_REFRESH_S = 6 * 3600
TARIFF_HISTORY_DAYS = 14
FORECAST_PLATFORMS = {   # solar forecast integrations found automatically: platform -> (today, tomorrow) unique_id endings
    "forecast_solar": ("energy_production_today", "energy_production_tomorrow"),
    "solcast_solar": ("forecast_today", "forecast_tomorrow"),
}
GLOW_DOMAIN = "hildebrandglow_dcc"  # ha-hildebrandglow-dcc (raymondjstone fork and HandyHat original)
KEEPALIVE_EVERY_S = 60
OFFLINE_ALERT_AFTER_S = 180
PROTECT_ALERT_AFTER_S = 60
UNCONFIRMED_ALERT_AFTER = 3
GRID_SLOW_AFTER_S = 120     # setup check: warn that the grid reading is not live
GRID_STALE_AFTER_S = 300    # control: too old to steer by (solar surplus tracking waits instead)
ACTION_PAUSE, ACTION_HAND_BACK = "HOMEPOWER_PAUSE", "HOMEPOWER_HAND_BACK"   # buttons on phone notifications
FORCING_REASONS = ("schedule", "goal", "manual override", "backup reserve")


@dataclass(slots=True)
class DeviceStatus:
    desired: Desired | None = None
    actual: Actual | None = None
    decision: Decision | None = None
    reason: str = ""
    last_apply: float | None = None
    last_error: str | None = None
    release_pending: bool = False   # we forced this device; hand it back when no longer managed
    guard_held: bool = False        # software SOC limit is currently holding this device
    last_read_ok: float | None = None
    unconfirmed: int = 0
    awaiting_confirm: bool = False  # the first command to an unproven battery is waiting for the user
    waited: int = 0                 # cycles a slow (cloud) battery has been given to show a command's effect
    last_keepalive: float = 0.0
    last_command: dict | None = None
    last_applied: Desired | None = None   # what we last sent, to tell a new instruction from a retry


@dataclass(slots=True)
class Snapshot:
    devices: dict[str, DeviceStatus] = field(default_factory=dict)
    events: deque = field(default_factory=lambda: deque(maxlen=200))  # diagnostics log


_PRIVATE_KEY = re.compile(r"ssid|wifi|pass|mac|^sn$|serial|lat|lon|addr|ip$|token", re.I)


class HomePowerCoordinator(DataUpdateCoordinator[Snapshot]):
    def __init__(self, hass: HomeAssistant, config_entry, manager, adapters: list[DeviceAdapter],
                 schedules: list[ScheduleEntry], goals: list[Goal]) -> None:
        super().__init__(hass, _LOGGER, config_entry=config_entry, name=DOMAIN,
                         update_interval=timedelta(seconds=UPDATE_INTERVAL_S))
        self.manager = manager
        self.adapters, self.schedules, self.goals = adapters, schedules, goals
        self.settings: AppSettings = build_settings({**config_entry.data, **config_entry.options})
        manager.set_live(self.settings.ecoflow_live)
        self.overrides: dict[str, tuple[Desired, datetime]] = {}   # manual "charge now" etc.
        self.testing: set[str] = set()                              # batteries under a control test
        self.meter = EnergyMeter(manager.energy)
        self._meter = MeterReader()   # smart-meter sensors -> watts
        self._tariff = None           # cheap window learnt from the rates sensor's history
        self._tariff_key, self._tariff_checked, self._tariff_busy = None, 0.0, False
        self._glow_cache: dict[str, str] = {}
        self._glow_at = -1e9
        self._fc_cache: dict[str, list[str]] = {}
        self._fc_at = -1e9
        self.solar_room: dict = {}       # the room-for-solar working, shown in the panel
        self.ledger = Ledger(manager.ledger_days, manager.ledger_stores)
        if manager.ledger_stores is None:
            # Saved by a version that booked charging cost when charging: today's figure would count last night's
            # charging twice (once then, once as it is used), so today starts again under the new method.
            self.ledger.days.pop(dt_util.now().strftime("%Y-%m-%d"), None)
        self.profile = LoadProfile(manager.profile_data)   # the household's usual load by time of day
        self.usage = UsageHistory(manager.usage_data)          # grid import/export per half-hour, for the tariff comparison
        self.daily = DailyStats(manager.daily_data)         # self-consumption / cheap-rate share per day
        self._unsub_action = hass.bus.async_listen("mobile_app_notification_action", self._on_action)
        self.startup_note: str | None = None
        self.temp_log = TempLog((manager.weather_data or {}).get("temps"))       # outdoor temperature per day
        self.cold_model = ColdModel((manager.weather_data or {}).get("model"))   # extra use per degree of cold
        self.weather: dict = {}                                                  # tomorrow's temperature and what it means, for the panel
        self._weather_at, self._weather_busy = -1e9, False
        self._started = False
        self.rate_learner = RateLearner(manager.charge_rates)   # real charge rate of batteries that pick their own
        self.budget = CommandBudget()                       # daily cap on commands to cloud batteries
        self.charge_watch = ChargeWatch()                    # told to charge but nothing happens
        self.traces = CommandTrace(manager.trace_data)       # what happened after each command to EcoFlow batteries
        self.plan_log = PlanLog(manager.plan_log_data)       # planned against actual power per half hour
        self.health = Health(manager.health_data)           # weekly round-trip efficiency per battery
        self.grid_events = GridEvents(manager.grid_events)  # each time a battery reported the grid down
        self.trend = HealthTrend(manager.trend_data)        # daily temperature and pack-voltage spread per battery
        self.forecast_floor: dict = {}                      # the forecast-based reserve working, shown in the panel
        self.why: deque = deque(maxlen=80)                  # what each battery was told to do and the inputs behind it
        self._why_sig: dict[str, tuple] = {}
        self._event_state: dict[str, bool] = dict(manager.event_state)   # which heads-up messages are showing (survives restarts)
        self.diverter = Diverter()                           # solar-surplus load
        self._divert_entity = manager.divert_on or ""        # the entity we switched on, to switch the same one off
        self.diverter.on = bool(manager.divert_on)           # still on from before a restart: we must be able to switch it off
        self.alerts = AlertTracker()
        self.last_result = Result()
        self.last_grid: float | None = None
        self._last_limit_sync = -1e9
        self._cheap_now: bool | None = None    # is it a cheap-rate period right now (None = unknown / flat tariff)
        self.grid_source = ""                  # where the grid reading comes from (shown in Settings / setup check)
        self.grid_age_s: float | None = None   # how old that reading is; None = read directly just now
        self._forced_ids = set(manager.forced)                     # persisted: devices we put into forced control
        self._last_persist = _time.monotonic()
        self._snap = Snapshot(devices={a.config.device_id: self._new_status(a.config.device_id) for a in adapters})

    # ------------------------------------------------------------------ flags owned by the manager
    @property
    def paused(self) -> bool:
        return self.manager.paused

    @property
    def backup_active(self) -> bool:
        return self.manager.backup or self._entity_on(self.settings.storm_entity)

    @property
    def away(self) -> bool:
        return self.manager.away or self._entity_on(self.settings.away_entity, ("on", "away", "not_home"))

    def _entity_on(self, entity_id: str, states: tuple = ("on",)) -> bool:
        st = self.hass.states.get(entity_id) if entity_id else None
        return bool(st and st.state in states)

    # ------------------------------------------------------------------ configuration
    def _new_status(self, did: str) -> DeviceStatus:
        return DeviceStatus(release_pending=did in self._forced_ids)

    def _set_release(self, did: str, st: DeviceStatus, value: bool) -> None:
        if st.release_pending == value:
            return
        st.release_pending = value
        (self._forced_ids.add if value else self._forced_ids.discard)(did)
        self.manager.on_forced_change(set(self._forced_ids))

    def apply_settings(self, cfg: dict) -> None:
        """Apply changed global settings in place (no integration reload)."""
        self.settings = build_settings(cfg)
        self.manager.set_live(self.settings.ecoflow_live)

    def set_config(self, adapters: list[DeviceAdapter], schedules: list[ScheduleEntry], goals: list[Goal]) -> None:
        for gone in {a.config.device_id for a in self.adapters} - {a.config.device_id for a in adapters}:
            self.alerts.forget(f"offline:{gone}"), self.alerts.forget(f"error:{gone}"), self.alerts.forget(f"grid:{gone}"), self.alerts.forget(f"alarm:{gone}:")
            self.alerts.forget(f"unconfirmed:{gone}"), self.alerts.forget(f"stuck:{gone}"), self.alerts.forget(f"gridsign:{gone}"), self.charge_watch.forget(gone), self.rate_learner.forget(gone), self.traces.forget(gone), self._forget_plan(gone), self.alerts.forget(f"protect:{gone}"), self.alerts.forget(f"health:{gone}"), self.alerts.forget(f"trend:{gone}")
            self.grid_events.forget(gone, dt_util.now())
            self.trend.days.pop(gone, None)
        self.adapters, self.schedules, self.goals = adapters, schedules, goals
        old = self._snap.devices
        self._snap.devices = {a.config.device_id: old.get(a.config.device_id) or self._new_status(a.config.device_id) for a in adapters}
        self.overrides = {k: v for k, v in self.overrides.items() if k in self._snap.devices}

    # ------------------------------------------------------------------ manual overrides
    def set_override(self, device_ids: list[str] | None, desired: Desired, minutes: int) -> None:
        until = dt_util.now() + timedelta(minutes=minutes)
        for a in self.adapters:
            if not device_ids or a.config.device_id in device_ids:
                self.overrides[a.config.device_id] = (desired, until)
                self._log(a.config.device_id, f"manual override {desired.mode.value} for {minutes} min")

    def clear_override(self, device_ids: list[str] | None = None) -> None:
        for did in list(self.overrides):
            if not device_ids or did in device_ids:
                self.overrides.pop(did)
                self._log(did, "manual override cleared")

    def _active_overrides(self, now: datetime) -> dict[str, Desired]:
        for did, (_, until) in list(self.overrides.items()):
            if now >= until:
                self.overrides.pop(did)
                self._log(did, "manual override expired")
        return {k: v[0] for k, v in self.overrides.items()}

    # ------------------------------------------------------------------ inputs from HA
    def _condition_ok(self, entity_id: str) -> bool:
        return self._entity_on(entity_id)

    def _glow(self) -> dict[str, str]:
        """Entities of the Hildebrand Glow (DCC) integration, if it is set up: its live electricity power sensor
        and its electricity unit-rate sensor. Used when the user has not picked a sensor themselves."""
        if _time.monotonic() - self._glow_at < 60:
            return self._glow_cache
        found: dict[str, str] = {}
        try:
            from homeassistant.helpers import device_registry as dr, entity_registry as er
            ereg, dreg = er.async_get(self.hass), dr.async_get(self.hass)
            for e in ereg.entities.values():
                if e.platform != GLOW_DOMAIN or e.domain != "sensor" or e.disabled_by:
                    continue
                uid = str(e.unique_id or "")
                dev = dreg.async_get(e.device_id) if e.device_id else None
                elec = dev is not None and "electric" in str(dev.name_by_user or dev.name or "").lower()
                if uid.endswith("-power") and (e.device_class or e.original_device_class) in ("power", None):
                    if elec or "power" not in found:
                        found["power"] = e.entity_id
                elif uid.endswith("-rate") and elec:
                    found["rate"] = e.entity_id
                elif elec and (e.device_class or e.original_device_class) == "energy" and uid and "-" not in uid[-12:]:
                    found["usage"] = e.entity_id   # HandyHat original: only a daily kWh counter, no live power
        except Exception as err:  # noqa: BLE001 - registries unavailable: no auto-detection
            _LOGGER.debug("glow lookup failed: %s", err)
        self._glow_cache, self._glow_at = found, _time.monotonic()
        return found

    def _rates_entity(self) -> str:
        return self.settings.rates_entity or self._glow().get("rate", "")

    def _rates(self) -> RateTable | None:
        s = self.settings
        slots = []
        ent = self._rates_entity()
        st = self.hass.states.get(ent) if ent else None
        if st is not None:
            slots = parse_rates(dict(st.attributes), dt_util.DEFAULT_TIME_ZONE)
            if not slots:  # a plain price sensor: its state is the price right now
                try:
                    now = dt_util.now()
                    lo = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)
                    slots = [Slot(lo, lo + SLOT, float(st.state))]
                except (TypeError, ValueError):
                    pass
        peak, off, a, b = s.price_peak, s.price_offpeak, s.offpeak_start, s.offpeak_end
        inf = self._tariff if s.offpeak_auto and ent else None
        if inf is not None:   # anything the user typed in wins; history only fills the blanks
            if (a is None or b is None) and not s.offpeak_extra:   # typed-in extra periods mean the user knows the hours
                # The window is read from when the sensor's value changed, and such sensors (smart-meter/DCC data in
                # particular) lag the real switch by up to an hour. A late *start* only wastes cheap time, but a late
                # *end* would plan charging into peak-rate time, so the end is pulled back one half-hour.
                a = inf.start
                b = (datetime.combine(dt_util.now().date(), inf.end) - SLOT).time()
            peak = inf.peak if peak is None else peak
            off = inf.offpeak if off is None else off
        if not slots and peak is None and off is None:
            return None
        if (a is None or b is None) and s.offpeak_extra:      # only the extra windows were entered: the first is the main one
            (a, b), rest = s.offpeak_extra[0], s.offpeak_extra[1:]
            return RateTable(slots, peak, off, a, b, rest)
        return RateTable(slots, peak, off, a, b, s.offpeak_extra)

    async def _refresh_tariff(self) -> None:
        """Learn the cheap (night) window from the rates sensor's recorded price history (every few hours)."""
        s, ent = self.settings, self._rates_entity()
        key = (ent, s.offpeak_auto)
        if not (ent and s.offpeak_auto):
            self._tariff, self._tariff_key = None, key
            return
        if (key == self._tariff_key and _time.monotonic() - self._tariff_checked < TARIFF_REFRESH_S) or self._tariff_busy:
            return
        self._tariff_busy, self._tariff_key, self._tariff_checked = True, key, _time.monotonic()
        try:
            from homeassistant.components.recorder import get_instance, history
            end = dt_util.utcnow()

            def load():
                return history.get_significant_states(self.hass, end - timedelta(days=TARIFF_HISTORY_DAYS), end, [ent],
                                                      include_start_time_state=True, significant_changes_only=False,
                                                      no_attributes=True).get(ent, [])

            samples = []
            for st in await get_instance(self.hass).async_add_executor_job(load):
                try:
                    samples.append((st.last_changed, float(st.state)))
                except (TypeError, ValueError):
                    continue
            found = infer_offpeak(samples, end, dt_util.DEFAULT_TIME_ZONE)
            if found != self._tariff:
                self._log("tariff", (f"cheap window from price history: {found.start:%H:%M}-{found.end:%H:%M} "
                                     f"({found.offpeak:g} vs {found.peak:g})") if found else "no repeating cheap window in the price history")
            self._tariff = found
        except Exception as err:  # noqa: BLE001 - recorder missing/unavailable: keep what we had
            _LOGGER.debug("tariff history unavailable: %s", err)
        finally:
            self._tariff_busy = False

    def _forecast_entities(self) -> dict[str, list[str]]:
        """Forecast.Solar / Solcast sensors for today's and tomorrow's generation (all planes / sites, to be added up)."""
        if _time.monotonic() - self._fc_at < 60:
            return self._fc_cache
        found: dict[str, list[str]] = {"today": [], "tomorrow": []}
        try:
            from homeassistant.helpers import entity_registry as er
            for e in er.async_get(self.hass).entities.values():
                ends = FORECAST_PLATFORMS.get(e.platform)
                if not ends or e.domain != "sensor" or e.disabled_by:
                    continue
                uid = str(e.unique_id or "")
                if uid.endswith(ends[0]):
                    found["today"].append(e.entity_id)
                elif uid.endswith(ends[1]):
                    found["tomorrow"].append(e.entity_id)
        except Exception as err:  # noqa: BLE001 - registry unavailable: nothing found
            _LOGGER.debug("forecast lookup failed: %s", err)
        self._fc_cache, self._fc_at = found, _time.monotonic()
        return found

    def _kwh(self, entity_id: str) -> float | None:
        st = self.hass.states.get(entity_id)
        try:
            v = float(st.state)  # type: ignore[union-attr]
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

    def _note_decision(self, did: str, name: str, st: DeviceStatus, act: Actual, world: World, res: Result, grid: float | None, now: datetime) -> None:
        """Remember what a battery was told to do, why, and the readings behind it (Diagnostics > Why it did that). Only
        a change is recorded, and power is rounded so a zero-import battery adjusting by a few watts is not a change."""
        d = st.desired
        sig = (d.mode.value if d else None, round((d.power_w or 0) / 500) if d else 0, st.reason)
        if self._why_sig.get(did) == sig:
            return
        self._why_sig[did] = sig
        self.why.append({
            "t": now.isoformat(timespec="seconds"), "did": did, "name": name,
            "mode": d.mode.value if d else "auto", "power_w": d.power_w if d else None, "reason": st.reason or "nothing manages it: the battery runs itself",
            "inputs": {"charge_pct": act.soc, "grid_w": None if grid is None else round(grid), "price_now": self._price_now(world.rates, now),
                       "cheap_rate": world.cheap_now, "backup_mode": world.backup_active, "away": world.away, "paused": self.paused,
                       "forecast_kwh": world.forecast_kwh, "reserve_floor_pct": world.reserve_floor_pct or None,
                       "solar_ceiling_pct": world.solar_ceiling.get(did), "notes": list(res.notes)[:3]}})

    def readiness(self) -> dict | None:
        """How long the batteries could run the house if the grid went down now: the usable energy (above each battery's
        minimum) against the usual household load for the coming hours, or the current load if that is not learned yet.
        Ignores solar, so it is the cautious figure."""
        now, wh, n = dt_util.now(), 0.0, 0
        for a in self.adapters:
            st = self._snap.devices.get(a.config.device_id)
            act = st.actual if st else None
            if act and act.online and act.soc is not None:
                wh += max(0.0, act.soc - a.config.caps.min_soc) / 100 * a.config.caps.capacity_wh
                n += 1
        if not n:
            return None
        left, t, hours, basis = wh, now, 0.0, "usual use at this time of day"
        while hours < 72:
            e = self.profile.expected_wh(t, 0.5)
            if e is None:
                hours = None
                break
            if e >= left:
                hours += 0.5 * left / max(e, 1e-9)
                left = 0.0
                break
            left -= e
            t += timedelta(minutes=30)
            hours += 0.5
        if hours is None:
            loads = [s.actual.load_w for s in self._snap.devices.values() if s.actual and s.actual.online and s.actual.load_w]
            basis = "the load right now"
            hours = wh / max(loads) if loads else None
        return {"usable_kwh": round(wh / 1000, 1), "hours": None if hours is None else round(min(hours, 72.0), 1),
                "capped": bool(hours is not None and hours >= 72), "basis": basis}

    def _cover_text(self) -> str:
        r = self.readiness()
        if not r or r["hours"] is None:
            return ""
        return f" About {'more than ' if r['capped'] else ''}{r['hours']:g} h of cover left at {r['basis']}, ignoring solar."

    def whatif(self, days: int, base: dict, alt: dict) -> dict:
        """Replay the recorded house load and solar with two different sets of battery settings (core/whatif.py)."""
        from .core.tariffs import parse_tariffs
        from .core.whatif import simulate
        ts = parse_tariffs(self.settings.compare_tariffs)
        if not ts:
            raise ValueError("List the tariff you are on, first, in Settings > Compare tariffs so there are prices to work with")
        usable = {k: v for k, v in self.usage.days.items() if "load" in v}
        keys = sorted(usable)[-days:]
        if len(keys) < 3:
            raise ValueError(f"needs at least 3 days of recorded house load and solar; HomePower has {len(keys)} so far (it records them from this version on)")
        cap = sum(a.config.caps.capacity_wh for a in self.adapters) / 1000
        kw = sum(a.config.caps.max_charge_w for a in self.adapters) / 1000
        if cap <= 0 or kw <= 0:
            raise ValueError("the batteries' capacity and charge power are not known")
        data = {k: usable[k] for k in keys}
        return {"days": len(keys), "capacity_kwh": round(cap, 1), "max_kw": round(kw, 1), "tariff": ts[0]["name"],
                "base": simulate(data, ts[0], cap, kw, **base), "alt": simulate(data, ts[0], cap, kw, **alt)}

    def _weekly_digest(self, now: datetime) -> str:
        """One message a week: savings, grid use, outages and how the batteries are doing."""
        cur = self.settings.currency
        days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, 8)]
        parts = [f"Last 7 days: saved about {cur}{sum(self.ledger.saved(d) for d in days):.2f}."]
        imp = sum(sum(self.usage.days[d]["imp"]) for d in days if d in self.usage.days)
        exp = sum(sum(self.usage.days[d]["exp"]) for d in days if d in self.usage.days)
        if imp or exp:
            parts.append(f"Imported {imp:.0f} kWh from the grid and exported {exp:.0f} kWh.")
        cutoff = (now - timedelta(days=7)).isoformat()
        outs = [e for e in self.grid_events.events if e["start"] >= cutoff]
        if outs:
            mins = sum(max(0, ((datetime.fromisoformat(e["end"]) if e["end"] else now) - datetime.fromisoformat(e["start"])).total_seconds()) for e in outs) / 60
            parts.append(f"{len(outs)} grid outage{'s' if len(outs) != 1 else ''}, {mins:.0f} min in total.")
        for a in self.adapters:
            eff = self.health.efficiency(a.config.device_id)
            if eff:
                parts.append(f"{a.config.name}: round-trip efficiency {eff[-1][1]:g}%.")
            if (w := self.trend.warning(a.config.device_id)):
                parts.append(f"{a.config.name}: {w}.")
        try:
            flag = self.tariff_comparison(30)["monthly"]["cheaper_than_current"]
        except ValueError:
            flag = None
        if flag:
            parts.append(f"{flag['tariff']} has been cheaper than {flag['current']} for {flag['months']} months running.")
        return " ".join(parts)

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
            v = float(st.state)  # type: ignore[union-attr]
        except (AttributeError, TypeError, ValueError):
            return None
        return v / 1000 if str(st.attributes.get("unit_of_measurement", "")).lower() == "wh" else v

    def _load_blocks(self) -> set[str]:
        blocks: set[str] = set()
        for r in self.settings.load_rules:
            st = self.hass.states.get(r["entity"])
            if st is None:
                continue
            if r.get("above_w") is not None:
                try:
                    active = float(st.state) > r["above_w"]
                except ValueError:
                    active = False
            else:
                active = st.state == "on"
            if active:
                blocks |= {"charge", "discharge"} if r["block"] == "both" else {r["block"]}
        return blocks

    def _grid_w(self, actuals: dict[str, Actual]) -> float | None:
        """House grid power (+ importing), from the first source in the user's order (Settings > Grid meter) that has a
        reading. Sources: "sensor" = the sensor the user picked; "battery" = a battery on the local network whose meter
        measures the whole house (many only measure themselves: see _measures_house); "glow" = Hildebrand Glow found
        automatically (smart-meter data, which can run late); "cloud" = a cloud-reported figure (EcoFlow)."""
        s = self.settings
        cfgs = {x.config.device_id: x.config for x in self.adapters}

        def from_battery(local: bool) -> float | None:
            for did, a in actuals.items():
                cfg = cfgs.get(did)
                if cfg is None or (cfg.extra.get("driver") != "ecoflow_stream") != local or not self._measures_house(cfg):
                    continue
                if a.online and a.grid_w is not None:
                    self.grid_source = f"{cfg.name} ({'its own meter reading' if local else 'cloud reading'})"
                    self.grid_age_s = None if local else 30.0
                    return a.grid_w
            return None

        def from_sensor(ent: str) -> float | None:
            v = self._meter_w(ent)
            if v is None:
                return None
            self.grid_source, self.grid_age_s = ent or s.grid_export_entity, self._entity_age(ent or s.grid_export_entity)
            return -v if s.grid_invert else v

        def glow() -> float | None:
            g = self._glow()
            ent = g.get("power", "") or g.get("usage", "")
            return from_sensor(ent) if ent and not (s.grid_entity or s.grid_export_entity) else None

        sources = {
            "sensor": lambda: from_sensor(s.grid_entity) if (s.grid_entity or s.grid_export_entity) else None,
            "battery": lambda: from_battery(True), "glow": glow, "cloud": lambda: from_battery(False),
        }
        for name in s.grid_order:
            v = sources[name]()
            if v is not None:
                return v
        self.grid_source, self.grid_age_s = "", None
        return None

    @staticmethod
    def _measures_house(cfg) -> bool:
        """Does this battery's grid figure cover the whole house? What the user said; else what is usual for the model
        (an X1 sits at the grid connection; plug-in batteries only see their own socket; EcoFlow's cloud figure is kept
        as a last resort)."""
        said = cfg.extra.get("grid_meter", "auto")
        if said != "auto":
            return said == "yes"
        return bool(cfg.extra.get("house_meter")) or cfg.extra.get("driver") == "ecoflow_stream"

    def _entity_age(self, entity_id: str) -> float | None:
        st = self.hass.states.get(entity_id) if entity_id else None
        if st is None:
            return None
        seen = getattr(st, "last_reported", None) or st.last_updated
        return max(0.0, (dt_util.utcnow() - seen).total_seconds())

    def _meter_w(self, imp_ent: str) -> float | None:
        """Smart-meter reading in W (+ importing): an import sensor, less an optional export sensor. Each may be
        a power sensor (W/kW) or a cumulative energy counter (Wh/kWh)."""
        s, now = self.settings, _time.monotonic()
        out = []
        for key, ent in (("imp", imp_ent), ("exp", s.grid_export_entity)):
            st = self.hass.states.get(ent) if ent else None
            out.append(None if st is None else self._meter.read(key, st.state, st.attributes.get("unit_of_measurement"), now))
        imp, exp = out
        if imp is None:
            return None if exp is None or imp_ent else -exp
        return imp - (exp or 0.0)

    def _house_load(self, actuals: dict[str, Actual], grid: float | None, pv_w: float) -> float | None:
        """Whole-house consumption: what a battery reports, else worked out from the meter, battery and solar flows."""
        reported = [a.load_w for a in actuals.values() if a.online and a.load_w is not None and a.load_w >= 0]
        if reported:
            return max(reported)
        if grid is None:
            return None
        bat = sum(a.power_w or 0.0 for a in actuals.values() if a.online)   # + charging
        return max(0.0, grid - bat + pv_w)

    def _has_solar(self, cfg) -> bool | None:
        """Are panels attached to this battery? What the user said, else whether it has ever reported solar power."""
        said = cfg.extra.get("solar", "auto")
        if said != "auto":
            return said == "yes"
        return True if cfg.device_id in self.manager.pv_seen else None

    def _export_better(self, rates: RateTable | None, now: datetime) -> bool:
        """Is spare solar worth more exported than stored? True when the export price beats buying the same energy back at
        the cheapest upcoming import price (allowing ~10% for what is lost going through the battery)."""
        exp = self.settings.export_price
        if not exp or rates is None:
            return False
        cheapest = self._cheapest_price(rates, now)
        if cheapest is None:
            return False
        unit = self.settings.rates_unit
        return to_main_units(exp, unit) > to_main_units(cheapest, unit) / 0.9

    def _is_cheap(self, rates: RateTable | None, now: datetime, price: float | None) -> bool | None:
        """Is `now` a cheap-rate period? None when there is nothing to compare with (a flat tariff)."""
        if rates is None or price is None:
            return None
        if rates.peak is not None and rates.offpeak is not None and rates.peak != rates.offpeak:
            return price <= to_main_units((rates.peak + rates.offpeak) / 2, self.settings.rates_unit)
        horizon = [s.price for s in rates.slots_between(now, now + timedelta(hours=24))]
        if len(horizon) < 4 or max(horizon) == min(horizon):
            return None
        return to_main_units(sum(horizon) / len(horizon), self.settings.rates_unit) > price * 1.1

    def _price_now(self, rates: RateTable | None, now: datetime) -> float | None:
        p = rates.price_at(now) if rates else None
        return None if p is None else to_main_units(p, self.settings.rates_unit)

    # ------------------------------------------------------------------ logging / notifications
    def _log(self, device_id: str, msg: str) -> None:
        self._snap.events.append((dt_util.now().isoformat(), device_id, msg))
        _LOGGER.info("%s: %s", device_id, msg)

    async def _notify(self, key: str, message: str, raised: bool) -> None:
        nid = f"homepower_alert_{key}"
        try:
            if raised:
                await self.hass.services.async_call("persistent_notification", "create",
                                                    {"title": "HomePower", "message": message, "notification_id": nid})
            else:
                await self.hass.services.async_call("persistent_notification", "dismiss", {"notification_id": nid})
            svc = self.settings.notify_service
            if svc and "." in svc:
                domain, name = svc.split(".", 1)
                data = {"title": "HomePower", "message": message if raised else f"Resolved: {message}"}
                if raised and name.startswith("mobile_app_"):     # the phone app shows these as buttons on the notification
                    data["data"] = {"tag": nid, "actions": [{"action": ACTION_PAUSE, "title": "Pause HomePower"},
                                                            {"action": ACTION_HAND_BACK, "title": "Hand back to Auto"}]}
                await self.hass.services.async_call(domain, name, data)
        except Exception:  # noqa: BLE001 - alerts must never break the control loop
            _LOGGER.debug("alert delivery failed", exc_info=True)

    async def _check_events(self, now: datetime, now_s: float, actuals: dict[str, Actual], rates: RateTable | None) -> None:
        """Optional heads-up messages: battery full, battery low, cheap rate starting soon with a low battery."""
        s = self.settings
        fig = self._combined(actuals)
        soc = fig["soc"]

        async def edge(key: str, active: bool, clear: bool, msg: str) -> None:
            was = self._event_state.get(key, False)
            if active and not was:
                self._event_state[key] = True
                self._log("events", msg)
                await self._notify(f"event_{key}", msg, True)
            elif was and clear:
                self._event_state[key] = False
                await self._notify(f"event_{key}", msg, False)

        today = f"{now:%Y%m%d}"
        for k in [k for k in self._event_state if k.startswith(("cheap_", "summary_", "tariff_", "digest_")) and today not in k]:
            del self._event_state[k]            # yesterday's once-a-day markers
        if s.weekly_digest and now.weekday() == 0:
            at = s.daily_summary_time or time(8, 0)
            due = now.time() >= at
            await edge(f"digest_{today}", due, False, self._weekly_digest(now) if due and not self._event_state.get(f"digest_{today}") else "")
        if f"tariff_{today}" not in self._event_state:
            self._event_state[f"tariff_{today}"] = True
            try:
                flag = self.tariff_comparison(30)["monthly"]["cheaper_than_current"]
            except ValueError:
                flag = None
            if flag:
                await self._notify(f"tariff_{flag['tariff']}", f"{flag['tariff']} would have been cheaper than {flag['current']} for the last {flag['months']} months running "
                                   f"(about {self.settings.currency}{flag['saved']:.2f} less in total), going by the usage HomePower recorded.", True)
        if soc is None:
            return
        # "Full" is 95%: many batteries stop at their own charge ceiling below 100. Switching a heads-up off clears it.
        await edge("full", s.notify_full and soc >= 95, not s.notify_full or soc < 88, f"The home battery is full ({soc:g}%).")
        await edge("low", bool(s.notify_low_pct) and soc <= s.notify_low_pct, not s.notify_low_pct or soc > s.notify_low_pct + 5,
                   f"The home battery is low ({soc:g}%).")
        nxt = rates.minutes_to_cheap(now.time()) if rates is not None else None
        if s.notify_cheap_soon and nxt is not None:
            await edge(f"cheap_{now:%Y%m%d}_{nxt[1]:%H%M}", 0 < nxt[0] <= 30 and soc < s.cheap_soon_below_pct, False,
                       f"Cheap rate starts at {nxt[1]:%H:%M} and the battery is at {soc:g}%.")
        if s.daily_summary_time is not None:
            due = now.time() >= s.daily_summary_time and (now.hour * 60 + now.minute) - (s.daily_summary_time.hour * 60 + s.daily_summary_time.minute) < 30
            await edge(f"summary_{now:%Y%m%d}", due, False, self._daily_summary(now, fig))

    def _daily_summary(self, now: datetime, fig: dict) -> str:
        """One message a day: where the battery is and how yesterday went."""
        y = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        led, cur = self.ledger.days.get(y), self.settings.currency
        parts = [f"Home battery {fig['soc']:g}% ({fig['stored_kwh']:g} kWh stored)."]
        if led:
            parts.append(f"Yesterday: charged {led['charged']:.1f} kWh, discharged {led['discharged']:.1f} kWh, "
                         f"saved about {cur}{led['value'] - led['cost']:.2f}.")
        sc, ch = self.daily.self_consumption(y), self.daily.cheap_share(y)
        if sc is not None:
            parts.append(f"{sc:g}% of your solar was used at home.")
        if ch is not None:
            parts.append(f"{ch:g}% of grid energy was bought at the cheap rate.")
        if fig.get("runtime_h") is not None:
            parts.append(f"At the current draw the battery lasts about {fig['runtime_h']:g} h.")
        return " ".join(parts)

    async def _divert(self, now_s: float, actuals: dict[str, Actual], grid: float | None) -> None:
        """Solar surplus: run a chosen load while the batteries are full and power is being exported."""
        s = self.settings
        if self._divert_entity and self._divert_entity != s.divert_entity:      # setting changed or cleared: release the old load
            if self.diverter.on:
                await self._switch(self._divert_entity, False)
                self.manager.divert_on = ""
            self.diverter = Diverter()
        self._divert_entity = s.divert_entity
        if not s.divert_entity:
            return
        # While paused HomePower switches nothing on, and lets go of the load if it is running.
        soc = None if self.paused else self._combined(actuals)["soc"]
        act = self.diverter.decide(now_s, soc, grid, s.divert_export_w, s.divert_min_soc, cheap=bool(s.divert_cheap and self._cheap_now and not self.paused))
        if act:
            await self._switch(s.divert_entity, act == "on")
            self.manager.divert_on = s.divert_entity if act == "on" else ""
            self.hass.async_create_task(self.manager.save_runtime())
            self._log("divert", f"{s.divert_entity} switched {act} ({('cheap rate' if s.divert_cheap and self._cheap_now else 'surplus solar') if act == 'on' else 'no surplus'})")

    async def _switch(self, entity_id: str, on: bool) -> None:
        try:
            await self.hass.services.async_call("homeassistant", "turn_on" if on else "turn_off", {"entity_id": entity_id})
        except Exception:  # noqa: BLE001 - a missing entity must not affect battery control
            _LOGGER.debug("could not switch %s", entity_id, exc_info=True)

    def setup_check(self) -> list[dict]:
        """Is everything set up? A plain checklist for the panel (ok / what is missing / where to fix it)."""
        s, items = self.settings, []

        def add(key: str, ok: bool, title: str, detail: str = "", action: str = "", target: str = "") -> None:
            items.append({"key": key, "ok": bool(ok), "title": title, "detail": detail, "action": action, "target": target})

        add("batteries", bool(self.adapters), "At least one battery added", "" if self.adapters else "Add your first battery.", "tab", "batteries")
        for a in self.adapters:
            did, name, st = a.config.device_id, a.config.name, self._snap.devices.get(a.config.device_id)
            act = st.actual if st else None
            reading = bool(act and act.online and act.soc is not None)
            add(f"reading:{did}", reading, f"{name} is reporting", "" if reading else
                ("It is offline or unreachable." if not (act and act.online) else "It is online but sends no charge level."), "tab", "diagnostics")
            modes = a.config.caps.supported_modes
            if len(modes) > 1:
                allowed = bool(a.config.extra.get("control_enabled", True))
                add(f"control:{did}", allowed, f"{name}: control allowed", "" if allowed else
                    "Monitor-only: HomePower reads it but sends no commands. Allow control on the Batteries tab when you are ready.", "tab", "batteries")
                if allowed:
                    t = self.manager.tests.get(did)
                    add(f"tested:{did}", bool(t and t.get("ok")), f"{name}: control tested",
                        "" if t and t.get("ok") else ("The last test failed." if t else "Run Test control once to check the battery follows commands."),
                        "test", did)
        for a in self.adapters:
            if not a.config.caps.power_control and a.config.extra.get("control_enabled", True):
                did, t = a.config.device_id, self.manager.tests.get(a.config.device_id)
                ok = did in self.manager.confirmed or bool(t and t.get("ok"))
                add(f"first:{did}", ok, f"{a.config.name}: first command allowed", "" if ok else
                    "Press 'Allow first command' on its card (or pass Test control). This battery's control has not been proven yet.", "tab", "overview")
        rates = self._rates()
        add("prices", rates is not None, "Electricity prices known", "" if rates is not None else
            "Pick a rates sensor or type your standard and cheap rate.", "tab", "settings")
        slots = bool(rates and rates.slots and len(rates.slots) > 2)
        exact = bool(s.offpeak_start and s.offpeak_end) or bool(s.offpeak_extra)
        add("tariff", exact or slots, "Cheap-rate hours confirmed", "" if exact or slots else
            ("They are only estimated from price history." if self._tariff else "Enter when your cheap rate starts and ends."), "tab", "settings")
        if any(self._has_solar(a.config) for a in self.adapters):
            fc = self._forecast_by_day(dt_util.now())
            ok = bool(fc)
            add("solar_forecast", ok, "Solar forecast available", "" if ok else
                "Some batteries have solar panels. Add the Forecast.Solar or Solcast integration (Settings > Devices & services > "
                "Add integration), or pick a forecast sensor in HomePower's Settings, so overnight charging leaves room for "
                "the next day's solar. Until then solar is counted as zero.", "tab", "settings")
        add("grid", self.last_grid is not None, "Grid meter reading available", "" if self.last_grid is not None else
            "Needed for peak shaving, the export limit, solar-only charging and savings. Pick a grid sensor in Settings.", "tab", "settings")
        if self.last_grid is not None:
            age = self.grid_age_s
            fresh = age is None or age <= GRID_SLOW_AFTER_S
            add("grid_fresh", fresh, "Grid reading is live", "" if fresh else
                f"The reading from {self.grid_source} is {age / 60:.0f} min old. Solar-only charging, peak shaving and the export "
                "limit need a live reading: pick a faster grid sensor in Settings.", "tab", "settings")
        return items

    def _combined(self, actuals: dict[str, Actual]) -> dict:
        items = []
        for a in self.adapters:
            act = actuals.get(a.config.device_id)
            if act is None:
                continue
            c = a.config.caps
            items.append(BatteryFigures(c.capacity_wh, act.soc, act.online, act.power_w, c.min_soc, act.load_w, c.max_charge_w))
        loads = [a.load_w for a in actuals.values() if a.online and a.load_w is not None]
        return combine(items, max(loads) if loads else None)

    async def _check_alerts(self, now_s: float, adapters: list[DeviceAdapter]) -> None:
        for a in adapters:
            did, name = a.config.device_id, a.config.name
            st = self._snap.devices.get(did)
            if st is None:
                continue
            checks = {
                f"offline:{did}": (bool(st.actual and not st.actual.online), f"{name} is offline: HomePower cannot read it.", OFFLINE_ALERT_AFTER_S),
                f"error:{did}": (st.last_error is not None, f"A command to {name} failed: {st.last_error}", 0),
                f"unconfirmed:{did}": (st.unconfirmed >= UNCONFIRMED_ALERT_AFTER, f"{name} is not following HomePower's commands (not confirmed after {UNCONFIRMED_ALERT_AFTER} tries).", 0),
                f"grid:{did}": (bool(st.actual and st.actual.off_grid), f"{name} reports the grid is down and is running the house itself. HomePower is not sending it commands until the grid is back."
                                + (self._cover_text() if st.actual and st.actual.off_grid else ""), 0),
                f"protect:{did}": (st.decision is Decision.SKIP_PROTECTED and not (st.actual and st.actual.off_grid), f"{name} has manufacturer protection active, so HomePower is not overriding it.", PROTECT_ALERT_AFTER_S),
            }
            act = st.actual
            if act and act.online and did not in self.testing and not self.settings.dry_run and a.config.extra.get("control_enabled", True):
                tgt = st.desired.target_soc if st.desired else None
                stuck = self.charge_watch.stuck(did, self._grid_charging(st), act.soc, act.power_w, tgt, now_s)
                checks[f"stuck:{did}"] = (stuck, f"{name} was told to charge but is not charging (no power and no rise in charge for "
                                                 f"{int(self.charge_watch.after_s // 60)} min). Check the battery's own app for a mode or schedule overriding HomePower.", 0)
            if act and act.online and a.config.extra.get("driver") == "ecoflow_stream":
                checks[f"gridsign:{did}"] = (grid_sign_mismatch(self.last_grid, act.grid_w),
                                             f"{name}'s grid reading ({act.grid_w:g} W) points the opposite way to your grid meter ({self.last_grid:g} W). "
                                             "One of them is reversed or measures a different point: check Settings > grid sensor and its 'invert' option."
                                             if self.last_grid is not None and act.grid_w is not None else "", 600)
            for text in X1_ALARMS.values():     # alarms the X1 itself reports (a failed meter or inverter link, ...)
                checks[f"alarm:{did}:{text}"] = (bool(act and text in act.alarms), f"{name} reports: {text}.", 0)
            fw, fw_key = act.firmware if act else None, f"fw:{did}"
            if fw:
                before = self._event_state.get(fw_key)
                if before and before != fw:
                    await self._notify(f"fwchg:{did}:{fw}", f"{name}'s software changed from {before} to {fw}. If something stops working, "
                                       "run Diagnostics > Registers: compare: register addresses can differ between versions.", True)
                self._event_state[fw_key] = fw
            weak = self.health.degraded(did)
            checks[f"health:{did}"] = (weak is not None, f"{name}: {weak}", 0)
            if act and act.online:
                self.grid_events.observe(did, name, act.off_grid, dt_util.now(), act.soc)
                self.trend.update(dt_util.now().strftime("%Y-%m-%d"), did, act.temp_c, act.pack_v)
            drift = self.trend.warning(did)
            checks[f"trend:{did}"] = (drift is not None, f"{name}: {drift}", 0)
            for key, (active, msg, after) in checks.items():
                ev = self.alerts.observe(key, active, now_s, after)
                if ev:
                    self._log(did, ("ALERT: " if ev == "raised" else "resolved: ") + msg)
                    await self._notify(key, msg, ev == "raised")

    # ------------------------------------------------------------------ the loop
    async def _read(self, a: DeviceAdapter) -> Actual:
        st = self._snap.devices.get(a.config.device_id)
        if st is not None and a.config.device_id in self.testing and st.actual is not None:
            return st.actual
        try:
            return await asyncio.wait_for(a.read(), READ_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - one device must not stop the others
            _LOGGER.debug("read failed for %s", a.config.device_id, exc_info=True)
            return Actual(online=False)

    async def _async_update_data(self) -> Snapshot:
        now, mono, wall = dt_util.now(), _time.monotonic(), _time.time()
        adapters = list(self.adapters)   # a snapshot: the panel can add/remove batteries while we await I/O
        reads = await asyncio.gather(*(self._read(a) for a in adapters))
        actuals = {a.config.device_id: act for a, act in zip(adapters, reads)}
        # A battery removed while we were reading is dropped; one added meanwhile waits for the next cycle.
        adapters = [a for a in adapters if a.config.device_id in self._snap.devices]
        actuals = {k: v for k, v in actuals.items() if k in self._snap.devices}
        for did, act in actuals.items():
            if act.online:
                self._snap.devices[did].last_read_ok = wall

        self._observe_temp(now)
        self.hass.async_create_task(self._refresh_tariff())
        self.hass.async_create_task(self._refresh_weather())
        grid = self._grid_w(actuals)
        rates = self._rates()
        world = World(
            now=now, settings=self.settings.engine, schedules=self.schedules, goals=self.goals,
            overrides=self._active_overrides(now), grid_w=grid, condition_ok=self._condition_ok, usage_wh=self.profile.expected_wh,
            backup_active=self.backup_active, away=self.away, load_blocks=self._load_blocks(), reserve_floor_pct=self._forecast_floor(now),
            grid_fresh=self.grid_age_s is None or self.grid_age_s <= GRID_STALE_AFTER_S,
            rates=rates, forecast_kwh=self._forecast_kwh(), cheap_now=self._is_cheap(rates, now, self._price_now(rates, now)),
            devices=[DevIn(self._effective_config(a.config), actuals[a.config.device_id],
                           self._snap.devices[a.config.device_id].release_pending,
                           self._snap.devices[a.config.device_id].guard_held,
                           bool(a.config.extra.get("control_enabled", True)), self._has_solar(a.config)) for a in adapters],
            export_better=self._export_better(rates, now),
            solar_ceiling=self._solar_ceilings(now, actuals, rates),
            efficiency={a.config.device_id: charge_efficiency((self.health.efficiency(a.config.device_id) or [(None, None)])[-1][1]) for a in adapters},
            price_now=self._price_now(rates, now), price_later_max=self._price_later_max(rates, now),
            cold_extra_wh=self.weather.get("extra_wh", 0.0) if self.settings.weather_boost else 0.0,
            cold_note=self.weather.get("note", ""),
        )
        try:
            res = compute(world)
        except Exception:  # noqa: BLE001 - a decision bug must never take the integration down or send odd commands
            _LOGGER.exception("decision engine failed; leaving the batteries alone this cycle")
            res = Result(notes=["internal error in the decision engine: no commands were sent this cycle (see the Home Assistant log)"])
        self.last_result = res

        for a in adapters:
            did = a.config.device_id
            st = self._snap.devices.get(did)
            if st is None:        # removed while this cycle was reading
                continue
            st.actual = actuals[did]
            st.desired = res.wants.get(did)
            st.reason = res.reasons.get(did, "")
            st.guard_held = res.guard_held.get(did, False)
            self._note_decision(did, a.config.name, st, actuals[did], world, res, grid, now)
            if st.desired is None:  # nothing manages it: device runs its own logic
                st.decision = None
                st.unconfirmed = 0
                st.last_error = None   # nothing is being asked of it, so an old failure no longer applies
                continue
            if st.desired.mode is Mode.AUTO and st.actual.online and st.actual.mode in (Mode.AUTO, None):
                self._set_release(did, st, False)  # handed back and confirmed
            if self.paused or did in self.testing or not a.config.extra.get("control_enabled", True):
                st.decision = None  # monitor-only / paused / being tested: read and report, never write
                continue
            fresh = materially_different(st.last_applied, st.desired)   # new instruction: no cooldown
            st.decision, diffs = decide(st.desired, st.actual, now=mono, last_apply=None if fresh else st.last_apply,
                                        cooldown_s=self.settings.cooldown_s)
            if st.decision is Decision.SKIP_PROTECTED:
                self._log(did, f"protection active, not overriding: {diffs}")
            elif st.decision is Decision.APPLY:
                await self._apply_and_verify(a, st, st.desired, diffs)
            elif st.decision is Decision.OK:
                st.unconfirmed = 0

        if not self._started:
            self._started = True
            try:
                self._startup_check(actuals)
            except Exception:  # noqa: BLE001 - a report must never stop control
                _LOGGER.debug("startup check failed", exc_info=True)
        if not self.paused:
            await self._keepalive(mono, adapters)
        self.last_grid = grid
        for step in (lambda: self._account(now, wall, actuals, grid, rates), lambda: self._check_alerts(wall, adapters), lambda: self._learn_rates(adapters), lambda: self._record_plan(now, actuals), lambda: self._watch_traces(wall, now, actuals), lambda: self._check_tariff_drift(wall),
                     lambda: self._check_events(now, wall, actuals, rates),
                     lambda: self._divert(wall, actuals, grid), lambda: self._sync_x1_limits(mono, adapters)):
            try:   # accounting / alerts are secondary: a bug there must not stop control
                r = step()
                if asyncio.iscoroutine(r):
                    await r
            except Exception:  # noqa: BLE001
                _LOGGER.exception("secondary step failed (accounting/alerts); control continues")
        if mono - self._last_persist >= PERSIST_EVERY_S:
            self._last_persist = mono
            try:
                await self.manager.save_runtime()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("could not save HomePower state")
        return self._snap

    async def _keepalive(self, mono: float, adapters: list[DeviceAdapter]) -> None:
        """Feed device-side comms watchdogs while we hold a battery in forced control."""
        for a in adapters:
            st = self._snap.devices.get(a.config.device_id)
            if st is not None and (st.release_pending and a.config.extra.get("watchdog") and a.config.extra.get("control_enabled", True)
                    and mono - st.last_keepalive >= KEEPALIVE_EVERY_S):
                st.last_keepalive = mono
                try:
                    await a.keepalive()
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("keepalive failed for %s", a.config.device_id, exc_info=True)

    async def _sync_x1_limits(self, mono: float, adapters: list[DeviceAdapter]) -> None:
        """Optional: keep an Anker X1's own import/export limits in line with HomePower's (every few minutes)."""
        s = self.settings
        if not s.x1_grid_limits or self.paused or s.dry_run or mono - self._last_limit_sync < 300:
            return
        self._last_limit_sync = mono
        for a in adapters:
            drv = getattr(a, "driver", None)
            if a.config.extra.get("house_meter") and a.config.extra.get("control_enabled", True) and hasattr(drv, "set_grid_limits"):
                st = self._snap.devices.get(a.config.device_id)
                if st is None or not (st.actual and st.actual.online) or (st.actual and st.actual.off_grid):
                    continue
                changed = await drv.set_grid_limits(s.engine.import_limit_w, s.engine.export_limit_w)
                if changed:
                    self._log(a.config.device_id, "set the inverter's own " + " and ".join(changed))

    def tariff_comparison(self, days: int = 30) -> dict:
        """The recorded half-hourly usage priced under each tariff typed in Settings (see core/tariffs.py)."""
        from .core.tariffs import compare, parse_tariffs
        return compare(self.usage.days, parse_tariffs(self.settings.compare_tariffs), days)

    def _account(self, now: datetime, wall: float, actuals: dict[str, Actual], grid: float | None, rates: RateTable | None) -> None:
        """Energy counters and the savings ledger (an estimate: see core/energy.py)."""
        steps: dict[str, tuple[float, float]] = {}
        for did, act in actuals.items():
            steps[did] = self.meter.add(f"bat:{did}", wall, act.power_w if act.online else None)
        g_in, g_out = self.meter.add("grid", wall, grid)
        for did, act in actuals.items():
            if act.online and (act.pv_w or 0) > 50 and did not in self.manager.pv_seen:
                self.manager.pv_seen.add(did)      # this battery has panels: remembered for 'solar: auto'
        pv = [a.pv_w for a in actuals.values() if a.online and a.pv_w is not None]
        pv_in, _ = self.meter.add("pv", wall, sum(pv) if pv else None)
        price = self._price_now(rates, now)
        self._cheap_now = self._is_cheap(rates, now, price)
        self.daily.add(now.strftime("%Y-%m-%d"), pv_kwh=pv_in, export_kwh=g_out, import_kwh=g_in, cheap=self._is_cheap(rates, now, price))
        load_w = self._house_load(actuals, grid, sum(pv) if pv else 0.0)
        l_in, _ = self.meter.add("load", wall, load_w)
        self.usage.add(now, g_in, g_out, l_in, pv_in)
        self.profile.record(now, load_w)
        self.health.update(now.strftime("%G-W%V"), {did: (self.meter.get(f"bat:{did}_in"), self.meter.get(f"bat:{did}_out"))
                                                     for did in actuals})
        self._account_savings(now, actuals, grid, rates, steps)

    def _account_savings(self, now: datetime, actuals: dict[str, Actual], grid: float | None, rates: RateTable | None,
                         steps: dict[str, tuple[float, float]]) -> None:
        """Savings ledger (an estimate: see core/energy.py Ledger)."""
        on = {did: a for did, a in actuals.items() if a.online}
        # Batteries told to charge from the grid: all their charging is bought. Solar-only and grid + solar ones, and
        # batteries charging by themselves, are judged from the grid reading: whatever is imported beyond the forced
        # charging is shared among them; with no reading their charging is counted as solar.
        forced = {did for did, st in self._snap.devices.items() if did in on and self._grid_charging(st)}
        forced_w = sum(max(0.0, on[d].power_w or 0.0) for d in forced)
        free_w = sum(max(0.0, a.power_w or 0.0) for d, a in on.items() if d not in forced)
        free_share = min(1.0, max(0.0, grid - forced_w) / free_w) if grid is not None and free_w > 0 else 0.0
        # Battery energy that went out to the grid replaced no import: it earns the export price (if any) instead.
        out_w = sum(max(0.0, -(a.power_w or 0.0)) for a in on.values())
        exp_share = min(1.0, max(0.0, -grid) / out_w) if grid is not None and out_w > 0 else 0.0
        unit, price = self.settings.rates_unit, self._price_now(rates, now)
        exp_price = to_main_units(self.settings.export_price, unit) if self.settings.export_price else 0.0
        cheapest = self._cheapest_price(rates, now)
        first = None if cheapest is None else to_main_units(cheapest, unit)
        caps = {a.config.device_id: a.config.caps.capacity_wh for a in self.adapters}
        day = now.strftime("%Y-%m-%d")
        for did, act in actuals.items():
            d_in, d_out = steps.get(did, (0.0, 0.0))
            stored = act.soc / 100 * caps.get(did, 0) / 1000 if act.online and act.soc is not None and did in caps else None
            self.ledger.add(day, did, d_in, 1.0 if did in forced else free_share, d_out, exp_share, price, exp_price, stored, first)
        self.ledger.forget(set(caps))

    # ------------------------------------------------------------------ weather (cold-day target)
    def _state_temp(self, entity: str) -> float | None:
        """A temperature (°C) from a sensor, or from a weather entity's current temperature."""
        st = self.hass.states.get(entity) if entity else None
        if st is None:
            return None
        attrs = getattr(st, "attributes", {}) or {}
        try:
            if str(entity).startswith("weather."):
                return to_celsius(float(attrs["temperature"]), attrs.get("temperature_unit"))
            return to_celsius(float(st.state), attrs.get("unit_of_measurement"))
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

    def _price_later_max(self, rates: RateTable | None, now: datetime, hours: int = 6) -> float | None:
        """The dearest import price in the next few hours (main units), for 'keep it for later'."""
        if rates is None:
            return None
        ahead = [to_main_units(x.price, self.settings.rates_unit) for x in rates.slots_between(now, now + timedelta(hours=hours))]
        return max(ahead) if ahead else None

    async def _check_tariff_drift(self, wall: float) -> None:
        """Alert when the cheap hours in the price history no longer match the hours typed into Settings."""
        s, inf = self.settings, self._tariff
        msg = window_drift(s.offpeak_start, s.offpeak_end, inf.start, inf.end) if inf is not None and inf.days >= 5 else None
        ev = self.alerts.observe("tariffdrift", bool(msg), wall, 6 * 3600)
        if ev:
            await self._notify("tariffdrift", msg or "Your cheap-rate hours match the price history again.", ev == "raised")

    def _startup_check(self, actuals: dict[str, Actual]) -> None:
        """Once after each start: what was picked up again, and which batteries cannot be reached."""
        m = self.manager
        restored = {"saved EcoFlow original settings": len(m.ecoflow_orig), "learned charge rates": len(self.rate_learner.rates),
                    "command results": sum(len(v) for v in self.traces.results.values()),
                    "days of plan history": len(self.plan_log.days), "batteries held from before": len(m.forced)}
        down = [a.config.name for a in self.adapters if not (actuals.get(a.config.device_id) and actuals[a.config.device_id].online)]
        self.startup_note = startup_summary(restored, down)
        self._log("events", self.startup_note)

    def _effective_config(self, cfg):
        """The battery's config with the charge rate it has really shown (batteries that pick their own rate)."""
        if cfg.caps.power_control:
            return cfg
        caps, rate = cfg.caps, self.rate_learner.rates.get(cfg.device_id)
        if rate and rate != caps.max_charge_w:
            caps = _replace(caps, max_charge_w=rate)
        if Mode.CHARGE in caps.supported_modes and self.traces.verdict(cfg.device_id) == "unreliable":
            caps = _replace(caps, supported_modes=frozenset(caps.supported_modes - {Mode.CHARGE}))   # it has not worked: stop relying on it
        return cfg if caps is cfg.caps else _replace(cfg, caps=caps)

    def _record_plan(self, now: datetime, actuals: dict[str, Actual]) -> None:
        """Remember what each battery was told to do, and what it did, for the plan-versus-actual chart."""
        for did, act in actuals.items():
            st = self._snap.devices.get(did)
            want = st.desired if st else None
            planned = None
            if want is not None and want.mode is not Mode.AUTO:
                planned = {Mode.CHARGE: 1, Mode.DISCHARGE: -1}.get(want.mode, 0) * float(want.power_w or 0)
            self.plan_log.add(now, did, planned, act.power_w if act.online else None)

    def _watch_traces(self, wall: float, now: datetime, actuals: dict[str, Actual]) -> None:
        for did, act in actuals.items():
            if act.online:
                rec = self.traces.observe(did, wall, act.soc, act.power_w, act.mode.value if act.mode else None)
                if rec:
                    self._log(did, f"command result: {rec['mode']} {'worked' if rec['ok'] else 'had no effect'} "
                                   f"(peak {rec['peak_w']} W, {'reacted after ' + str(rec['reaction_s']) + ' s' if rec['ok'] else 'no reaction in 10 min'})")

    def _forget_plan(self, did: str) -> None:
        for devs in self.plan_log.days.values():
            devs.pop(did, None)

    def _learn_rates(self, adapters) -> None:
        changed = False
        for a in adapters:
            st = self._snap.devices.get(a.config.device_id)
            if st is None or st.actual is None or a.config.caps.power_control:
                continue
            changed |= self.rate_learner.observe(a.config.device_id, self._grid_charging(st), st.actual.power_w, st.actual.soc)
        if changed:
            self.manager.charge_rates = dict(self.rate_learner.rates)

    @staticmethod
    def _grid_charging(st: DeviceStatus) -> bool:
        """Is this battery being told to charge from the grid at a fixed power (schedule, goal, override, backup)?"""
        return (st.desired is not None and st.desired.mode is Mode.CHARGE and st.reason.startswith(FORCING_REASONS)
                and "(solar only)" not in st.reason and "(grid + solar)" not in st.reason)

    @staticmethod
    def _cheapest_price(rates: RateTable | None, now: datetime) -> float | None:
        """The cheapest import price in the next 24 hours (rates unit)."""
        if rates is None:
            return None
        ahead = [x.price for x in rates.slots_between(now, now + timedelta(hours=24))]
        return min(ahead) if ahead else rates.offpeak

    async def _apply_and_verify(self, a: DeviceAdapter, st: DeviceStatus, want: Desired, diffs) -> None:
        did = a.config.device_id
        self._log(did, f"{st.reason or 'plan'}: mismatch {[(d.field, d.desired, d.actual) for d in diffs]}; applying")
        st.last_apply = _time.monotonic()
        if self.settings.dry_run:   # test run: show what would be sent, send nothing (re-logged once per cooldown)
            what = want.mode.value + (f" {want.power_w} W" if want.power_w else "")
            st.last_command = {"at": dt_util.now().isoformat(), "mode": want.mode.value, "power_w": want.power_w, "result": "dry run: not sent"}
            self._log(did, f"DRY RUN: would send {what}")
            return
        if (not a.config.caps.power_control and want.mode is not Mode.AUTO and did not in self.manager.confirmed
                and not (self.manager.tests.get(did) or {}).get("ok")):
            st.awaiting_confirm = True        # a battery whose control is unproven: the first command waits for the user
            self._log(did, f"waiting for you to allow the first {want.mode.value} command (button on the battery's card), or pass Test control")
            return
        if not a.config.caps.power_control and not self.budget.allow(did, dt_util.now().strftime("%Y-%m-%d"), want.mode is Mode.AUTO):
            st.last_error = (f"Daily limit of {self.budget.limit} commands reached for {a.config.name}: HomePower will not send more "
                             "until tomorrow (it can still hand the battery back).")
            self._log(did, st.last_error)
            return
        st.awaiting_confirm = False
        st.last_applied = want
        if want.mode is not Mode.AUTO:
            self._set_release(did, st, True)
        st.last_command = {"at": dt_util.now().isoformat(), "mode": want.mode.value, "power_w": want.power_w, "result": "sent",
                           "why": st.reason, "soc": st.actual.soc if st.actual else None, "target_soc": want.target_soc,
                           "price": self._price_now(self._rates(), dt_util.now()), "grid_w": self.last_grid}
        try:
            await a.apply(want)
            st.actual = await a.read()
            st.last_error = None
            if not a.config.caps.power_control and want.mode in (Mode.CHARGE, Mode.HOLD):
                room = want.mode is Mode.HOLD or st.actual.soc is None or st.actual.soc < (want.target_soc or 95) - 3
                if room:
                    self.traces.start(did, want.mode.value, _time.time(), dt_util.now().isoformat(), st.actual.soc, st.actual.power_w)
                else:
                    self.traces.cancel(did)
            # Vendor state can lag; a remaining mismatch is retried after the cooldown.
            _, remaining = decide(want, st.actual, now=st.last_apply, last_apply=None)
            st.unconfirmed, st.waited = next_unconfirmed(st.unconfirmed, st.waited, bool(remaining), not a.config.caps.power_control,
                                                         self.traces.grace(did, self.settings.cooldown_s))
            st.last_command["result"] = "confirmed" if not remaining else ("waiting for the cloud to show it" if st.waited and not st.unconfirmed else "not yet confirmed")
            if want.mode is Mode.AUTO and not remaining:
                self._set_release(did, st, False)  # handed back and confirmed
            self._log(did, "verified" if not remaining else f"not yet confirmed: {remaining}")
        except Exception as err:  # noqa: BLE001 - one device must not stop the others
            st.last_error = str(err)
            st.last_command["result"] = f"failed: {err}"
            self._log(did, f"apply failed: {err}")

    # ------------------------------------------------------------------ control test
    async def test_control(self, device_id: str) -> dict:
        """Send a tiny safe command, check the battery follows it, then hand it back. Pauses normal control of it."""
        a = next((x for x in self.adapters if x.config.device_id == device_id), None)
        if a is None:
            return {"ok": False, "steps": [{"name": "find battery", "ok": False, "detail": "unknown battery"}]}
        steps: list[dict] = []

        def step(name: str, ok: bool, detail: str = "") -> bool:
            steps.append({"name": name, "ok": ok, "detail": detail})
            return ok

        caps = a.config.caps
        if not step("control allowed", bool(a.config.extra.get("control_enabled", True)),
                    "turn on 'Let HomePower send commands' for this battery first"):
            return {"ok": False, "steps": steps}
        self.testing.add(device_id)
        try:
            before = await a.read()
            if not step("battery readable", before.online, "could not read the battery"):
                return {"ok": False, "steps": steps}
            soc = before.soc if before.soc is not None else 50
            if Mode.CHARGE in caps.supported_modes and soc < 95:
                want = Desired(Mode.CHARGE, min(100, caps.max_charge_w or 100))
            elif Mode.DISCHARGE in caps.supported_modes and soc > max(caps.min_soc, 15) + 5:
                want = Desired(Mode.DISCHARGE, min(100, caps.max_discharge_w or 100))
            else:
                want = Desired(Mode.HOLD)
            self._set_release(device_id, self._snap.devices[device_id], True)
            drv = getattr(a, "driver", None)
            slow = not caps.power_control          # cloud batteries (EcoFlow) take a while to show a change
            if slow and want.mode is Mode.CHARGE and Mode.HOLD in caps.supported_modes:
                # Guided check: first prove the simpler command (Hold) takes effect, then Charge.
                await a.apply(Desired(Mode.HOLD))
                held, waited_h = None, 0
                while held is None or (held.mode is not Mode.HOLD and waited_h < 45):
                    await asyncio.sleep(10)
                    waited_h += 10
                    if drv is not None and hasattr(drv, "_cache"):
                        drv._cache = None
                    held = await a.read()
                step("sent hold", held.mode is Mode.HOLD,
                     f"after {waited_h} s the battery reports mode={held.mode.value if held.mode else '?'} (Hold raises its backup reserve to the current charge level)")
            await a.apply(want)
            after, waited = None, 0
            while after is None or (after.mode != want.mode and waited < (45 if slow else 0)):
                await asyncio.sleep(10 if slow else 6)
                waited += 10 if slow else 6
                if drv is not None and hasattr(drv, "_cache"):
                    drv._cache = None                  # make the next read ask the battery, not repeat the last answer
                after = await a.read()
            ok_mode = after.mode == want.mode
            ok_pw = want.power_w is None or after.set_power_w is None or abs(after.set_power_w - want.power_w) <= 10
            step(f"sent {want.mode.value}" + (f" {want.power_w} W" if want.power_w else ""), ok_mode and ok_pw,
                 f"battery reports mode={after.mode.value if after.mode else '?'}, setpoint={after.set_power_w}")
            if slow and want.mode is Mode.CHARGE:
                if drv is not None and hasattr(drv, "_cache"):
                    drv._cache = None
                later = await a.read()
                moved = (later.power_w or 0) >= 50 or (later.soc or 0) > soc + 0.5
                if moved:
                    self.traces.reset(device_id)       # proven by hand: Charge is offered again
                steps.append({"name": "battery actually charged", "ok": moved, "info": True, "detail": (
                    f"after {waited} s: power {later.power_w if later.power_w is not None else '?'} W, charge {later.soc if later.soc is not None else '?'} %. "
                    + ("Raising the reserve does start charging on this battery." if moved else
                       "The reserve was raised but it did not start charging yet: it may ignore the reserve in its current operating mode, "
                       "or it was already full. Check its app, and try again with the charge level lower."))})
            await a.apply(Desired(Mode.AUTO))
            await asyncio.sleep(3)
            if drv is not None and hasattr(drv, "_cache"):
                drv._cache = None
            final = await a.read()
            ok_back = final.mode in (Mode.AUTO, None)
            step("handed back", ok_back, f"battery reports mode={final.mode.value if final.mode else '?'}")
            if ok_back:
                self._set_release(device_id, self._snap.devices[device_id], False)
        except Exception as err:  # noqa: BLE001 - report, then still try to hand back
            step("error", False, str(err))
            try:
                await a.apply(Desired(Mode.AUTO))
            except Exception:  # noqa: BLE001
                pass
        finally:
            self.testing.discard(device_id)
        result = {"ok": all(s["ok"] for s in steps if not s.get("info")), "steps": steps}
        self.manager.tests[device_id] = {"ok": result["ok"], "at": dt_util.now().isoformat()}   # shown in the setup check
        self.hass.async_create_task(self.manager.save_runtime())
        return result

    # ------------------------------------------------------------------ views for the UI
    def preview_view(self, hours: int = 24) -> dict:
        now = dt_util.now()
        ids = [a.config.device_id for a in self.adapters]
        kw = dict(condition_ok=self._condition_ok, away=self.away)
        segs = build_preview(self.schedules, ids, now, hours, allocs=self.last_result.allocs, **kw)
        return {
            "segments": {d: [{"start": s.start.isoformat(), "end": s.end.isoformat(), "mode": s.mode,
                              "power_w": s.power_w, "source": s.source} for s in lst] for d, lst in segs.items()},
            "conflicts": [{**c, "start": c["start"].isoformat(), "end": c["end"].isoformat()}
                          for c in find_conflicts(self.schedules, ids, now, hours * 2, **kw)],
            "plan": [{"device_id": p.device_id, "start": p.start.isoformat(), "end": p.end.isoformat(),
                      "power_w": p.power_w, "price": p.price} for p in self.last_result.allocs],
            "notes": self.last_result.notes,
            "prices": self._price_slots(now, hours),
        }

    def _price_slots(self, now: datetime, hours: int) -> list[dict]:
        rates = self._rates()
        if rates is None:
            return []
        lo = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)
        found = rates.slots_between(lo, lo + timedelta(hours=hours))
        mean = sum(x.price for x in found) / len(found) if found else 0.0   # per-slot tariffs: 'cheap' = well below average
        return [{"start": x.start.isoformat(), "end": x.end.isoformat(), "price": round(to_main_units(x.price, self.settings.rates_unit), 4),
                 "cheap": rates.in_cheap_window(x.start.astimezone(now.tzinfo).time()) if rates.windows else (x.price < 0.9 * mean)}
                for x in found]

    def debug_bundle(self) -> dict:
        """One file for a bug report: everything below is already free of keys; serial numbers and addresses are removed too."""
        now = dt_util.now()
        diag = self.diagnostics()
        for d in diag.values():
            d["connection"] = {k: v for k, v in (d.get("connection") or {}).items() if k in ("region", "port", "unit_id")}
        from dataclasses import fields

        plain = {f.name: getattr(self.settings, f.name) for f in fields(self.settings)
                 if isinstance(getattr(self.settings, f.name), (int, float, str, bool, type(None))) and not re.search(r"notify|entity", f.name)}
        return {"made_at": now.isoformat(), "startup": self.startup_note, "settings": plain,
                "batteries": diag, "command_traces": self.traces.dump(), "charge_rates": dict(self.rate_learner.rates),
                "plan_against_actual": {a.config.device_id: self.plan_log.view(a.config.device_id, now) for a in self.adapters},
                "events": [list(e) for e in list(self._snap.events)[-150:]], "alerts": sorted(self.alerts.active())}

    def diagnostics(self) -> dict:
        out = {}
        for a in self.adapters:
            did, st = a.config.device_id, self._snap.devices.get(a.config.device_id)
            conn = dict(a.config.extra.get("connection", {}))
            for secret in ("secret_key", "access_key"):
                if secret in conn:
                    conn[secret] = "***"
            out[did] = {
                "name": a.config.name, "kind": a.config.kind, "driver": a.config.extra.get("driver"), "connection": conn,
                "control_enabled": bool(a.config.extra.get("control_enabled", True)),
                "last_read_ok": datetime.fromtimestamp(st.last_read_ok, dt_util.UTC).isoformat() if st and st.last_read_ok else None,
                "online": bool(st and st.actual and st.actual.online),
                "actual_mode": st.actual.mode.value if st and st.actual and st.actual.mode else None,
                "desired": ({"mode": st.desired.mode.value, "power_w": st.desired.power_w} if st and st.desired else None),
                "reason": st.reason if st else "", "decision": st.decision.value if st and st.decision else None,
                "last_command": st.last_command if st else None, "last_error": st.last_error if st else None,
                "unconfirmed": st.unconfirmed if st else 0, "release_pending": bool(st and st.release_pending),
                "word_order": getattr(getattr(a, "driver", None), "_order", None),
                "driver_error": getattr(getattr(a, "driver", None), "last_error", None),
                "command_traces": self.traces.recent(did, 10) or None,
                "recent_setting_changes": list(getattr(getattr(a, "driver", None), "changes", None) or []) or None,
                "raw_readings": {k: v for k, v in (getattr(getattr(a, "driver", None), "last_quota", None) or {}).items()
                                 if not _PRIVATE_KEY.search(k)} or None,
            }
        return out

    def devices_view(self, now: datetime | None = None) -> dict:
        """Per-battery part of the live view (also what the sensors' attributes are built from)."""
        now = now or dt_util.now()
        out = {}
        for a in self.adapters:
            did = a.config.device_id
            st = self._snap.devices.get(did) or DeviceStatus()
            act, want = st.actual, st.desired
            active = [s.id for s in self.schedules if s.applies(did, now)
                      and (not s.condition_entity or self._condition_ok(s.condition_entity))]
            ov = self.overrides.get(did)
            out[did] = {
                "online": bool(act and act.online),
                "soc": act.soc if act else None,
                "power_w": act.power_w if act else None,
                "pv_w": act.pv_w if act and act.online else None, "has_solar": self._has_solar(a.config),
                "mode": act.mode.value if act and act.mode else None,
                "protected": bool(act and act.protected), "off_grid": bool(act and act.off_grid),
                "limits": list(act.limits) if act and act.limits else None,
                "temp_c": act.temp_c if act else None, "firmware": act.firmware if act else None,
                "alarms": list(act.alarms) if act else [],
                "desired_mode": want.mode.value if want else None,
                "desired_power_w": want.power_w if want else None,
                "reason": st.reason,
                "decision": st.decision.value if st.decision else None,
                "managed": want is not None,
                "control_enabled": bool(a.config.extra.get("control_enabled", True)),
                "active_schedules": active,
                "override": ({"mode": ov[0].mode.value, "power_w": ov[0].power_w, "until": ov[1].isoformat()} if ov else None),
                "guard_held": st.guard_held,
                "last_error": st.last_error,
                "awaiting_confirm": st.awaiting_confirm,
                "charge_verdict": self.traces.verdict(did) if not a.config.caps.power_control else None,
                "traces": self.traces.recent(did) if not a.config.caps.power_control else [],
                "report": (ecoflow_report(self.traces.results.get(did, []), self.plan_log.view(did, now)["match_pct"],
                                          self.rate_learner.rates.get(did), self.traces.verdict(did))
                           if not a.config.caps.power_control else None),
                "measured_charge_w": self.rate_learner.rates.get(did) if not a.config.caps.power_control else None,
                "why": explain_idle(control_enabled=bool(a.config.extra.get("control_enabled", True)), paused=self.paused,
                                    dry_run=self.settings.dry_run, online=bool(act and act.online), off_grid=bool(act and act.off_grid),
                                    protected=bool(act and act.protected), last_error=st.last_error, guard_held=st.guard_held,
                                    unconfirmed=st.unconfirmed, awaiting_confirm=st.awaiting_confirm),
                "setting_changes": list(getattr(getattr(a, "driver", None), "changes", None) or [])[-8:],
            }
        return out

    def view(self) -> dict:
        """JSON-safe live view for the panel."""
        now = dt_util.now()
        day_key = now.strftime("%Y-%m-%d")
        out = self.devices_view(now)
        actuals = {did: st.actual for did, st in self._snap.devices.items() if st.actual}
        day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
        rates = self._rates()
        return {
            "devices": out,
            "events": [list(e) for e in list(self._snap.events)[-60:]],
            "grid_w": self.last_grid, "grid_source": self.grid_source, "grid_age_s": None if self.grid_age_s is None else round(self.grid_age_s),
            "now": now.isoformat(),
            "paused": self.paused, "backup": self.backup_active, "away": self.away,
            "alerts": sorted(self.alerts.active()), "notes": self.last_result.notes,
            "price_now": self._price_now(rates, now),
            "meter_entity": self.grid_source, "rates_entity": self._rates_entity(),
            "combined": self._combined(actuals), "setup": self.setup_check(), "solar_room": self.solar_room, "export_better": self._export_better(rates, now),
            "divert": {"entity": self.settings.divert_entity, "on": self.diverter.on} if self.settings.divert_entity else None,
            "daily": {"self_consumption": self.daily.self_consumption(day_key), "cheap_share": self.daily.cheap_share(day_key)},
            "plan_actual": {a.config.device_id: self.plan_log.view(a.config.device_id, now) for a in self.adapters},
            "health": {a.config.device_id: {"efficiency": self.health.efficiency(a.config.device_id)[-8:],
                                             "warning": self.health.degraded(a.config.device_id),
                                             "cycling": self._cycling(a.config.device_id, rates, now)} for a in self.adapters},
            "grid_events": self.grid_events.view(),
            "trend": {a.config.device_id: {"days": self.trend.series(a.config.device_id, 60), "warning": self.trend.warning(a.config.device_id)} for a in self.adapters},
            "forecast_reserve": self.forecast_floor, "readiness": self.readiness(), "startup": self.startup_note, "weather": self.weather,
            "learning": {"known_slots": sum(1 for n in self.profile.n["wd"] if n >= self.profile.MIN_VISITS)},
            "tariff_inferred": ({"start": self._tariff.start.strftime("%H:%M"), "end": self._tariff.end.strftime("%H:%M"),
                                 "offpeak": self._tariff.offpeak, "peak": self._tariff.peak, "days": round(self._tariff.days, 1)}
                                if self._tariff else None), "currency": self.settings.currency,
            "savings": {"today": round(self.ledger.saved(day), 2) + 0.0, "month": round(self.ledger.saved(month), 2) + 0.0},
            "energy": {k: round(v, 3) for k, v in self.meter.totals.items()},
        }

    def _cycling(self, did: str, rates: RateTable | None, now: datetime) -> dict | None:
        """What storing a kWh in this battery is worth right now: the cheapest import in the next 24 h against the dearest
        price it could avoid, at this battery's own measured round-trip efficiency (or an 85 % estimate until it has one)."""
        if rates is None:
            return None
        ahead = [to_main_units(x.price, self.settings.rates_unit) for x in rates.slots_between(now, now + timedelta(hours=24))]
        if len(ahead) < 2:
            return None
        eff = self.health.efficiency(did)
        measured = bool(eff)
        out = cycling_margin(min(ahead), max(ahead), (eff[-1][1] / 100) if measured else 0.85)
        out["measured"] = measured
        return out

    async def _on_action(self, event) -> None:
        """A button on a HomePower phone notification was tapped."""
        action = (event.data or {}).get("action")
        try:
            if action == ACTION_PAUSE:
                await self.manager.set_paused(True)
                self._log("events", "paused from a notification button")
            elif action == ACTION_HAND_BACK:
                await self.release_all()
                await self.manager.set_paused(True)          # stay handed back: otherwise the plan would take them over again
                self._log("events", "batteries handed back and HomePower paused from a notification button")
        except Exception:  # noqa: BLE001 - a button must never break the control loop
            _LOGGER.warning("notification action failed", exc_info=True)

    async def async_close(self) -> None:
        unsub = getattr(self, "_unsub_action", None)
        if unsub:
            unsub()
        await self.manager.save_runtime()
        for a in self.adapters:
            await a.close()

    async def release_all(self) -> None:
        """Best effort on shutdown: hand every battery we forced back to its own control."""
        if self.paused:
            return
        for a in self.adapters:
            st = self._snap.devices.get(a.config.device_id)
            if st and st.release_pending:
                try:
                    await a.apply(Desired(Mode.AUTO))
                    self._log(a.config.device_id, "released to the device's own control on shutdown")
                except Exception as err:  # noqa: BLE001 - flag stays set, retried on next start
                    _LOGGER.warning("release failed for %s: %s", a.config.device_id, err)
        await self.manager.save_runtime()
