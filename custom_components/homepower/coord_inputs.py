"""Readings from Home Assistant: the grid power from the chosen sources, the Hildebrand Glow meter, smart-meter sensors, the house load, protected loads and whether a battery has panels.

Part of HomePowerCoordinator (coordinator.py), kept in its own file so each file has one concern: the methods run on
the coordinator itself and use its state."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

import time as _time

from homeassistant.util import dt as dt_util

from .core.models import Actual
from .coord_base import _LOGGER, GLOW_DOMAIN, _real

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .core.meter import MeterReader
    from .core.models import DeviceConfig

    from .adapters.base import DeviceAdapter
    from .core.settings import AppSettings
    from .manager import HomePowerManager


class InputsMixin:
    if TYPE_CHECKING:    # used here but defined on the class this is mixed into: declared for the type checker only
        _glow_at: Any
        _glow_cache: dict[str, str]
        _meter: MeterReader
        adapters: list[DeviceAdapter]
        hass: HomeAssistant
        manager: HomePowerManager
        settings: AppSettings

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

    def _load_blocks(self) -> set[str]:
        blocks: set[str] = set()
        for r in self.settings.load_rules:
            st = self.hass.states.get(r["entity"])
            if st is None:
                continue
            if r.get("above_w") is not None:
                # the threshold is in watts: a charger reporting 7.2 kW must count as 7200 W, or the rule never fires
                scale = {"kw": 1000.0, "mw": 1e6}.get(str(st.attributes.get("unit_of_measurement") or "").strip().lower(), 1.0)
                try:
                    active = _real(st.state) * scale > r["above_w"]
                except (TypeError, ValueError):
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

        sources: dict[str, Callable[[], float | None]] = {
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
    def _measures_house(cfg: DeviceConfig) -> bool:
        """Does this battery's grid figure cover the whole house? What the user said; else what is usual for the model
        (an X1 sits at the grid connection; plug-in batteries only see their own socket; EcoFlow's cloud figure is kept
        as a last resort)."""
        said: object = cfg.extra.get("grid_meter", "auto")
        if said != "auto":
            return said == "yes"
        return bool(cfg.extra.get("house_meter")) or cfg.extra.get("driver") in ("ecoflow_stream", "ecoflow_modbus")

    def _entity_age(self, entity_id: str) -> float | None:
        st = self.hass.states.get(entity_id) if entity_id else None
        if st is None:
            return None
        seen: datetime = getattr(st, "last_reported", None) or st.last_updated
        return max(0.0, (dt_util.utcnow() - seen).total_seconds())

    def _meter_w(self, imp_ent: str) -> float | None:
        """Smart-meter reading in W (+ importing): an import sensor, less an optional export sensor. Each may be
        a power sensor (W/kW) or a cumulative energy counter (Wh/kWh)."""
        s, now = self.settings, _time.monotonic()
        out: list[float | None] = []
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

    def _has_solar(self, cfg: DeviceConfig) -> bool | None:
        """Are panels attached to this battery? What the user said, else whether it has ever reported solar power."""
        said: object = cfg.extra.get("solar", "auto")
        if said != "auto":
            return said == "yes"
        return True if cfg.device_id in self.manager.pv_seen else None
