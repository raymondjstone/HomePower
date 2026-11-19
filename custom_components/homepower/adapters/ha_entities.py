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

    def _kw(self, key: str) -> bool:
        """Is this power entity in kW? HomePower works in W; a kW sensor read as W would be 1000 times too small."""
        st = self.hass.states.get(self.ent.get(key, ""))
        return bool(st) and str(st.attributes.get("unit_of_measurement") or "").strip().lower() == "kw"

    def _states(self) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for key, eid in self.ent.items():
            st = self.hass.states.get(eid)
            out[key] = st.state if st else None
            if key in POWER_KEYS and self._kw(key) and (v := strategies.num(out[key])) is not None:
                out[key] = str(v * 1000)
        return out

    async def read(self) -> Actual:
        return strategies.interpret(self.config.extra, self.config.caps, self._states())

    async def apply(self, desired: Desired) -> None:
        for domain, service, data in strategies.plan(self.config.extra, self.config.caps, desired):
            if service == "set_value" and data.get("entity_id") == self.ent.get("set_power") and self._kw("set_power"):
                data = {**data, "value": round(data["value"] / 1000, 3)}          # the setpoint entity takes kW
            await self.hass.services.async_call(domain, service, data, blocking=True)


POWER_KEYS = ("power", "set_power")
