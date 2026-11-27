"""HomePower switches: pause control, backup mode, away mode."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import HomePowerCoordinator
from .entity import HomePowerEntity

PARALLEL_UPDATES = 1    # one change at a time

# key (also the icon's name in icons.json), name, manager attribute, manager setter
SWITCHES = (
    ("pause", "Pause control", "paused", "set_paused"),
    ("backup", "Backup mode", "backup", "set_backup"),
    ("away", "Away mode", "away", "set_away"),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    coord: HomePowerCoordinator = entry.runtime_data.coordinator
    add([HomePowerSwitch(coord, entry, *s) for s in SWITCHES])


class HomePowerSwitch(HomePowerEntity, SwitchEntity):
    def __init__(self, coord, entry, key, name, attr, setter) -> None:
        super().__init__(coord, entry)
        self._attr, self._setter = attr, setter
        self._attr_name, self._attr_translation_key = name, key
        self._attr_unique_id = f"{entry.entry_id}_switch_{key}"

    @property
    def is_on(self) -> bool:
        return bool(getattr(self.coordinator.manager, self._attr))

    async def async_turn_on(self, **kwargs) -> None:
        await getattr(self.coordinator.manager, self._setter)(True)

    async def async_turn_off(self, **kwargs) -> None:
        await getattr(self.coordinator.manager, self._setter)(False)
