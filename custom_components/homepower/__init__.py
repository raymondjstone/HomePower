"""HomePower: central battery scheduling for Anker SOLIX and EcoFlow STREAM systems."""
from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant

from . import services
from .const import DOMAIN
from .coordinator import HomePowerCoordinator
from .manager import HomePowerManager

_LOGGER = logging.getLogger(__name__)
PANEL_URL = "homepower"
STATIC_URL = "/homepower_static"
PLATFORMS = ["sensor", "switch"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    domain_data = hass.data.setdefault(DOMAIN, {})

    if not domain_data.get("registered"):
        services.async_register(hass)
        domain_data["registered"] = True
    # The control loop must not depend on the UI: the panel, its API and its static files are
    # best effort, so a missing/failed frontend never stops batteries being scheduled.
    await _async_setup_ui_api(hass, domain_data)

    manager = HomePowerManager(hass)
    await manager.async_load()
    adapters, schedules, goals = manager.build()
    coordinator = HomePowerCoordinator(hass, entry, manager, adapters, schedules, goals)
    manager.coordinator = coordinator
    entry.async_on_unload(entry.add_update_listener(_options_updated))

    # If HA shuts down while a battery is being forced, hand it back (best effort; the
    # persisted flag makes the next start retry if this does not complete).
    async def _on_stop(_event) -> None:
        await coordinator.release_all()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop))

    await coordinator.async_config_entry_first_refresh()
    domain_data[entry.entry_id] = coordinator
    domain_data["manager"] = manager
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    await _async_register_panel(hass)
    return True


def _notify(hass: HomeAssistant, key: str, message: str) -> None:
    try:
        from homeassistant.components import persistent_notification

        persistent_notification.async_create(hass, message, title="HomePower", notification_id=f"homepower_{key}")
    except Exception:  # noqa: BLE001
        _LOGGER.debug("could not create notification", exc_info=True)


async def _async_setup_ui_api(hass: HomeAssistant, domain_data: dict) -> None:
    if domain_data.get("ui_api"):
        return
    try:
        from homeassistant.components.http import StaticPathConfig

        from . import websocket_api

        websocket_api.async_register(hass)
        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC_URL, str(Path(__file__).parent / "frontend"), cache_headers=False)]
        )
        domain_data["ui_api"] = True
    except Exception as err:  # noqa: BLE001 - UI is optional
        _LOGGER.warning("HomePower UI API unavailable; scheduling still works", exc_info=True)
        _notify(hass, "ui_api", f"HomePower's panel API could not start: {err!r}. Use Settings > Devices & Services > "
                "HomePower > Configure to add batteries and schedules.")


async def _async_register_panel(hass: HomeAssistant) -> None:
    try:
        from homeassistant.components import frontend, panel_custom

        frontend.async_remove_panel(hass, PANEL_URL, warn_if_unknown=False)  # clear stale registration
        await panel_custom.async_register_panel(
            hass,
            webcomponent_name="homepower-panel",
            frontend_url_path=PANEL_URL,
            module_url=f"{STATIC_URL}/homepower-panel.js?v=0.1.0",
            sidebar_title="HomePower",
            sidebar_icon="mdi:home-battery",
            require_admin=True,
            config={},
        )
        _LOGGER.info("HomePower panel registered at /%s", PANEL_URL)
    except Exception as err:  # noqa: BLE001 - UI is optional
        _LOGGER.warning("HomePower sidebar panel could not be registered", exc_info=True)
        _notify(hass, "panel", f"HomePower's sidebar panel could not be registered: {err!r}. Use Settings > Devices & "
                "Services > HomePower > Configure to add batteries and schedules.")


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        mgr = hass.data.get(DOMAIN, {}).get("manager")
        coord = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if coord:
            await coord.manager.save_runtime()
        if mgr:
            await mgr.async_close()
        try:
            from homeassistant.components import frontend

            frontend.async_remove_panel(hass, PANEL_URL, warn_if_unknown=False)
        except Exception:  # noqa: BLE001
            pass
        data = hass.data.get(DOMAIN, {})
        data.pop(entry.entry_id, None)
        data.pop("manager", None)
    return ok


async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Settings changed in the panel: apply in place, no reload (keeps live subscriptions)."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator:
        coordinator.apply_settings({**entry.data, **entry.options})
        await coordinator.async_request_refresh()
