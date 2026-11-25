"""Control loop: read batteries -> decide (core.engine) -> apply -> verify -> account -> alert."""
from __future__ import annotations

import asyncio
import sys
import time as _time
from collections import deque
from dataclasses import replace as _replace
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
from .core.meter import MeterReader
from .core.pv_health import PvHealth
from .core.insights import BatteryFigures, DailyStats, Diverter, GridEvents, Health, HealthTrend, LoadProfile, PlanLog, UsageHistory, combine
from .core.rates import RateTable
from .core.report import charge_efficiency, startup_summary
from .core.traces import CommandTrace
from .core.weather import ColdModel, TempLog
from .core.sanity import ChargeWatch, CommandBudget, RateLearner, next_unconfirmed
from .core.reconcile import Decision, decide, materially_different
from .core.schedule import ScheduleEntry
from .core.settings import AppSettings, build as build_settings
from .core.solar_forecast import SolarForecastLog
from .coord_base import (  # noqa: F401 - shared names, also re-exported from coordinator.py
    _LOGGER, READ_TIMEOUT_S, HANDBACK_TIMEOUT_S, APPLY_TIMEOUT_S, PERSIST_EVERY_S, TARIFF_REFRESH_S,
    TARIFF_HISTORY_DAYS, FORECAST_PLATFORMS, GLOW_DOMAIN, KEEPALIVE_EVERY_S, OFFLINE_ALERT_AFTER_S, PROTECT_ALERT_AFTER_S,
    UNCONFIRMED_ALERT_AFTER, GRID_SLOW_AFTER_S, GRID_STALE_AFTER_S, ACTION_PAUSE, ACTION_HAND_BACK, FORCING_REASONS,
    REPAIR_KINDS, repair_id, DeviceStatus, x1_limits_take, x1_limits_keep, Snapshot, _PRIVATE_KEY, _real, MONEY_SAVED, MONEY_EXPORT,
)
from .coord_prices import PricesMixin
from .coord_alerts import AlertsMixin
from .coord_forecast import ForecastMixin
from .coord_reports import ReportsMixin
from .coord_inputs import InputsMixin
from .coord_accounting import AccountingMixin


