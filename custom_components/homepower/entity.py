"""What every HomePower entity has in common: it follows the coordinator and sits on the one HomePower device."""
from __future__ import annotations
from collections.abc import Iterable
from typing import Any, cast

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HomePowerCoordinator
from .core.models import DeviceConfig
from .core.profiles import PROFILE_BY_ID

_LOGGER = logging.getLogger(__name__)


def system_device(entry_id: str) -> DeviceInfo:
    """HomePower itself: the totals, prices and switches."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)}, name="HomePower", manufacturer="HomePower", entry_type=DeviceEntryType.SERVICE,
        configuration_url="homeassistant://homepower",  # "Visit" button on the device page
    )


def battery_identifier(entry_id: str, device_id: str) -> tuple[str, str]:
    return (DOMAIN, f"{entry_id}_{device_id}")


def battery_device_name(name: str) -> str:
    """With each sensor's own name after it this reads as the sensors always have: "HomePower Garage charge"."""
    return f"HomePower {name}"


def find_device(registry: dr.DeviceRegistry, identifier: tuple[str, str], entry_id: str) -> dr.DeviceEntry | None:
    """One of HomePower's devices. Newer Home Assistant releases look it up by identifier and config entry."""
    if by_identifier := getattr(registry, "async_get_device_by_identifier", None):
        return cast("dr.DeviceEntry | None", by_identifier(identifier, entry_id))
    return registry.async_get_device(identifiers={identifier})


def _under_system(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    """"Connected via HomePower": newer releases want the parent device's id, older ones its identifier."""
    if "via_device_id" not in DeviceInfo.__annotations__:
        return {"via_device": (DOMAIN, entry_id)}
    try:
        parent = find_device(dr.async_get(hass), (DOMAIN, entry_id), entry_id)
    except Exception:  # noqa: BLE001 - only how devices are shown
        _LOGGER.debug("could not find HomePower's device", exc_info=True)
        return {}
    return {"via_device_id": parent.id} if parent else {}


def battery_device(hass: HomeAssistant, entry_id: str, config: DeviceConfig) -> DeviceInfo:
    profile = PROFILE_BY_ID.get(config.kind) or {}
    return DeviceInfo(
        identifiers={battery_identifier(entry_id, config.device_id)}, name=battery_device_name(config.name),
        manufacturer=profile.get("vendor"), model=profile.get("label"),
        configuration_url="homeassistant://homepower", **cast(DeviceInfo, _under_system(hass, entry_id)),
    )


def battery_ids(entry_id: str, stored: Iterable[str]) -> set[tuple[str, str]]:
    """The device identifiers that belong to something HomePower still has: itself and each stored battery."""
    return {(DOMAIN, entry_id)} | {battery_identifier(entry_id, d) for d in stored}


def prepare_devices(hass: HomeAssistant, entry: ConfigEntry, stored: Iterable[str]) -> None:
    """Before the sensors are added: HomePower's own device exists (the batteries hang off it), and devices of batteries
    deleted while HomePower was not loaded are gone. Cosmetic: it never stops setup."""
    try:
        registry = dr.async_get(hass)
        registry.async_get_or_create(config_entry_id=entry.entry_id, **system_device(entry.entry_id))
        keep = battery_ids(entry.entry_id, stored)
        for device in list(dr.async_entries_for_config_entry(registry, entry.entry_id)):
            if not device.identifiers & keep:
                registry.async_remove_device(device.id)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("could not tidy the device registry", exc_info=True)


class HomePowerEntity(CoordinatorEntity[HomePowerCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coord: HomePowerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coord)
        self._entry = entry
        self._attr_device_info = system_device(entry.entry_id)

    def _on_battery(self, device_id: str) -> None:
        """This entity belongs to one battery's own device."""
        config = next((a.config for a in self.coordinator.adapters if a.config.device_id == device_id), None)
        if config is not None:
            self._attr_device_info = battery_device(self.coordinator.hass, self._entry.entry_id, config)
