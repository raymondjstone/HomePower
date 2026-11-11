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
from .core.rates import RateTable, parse_rates, to_main_units
from .core.reconcile import Decision, decide, materially_different
from .core.schedule import ScheduleEntry
from .core.settings import AppSettings, build as build_settings

_LOGGER = logging.getLogger(__name__)
READ_TIMEOUT_S = 20
PERSIST_EVERY_S = 300
KEEPALIVE_EVERY_S = 60
OFFLINE_ALERT_AFTER_S = 180
PROTECT_ALERT_AFTER_S = 60
UNCONFIRMED_ALERT_AFTER = 3
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
        self.overrides: dict[str, tuple[Desired, datetime]] = {}   # manual "charge now" etc.
        self.testing: set[str] = set()                              # batteries under a control test
        self.meter = EnergyMeter(manager.energy)
        self.ledger = Ledger(manager.ledger_days)
        self.alerts = AlertTracker()
        self.last_result = Result()
        self.last_grid: float | None = None
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

    def set_config(self, adapters: list[DeviceAdapter], schedules: list[ScheduleEntry], goals: list[Goal]) -> None:
        for gone in {a.config.device_id for a in self.adapters} - {a.config.device_id for a in adapters}:
            self.alerts.forget(f"offline:{gone}"), self.alerts.forget(f"error:{gone}")
            self.alerts.forget(f"unconfirmed:{gone}"), self.alerts.forget(f"protect:{gone}")
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

    def _rates(self) -> RateTable | None:
        s = self.settings
        slots = []
        st = self.hass.states.get(s.rates_entity) if s.rates_entity else None
        if st is not None:
            slots = parse_rates(dict(st.attributes), dt_util.DEFAULT_TIME_ZONE)
        if not slots and s.price_peak is None and s.price_offpeak is None:
            return None
        return RateTable(slots, s.price_peak, s.price_offpeak, s.offpeak_start, s.offpeak_end)

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
        """House grid power (+ importing): the user's sensor if set, else a battery that sees the meter."""
        ent = self.settings.grid_entity
        st = self.hass.states.get(ent) if ent else None
        try:
            v = float(st.state)  # type: ignore[union-attr]
            return -v if self.settings.grid_invert else v
        except (AttributeError, TypeError, ValueError):
            pass
        # Local meter readings first: a cloud-reported "grid" figure (EcoFlow) is a slow, cached value.
        cloud = {x.config.device_id for x in self.adapters if x.config.extra.get("driver") == "ecoflow_stream"}
        for did, a in sorted(actuals.items(), key=lambda kv: kv[0] in cloud):
            if a.online and a.grid_w is not None:
                return a.grid_w
        return None

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

        grid = self._grid_w(actuals)
        rates = self._rates()
        world = World(
            now=now, settings=self.settings.engine, schedules=self.schedules, goals=self.goals,
            overrides=self._active_overrides(now), grid_w=grid, condition_ok=self._condition_ok,
            backup_active=self.backup_active, away=self.away, load_blocks=self._load_blocks(),
            rates=rates, forecast_kwh=self._forecast_kwh(),
            devices=[DevIn(a.config, actuals[a.config.device_id],
                           self._snap.devices[a.config.device_id].release_pending,
                           self._snap.devices[a.config.device_id].guard_held,
                           bool(a.config.extra.get("control_enabled", True))) for a in adapters],
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
        for step in (lambda: self._account(now, wall, actuals, grid, rates), lambda: self._check_alerts(wall, adapters)):
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
        self.meter.add("grid", wall, grid)
        pv = [a.pv_w for a in actuals.values() if a.online and a.pv_w is not None]
        self.meter.add("pv", wall, sum(pv) if pv else None)
        charging_w = sum(max(0.0, a.power_w or 0.0) for a in actuals.values() if a.online)
        forced = any(st.reason.startswith(FORCING_REASONS) and st.desired is not None and st.desired.mode is Mode.CHARGE
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
        return {"ok": all(s["ok"] for s in steps), "steps": steps}

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
        }

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

    def view(self) -> dict:
        """JSON-safe live view for the panel."""
        now = dt_util.now()
        out = {}
        actuals = {}
        for a in self.adapters:
            did = a.config.device_id
            st = self._snap.devices.get(did) or DeviceStatus()
            act, want = st.actual, st.desired
            if act:
                actuals[did] = act
            active = [s.id for s in self.schedules if s.applies(did, now)
                      and (not s.condition_entity or self._condition_ok(s.condition_entity))]
            ov = self.overrides.get(did)
            out[did] = {
                "online": bool(act and act.online),
                "soc": act.soc if act else None,
                "power_w": act.power_w if act else None,
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
        day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
        rates = self._rates()
        return {
            "devices": out,
            "events": [list(e) for e in list(self._snap.events)[-60:]],
            "grid_w": self._grid_w(actuals),
            "now": now.isoformat(),
            "paused": self.paused, "backup": self.backup_active, "away": self.away,
            "alerts": sorted(self.alerts.active()), "notes": self.last_result.notes,
            "price_now": self._price_now(rates, now), "currency": self.settings.currency,
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
