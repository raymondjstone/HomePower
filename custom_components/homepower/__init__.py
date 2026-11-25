"""HomePower: central battery scheduling for Anker SOLIX and EcoFlow STREAM systems."""
from __future__ import annotations

import logging
import json
import os
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from datetime import timedelta

from . import services
from .const import DOMAIN, SIGNAL_UPDATE, STORAGE_KEY, STORAGE_VERSION
from .coordinator import HomePowerCoordinator
from .manager import HomePowerManager
from .runtime import HomePowerConfigEntry, HomePowerData

_LOGGER = logging.getLogger(__name__)
PANEL_URL = "homepower"
STATIC_URL = "/homepower_static"
# The one place the version lives is manifest.json; it is also the cache-buster for the panel and card scripts.
VERSION = json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf8")).get("version", "0")


def _asset_tag() -> str:
    """Cache-buster for the panel and card scripts: the version plus a hash of the files, so a beta that keeps the same
    version number still makes browsers fetch the new script."""
    import hashlib

    h = hashlib.sha1()
    for path in sorted((Path(__file__).parent / "frontend").glob("*.js")):   # the panel is split over several files
        try:
            h.update(path.read_bytes())
        except OSError:
            pass
    return f"{VERSION}-{h.hexdigest()[:8]}"


ASSET_TAG = _asset_tag()
PLATFORMS = ["sensor", "switch"]


CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)     # set up from the UI only, nothing in configuration.yaml


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """HomePower's actions are registered here, once, so they exist even while the integration is not loaded (they then
    say so instead of doing nothing)."""
    services.async_register(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: HomePowerConfigEntry) -> bool:
    domain_data = hass.data.setdefault(DOMAIN, {})          # domain-wide only: whether the panel's API is registered

    # The control loop must not depend on the UI: the panel, its API and its static files are
    # best effort, so a missing/failed frontend never stops batteries being scheduled.
    await _async_setup_ui_api(hass, domain_data)

    manager = HomePowerManager(hass)
    await manager.async_load()
    adapters, schedules, goals = manager.build()
    coordinator = HomePowerCoordinator(hass, entry, manager, adapters, schedules, goals)
    manager.coordinator = coordinator
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    entry.async_on_unload(coordinator.stop_listening)      # the phone-notification buttons: gone when this setup is

    # If HA shuts down while a battery is being forced, hand it back (best effort; the
    # persisted flag makes the next start retry if this does not complete).
    async def _on_stop(_event) -> None:
        await coordinator.release_all()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop))

    try:
        await coordinator.async_config_entry_first_refresh()
    except BaseException:
        coordinator.stop_listening()
        await manager.async_close()     # Home Assistant retries setup: don't leave battery connections open meanwhile
        raise
    entry.runtime_data = HomePowerData(manager, coordinator)

    # The panel's live feed follows this signal rather than one coordinator, so it keeps working after a reload.
    @callback
    def _cycle_done() -> None:
        async_dispatcher_send(hass, SIGNAL_UPDATE)

    entry.async_on_unload(coordinator.async_add_listener(_cycle_done))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # A daily safety copy of batteries, schedules, goals, scenes and settings (no secrets), newest 7 kept.
    async def _backup(_now=None) -> None:
        try:
            from homeassistant.util import dt as dt_util
            await hass.async_add_executor_job(_write_backup, hass.config.path("homepower_backups"),
                                              manager.export_all(dict(entry.options)), dt_util.now().date().isoformat())
        except Exception:  # noqa: BLE001 - a failed safety copy must never affect control
            _LOGGER.warning("could not write the HomePower backup copy", exc_info=True)

    entry.async_on_unload(async_track_time_interval(hass, _backup, timedelta(days=1)))
    # A restart more often than daily would otherwise mean no copy is ever written: make today's now if it is missing.
    entry.async_create_background_task(hass, _backup_if_missing(hass, manager, entry), "homepower start-up backup")

    await _async_register_panel(hass)
    return True


