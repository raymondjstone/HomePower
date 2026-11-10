"""Adapter that drives an *existing* HA integration's entities.

We deliberately don't reimplement Anker/EcoFlow protocols. All integration-specific logic
(how to read a state and which services to call) lives in core/strategies.py, which is
HA-free and unit tested; this class only performs the I/O.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant

from ..core import strategies
from ..core.models import Actual, Desired, DeviceConfig
from .base import DeviceAdapter


class HaEntityAdapter(DeviceAdapter):
    def __init__(self, hass: HomeAssistant, config: DeviceConfig) -> None:
        super().__init__(config)
        self.hass = hass
        self.ent: dict[str, str] = config.extra["entities"]

    def _states(self) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for key, eid in self.ent.items():
            st = self.hass.states.get(eid)
            out[key] = st.state if st else None
        return out

    async def read(self) -> Actual:
        return strategies.interpret(self.config.extra, self.config.caps, self._states())

    async def apply(self, desired: Desired) -> None:
        for domain, service, data in strategies.plan(self.config.extra, self.config.caps, desired):
            await self.hass.services.async_call(domain, service, data, blocking=True)
