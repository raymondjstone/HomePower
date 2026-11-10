"""Download diagnostics from Settings > Devices & Services > HomePower (secrets are redacted)."""
from __future__ import annotations

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

REDACT = {"secret_key", "access_key", "host", "sn", "serial", "notify_service"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    coord = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coord is None:
        return {"loaded": False}
    return async_redact_data({
        "settings": {**entry.options},
        "devices": coord.diagnostics(),
        "view": {k: v for k, v in coord.view().items() if k != "events"},
        "events": [list(e) for e in list(coord._snap.events)[-50:]],
        "plan_notes": coord.last_result.notes,
    }, REDACT)