BACKUPS_KEPT = 7


async def _backup_if_missing(hass, manager, entry) -> None:
    from homeassistant.util import dt as dt_util

    folder = hass.config.path("homepower_backups")
    day = dt_util.now().date().isoformat()          # Home Assistant's date, not the operating system's (a UTC container)
    try:
        # everything that touches the disk runs in the executor, the check for today's copy included
        await hass.async_add_executor_job(_write_backup, folder, manager.export_all(dict(entry.options)), day, True)
    except Exception:  # noqa: BLE001 - a failed safety copy must never affect control
        _LOGGER.warning("could not write the HomePower backup copy", exc_info=True)


def _write_backup(folder: str, data: dict, day: str | None = None, only_if_missing: bool = False) -> None:
    """Write the copy for `day` (Home Assistant's date; the system date if not given) and delete the oldest beyond the
    newest BACKUPS_KEPT. With only_if_missing, a copy already made that day is left as it is."""
    from datetime import date

    d = Path(folder)
    day = day or date.today().isoformat()
    target = d / f"homepower-{day}.json"
    if only_if_missing and target.exists():
        return
    d.mkdir(parents=True, exist_ok=True)
    # written to a temporary file and then swapped in, so a crash mid-write never leaves today's copy cut short
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf8")
    os.replace(tmp, target)
    for old in sorted(backup_files(d))[:-BACKUPS_KEPT]:      # only HomePower's own dated copies: a file put there by hand is never deleted
        old.unlink(missing_ok=True)


def backup_files(folder: Path) -> list[Path]:
    """HomePower's own daily copies (homepower-YYYY-MM-DD.json) in the backup folder: the ones it lists, keeps and restores."""
    import re

    return [p for p in folder.glob("homepower-*.json") if re.fullmatch(r"homepower-\d{4}-\d\d-\d\d\.json", p.name)]


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
        # Cached by the browser: every URL carries ASSET_TAG (the version and a hash of the files), so a changed file gets a
        # new URL. Without caching, each visit to the sidebar panel re-fetched all its files before showing anything.
        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC_URL, str(Path(__file__).parent / "frontend"), cache_headers=True)]
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
            module_url=f"{STATIC_URL}/homepower-panel.js?v={ASSET_TAG}",
            sidebar_title="Home Power",
            sidebar_icon="mdi:home-battery",
            require_admin=True,
            config={},
        )
        frontend.add_extra_js_url(hass, f"{STATIC_URL}/homepower-card.js?v={ASSET_TAG}")   # the optional Lovelace card
        _LOGGER.info("HomePower panel registered at /%s", PANEL_URL)
    except Exception as err:  # noqa: BLE001 - UI is optional
        _LOGGER.warning("HomePower sidebar panel could not be registered", exc_info=True)
        _notify(hass, "panel", f"HomePower's sidebar panel could not be registered: {err!r}. Use Settings > Devices & "
                "Services > HomePower > Configure to add batteries and schedules.")


