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
from .core.models import Actual, Desired
from .core.reconcile import Decision, clamp, decide
from .core.schedule import ScheduleEntry, resolve

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
    ) -> None:
        super().__init__(
            hass, _LOGGER, name=DOMAIN, update_interval=timedelta(seconds=UPDATE_INTERVAL_S)
        )
        self.adapters = adapters
        self.schedules = schedules
        self.cooldown_s = cooldown_s
        self._snap = Snapshot(devices={a.config.device_id: DeviceStatus() for a in adapters})

    def _log(self, device_id: str, msg: str) -> None:
        self._snap.events.append((dt_util.now().isoformat(), device_id, msg))
        _LOGGER.info("%s: %s", device_id, msg)

    async def _async_update_data(self) -> Snapshot:
        now = dt_util.now()
        for a in self.adapters:
            did, caps = a.config.device_id, a.config.caps
            st = self._snap.devices[did]
            st.actual = await a.read()
            want = resolve(self.schedules, did, now)
            if want is None:  # no schedule: hands off, device runs its own logic
                st.desired, st.decision = None, None
                continue
            want = clamp(
                want,
                max_charge_w=caps.max_charge_w,
                max_discharge_w=caps.max_discharge_w,
                min_soc=caps.min_soc,
            )
            st.desired = want
            st.decision, diffs = decide(
                want, st.actual, now=_time.monotonic(), last_apply=st.last_apply,
                cooldown_s=self.cooldown_s,
            )
            if st.decision is Decision.SKIP_PROTECTED:
                self._log(did, f"protection active, not overriding: {diffs}")
            elif st.decision is Decision.APPLY:
                await self._apply_and_verify(a, st, want, diffs)
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
