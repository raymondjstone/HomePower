"""Persistent batteries + schedules, and the bridge between them and the coordinator."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .adapters.ha_entities import HaEntityAdapter
from .const import STORAGE_KEY, STORAGE_VERSION
from .coordinator import HomePowerCoordinator
from .core.serde import (
    ValidationError,
    device_from_dict,
    device_to_dict,
    schedule_from_dict,
    schedule_to_dict,
)


class HomePowerManager:
    """Source of truth for user-defined batteries and schedules (JSON dicts, validated)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.devices: dict[str, dict] = {}
        self.schedules: dict[str, dict] = {}
        self.coordinator: HomePowerCoordinator | None = None
        self.paused = False
        self.forced: set[str] = set()  # batteries HomePower has put into forced control

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.devices = {d["device_id"]: d for d in data.get("devices", [])}
        self.schedules = {s["id"]: s for s in data.get("schedules", [])}
        self.paused = bool(data.get("paused", False))
        self.forced = set(data.get("forced", []))

    async def _save(self) -> None:
        await self._store.async_save(
            {"devices": list(self.devices.values()), "schedules": list(self.schedules.values()), "paused": self.paused, "forced": sorted(self.forced)}
        )

    def build(self):
        """Validate everything stored and build adapters/schedules; bad rows are skipped."""
        adapters, schedules = [], []
        for d in self.devices.values():
            try:
                adapters.append(HaEntityAdapter(self.hass, device_from_dict(d)))
            except (ValidationError, KeyError):
                continue
        for s in self.schedules.values():
            try:
                schedules.append(schedule_from_dict(s))
            except ValidationError:
                continue
        return adapters, schedules

    async def _apply(self) -> None:
        await self._save()
        if self.coordinator:
            self.coordinator.set_config(*self.build())
            await self.coordinator.async_request_refresh()

    def on_forced_change(self, ids: set[str]) -> None:
        self.forced = ids
        self.hass.async_create_task(self._save())

    async def set_paused(self, paused: bool) -> None:
        self.paused = paused
        if self.coordinator:
            self.coordinator.paused = paused
            self.coordinator.async_update_listeners()
        await self._save()

    # --- mutations (raise ValidationError on bad input) ---
    async def save_device(self, raw: dict) -> dict:
        d = device_to_dict(device_from_dict(raw))
        self.devices[d["device_id"]] = d
        await self._apply()
        return d

    async def delete_device(self, device_id: str) -> None:
        self.devices.pop(device_id, None)
        for s in self.schedules.values():  # drop references; empty list would mean "all devices"
            if device_id in s.get("device_ids", []):
                s["device_ids"] = [x for x in s["device_ids"] if x != device_id]
                if not s["device_ids"]:
                    s["enabled"] = False
        await self._apply()

    async def save_schedule(self, raw: dict) -> dict:
        s = schedule_to_dict(schedule_from_dict(raw))
        self.schedules[s["id"]] = s
        await self._apply()
        return s

    async def delete_schedule(self, schedule_id: str) -> None:
        self.schedules.pop(schedule_id, None)
        await self._apply()
