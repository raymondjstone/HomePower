"""HomePower switches: pause control, backup mode, away mode."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HomePowerCoordinator

# key, name, icon, manager attribute, manager setter
SWITCHES = (
    ("pause", "Pause control", "mdi:pause-circle", "paused", "set_paused"),
    ("backup", "Backup mode", "mdi:shield-home", "backup", "set_backup"),
    ("away", "Away mode", "mdi:airplane", "away", "set_away"),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    coord: HomePowerCoordinator = hass.data[DOMAIN][entry.entry_id]
    add([HomePowerSwitch(coord, entry, *s) for s in SWITCHES])


class HomePowerSwitch(CoordinatorEntity[HomePowerCoordinator], SwitchEntity):
    _attr_has_entity_name = True

    def __init__(self, coord, entry, key, name, icon, attr, setter) -> None:
        super().__init__(coord)
        self._attr, self._setter = attr, setter
        self._attr_name, self._attr_icon = name, icon
        self._attr_unique_id = f"{entry.entry_id}_switch_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name="HomePower", manufacturer="HomePower",
                                            entry_type=DeviceEntryType.SERVICE, configuration_url="homeassistant://homepower")

    @property
    def is_on(self) -> bool:
        return bool(getattr(self.coordinator.manager, self._attr))

    async def async_turn_on(self, **kwargs) -> None:
        await getattr(self.coordinator.manager, self._setter)(True)

    async def async_turn_off(self, **kwargs) -> None:
        await getattr(self.coordinator.manager, self._setter)(False)
