"""Control loop: resolve schedule -> read -> decide -> apply -> verify -> log."""
from __future__ import annotations

import logging
import time as _time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from collections.abc import Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .adapters.base import DeviceAdapter
from .const import DOMAIN, UPDATE_INTERVAL_S
from .core.limits import apply_totals, charge_ceiling, soc_guard
from .core.models import Actual, Desired, Mode
from .core.reconcile import Decision, clamp, decide
from .core.schedule import ScheduleEntry, resolve
from .core.zero_import import desired_for, setpoint

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class DeviceStatus:
    desired: Desired | None = None
    actual: Actual | None = None
    decision: Decision | None = None
    last_apply: float | None = None
    last_error: str | None = None
    release_pending: bool = False  # we forced this device; hand it back when no longer managed
    guard_held: bool = False       # software SOC limit is currently holding this device


@dataclass(slots=True)
class Snapshot:
    devices: dict[str, DeviceStatus] = field(default_factory=dict)
    events: deque = field(default_factory=lambda: deque(maxlen=200))  # diagnostics log


class HomePowerCoordinator(DataUpdateCoordinator[Snapshot]):
    def __init__(
        self,
        hass: HomeAssistant,
        adapters: list[DeviceAdapter],
        schedules: list[ScheduleEntry],
        cooldown_s: float,
        *,
        charge_cap_w: int | None = None,
        discharge_cap_w: int | None = None,
        zero_import_device: str | None = None,
        grid_import_entity: str | None = None,
        grid_invert: bool = False,
        import_limit_w: int | None = None,
        forced_ids: set[str] | None = None,
        on_forced_change: Callable[[set[str]], None] | None = None,
    ) -> None:
        super().__init__(
            hass, _LOGGER, name=DOMAIN, update_interval=timedelta(seconds=UPDATE_INTERVAL_S)
        )
        self.adapters = adapters
        self.schedules = schedules
        self.cooldown_s = cooldown_s
        self.charge_cap_w = charge_cap_w          # total across all batteries, all schedules
        self.discharge_cap_w = discharge_cap_w
        self.zero_import_device = zero_import_device
        self.grid_import_entity = grid_import_entity
        self.grid_invert = grid_invert            # True if sensor is + when exporting
        self.import_limit_w = import_limit_w      # whole-house import ceiling (W)
        self.paused = False                       # kill switch: read only, never write
        self._forced_ids = set(forced_ids or ())   # persisted: devices we put into forced control
        self._on_forced_change = on_forced_change
        self.overrides: dict[str, tuple[Desired, datetime]] = {}  # manual "charge now" etc.
        self._snap = Snapshot(devices={a.config.device_id: self._new_status(a.config.device_id) for a in adapters})

    def _new_status(self, did: str) -> DeviceStatus:
        return DeviceStatus(release_pending=did in self._forced_ids)

    def _set_release(self, did: str, st: DeviceStatus, value: bool) -> None:
        if st.release_pending == value:
            return
        st.release_pending = value
        (self._forced_ids.add if value else self._forced_ids.discard)(did)
        if self._on_forced_change:
            self._on_forced_change(set(self._forced_ids))

    def apply_settings(self, cfg: dict) -> None:
        """Apply changed global settings in place (no integration reload)."""
        from .const import (CONF_CHARGE_CAP_W, CONF_COOLDOWN_S, CONF_DISCHARGE_CAP_W, CONF_GRID_ENTITY,
                            CONF_GRID_INVERT, CONF_ZERO_IMPORT_DEVICE, DEFAULT_COOLDOWN_S)
        self.cooldown_s = cfg.get(CONF_COOLDOWN_S, DEFAULT_COOLDOWN_S)
        self.import_limit_w = cfg.get("import_limit_w") or None
        self.charge_cap_w = cfg.get(CONF_CHARGE_CAP_W) or None
        self.discharge_cap_w = cfg.get(CONF_DISCHARGE_CAP_W) or None
        self.zero_import_device = cfg.get(CONF_ZERO_IMPORT_DEVICE) or None
        self.grid_import_entity = cfg.get(CONF_GRID_ENTITY) or None
        self.grid_invert = bool(cfg.get(CONF_GRID_INVERT, False))

    # --- manual overrides ("charge now for 2 hours") -------------------------------------
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

    def _active_override(self, did: str, now: datetime) -> Desired | None:
        item = self.overrides.get(did)
        if item and now >= item[1]:
            self.overrides.pop(did)
            self._log(did, "manual override expired")
            return None
        return item[0] if item else None

    def _condition_ok(self, entity_id: str) -> bool:
        st = self.hass.states.get(entity_id)
        return bool(st and st.state == "on")

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

    def set_config(self, adapters: list[DeviceAdapter], schedules: list[ScheduleEntry]) -> None:
        """Swap in a new device/schedule set (after the user edits them in the panel)."""
        self.adapters = adapters
        self.schedules = schedules
        old = self._snap.devices
        self._snap.devices = {a.config.device_id: old.get(a.config.device_id) or self._new_status(a.config.device_id) for a in adapters}
        self.overrides = {k: v for k, v in self.overrides.items() if k in self._snap.devices}

    def view(self) -> dict:
        """JSON-safe live view for the panel."""
        now = dt_util.now()
        out = {}
        for a in self.adapters:
            did = a.config.device_id
            st = self._snap.devices.get(did) or DeviceStatus()
            act, want = st.actual, st.desired
            active = [
                s.id for s in self.schedules
                if s.applies(did, now) and (not s.condition_entity or self._condition_ok(s.condition_entity))
            ]
            out[did] = {
                "online": bool(act and act.online),
                "soc": act.soc if act else None,
                "power_w": act.power_w if act else None,
                "mode": act.mode.value if act and act.mode else None,
                "protected": bool(act and act.protected),
                "desired_mode": want.mode.value if want else None,
                "desired_power_w": want.power_w if want else None,
                "decision": st.decision.value if st.decision else None,
                "managed": want is not None,
                "active_schedules": active,
                "override": ({"mode": ov[0].mode.value, "power_w": ov[0].power_w, "until": ov[1].isoformat()}
                             if (ov := self.overrides.get(did)) else None),
                "guard_held": st.guard_held,
                "last_error": st.last_error,
            }
        return {
            "devices": out,
            "events": [list(e) for e in list(self._snap.events)[-60:]],
            "now": now.isoformat(),
            "paused": self.paused,
        }

    def _log(self, device_id: str, msg: str) -> None:
        self._snap.events.append((dt_util.now().isoformat(), device_id, msg))
        _LOGGER.info("%s: %s", device_id, msg)

    def _grid_import_w(self) -> float | None:
        st = self.hass.states.get(self.grid_import_entity) if self.grid_import_entity else None
        try:
            v = float(st.state)  # type: ignore[union-attr]
        except (AttributeError, TypeError, ValueError):
            return None
        return -v if self.grid_invert else v

    def _zero_import_desired(self, actuals: dict[str, Actual]) -> Desired:
        zid = self.zero_import_device
        a = next(x for x in self.adapters if x.config.device_id == zid)
        act = actuals[zid]
        grid = self._grid_import_w()
        if grid is None or not act.online:
            return desired_for(None, a.config.caps, act.soc)  # fail safe: HOLD
        # Deliberate charging on other batteries is not "import" to be cancelled.
        other_charge = sum(
            max(0.0, o.power_w or 0.0) for k, o in actuals.items() if k != zid and o.online
        )
        cur = act.set_power_w or 0
        signed_cur = cur if act.mode is Mode.DISCHARGE else -cur if act.mode is Mode.CHARGE else 0
        sp = setpoint(
            grid,
            own_net_discharge_w=-(act.power_w or 0.0),  # power_w: + charging, - discharging
            other_charge_w=other_charge,
            max_discharge_w=a.config.caps.max_discharge_w,
            max_charge_w=a.config.caps.max_charge_w,
            current_set_w=signed_cur,
        )
        return desired_for(sp, a.config.caps, act.soc)

    async def _async_update_data(self) -> Snapshot:
        now = dt_util.now()
        actuals = {a.config.device_id: await a.read() for a in self.adapters}

        # Pass 1: what does each device want? Schedule wins; otherwise the
        # zero-import battery follows the grid; everything else is left alone.
        wants: dict[str, Desired] = {}
        for a in self.adapters:
            did, caps = a.config.device_id, a.config.caps
            want = self._active_override(did, now) or resolve(self.schedules, did, now, self._condition_ok)
            if want is None and did == self.zero_import_device:
                want = self._zero_import_desired(actuals)
            if want is not None and want.mode not in caps.supported_modes:
                self._log(did, f"{want.mode.value} is not supported by this battery; leaving it alone")
                want = None
            if want is None and self._snap.devices[did].release_pending:
                want = Desired(Mode.AUTO)  # window ended: give the battery back to its own control
            if want is not None:
                want = clamp(
                    want, max_charge_w=caps.max_charge_w,
                    max_discharge_w=caps.max_discharge_w, min_soc=caps.min_soc,
                )
                act, dst = actuals[did], self._snap.devices[did]
                guarded = soc_guard(want, act.soc if act.online else None, caps, held=dst.guard_held)
                dst.guard_held = guarded.mode is not want.mode
                wants[did] = guarded

        # Pass 2: overall charge/discharge caps across all devices combined.
        by_id = {a.config.device_id: a for a in self.adapters}
        wants.update(
            apply_totals(
                {k: (d, by_id[k].config.caps, by_id[k].config.priority) for k, d in wants.items()},
                charge_ceiling(
                    self.charge_cap_w, self.import_limit_w, self._grid_import_w(),
                    sum(x.power_w or 0.0 for x in actuals.values() if x.online),
                ),
                self.discharge_cap_w,
            )
        )

        # Pass 3: reconcile each device.
        for a in self.adapters:
            did = a.config.device_id
            st = self._snap.devices[did]
            st.actual = actuals[did]
            st.desired = wants.get(did)
            if st.desired is None:  # nothing manages it: device runs its own logic
                st.decision = None
                st.guard_held = False
                continue
            if self.paused:
                st.decision = None
                continue
            if st.desired.mode is Mode.AUTO and st.actual.online and st.actual.mode in (Mode.AUTO, None):
                self._set_release(did, st, False)  # handed back and confirmed
            st.decision, diffs = decide(
                st.desired, st.actual, now=_time.monotonic(), last_apply=st.last_apply,
                cooldown_s=self.cooldown_s,
            )
            if st.decision is Decision.SKIP_PROTECTED:
                self._log(did, f"protection active, not overriding: {diffs}")
            elif st.decision is Decision.APPLY:
                await self._apply_and_verify(a, st, st.desired, diffs)
        return self._snap

    async def _apply_and_verify(self, a: DeviceAdapter, st: DeviceStatus, want, diffs) -> None:
        did = a.config.device_id
        self._log(did, f"mismatch {[(d.field, d.desired, d.actual) for d in diffs]}; applying")
        st.last_apply = _time.monotonic()
        if want.mode is not Mode.AUTO:
            self._set_release(did, st, True)
        try:
            await a.apply(want)
            st.actual = await a.read()
            st.last_error = None
            # Vendor state can lag; a remaining mismatch is retried after the cooldown.
            _, remaining = decide(want, st.actual, now=st.last_apply, last_apply=None)
            self._log(did, "verified" if not remaining else f"not yet confirmed: {remaining}")
        except Exception as err:  # noqa: BLE001 - one device must not stop the others
            st.last_error = str(err)
            self._log(did, f"apply failed: {err}")
