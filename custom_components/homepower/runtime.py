"""What a loaded HomePower keeps on its config entry (entry.runtime_data), and how code without the entry in hand (the panel's
API, the actions, the options and repair flows) finds the running one. HomePower allows a single config entry."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant

from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import HomePowerCoordinator
    from .manager import HomePowerManager


@dataclass
class HomePowerData:
    manager: HomePowerManager
    coordinator: HomePowerCoordinator


type HomePowerConfigEntry = ConfigEntry[HomePowerData]

# An entry's runtime_data is not cleared when it unloads, so only an entry that is set up (or finishing its setup, while
# its platforms are added) counts: a call after an unload must not reach the closed manager.
_RUNNING = (ConfigEntryState.LOADED, ConfigEntryState.SETUP_IN_PROGRESS)


def running(hass: HomeAssistant) -> HomePowerData | None:
    """The running HomePower's manager and coordinator, or None while it is not loaded."""
    find = getattr(getattr(hass, "config_entries", None), "async_entries", None)
    for entry in (find(DOMAIN) if find is not None else []):
        data = getattr(entry, "runtime_data", None)
        if isinstance(data, HomePowerData) and entry.state in _RUNNING:
            return data
    return None


def running_manager(hass: HomeAssistant) -> HomePowerManager | None:
    data = running(hass)
    return data.manager if data else None
