"""Websocket API used by the HomePower sidebar panel."""
from __future__ import annotations

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from ._schema import vol
from .const import DOMAIN, SIGNAL_UPDATE
from .core.profiles import catalogue
from .core.serde import ValidationError
from .core.settings import DEFAULTS, SettingsError, clean


def _mgr(hass: HomeAssistant):
    mgr = hass.data.get(DOMAIN, {}).get("manager")
    if mgr is None:
        raise ValidationError("HomePower is still starting; try again in a moment")
    return mgr


def _entry(hass: HomeAssistant):
    entries = hass.config_entries.async_entries(DOMAIN)
    return entries[0] if entries else None


def _settings(entry) -> dict:
    cfg = {**entry.data, **entry.options}
    return {k: cfg.get(k, d) for k, d in DEFAULTS.items()}


def _payload(hass: HomeAssistant) -> dict:
    mgr, entry = _mgr(hass), _entry(hass)
    return {
        "devices": mgr.public_devices(),
        "schedules": list(mgr.schedules.values()),
        "goals": list(mgr.goals.values()),
        "settings": _settings(entry) if entry else {},
        "catalogue": catalogue(),
        "paused": mgr.paused, "backup": mgr.backup, "away": mgr.away, "scenes": list(mgr.scenes.values()),
        "entity_ids": mgr.entity_ids(),
        "live": mgr.coordinator.view() if mgr.coordinator else {"devices": {}, "events": []},
    }


@callback
def async_register(hass: HomeAssistant) -> None:
    for cmd in (ws_get, ws_subscribe, ws_save_device, ws_delete_device,
                ws_save_schedule, ws_delete_schedule, ws_save_settings, ws_pause,
                ws_override, ws_clear_override,
                ws_discover_anker, ws_discover_ecoflow, ws_probe, ws_control,
                ws_save_goal, ws_delete_goal, ws_flag, ws_preview, ws_diagnostics, ws_test,
                ws_save_scene, ws_delete_scene, ws_apply_scene, ws_export, ws_import):
        websocket_api.async_register_command(hass, cmd)


@websocket_api.websocket_command({vol.Required("type"): "homepower/get"})
@websocket_api.require_admin
@callback
def ws_get(hass, connection, msg):
    try:
        connection.send_result(msg["id"], _payload(hass))
    except ValidationError as err:   # e.g. still starting
        connection.send_error(msg["id"], "invalid", str(err))