class HomePowerCoordinator(InputsMixin, AccountingMixin, PricesMixin, AlertsMixin, ForecastMixin, ReportsMixin, DataUpdateCoordinator[Snapshot]):
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
        self._logged_failures: dict[str, tuple[str, float]] = {}   # see _log_failure
        self.solar_log = SolarForecastLog(getattr(manager, "solar_forecast_data", None))   # forecast per day, for forecast vs actual
        self.solar_hours: dict[str, list[float]] = {}    # latest hourly solar forecast (kWh per local hour) by day
        self.forecast_source: str | None = None          # which forecast integration is used
        self._solar_at, self._solar_busy, self._solar_pruned = -1e9, False, ""
        self._started = False
        self._began = _time.monotonic()   # a battery not read since start-up counts its failed reads from here
        self.rate_learner = RateLearner(manager.charge_rates)   # real charge rate of batteries that pick their own
        self.budget = CommandBudget()                       # daily cap on commands to cloud batteries
        self.charge_watch = ChargeWatch()                    # told to charge but nothing happens
        self.traces = CommandTrace(manager.trace_data)       # what happened after each command to EcoFlow batteries
        self.plan_log = PlanLog(manager.plan_log_data)       # planned against actual power per half hour
        self.health = Health(manager.health_data)           # weekly round-trip efficiency per battery
        self.grid_events = GridEvents(manager.grid_events)  # each time a battery reported the grid down
        self.trend = HealthTrend(manager.trend_data)        # daily temperature and pack-voltage spread per battery
        self.pv_health = PvHealth(getattr(manager, "pv_health_data", None))   # X1: each solar string against the others
        self._exp_table: RateTable | None = None            # export prices (Settings > export rates sensor), read each cycle
        self.grid_event = False                             # a saving session (or similar) is running
        self.day_plan = None                                # where stored energy is worth most (core/dayplan.py), for the Plan tab
        self._clear_stale_repairs()
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
        self._retry_handback: set[str] = set()                     # a hand-back that failed (safe mode, the phone button): the loop keeps trying even while paused
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
        if not value:
            self._retry_handback.discard(did)
        self.manager.on_forced_change(set(self._forced_ids))

    def apply_settings(self, cfg: dict) -> None:
        """Apply changed global settings in place (no integration reload)."""
        self.settings = build_settings(cfg)
        self.manager.set_live(self.settings.ecoflow_live)

    def set_config(self, adapters: list[DeviceAdapter], schedules: list[ScheduleEntry], goals: list[Goal]) -> None:
        for gone in {a.config.device_id for a in self.adapters} - {a.config.device_id for a in adapters}:
            dismissed: list[str] = []
            for kind in ("offline", "error", "grid", "unconfirmed", "stuck", "gridsign", "protect", "health", "trend"):
                dismissed += self.alerts.forget(f"{kind}:{gone}")
            dismissed += self.alerts.forget(f"alarm:{gone}:")
            dismissed += self.alerts.forget(f"pv:{gone}:")
            self.pv_health.forget(gone)
            for key in dismissed:                      # a notification about a battery that no longer exists must not stay on screen
                self.hass.async_create_task(self._notify(key, "", False))
            self.charge_watch.forget(gone), self.rate_learner.forget(gone), self.traces.forget(gone), self._forget_plan(gone)
            self.health.forget(gone)
            for key in (f"bat:{gone}_in", f"bat:{gone}_out"):      # exactly its own: a prefix would also catch "garage_2" for "garage"
                self.meter.totals.pop(key, None)                      # its lifetime energy: a new battery with the same name starts from zero
            self._event_state.pop(f"fw:{gone}", None)
            for k in [k for k in self._event_state if k.startswith(f"x1lim:{gone}:")]:
                del self._event_state[k]
            self.grid_events.forget(gone, dt_util.now())
            self.trend.days.pop(gone, None)
        self.adapters, self.schedules, self.goals = adapters, schedules, goals
        old = self._snap.devices
        self._snap.devices = {a.config.device_id: old.get(a.config.device_id) or self._new_status(a.config.device_id) for a in adapters}
        self.overrides = {k: v for k, v in self.overrides.items() if k in self._snap.devices}

    # ------------------------------------------------------------------ manual overrides
    def set_override(self, device_ids: list[str] | None, desired: Desired, minutes: int) -> None:
        known = {a.config.device_id for a in self.adapters}
        if device_ids and (unknown := sorted(set(device_ids) - known)):
            # a mistyped id in an automation would otherwise do nothing at all, silently
            raise ValueError(f"no battery called {', '.join(unknown)} (known: {', '.join(sorted(known)) or 'none'})")
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

    # ------------------------------------------------------------------ logging / notifications
    def _log(self, device_id: str, msg: str) -> None:
        self._snap.events.append((dt_util.now().isoformat(), device_id, msg))
        _LOGGER.info("%s: %s", device_id, msg)

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

    # ------------------------------------------------------------------ the loop
    async def _read(self, a: DeviceAdapter) -> Actual:
        st = self._snap.devices.get(a.config.device_id)
        if st is not None and a.config.device_id in self.testing and st.actual is not None:
            return st.actual
        try:
            return self._finite(await asyncio.wait_for(a.read(), READ_TIMEOUT_S))
        except Exception:  # noqa: BLE001 - one device must not stop the others
            _LOGGER.debug("read failed for %s", a.config.device_id, exc_info=True)
            return Actual(online=False)

    def _note_link(self, st: DeviceStatus, act: Actual, mono: float) -> None:
        """Online, stale or offline. A failed read is often only another Modbus client holding the battery for a moment, so it
        is shown offline only once reads have failed for the offline-after time, or when the failure shows it is off the network.
        (Control is unchanged: nothing is commanded and nothing is counted from a battery that was not read this cycle.)"""
        # The monotonic clock: a Raspberry Pi without a clock chip can jump the wall clock by years once it syncs.
        if act.online:
            st.last_read_ok, st.ok_mono, st.last_good, st.link = _time.time(), mono, act, "online"
            return
        since = st.ok_mono if st.ok_mono is not None else self._began
        # Once offline it stays offline until a read works: a battery off the network can fail with "unreachable" one cycle
        # and a time-out the next, and must not flip back to "stale".
        st.link = ("offline" if st.link == "offline" or act.gone or mono - since >= 60 * self.settings.offline_after_min
                   else "stale")

    def link(self, did: str) -> str:
        st = self._snap.devices.get(did)
        if st is None:
            return "offline"
        return "stale" if st.actual is None else st.link      # not read yet (just added): waiting, not offline

    def shown_actual(self, did: str) -> Actual | None:
        """The reading to show for a battery: this cycle's, or while the readings are only stale, the last one that worked."""
        st = self._snap.devices.get(did)
        if st is None or st.actual is None:
            return None
        if st.actual.online:
            return st.actual
        return st.last_good if st.link == "stale" else None

    def shown_actuals(self) -> dict[str, Actual]:
        """Every battery's reading as shown (see shown_actual), for the panel's and the sensors' totals."""
        return {did: self.shown_actual(did) or st.actual for did, st in self._snap.devices.items() if st.actual}

    def _log_failure(self, key: str, msg: str, *args) -> None:
        """For the safety nets of the control loop (it runs every 10 s): the first time a failure happens, or when it
        changes, or an hour after it was last logged, log it with its traceback; otherwise only at debug level, so a lasting
        problem does not fill the log with the same traceback thousands of times a day."""
        err = sys.exc_info()[1]
        sig, now = f"{type(err).__name__}: {err}", _time.monotonic()
        last = self._logged_failures.get(key)
        if last is None or last[0] != sig or now - last[1] >= 3600:
            self._logged_failures[key] = (sig, now)
            _LOGGER.exception(msg + (" (logged once an hour while it lasts)" if last and last[0] == sig else ""), *args)
        else:
            _LOGGER.debug(msg + ": %s", *args, sig)

    def _commanded(self, did: str) -> Mode | None:
        """What HomePower last told this battery to do, if it was sent (a paused, monitor-only or tested battery was told nothing)."""
        st = self._snap.devices.get(did)
        if st is None or st.desired is None or st.decision is None:
            return None
        return st.desired.mode

    @staticmethod
    def _finite(act: Actual) -> Actual:
        """NaN and infinity from a device or its cloud are no reading: they would stop the decision engine and poison the saved totals."""
        bad = {f: None for f in ("soc", "power_w", "grid_w", "pv_w", "load_w", "temp_c", "pv_total_w")
               if isinstance(getattr(act, f), float) and (getattr(act, f) != getattr(act, f) or abs(getattr(act, f)) == float("inf"))}
        return _replace(act, **bad) if bad else act

    async def _async_update_data(self) -> Snapshot:
        now, mono, wall = dt_util.now(), _time.monotonic(), _time.time()
        adapters = list(self.adapters)   # a snapshot: the panel can add/remove batteries while we await I/O
        reads = await asyncio.gather(*(self._read(a) for a in adapters))
        actuals = {a.config.device_id: act for a, act in zip(adapters, reads)}
        # A battery removed while we were reading is dropped; one added meanwhile waits for the next cycle.
        adapters = [a for a in adapters if a.config.device_id in self._snap.devices]
        actuals = {k: v for k, v in actuals.items() if k in self._snap.devices}
        for did, act in actuals.items():
            self._note_link(self._snap.devices[did], act, mono)

        self._observe_temp(now)
        # tied to the config entry, so an unload or reload cancels a fetch still running (saves stay on hass: they must finish)
        for name, job in (("tariff", self._refresh_tariff), ("weather", self._refresh_weather), ("solar forecast", self._refresh_solar_hours)):
            self.config_entry.async_create_background_task(self.hass, job(), f"homepower {name} refresh")
        grid = self._grid_w(actuals)
        rates = self._rates()
        self._exp_table = self._export_table()
        event = self._grid_event_on()
        if event != self.grid_event:
            self.grid_event = event
            self._log("events", "grid event started: batteries discharge and nothing charges from the grid" if event else "grid event ended")
        try:
            self.day_plan = self._day_plan(rates, now, actuals)
        except Exception:  # noqa: BLE001 - without a plan the simpler rules apply
            self._log_failure("day plan", "day plan failed; using the simpler rules this cycle")
            self.day_plan = None
        before_event = self._event_goal(now)
        world = World(
            now=now, settings=_replace(self.settings.engine, wear_cost=self._wear(rates)), schedules=self.schedules,
            goals=self.goals + ([before_event] if before_event else []), day_plan=self.day_plan,
            export_price_now=self._export_price_now(rates, now), buyback_price=self._buyback_price(rates, now),
            # with a day plan the house's own use is in the plan, so only the level the user set is kept back from paid export
            export_keep_pct=(0 if not self.settings.engine.export_discharge else int(self.settings.export_keep_pct or 0) if self.day_plan
                             else self._export_keep_pct(rates, now)), grid_event=event,
            overrides=self._active_overrides(now), grid_w=grid, condition_ok=self._condition_ok, usage_wh=self.profile.expected_wh,
            backup_active=self.backup_active, away=self.away, load_blocks=self._load_blocks(), reserve_floor_pct=self._forecast_floor(now),
            grid_fresh=self.grid_age_s is None or self.grid_age_s <= GRID_STALE_AFTER_S,
            rates=rates, forecast_kwh=self._forecast_kwh(), cheap_now=self._is_cheap(rates, now, self._price_now(rates, now)),
            devices=[DevIn(self._effective_config(a.config), actuals[a.config.device_id],
                           self._snap.devices[a.config.device_id].release_pending,
                           self._snap.devices[a.config.device_id].guard_held,
                           bool(a.config.extra.get("control_enabled", True)), self._has_solar(a.config),
                           self._commanded(a.config.device_id)) for a in adapters],
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
            self._log_failure("engine", "decision engine failed; leaving the batteries alone this cycle")
            res = Result(notes=["internal error in the decision engine: no commands were sent this cycle (see the Home Assistant log)"])
        self.last_result = res

        for a in adapters:
            did = a.config.device_id
            st = self._snap.devices.get(did)
            if st is None:        # removed while this cycle was reading
                continue
            if next((x for x in self.adapters if x.config.device_id == did), None) is not a:
                continue          # replaced while this cycle was reading (settings saved, control switched off): its next cycle decides with the new settings
            st.actual = actuals[did]
            st.desired = res.wants.get(did)
            st.reason = res.reasons.get(did, "")
            st.guard_held = res.guard_held.get(did, False)
            try:
                self._note_decision(did, a.config.name, st, actuals[did], world, res, grid, now)
            except Exception:  # noqa: BLE001 - the decision log is for people: a bug there must not stop control of this or the next battery
                self._log_failure(f"decision log {did}", "could not record the decision for %s", did)
            if st.desired is None:  # nothing manages it: device runs its own logic
                st.decision = None
                st.unconfirmed = 0
                st.last_error = None   # nothing is being asked of it, so an old failure no longer applies
                continue
            if st.desired.mode is Mode.AUTO and st.actual.online and st.actual.mode in (Mode.AUTO, None) and did not in self.testing:
                self._set_release(did, st, False)  # handed back and confirmed
            control_on = bool(a.config.extra.get("control_enabled", True))
            handing_back = st.desired.mode is Mode.AUTO and st.release_pending     # giving a battery back is always safe to send
            may_hand_back = handing_back and (not control_on or did in self._retry_handback or self.manager.safe)
            if did in self.testing or ((self.paused or not control_on) and not may_hand_back):
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
        for i, step in enumerate((lambda: self._account(now, wall, actuals, grid, rates), lambda: self._check_alerts(mono, adapters), lambda: self._learn_rates(adapters), lambda: self._record_plan(now, actuals), lambda: self._watch_traces(wall, now, actuals), lambda: self._check_tariff_drift(wall),
                     lambda: self._check_events(now, wall, actuals, rates), lambda: self._watch_pv(now, mono, actuals),
                     lambda: self._divert(mono, actuals, grid), lambda: self._sync_x1_limits(mono, adapters),
                     lambda: self._note_solar(now))):
            try:   # accounting / alerts are secondary: a bug there must not stop control
                r = step()
                if asyncio.iscoroutine(r):
                    await r
            except Exception:  # noqa: BLE001
                self._log_failure(f"secondary step {i}", "secondary step failed (accounting/alerts); control continues")
        if mono - self._last_persist >= PERSIST_EVERY_S:
            self._last_persist = mono
            try:
                await self.manager.save_runtime()
            except Exception:  # noqa: BLE001
                self._log_failure("save", "could not save HomePower state")
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
        """Optional: keep an Anker X1's own import/export limits in line with HomePower's (every few minutes). With the option
        off, or the battery monitor-only, the limits HomePower wrote are taken back off, putting back what was there before."""
        s = self.settings
        if self.paused or mono - self._last_limit_sync < 300:
            return                                     # paused: batteries are left exactly as they are
        self._last_limit_sync = mono
        for a in adapters:
            drv = getattr(a, "driver", None)
            if not (a.config.extra.get("house_meter") and hasattr(drv, "set_grid_limits")):
                continue
            did = a.config.device_id
            mirror = s.x1_grid_limits and a.config.extra.get("control_enabled", True)
            if mirror and s.dry_run:
                continue                               # test run: nothing new is written (handing back still is, below)
            if not mirror and not any(k.startswith(f"x1lim:{did}:") for k in self._event_state):
                continue                               # nothing of HomePower's in the inverter
            st = self._snap.devices.get(did)
            if st is None or not (st.actual and st.actual.online) or (st.actual and st.actual.off_grid):
                continue
            await self._x1_limits(a, s.engine.import_limit_w if mirror else None, s.engine.export_limit_w if mirror else None)

    async def _x1_limits(self, a: DeviceAdapter, import_w: int | None, export_w: int | None) -> None:
        did = a.config.device_id
        mine, orig = x1_limits_take(self._event_state, did)
        try:
            changed = await a.driver.set_grid_limits(import_w, export_w, mine, orig)
        finally:
            x1_limits_keep(self._event_state, did, mine, orig)
        if changed:
            self._log(did, "set the inverter's own " + " and ".join(changed))

    async def release_x1_limits(self, device_id: str) -> None:
        """A battery is being deleted: take HomePower's own X1 limits off it first, putting back what was there before."""
        a = next((x for x in self.adapters if x.config.device_id == device_id), None)
        if a is None or not hasattr(getattr(a, "driver", None), "set_grid_limits") or \
                not any(k.startswith(f"x1lim:{device_id}:") for k in self._event_state):
            return
        try:
            await asyncio.wait_for(self._x1_limits(a, None, None), HANDBACK_TIMEOUT_S)
        except Exception as err:  # noqa: BLE001 - best effort: deleting must still work with the battery unreachable
            from .drivers.modbus import ModbusError
            expected = isinstance(err, (OSError, asyncio.TimeoutError, ModbusError))
            _LOGGER.warning("could not take HomePower's limits off %s (%s); check them in the Anker app", device_id,
                            err or type(err).__name__, exc_info=not expected)
        for k in [k for k in self._event_state if k.startswith((f"x1lim:{device_id}:", f"x1orig:{device_id}:"))]:
            del self._event_state[k]

    # ------------------------------------------------------------------ weather (cold-day target)

    def _startup_check(self, actuals: dict[str, Actual]) -> None:
        """Once after each start: what was picked up again, and which batteries cannot be reached."""
        m = self.manager
        restored = {"saved hand-back settings": len(m.ecoflow_orig), "learned charge rates": len(self.rate_learner.rates),
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

    @staticmethod
    def _grid_charging(st: DeviceStatus) -> bool:
        """Is this battery being told to charge from the grid at a fixed power (schedule, goal, override, backup)?"""
        return (st.desired is not None and st.desired.mode is Mode.CHARGE and st.reason.startswith(FORCING_REASONS)
                and "(solar only)" not in st.reason and "(grid + solar)" not in st.reason)

    async def _apply_and_verify(self, a: DeviceAdapter, st: DeviceStatus, want: Desired, diffs) -> None:
        did = a.config.device_id
        self._log(did, f"{st.reason or 'plan'}: mismatch {[(d.field, d.desired, d.actual) for d in diffs]}; applying")
        st.last_apply = _time.monotonic()
        if self.settings.dry_run and want.mode is not Mode.AUTO:   # test run: show what would be sent, send nothing (re-logged once per cooldown); handing back is still sent
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
            try:
                await asyncio.wait_for(a.apply(want), APPLY_TIMEOUT_S)
            except asyncio.TimeoutError as err:
                raise RuntimeError(f"the battery did not answer the command within {APPLY_TIMEOUT_S} s") from err
            st.actual = await self._read(a)
            self._note_link(st, st.actual, _time.monotonic())     # a failed re-read is stale readings too, not offline at once
            st.last_error = None
            if not a.config.caps.power_control and want.mode in (Mode.CHARGE, Mode.HOLD):
                room = want.mode is Mode.HOLD or st.actual.soc is None or st.actual.soc < (want.target_soc or 95) - 3
                if room:
                    self.traces.start(did, want.mode.value, _time.time(), dt_util.now().isoformat(), st.actual.soc, st.actual.power_w)
                else:
                    self.traces.cancel(did)
            # Vendor state can lag; a remaining mismatch is retried after the cooldown.
            # No reading after the command is not a confirmation (an offline battery shows no differences at all).
            remaining = decide(want, st.actual, now=st.last_apply, last_apply=None)[1] if st.actual.online else ["no reading after the command"]
            st.unconfirmed, st.waited = next_unconfirmed(st.unconfirmed, st.waited, bool(remaining), not a.config.caps.power_control,
                                                         self.traces.grace(did, self.settings.cooldown_s))
            st.last_command["result"] = "confirmed" if not remaining else ("waiting for the cloud to show it" if st.waited and not st.unconfirmed else "not yet confirmed")
            if want.mode is Mode.AUTO:
                self.traces.cancel(did)            # handed back: whatever it was doing before is no longer being judged
            if want.mode is Mode.AUTO and not remaining:
                self._set_release(did, st, False)  # handed back and confirmed
            self._log(did, "verified" if not remaining else f"not yet confirmed: {remaining}")
        except Exception as err:  # noqa: BLE001 - one device must not stop the others
            st.last_error = str(err)
            st.last_command["result"] = f"failed: {err}"
            self._log(did, f"apply failed: {err}")

    # ------------------------------------------------------------------ control test
    @staticmethod
    def _test_watts(a) -> int:
        """Test power: 100 W, but 300 W for the local EcoFlow driver, whose power sign is unconfirmed (the direction check needs a step
        clearly above the house's own ups and downs; 300 W for a few seconds either way is harmless)."""
        return 300 if a.config.extra.get("driver") == "ecoflow_modbus" else 100

    def repair_done(self, device_id: str, tested_ok: bool = False) -> None:
        """A Repairs fix for this battery finished, and Home Assistant removed the issue. Its alerts are forgotten so they are
        raised again (with a new issue) if the problem carries on; after a passed test the old count of missed commands goes."""
        for kind in ("unconfirmed", "stuck"):
            self.alerts.forget(f"{kind}:{device_id}")
        if tested_ok:
            st = self._snap.devices.get(device_id)
            if st is not None:
                st.unconfirmed = 0
            self.charge_watch.forget(device_id)

    async def test_control(self, device_id: str) -> dict:
        """Send a tiny safe command, check the battery follows it, then hand it back. Pauses normal control of it."""
        a = next((x for x in self.adapters if x.config.device_id == device_id), None)
        if a is None:
            return {"ok": False, "steps": [{"name": "find battery", "ok": False, "detail": "unknown battery"}]}
        steps: list[dict] = []
        if device_id in self.testing:
            return {"ok": False, "steps": [{"name": "already testing", "ok": False, "detail": "a test of this battery is already running: wait for it to finish"}]}
        if self.manager.safe:
            return {"ok": False, "steps": [{"name": "safe mode", "ok": False, "detail": "safe mode is on, so HomePower sends nothing: leave safe mode first"}]}

        async def within(coro, limit: float = APPLY_TIMEOUT_S):
            """A battery that stops answering mid-test must not hang the test (and keep the battery out of control) for ever."""
            try:
                return await asyncio.wait_for(coro, limit)
            except asyncio.TimeoutError as err:
                raise RuntimeError(f"the battery did not answer within {limit:g} s") from err

        def step(name: str, ok: bool, detail: str = "") -> bool:
            steps.append({"name": name, "ok": ok, "detail": detail})
            return ok

        caps = a.config.caps
        allowed = bool(a.config.extra.get("control_enabled", True))
        if not step("control allowed", allowed, "" if allowed else "turn on 'Let HomePower send commands' for this battery first"):
            return {"ok": False, "steps": steps}
        self.testing.add(device_id)
        try:
            before = await within(a.read())
            if not step("battery readable", before.online, "" if before.online else "could not read the battery"):
                return {"ok": False, "steps": steps}
            soc = before.soc if before.soc is not None else 50
            if Mode.CHARGE in caps.supported_modes and soc < 95:
                want = Desired(Mode.CHARGE, min(self._test_watts(a), caps.max_charge_w or 100))
            elif Mode.DISCHARGE in caps.supported_modes and soc > max(caps.min_soc, 15) + 5:
                want = Desired(Mode.DISCHARGE, min(self._test_watts(a), caps.max_discharge_w or 100))
            else:
                want = Desired(Mode.HOLD)
            self._set_release(device_id, self._snap.devices[device_id], True)
            drv = getattr(a, "driver", None)
            slow = not caps.power_control          # cloud batteries (EcoFlow) take a while to show a change
            if slow and want.mode is Mode.CHARGE and Mode.HOLD in caps.supported_modes:
                # Guided check: first prove the simpler command (Hold) takes effect, then Charge.
                await within(a.apply(Desired(Mode.HOLD)))
                held, waited_h = None, 0
                while held is None or (held.mode is not Mode.HOLD and waited_h < 45):
                    await asyncio.sleep(10)
                    waited_h += 10
                    if drv is not None and hasattr(drv, "_cache"):
                        drv._cache = None
                    held = await within(a.read())
                step("sent hold", held.mode is Mode.HOLD,
                     f"after {waited_h} s the battery reports mode={held.mode.value if held.mode else '?'} (Hold raises its backup reserve to the current charge level)")
            await within(a.apply(want))
            after, waited = None, 0
            while after is None or (after.mode != want.mode and waited < (45 if slow else 0)):
                await asyncio.sleep(10 if slow else 6)
                waited += 10 if slow else 6
                if drv is not None and hasattr(drv, "_cache"):
                    drv._cache = None                  # make the next read ask the battery, not repeat the last answer
                after = await within(a.read())
            ok_mode = after.mode == want.mode
            ok_pw = want.power_w is None or after.set_power_w is None or abs(after.set_power_w - want.power_w) <= 10
            step(f"sent {want.mode.value}" + (f" {want.power_w} W" if want.power_w and caps.power_control else ""), ok_mode and ok_pw,
                 f"battery reports mode={after.mode.value if after.mode else '?'}, setpoint={after.set_power_w}")
            if slow and want.mode is Mode.CHARGE:
                if drv is not None and hasattr(drv, "_cache"):
                    drv._cache = None
                later = await within(a.read())
                moved = (later.power_w or 0) >= 50 or (later.soc or 0) > soc + 0.5
                if moved:
                    self.traces.reset(device_id)       # proven by hand: Charge is offered again
                steps.append({"name": "battery actually charged", "ok": moved, "info": True, "detail": (
                    f"after {waited} s: power {later.power_w if later.power_w is not None else '?'} W, charge {later.soc if later.soc is not None else '?'} %. "
                    + ("Raising the reserve does start charging on this battery." if moved else
                       "The reserve was raised but it did not start charging yet: it may ignore the reserve in its current operating mode, "
                       "or it was already full. Check its app, and try again with the charge level lower."))})
            if a.config.extra.get("driver") == "ecoflow_modbus" and want.mode in (Mode.CHARGE, Mode.DISCHARGE) and ok_mode:
                # The set point reads back from the same register that was written, so it cannot show a reversed sign: check the
                # battery's own power reading moves the way the command says (only for the local EcoFlow driver, whose sign is unconfirmed; an info line).
                await asyncio.sleep(6)
                later = await within(a.read())
                sign = 1 if want.mode is Mode.CHARGE else -1
                p, p0 = later.power_w, before.power_w
                moved = None if p is None or p0 is None else (p - p0) * sign       # how far the power moved the way the command asks
                half = (want.power_w or 100) * 0.4
                if moved is None:
                    detail, good = "the battery did not report its power", True
                elif moved < -half:
                    detail = (f"asked to {want.mode.value} {want.power_w} W but the battery's power went from {p0:.0f} W to {p:.0f} W, the opposite way."
                              + (" Its charge/discharge sign looks reversed: tick 'Reverse the charge/discharge sign' in this battery's connection settings and test again."
                                 if a.config.extra.get("driver") == "ecoflow_modbus" else " Check its app for a mode or schedule overriding HomePower."))
                    good = False
                elif moved > half:
                    detail, good = f"asked to {want.mode.value} {want.power_w} W; the battery's power went from {p0:.0f} W to {p:.0f} W.", True
                else:
                    detail, good = f"asked to {want.mode.value} {want.power_w} W; the battery's power is {p:.0f} W (it had not clearly moved yet, so the direction could not be checked).", True
                steps.append({"name": "power moves the right way", "ok": good, "info": True, "detail": detail})
            await within(a.apply(Desired(Mode.AUTO)))
            await asyncio.sleep(3)
            if drv is not None and hasattr(drv, "_cache"):
                drv._cache = None
            final = await within(a.read())
            ok_back = final.mode in (Mode.AUTO, None)
            step("handed back", ok_back, f"battery reports mode={final.mode.value if final.mode else '?'}")
            if ok_back:
                self._set_release(device_id, self._snap.devices[device_id], False)
        except Exception as err:  # noqa: BLE001 - report, then still try to hand back
            step("error", False, str(err))
            try:
                await within(a.apply(Desired(Mode.AUTO)), HANDBACK_TIMEOUT_S)
            except Exception:  # noqa: BLE001
                pass
        finally:
            self.testing.discard(device_id)
        result = {"ok": all(s["ok"] for s in steps if not s.get("info")), "steps": steps}
        # shown in the setup check; the steps also go into the hardware report
        self.manager.tests[device_id] = {"ok": result["ok"], "at": dt_util.now().isoformat(),
                                         "steps": [{k: s.get(k) for k in ("name", "ok", "detail", "info")} for s in steps][:12]}
        self.hass.async_create_task(self.manager.save_runtime())
        return result

    # ------------------------------------------------------------------ views for the UI

    def stop_listening(self) -> None:
        """Stop reacting to phone notification buttons. Safe to call more than once; needed on unload or reload, or the
        old coordinator would keep answering taps through a manager that no longer owns the saved state."""
        unsub, self._unsub_action = getattr(self, "_unsub_action", None), None
        if unsub:
            unsub()

    async def async_close(self) -> None:
        self.stop_listening()
        await self.manager.save_runtime()
        for a in self.adapters:
            await a.close()

    async def release_all(self, force: bool = False) -> None:
        """Best effort on shutdown: hand every battery we forced back to its own control. While paused nothing is touched,
        unless `force` (safe mode, and the notification's hand-back button, mean it even when paused)."""
        if self.paused and not force:
            await self.manager.save_runtime()      # nothing to hand back, but the running totals must still be kept
            return
        for a in self.adapters:
            st = self._snap.devices.get(a.config.device_id)
            if st and st.release_pending:
                try:
                    self.traces.cancel(a.config.device_id)
                    await asyncio.wait_for(a.apply(Desired(Mode.AUTO)), HANDBACK_TIMEOUT_S)   # one hung battery must not hold up the rest
                    self._set_release(a.config.device_id, st, False)     # handed back: not "held from before" at the next start
                    self._log(a.config.device_id, "released to the device's own control on shutdown")
                except Exception as err:  # noqa: BLE001 - flag stays set; the control loop keeps retrying, and so does the next start
                    self._retry_handback.add(a.config.device_id)
                    _LOGGER.warning("release failed for %s: %s", a.config.device_id, err)
        await self.manager.save_runtime()
