"""Finding Anker batteries by themselves: a quiet scan of the local network after start-up and twice a day.

Anker batteries announce nothing (no zeroconf or DHCP name to match), so Home Assistant cannot find them; HomePower's own scan
(the one behind "Add battery") is run in the background instead. A battery found that HomePower does not have yet is offered
in Settings > Devices & services as discovered; one it has that has moved to another address gets its new address. The
addresses HomePower already talks to are left out of the scan: an Anker battery accepts one Modbus client at a time.
Switched off with Settings > "Find batteries automatically".
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import discovery_flow
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN
from .core.found import known_hosts, match_known, norm_serial
from .core.serde import ValidationError

_LOGGER = logging.getLogger(__name__)
FIRST_SCAN_AFTER_S = 120          # after start-up has settled (and never in the middle of it)
SCAN_EVERY = timedelta(hours=12)


def enabled(entry) -> bool:
    return bool(entry.options.get("auto_discover", True))


async def learn_serials(manager) -> None:
    """Anker batteries saved before serials were kept: read each one's serial over HomePower's own connection, so a later move
    to another address is recognised."""
    coord = manager.coordinator
    for a in list(getattr(coord, "adapters", []) or []):
        dev = manager.devices.get(a.config.device_id)
        if not dev or dev.get("driver") != "anker_modbus" or (dev.get("connection") or {}).get("serial"):
            continue
        reader = getattr(getattr(a, "driver", None), "read_serial", None)
        if reader is None:
            continue
        try:
            serial = norm_serial(await asyncio.wait_for(reader(), 10))
        except Exception:  # noqa: BLE001 - offline or busy: try again at the next scan
            continue
        if serial:
            try:
                await manager.save_device({**dev, "connection": {**dev["connection"], "serial": serial}})
            except ValidationError:
                _LOGGER.debug("could not save the serial of %s", a.config.device_id, exc_info=True)


async def scan_once(hass: HomeAssistant, entry, manager) -> list[dict]:
    """One scan: put right the address of a known battery that moved, and offer the new ones. Returns what was found."""
    if not enabled(entry):
        return []
    await learn_serials(manager)
    skip = known_hosts(manager.devices)
    found = [f for f in await manager.discover_anker(skip=skip) if f.get("host") not in skip]
    for f in found:
        host, port, serial = f["host"], int(f.get("port") or 502), norm_serial(f.get("serial"))
        did, update = match_known(manager.devices, "anker", host, port, serial)
        if did is not None:
            if update is not None:
                try:
                    await manager.save_device(update)
                    _LOGGER.info("%s is now at %s:%s; its address was updated", did, host, port)
                except ValidationError:
                    _LOGGER.warning("%s moved to %s:%s but its address could not be updated", did, host, port, exc_info=True)
            continue
        discovery_flow.async_create_flow(
            hass, DOMAIN, context={"source": SOURCE_INTEGRATION_DISCOVERY},
            data={"vendor": "anker", "host": host, "port": port, "serial": serial, "model": f.get("model"), "x1": bool(f.get("x1"))})
    return found


def async_start(hass: HomeAssistant, entry, manager) -> None:
    """The first scan a little after start-up, then every 12 hours, for as long as the entry is loaded."""

    async def _run() -> None:
        try:
            await scan_once(hass, entry, manager)
        except Exception:  # noqa: BLE001 - finding batteries is a convenience: it must never affect control
            _LOGGER.debug("the battery scan failed", exc_info=True)

    async def _first() -> None:
        if not enabled(entry):
            return              # switched on later: the 12-hourly scan picks it up
        await asyncio.sleep(FIRST_SCAN_AFTER_S)
        await _run()

    @callback
    def _every(_now) -> None:
        entry.async_create_background_task(hass, _run(), "homepower battery scan")

    entry.async_create_background_task(hass, _first(), "homepower battery scan")
    entry.async_on_unload(async_track_time_interval(hass, _every, SCAN_EVERY))
