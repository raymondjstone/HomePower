"""homepower.* services: pause/resume and manual overrides, usable from automations."""
from __future__ import annotations

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError, Unauthorized

from ._schema import vol
from .const import DOMAIN
from .core.models import Desired, Mode


def _mgr(hass: HomeAssistant):
    return hass.data.get(DOMAIN, {}).get("manager")


def async_register(hass: HomeAssistant) -> None:
    def admin(handler):
        """Like the panel and websocket API, these services need an administrator. A call with no user behind it (an automation,
        a script) is allowed, as for Home Assistant's own admin services."""
        async def wrapper(call: ServiceCall):
            uid = call.context.user_id
            if uid:
                user = await hass.auth.async_get_user(uid)
                if user is None or not user.is_admin:
                    raise Unauthorized(context=call.context)
            return await handler(call)
        return wrapper

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
        try:
            m.coordinator.set_override(call.data.get("device_ids"), d, call.data["minutes"])
        except ValueError as err:          # e.g. a mistyped battery id: say so instead of doing nothing
            raise ServiceValidationError(str(err)) from err
        await m.coordinator.async_request_refresh()

    async def clear_override(call: ServiceCall) -> None:
        m = _mgr(hass)
        if m and m.coordinator:
            m.coordinator.clear_override(call.data.get("device_ids"))
            await m.coordinator.async_request_refresh()

    async def backup(call: ServiceCall) -> None:
        if m := _mgr(hass):
            await m.set_backup(bool(call.data["enabled"]))

    async def away(call: ServiceCall) -> None:
        if m := _mgr(hass):
            await m.set_away(bool(call.data["enabled"]))

    async def scene(call: ServiceCall) -> None:
        if m := _mgr(hass):
            try:
                await m.apply_scene(call.data["name"])
            except ValueError as err:   # e.g. no scene with that name: a clear message, not a stack trace in the log
                raise ServiceValidationError(str(err)) from err

    async def test_control(call: ServiceCall):
        m = _mgr(hass)
        return await m.coordinator.test_control(call.data["device_id"]) if m and m.coordinator else {"ok": False}

    hass.services.async_register(DOMAIN, "backup", admin(backup), schema=vol.Schema({vol.Required("enabled"): bool}))
    hass.services.async_register(DOMAIN, "apply_scene", admin(scene), schema=vol.Schema({vol.Required("name"): str}))
    hass.services.async_register(DOMAIN, "away", admin(away), schema=vol.Schema({vol.Required("enabled"): bool}))
    hass.services.async_register(DOMAIN, "test_control", admin(test_control), schema=vol.Schema({vol.Required("device_id"): str}),
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "pause", admin(pause))
    hass.services.async_register(DOMAIN, "resume", admin(resume))
    hass.services.async_register(
        DOMAIN, "override", admin(override),
        schema=vol.Schema({
            vol.Optional("device_ids"): [str],
            vol.Required("mode"): vol.In([m.value for m in Mode]),
            vol.Optional("power_w"): vol.All(vol.Coerce(int), vol.Range(min=0, max=50000)),
            vol.Optional("target_soc"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
            vol.Optional("minutes", default=60): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
        }),
    )
    hass.services.async_register(
        DOMAIN, "clear_override", admin(clear_override),
        schema=vol.Schema({vol.Optional("device_ids"): [str]}),
    )
