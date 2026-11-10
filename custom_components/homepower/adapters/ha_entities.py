"""Adapter that drives an *existing* HA integration's entities.

We deliberately don't reimplement Anker/EcoFlow protocols (stage 1): each device is
described by an entity map pointing at the entities the upstream integration created,
plus a mapping from neutral Mode to that integration's select/option values.
"""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from ..core.models import Actual, Desired, DeviceConfig, Mode
from .base import DeviceAdapter

# Keys expected in DeviceConfig.extra["entities"]:
#   soc (sensor), power (sensor), mode (select), set_power (number),
#   target_soc (number, optional), reserve_soc (number, optional), protected (binary_sensor, optional)
# DeviceConfig.extra["mode_map"]: {"charge": "<option>", ...}  neutral -> vendor option


def _num(state: Any) -> float | None:
    try:
        return float(state.state)
    except (AttributeError, TypeError, ValueError):
        return None


class HaEntityAdapter(DeviceAdapter):
    def __init__(self, hass: HomeAssistant, config: DeviceConfig) -> None:
        super().__init__(config)
        self.hass = hass
        self.ent: dict[str, str] = config.extra["entities"]
        self.mode_map: dict[str, str] = config.extra["mode_map"]
        self._reverse = {v: Mode(k) for k, v in self.mode_map.items()}

    def _state(self, key: str):
        eid = self.ent.get(key)
        return self.hass.states.get(eid) if eid else None

    async def read(self) -> Actual:
        mode_state = self._state("mode")
        if mode_state is None or mode_state.state in ("unavailable", "unknown"):
            return Actual(online=False)
        prot = self._state("protected")
        return Actual(
            online=True,
            soc=_num(self._state("soc")),
            power_w=_num(self._state("power")),
            mode=self._reverse.get(mode_state.state),
            set_power_w=(int(v) if (v := _num(self._state("set_power"))) is not None else None),
            target_soc=(int(v) if (v := _num(self._state("target_soc"))) is not None else None),
            reserve_soc=(int(v) if (v := _num(self._state("reserve_soc"))) is not None else None),
            protected=bool(prot and prot.state == "on"),
        )

    async def _set_number(self, key: str, value: int) -> None:
        if eid := self.ent.get(key):
            await self.hass.services.async_call(
                "number", "set_value", {"entity_id": eid, "value": value}, blocking=True
            )

    async def apply(self, desired: Desired) -> None:
        # Limits first, mode last, so a mode flip never runs against stale setpoints.
        if desired.reserve_soc is not None:
            await self._set_number("reserve_soc", desired.reserve_soc)
        if desired.target_soc is not None:
            await self._set_number("target_soc", desired.target_soc)
        if desired.power_w is not None:
            await self._set_number("set_power", desired.power_w)
        await self.hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": self.ent["mode"], "option": self.mode_map[desired.mode.value]},
            blocking=True,
        )
