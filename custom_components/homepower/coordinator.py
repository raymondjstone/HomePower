"""Control loop: resolve schedule -> read -> decide -> apply -> verify -> log."""
from __future__ import annotations

import logging
import time as _time
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .adapters.base import DeviceAdapter
from .const import DOMAIN, UPDATE_INTERVAL_S
from .core.limits import apply_totals
from .core.models import Actual, Desired
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
        self._snap = Snapshot(devices={a.config.device_id: DeviceStatus() for a in adapters})

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
        sp = setpoint(
            grid,
            own_discharge_w=max(0.0, -(act.power_w or 0.0)),
            other_charge_w=other_charge,
            max_discharge_w=a.config.caps.max_discharge_w,
            current_set_w=act.set_power_w,
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
            want = resolve(self.schedules, did, now)
            if want is None and did == self.zero_import_device:
                want = self._zero_import_desired(actuals)
            if want is not None:
                wants[did] = clamp(
                    want, max_charge_w=caps.max_charge_w,
                    max_discharge_w=caps.max_discharge_w, min_soc=caps.min_soc,
                )

        # Pass 2: overall charge/discharge caps across all devices combined.
        by_id = {a.config.device_id: a for a in self.adapters}
        wants.update(
            apply_totals(
                {k: (d, by_id[k].config.caps, by_id[k].config.priority) for k, d in wants.items()},
                self.charge_cap_w,
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
                continue
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
