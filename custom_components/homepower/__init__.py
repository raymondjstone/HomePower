"""HomePower: central battery scheduling for Anker SOLIX and EcoFlow STREAM systems."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_COOLDOWN_S, DEFAULT_COOLDOWN_S, DOMAIN
from .coordinator import HomePowerCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # TODO(stage 1): build HaEntityAdapter list from entry.options["devices"].
    # TODO(stage 2): load ScheduleEntry list from helpers.storage.Store (survives restarts).
    coordinator = HomePowerCoordinator(
        hass,
        adapters=[],
        schedules=[],
        cooldown_s=entry.data.get(CONF_COOLDOWN_S, DEFAULT_COOLDOWN_S),
    )
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data[DOMAIN].pop(entry.entry_id, None)
    return True
