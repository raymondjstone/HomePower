"""What every HomePower entity has in common: it follows the coordinator and sits on the one HomePower device."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HomePowerCoordinator


class HomePowerEntity(CoordinatorEntity[HomePowerCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coord: HomePowerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coord)
        self._entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name="HomePower", manufacturer="HomePower", entry_type=DeviceEntryType.SERVICE,
            configuration_url="homeassistant://homepower",  # "Visit" button on the device page
        )
