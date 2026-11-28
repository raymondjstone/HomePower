"""homepower.* services: pause/resume and manual overrides, usable from automations."""
from __future__ import annotations

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.helpers import config_validation as cv

from ._schema import vol
from .const import DOMAIN
from .runtime import running_manager
from .core.models import Desired, Mode


def _error(key: str, message: str, **placeholders: str) -> ServiceValidationError:
    """An action's error, translated from strings.json (exceptions.<key>) in the frontend. The English message is passed too, so
    the log and anything reading str(err) get the same words without the translations having to be loaded."""
    return ServiceValidationError(message, translation_domain=DOMAIN, translation_key=key, translation_placeholders=placeholders)


def _unknown(ids, known) -> ServiceValidationError:
    names, have = ", ".join(ids), ", ".join(sorted(known)) or "none"
    return _error("unknown_battery", f"no battery called {names} (known: {have})", names=names, known=have)


def _mgr(hass: HomeAssistant):
    """The running HomePower, or a clear error: an action called while HomePower is not loaded must not quietly do nothing."""
    m = running_manager(hass)
    if m is None or m.coordinator is None:
        raise _error("not_loaded", "HomePower is not loaded (it may still be starting, or be disabled)")
    return m


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
            raise _error("override_refused", str(err), error=str(err)) from err
        await m.coordinator.async_request_refresh()

    async def clear_override(call: ServiceCall) -> None:
        m = _mgr(hass)
        if m and m.coordinator:
            ids = call.data.get("device_ids")
            known = {a.config.device_id for a in m.coordinator.adapters}
            if ids and (unknown := sorted(set(ids) - known)):     # as for override: a mistyped id must not silently leave one forced
                raise _unknown(unknown, known)
            m.coordinator.clear_override(ids)
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
                raise _error("scene_refused", str(err), error=str(err)) from err

    async def test_control(call: ServiceCall):
        m = _mgr(hass)
        if not m or not m.coordinator:
            return {"ok": False}
        known = {a.config.device_id for a in m.coordinator.adapters}
        if call.data["device_id"] not in known:     # the response is optional: an automation not reading it must still hear of a typo
            raise _unknown([call.data["device_id"]], known)
        return await m.coordinator.test_control(call.data["device_id"])

    hass.services.async_register(DOMAIN, "backup", admin(backup), schema=vol.Schema({vol.Required("enabled"): bool}))
    hass.services.async_register(DOMAIN, "apply_scene", admin(scene), schema=vol.Schema({vol.Required("name"): str}))
    hass.services.async_register(DOMAIN, "away", admin(away), schema=vol.Schema({vol.Required("enabled"): bool}))
    hass.services.async_register(DOMAIN, "test_control", admin(test_control), schema=vol.Schema({vol.Required("device_id"): str}),
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "pause", admin(pause), schema=vol.Schema({}))      # no fields: anything else is refused
    hass.services.async_register(DOMAIN, "resume", admin(resume), schema=vol.Schema({}))
    hass.services.async_register(
        DOMAIN, "override", admin(override),
        schema=vol.Schema({
            vol.Optional("device_ids"): vol.All(cv.ensure_list, [cv.string]),   # one id, or a list of them
            vol.Required("mode"): vol.In([m.value for m in Mode]),
            vol.Optional("power_w"): vol.All(vol.Coerce(int), vol.Range(min=0, max=50000)),
            vol.Optional("target_soc"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
            vol.Optional("minutes", default=60): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
        }),
    )
    hass.services.async_register(
        DOMAIN, "clear_override", admin(clear_override),
        schema=vol.Schema({vol.Optional("device_ids"): vol.All(cv.ensure_list, [cv.string])}),   # one id, or a list of them
    )
