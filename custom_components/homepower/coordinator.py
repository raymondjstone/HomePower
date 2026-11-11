"""Control loop: read batteries -> decide (core.engine) -> apply -> verify -> account -> alert."""
from __future__ import annotations

import re
import asyncio
import logging
import time as _time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

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
from .core.insights import BatteryFigures, DailyStats, Diverter, Health, LoadProfile, combine
from .core.rates import SLOT, RateTable, Slot, infer_offpeak, parse_rates, to_main_units
from .core.reconcile import Decision, decide, materially_different
from .core.schedule import ScheduleEntry
from .core.settings import AppSettings, build as build_settings

_LOGGER = logging.getLogger(__name__)
READ_TIMEOUT_S = 20
PERSIST_EVERY_S = 300
TARIFF_REFRESH_S = 6 * 3600
TARIFF_HISTORY_DAYS = 14
GLOW_DOMAIN = "hildebrandglow_dcc"  # ha-hildebrandglow-dcc (raymondjstone fork and HandyHat original)
KEEPALIVE_EVERY_S = 60
OFFLINE_ALERT_AFTER_S = 180
PROTECT_ALERT_AFTER_S = 60
UNCONFIRMED_ALERT_AFTER = 3
GRID_SLOW_AFTER_S = 120     # setup check: warn that the grid reading is not live
GRID_STALE_AFTER_S = 300    # control: too old to steer by (solar surplus tracking waits instead)
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
        self.ledger = Ledger(manager.ledger_days)
        self.profile = LoadProfile(manager.profile_data)   # the household's usual load by time of day
        self.daily = DailyStats(manager.daily_data)         # self-consumption / cheap-rate share per day
        self.health = Health(manager.health_data)           # weekly round-trip efficiency per battery
        self._event_state: dict[str, bool] = dict(manager.event_state)   # which heads-up messages are showing (survives restarts)
        self.diverter = Diverter()                           # solar-surplus load
        self._divert_entity = manager.divert_on or ""        # the entity we switched on, to switch the same one off
        self.diverter.on = bool(manager.divert_on)           # still on from before a restart: we must be able to switch it off
        self.alerts = AlertTracker()
        self.last_result = Result()
        self.last_grid: float | None = None
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
            self.alerts.forget(f"offline:{gone}"), self.alerts.forget(f"error:{gone}")
            self.alerts.forget(f"unconfirmed:{gone}"), self.alerts.forget(f"protect:{gone}"), self.alerts.forget(f"health:{gone}")
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
        ahead = [x.price for x in rates.slots_between(now, now + timedelta(hours=24))]
        cheapest = min(ahead) if ahead else rates.offpeak
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
                await self.hass.services.async_call(domain, name, {"title": "HomePower", "message": message if raised else f"Resolved: {message}"})
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
        for k in [k for k in self._event_state if k.startswith(("cheap_", "summary_")) and today not in k]:
            del self._event_state[k]            # yesterday's once-a-day markers
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
        act = self.diverter.decide(now_s, soc, grid, s.divert_export_w, s.divert_min_soc)
        if act:
            await self._switch(s.divert_entity, act == "on")
            self.manager.divert_on = s.divert_entity if act == "on" else ""
            self.hass.async_create_task(self.manager.save_runtime())
            self._log("divert", f"{s.divert_entity} switched {act} ({'surplus solar' if act == 'on' else 'no surplus'})")

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
        rates = self._rates()
        add("prices", rates is not None, "Electricity prices known", "" if rates is not None else
            "Pick a rates sensor or type your standard and cheap rate.", "tab", "settings")
        slots = bool(rates and rates.slots and len(rates.slots) > 2)
        exact = bool(s.offpeak_start and s.offpeak_end) or bool(s.offpeak_extra)
        add("tariff", exact or slots, "Cheap-rate hours confirmed", "" if exact or slots else
            ("They are only estimated from price history." if self._tariff else "Enter when your cheap rate starts and ends."), "tab", "settings")
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
                f"protect:{did}": (st.decision is Decision.SKIP_PROTECTED, f"{name} has manufacturer protection active, so HomePower is not overriding it.", PROTECT_ALERT_AFTER_S),
            }
            weak = self.health.degraded(did)
            checks[f"health:{did}"] = (weak is not None, f"{name}: {weak}", 0)
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

        self.hass.async_create_task(self._refresh_tariff())
        grid = self._grid_w(actuals)
        rates = self._rates()
        world = World(
            now=now, settings=self.settings.engine, schedules=self.schedules, goals=self.goals,
            overrides=self._active_overrides(now), grid_w=grid, condition_ok=self._condition_ok, usage_wh=self.profile.expected_wh,
            backup_active=self.backup_active, away=self.away, load_blocks=self._load_blocks(),
            grid_fresh=self.grid_age_s is None or self.grid_age_s <= GRID_STALE_AFTER_S,
            rates=rates, forecast_kwh=self._forecast_kwh(), cheap_now=self._is_cheap(rates, now, self._price_now(rates, now)),
            devices=[DevIn(a.config, actuals[a.config.device_id],
                           self._snap.devices[a.config.device_id].release_pending,
                           self._snap.devices[a.config.device_id].guard_held,
                           bool(a.config.extra.get("control_enabled", True)), self._has_solar(a.config)) for a in adapters],
            export_better=self._export_better(rates, now),
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

        if not self.paused:
            await self._keepalive(mono, adapters)
        self.last_grid = grid
        for step in (lambda: self._account(now, wall, actuals, grid, rates), lambda: self._check_alerts(wall, adapters),
                     lambda: self._check_events(now, wall, actuals, rates),
                     lambda: self._divert(wall, actuals, grid)):
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

    def _account(self, now: datetime, wall: float, actuals: dict[str, Actual], grid: float | None, rates: RateTable | None) -> None:
        """Energy counters and the savings ledger (an estimate: see core/energy.py)."""
        charged = discharged = 0.0
        for did, act in actuals.items():
            d_in, d_out = self.meter.add(f"bat:{did}", wall, act.power_w if act.online else None)
            charged, discharged = charged + d_in, discharged + d_out
        g_in, g_out = self.meter.add("grid", wall, grid)
        for did, act in actuals.items():
            if act.online and (act.pv_w or 0) > 50 and did not in self.manager.pv_seen:
                self.manager.pv_seen.add(did)      # this battery has panels: remembered for 'solar: auto'
        pv = [a.pv_w for a in actuals.values() if a.online and a.pv_w is not None]
        pv_in, _ = self.meter.add("pv", wall, sum(pv) if pv else None)
        price = self._price_now(rates, now)
        self.daily.add(now.strftime("%Y-%m-%d"), pv_kwh=pv_in, export_kwh=g_out, import_kwh=g_in, cheap=self._is_cheap(rates, now, price))
        self.profile.record(now, self._house_load(actuals, grid, sum(pv) if pv else 0.0))
        self.health.update(now.strftime("%G-W%V"), {did: (self.meter.get(f"bat:{did}_in"), self.meter.get(f"bat:{did}_out"))
                                                     for did in actuals})
        charging_w = sum(max(0.0, a.power_w or 0.0) for a in actuals.values() if a.online)
        forced = any(st.reason.startswith(FORCING_REASONS) and "(solar only)" not in st.reason   # solar-only charging costs nothing
                     and st.desired is not None and st.desired.mode is Mode.CHARGE
                     for st in self._snap.devices.values())
        share = 1.0 if forced else (min(1.0, max(0.0, grid) / charging_w) if grid is not None and charging_w > 0 else 0.0)
        self.ledger.add(now.strftime("%Y-%m-%d"), charged, share, discharged, self._price_now(rates, now))

    async def _apply_and_verify(self, a: DeviceAdapter, st: DeviceStatus, want: Desired, diffs) -> None:
        did = a.config.device_id
        self._log(did, f"{st.reason or 'plan'}: mismatch {[(d.field, d.desired, d.actual) for d in diffs]}; applying")
        st.last_apply = _time.monotonic()
        st.last_applied = want
        if want.mode is not Mode.AUTO:
            self._set_release(did, st, True)
        st.last_command = {"at": dt_util.now().isoformat(), "mode": want.mode.value, "power_w": want.power_w, "result": "sent"}
        try:
            await a.apply(want)
            st.actual = await a.read()
            st.last_error = None
            # Vendor state can lag; a remaining mismatch is retried after the cooldown.
            _, remaining = decide(want, st.actual, now=st.last_apply, last_apply=None)
            st.unconfirmed = st.unconfirmed + 1 if remaining else 0
            st.last_command["result"] = "confirmed" if not remaining else "not yet confirmed"
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
            await a.apply(want)
            await asyncio.sleep(6)
            after = await a.read()
            ok_mode = after.mode == want.mode
            ok_pw = want.power_w is None or after.set_power_w is None or abs(after.set_power_w - want.power_w) <= 10
            step(f"sent {want.mode.value}" + (f" {want.power_w} W" if want.power_w else ""), ok_mode and ok_pw,
                 f"battery reports mode={after.mode.value if after.mode else '?'}, setpoint={after.set_power_w}")
            await a.apply(Desired(Mode.AUTO))
            await asyncio.sleep(3)
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
        result = {"ok": all(s["ok"] for s in steps), "steps": steps}
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
                "protected": bool(act and act.protected),
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
            "combined": self._combined(actuals), "setup": self.setup_check(), "export_better": self._export_better(rates, now),
            "divert": {"entity": self.settings.divert_entity, "on": self.diverter.on} if self.settings.divert_entity else None,
            "daily": {"self_consumption": self.daily.self_consumption(day_key), "cheap_share": self.daily.cheap_share(day_key)},
            "health": {a.config.device_id: {"efficiency": self.health.efficiency(a.config.device_id)[-8:],
                                             "warning": self.health.degraded(a.config.device_id)} for a in self.adapters},
            "learning": {"known_slots": sum(1 for n in self.profile.n["wd"] if n >= self.profile.MIN_VISITS)},
            "tariff_inferred": ({"start": self._tariff.start.strftime("%H:%M"), "end": self._tariff.end.strftime("%H:%M"),
                                 "offpeak": self._tariff.offpeak, "peak": self._tariff.peak, "days": round(self._tariff.days, 1)}
                                if self._tariff else None), "currency": self.settings.currency,
            "savings": {"today": round(self.ledger.saved(day), 2) + 0.0, "month": round(self.ledger.saved(month), 2) + 0.0},
            "energy": {k: round(v, 3) for k, v in self.meter.totals.items()},
        }

    async def async_close(self) -> None:
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
