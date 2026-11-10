"""HomePower: central battery scheduling for Anker SOLIX and EcoFlow STREAM systems."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CHARGE_CAP_W,
    CONF_COOLDOWN_S,
    CONF_DISCHARGE_CAP_W,
    CONF_GRID_ENTITY,
    CONF_GRID_INVERT,
    CONF_ZERO_IMPORT_DEVICE,
    DEFAULT_COOLDOWN_S,
    DOMAIN,
)
from .coordinator import HomePowerCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # TODO(stage 1): build HaEntityAdapter list from entry.options["devices"].
    # TODO(stage 2): load ScheduleEntry list from helpers.storage.Store (survives restarts).
    cfg = {**entry.data, **entry.options}  # options override initial setup values
    coordinator = HomePowerCoordinator(
        hass,
        adapters=[],
        schedules=[],
        cooldown_s=cfg.get(CONF_COOLDOWN_S, DEFAULT_COOLDOWN_S),
        charge_cap_w=cfg.get(CONF_CHARGE_CAP_W) or None,
        discharge_cap_w=cfg.get(CONF_DISCHARGE_CAP_W) or None,
        zero_import_device=cfg.get(CONF_ZERO_IMPORT_DEVICE) or None,
        grid_import_entity=cfg.get(CONF_GRID_ENTITY) or None,
        grid_invert=cfg.get(CONF_GRID_INVERT, False),
    )
    entry.async_on_unload(entry.add_update_listener(_reload))
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data[DOMAIN].pop(entry.entry_id, None)
    return True


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