@websocket_api.websocket_command({vol.Required("type"): "homepower/subscribe"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_subscribe(hass, connection, msg):
    """Push the live view whenever the control loop finishes a cycle."""
    @callback
    def push() -> None:
        # Looked up every time: after the integration is reloaded there is a new control loop to follow.
        mgr = hass.data.get(DOMAIN, {}).get("manager")
        coord = mgr.coordinator if mgr else None
        if coord:
            connection.send_message(websocket_api.event_message(msg["id"], coord.view()))

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(hass, SIGNAL_UPDATE, push)
    connection.send_result(msg["id"])
    push()


def _guard(fn):
    async def wrapper(hass, connection, msg):
        try:
            result = await fn(hass, msg)
        except ValidationError as err:
            connection.send_error(msg["id"], "invalid", str(err))
            return
        connection.send_result(msg["id"], result)

    return wrapper


@websocket_api.websocket_command(
    {vol.Required("type"): "homepower/device/save", vol.Required("device"): dict}
)
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_save_device(hass, msg):
    return await _mgr(hass).save_device(msg["device"])


@websocket_api.websocket_command(
    {vol.Required("type"): "homepower/device/delete", vol.Required("device_id"): str}
)
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_delete_device(hass, msg):
    await _mgr(hass).delete_device(msg["device_id"])
    return {}


@websocket_api.websocket_command(
    {vol.Required("type"): "homepower/schedule/save", vol.Required("schedule"): dict}
)
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_save_schedule(hass, msg):
    return await _mgr(hass).save_schedule(msg["schedule"])


@websocket_api.websocket_command(
    {vol.Required("type"): "homepower/schedule/delete", vol.Required("schedule_id"): str}
)
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_delete_schedule(hass, msg):
    await _mgr(hass).delete_schedule(msg["schedule_id"])
    return {}


@websocket_api.websocket_command(
    {vol.Required("type"): "homepower/settings/save", vol.Required("settings"): dict}
)
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_save_settings(hass, msg):
    entry = _entry(hass)
    try:
        new = clean(msg["settings"])
    except SettingsError as err:
        raise ValidationError(str(err)) from err
    # The options update listener applies these in place (no reload).
    hass.config_entries.async_update_entry(entry, options={**entry.options, **new})
    return new


@websocket_api.websocket_command({vol.Required("type"): "homepower/pause", vol.Required("paused"): bool})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_pause(hass, msg):
    await _mgr(hass).set_paused(msg["paused"])
    return {"paused": msg["paused"]}


@websocket_api.websocket_command({
    vol.Required("type"): "homepower/override",
    vol.Required("device_ids"): [str],
    vol.Required("mode"): vol.In(["auto", "charge", "discharge", "hold"]),
    vol.Optional("power_w"): vol.Any(None, vol.All(vol.Coerce(int), vol.Range(min=0, max=50000))),
    vol.Optional("target_soc"): vol.Any(None, vol.All(vol.Coerce(int), vol.Range(min=0, max=100))),
    vol.Required("minutes"): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_override(hass, msg):
    from .core.models import Desired, Mode

    coord = _mgr(hass).coordinator
    coord.set_override(msg["device_ids"] or None,
                       Desired(Mode(msg["mode"]), msg.get("power_w"), msg.get("target_soc")), msg["minutes"])
    await coord.async_request_refresh()
    return {}


@websocket_api.websocket_command({vol.Required("type"): "homepower/override/clear", vol.Required("device_ids"): [str]})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_clear_override(hass, msg):
    coord = _mgr(hass).coordinator
    coord.clear_override(msg["device_ids"] or None)
    await coord.async_request_refresh()
    return {}


@websocket_api.websocket_command({vol.Required("type"): "homepower/discover/anker"})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_discover_anker(hass, msg):
    """Scan the local network for Anker batteries that have Modbus TCP switched on."""
    return {"found": await _mgr(hass).discover_anker()}


@websocket_api.websocket_command({
    vol.Required("type"): "homepower/discover/ecoflow",
    vol.Required("region"): str, vol.Required("access_key"): str, vol.Required("secret_key"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_discover_ecoflow(hass, msg):
    return {"devices": await _mgr(hass).discover_ecoflow(msg["region"], msg["access_key"], msg["secret_key"])}


@websocket_api.websocket_command({vol.Required("type"): "homepower/probe", vol.Required("device"): dict})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_probe(hass, msg):
    """Read the battery once (no writes) so the user can see it works before saving."""
    return await _mgr(hass).probe(msg["device"])


@websocket_api.websocket_command({
    vol.Required("type"): "homepower/device/control",
    vol.Required("device_id"): str, vol.Required("enabled"): bool,
})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_control(hass, msg):
    """Allow / stop HomePower sending commands to one battery (monitor-only when off)."""
    mgr = _mgr(hass)
    dev = mgr.devices.get(msg["device_id"])
    if dev is None:
        raise ValidationError("unknown battery")
    await mgr.save_device({**dev, "control_enabled": msg["enabled"]})
    return {}


@websocket_api.websocket_command({vol.Required("type"): "homepower/goal/save", vol.Required("goal"): dict})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_save_goal(hass, msg):
    return await _mgr(hass).save_goal(msg["goal"])


@websocket_api.websocket_command({vol.Required("type"): "homepower/goal/delete", vol.Required("goal_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_delete_goal(hass, msg):
    await _mgr(hass).delete_goal(msg["goal_id"])
    return {}


@websocket_api.websocket_command({
    vol.Required("type"): "homepower/flag", vol.Required("flag"): vol.In(["backup", "away"]), vol.Required("enabled"): bool,
})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_flag(hass, msg):
    mgr = _mgr(hass)
    await (mgr.set_backup if msg["flag"] == "backup" else mgr.set_away)(msg["enabled"])
    return {}


@websocket_api.websocket_command({vol.Required("type"): "homepower/preview", vol.Optional("hours", default=24): vol.All(int, vol.Range(min=1, max=72))})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_preview(hass, msg):
    return _mgr(hass).coordinator.preview_view(msg["hours"])


@websocket_api.websocket_command({vol.Required("type"): "homepower/diagnostics"})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_diagnostics(hass, msg):
    c = _mgr(hass).coordinator
    return {"devices": c.diagnostics(), "events": [list(e) for e in list(c._snap.events)[-100:]],
            "alerts": sorted(c.alerts.active()), "notes": c.last_result.notes}


@websocket_api.websocket_command({vol.Required("type"): "homepower/device/test", vol.Required("device_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_test(hass, msg):
    return await _mgr(hass).coordinator.test_control(msg["device_id"])


@websocket_api.websocket_command({vol.Required("type"): "homepower/scene/save", vol.Required("scene"): dict})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_save_scene(hass, msg):
    return await _mgr(hass).save_scene(msg["scene"])


@websocket_api.websocket_command({vol.Required("type"): "homepower/scene/delete", vol.Required("name"): str})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_delete_scene(hass, msg):
    await _mgr(hass).delete_scene(msg["name"])
    return {}


@websocket_api.websocket_command({vol.Required("type"): "homepower/scene/apply", vol.Required("name"): str})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_apply_scene(hass, msg):
    await _mgr(hass).apply_scene(msg["name"])
    return {}


@websocket_api.websocket_command({vol.Required("type"): "homepower/export", vol.Optional("include_secrets"): bool})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_export(hass, msg):
    entry = _entry(hass)
    return _mgr(hass).export_all(_settings(entry) if entry else {}, bool(msg.get("include_secrets")))


@websocket_api.websocket_command({vol.Required("type"): "homepower/import", vol.Required("data"): dict})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_import(hass, msg):
    return await _mgr(hass).import_all(msg["data"], _entry(hass))
