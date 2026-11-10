"""homepower.* services: pause/resume and manual overrides, usable from automations."""
from __future__ import annotations

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall

from .const import DOMAIN
from .core.models import Desired, Mode


def _mgr(hass: HomeAssistant):
    return hass.data.get(DOMAIN, {}).get("manager")


def async_register(hass: HomeAssistant) -> None:
    async def pause(call: ServiceCall) -> None:
        if m := _mgr(hass):
            await m.set_paused(True)

    async def resume(call: ServiceCall) -> None:
        if m := _mgr(hass):
            await m.set_paused(False)

    async def override(call: ServiceCall) -> None:
        m = _mgr(hass)
        if not m or not m.coordinator:
            return
        d = Desired(
            Mode(call.data["mode"]),
            power_w=call.data.get("power_w"),
            target_soc=call.data.get("target_soc"),
        )
        m.coordinator.set_override(call.data.get("device_ids"), d, call.data["minutes"])
        await m.coordinator.async_request_refresh()

    async def clear_override(call: ServiceCall) -> None:
        m = _mgr(hass)
        if m and m.coordinator:
            m.coordinator.clear_override(call.data.get("device_ids"))
            await m.coordinator.async_request_refresh()

    hass.services.async_register(DOMAIN, "pause", pause)
    hass.services.async_register(DOMAIN, "resume", resume)
    hass.services.async_register(
        DOMAIN, "override", override,
        schema=vol.Schema({
            vol.Optional("device_ids"): [str],
            vol.Required("mode"): vol.In([m.value for m in Mode]),
            vol.Optional("power_w"): vol.All(int, vol.Range(min=0, max=50000)),
            vol.Optional("target_soc"): vol.All(int, vol.Range(min=0, max=100)),
            vol.Optional("minutes", default=60): vol.All(int, vol.Range(min=1, max=1440)),
        }),
    )
    hass.services.async_register(
        DOMAIN, "clear_override", clear_override,
        schema=vol.Schema({vol.Optional("device_ids"): [str]}),
    )