async def async_unload_entry(hass: HomeAssistant, entry: HomePowerConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        rd = getattr(entry, "runtime_data", None)
        mgr, coord = (rd.manager, rd.coordinator) if isinstance(rd, HomePowerData) else (None, None)
        if coord:
            coord.stop_listening()
            # Reloaded, disabled or removed: nothing will be steering the batteries, so hand back any we are forcing.
            try:
                await coord.release_all()
            except Exception:  # noqa: BLE001 - unloading must still finish
                _LOGGER.warning("could not hand the batteries back while unloading", exc_info=True)
        if mgr:
            await mgr.async_close()
        try:
            from homeassistant.components import frontend

            frontend.async_remove_panel(hass, PANEL_URL, warn_if_unknown=False)
            frontend.remove_extra_js_url(hass, f"{STATIC_URL}/homepower-card.js?v={ASSET_TAG}")
        except Exception:  # noqa: BLE001
            pass
    return ok


async def _release_x1_limits_on_remove(hass: HomeAssistant) -> None:
    """HomePower is gone for good: take the import/export limits it wrote into any Anker X1 back off, putting back what was
    there before (an installer's limit, or off). On a restart or reload they stay, so they still hold while HA is down."""
    import asyncio

    from .coord_base import x1_limits_keep, x1_limits_take

    mgr = HomePowerManager(hass)
    await mgr.async_load()
    state = mgr.event_state
    if not any(k.startswith("x1lim:") for k in state):
        return
    from .core.serde import device_from_dict
    from .drivers.anker import AnkerModbusDriver
    from .drivers.modbus import ModbusError

    drivers = []                        # only the X1s with limits of HomePower's: nothing else is connected to
    for d in mgr.devices.values():
        if d.get("driver") == "anker_modbus" and any(k.startswith(f"x1lim:{d.get('device_id')}:") for k in state):
            try:
                drivers.append(AnkerModbusDriver(device_from_dict(d)))
            except Exception:  # noqa: BLE001 - a damaged row is skipped
                _LOGGER.debug("could not build %s", d.get("device_id"), exc_info=True)
    try:
        for drv in drivers:
            did = drv.config.device_id
            mine, orig = x1_limits_take(state, did)
            try:
                if not (await asyncio.wait_for(drv.read(), 15)).online:     # also learns the 32-bit word order the limits need
                    raise ConnectionError("the battery did not answer")
                changed = await asyncio.wait_for(drv.set_grid_limits(None, None, mine, orig), 30)
                if mine:                                    # still in place: e.g. the word order could not be learned
                    raise RuntimeError("its limits could not be written")
                _LOGGER.info("HomePower removed: %s: %s", did, ", ".join(changed) or "its own limits were already gone")
            except Exception as err:  # noqa: BLE001 - best effort: removal must finish with the battery unreachable
                expected = isinstance(err, (ConnectionError, RuntimeError, OSError, asyncio.TimeoutError, ModbusError))
                _LOGGER.warning("HomePower removed, but the import/export limits it wrote into %s could not be taken off (%s): "
                                "check them in the Anker app", did, err or type(err).__name__, exc_info=not expected)
            finally:
                x1_limits_keep(state, did, mine, orig)      # whatever is still HomePower's stays recorded
    finally:
        for drv in drivers:
            try:
                await drv.close()
            except Exception:  # noqa: BLE001
                pass
    raw = await mgr._store.async_load() or {}                       # only the limit flags change: everything else is saved as it was
    raw["event_state"] = {k: v for k, v in state.items() if v}
    await mgr._store.async_save(raw)                                # what is still HomePower's (if anything) is still known


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """HomePower was deleted. Its batteries, schedules and history are kept (so adding it again restores everything)
    unless the user ticked 'erase everything when HomePower is removed' in Settings. Any X1 limits it wrote are taken off."""
    try:
        await _release_x1_limits_on_remove(hass)
    except Exception:  # noqa: BLE001 - removal must always finish
        _LOGGER.warning("could not check for X1 limits HomePower wrote", exc_info=True)
    if not entry.options.get("erase_on_remove"):
        _LOGGER.info("HomePower removed; its saved batteries, schedules and history were kept")
        return
    from homeassistant.helpers.storage import Store

    await Store(hass, STORAGE_VERSION, STORAGE_KEY).async_remove()
    _LOGGER.info("HomePower removed; its saved batteries, keys, schedules and history were erased")


async def _options_updated(hass: HomeAssistant, entry: HomePowerConfigEntry) -> None:
    """Settings changed in the panel: apply in place, no reload (keeps live subscriptions)."""
    rd = getattr(entry, "runtime_data", None)
    coordinator = rd.coordinator if isinstance(rd, HomePowerData) else None
    if coordinator:
        coordinator.apply_settings({**entry.data, **entry.options})
        await coordinator.async_request_refresh()
