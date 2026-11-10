"""Websocket API used by the HomePower sidebar panel."""
from __future__ import annotations

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from ._schema import vol
from .const import (
    CONF_CHARGE_CAP_W,
    CONF_COOLDOWN_S,
    CONF_DISCHARGE_CAP_W,
    CONF_GRID_ENTITY,
    CONF_GRID_INVERT,
    CONF_IMPORT_LIMIT_W,
    CONF_ZERO_IMPORT_DEVICE,
    DEFAULT_COOLDOWN_S,
    DEFAULT_IMPORT_LIMIT_W,
    DOMAIN,
)
from .core.profiles import catalogue
from .core.serde import ValidationError

SETTING_KEYS = (
    CONF_IMPORT_LIMIT_W, CONF_COOLDOWN_S, CONF_CHARGE_CAP_W, CONF_DISCHARGE_CAP_W,
    CONF_GRID_ENTITY, CONF_GRID_INVERT, CONF_ZERO_IMPORT_DEVICE,
)


def _mgr(hass: HomeAssistant):
    return hass.data.get(DOMAIN, {}).get("manager")


def _entry(hass: HomeAssistant):
    entries = hass.config_entries.async_entries(DOMAIN)
    return entries[0] if entries else None


def _settings(entry) -> dict:
    cfg = {**entry.data, **entry.options}
    return {
        CONF_IMPORT_LIMIT_W: cfg.get(CONF_IMPORT_LIMIT_W, DEFAULT_IMPORT_LIMIT_W),
        CONF_COOLDOWN_S: cfg.get(CONF_COOLDOWN_S, DEFAULT_COOLDOWN_S),
        CONF_CHARGE_CAP_W: cfg.get(CONF_CHARGE_CAP_W) or 0,
        CONF_DISCHARGE_CAP_W: cfg.get(CONF_DISCHARGE_CAP_W) or 0,
        CONF_GRID_ENTITY: cfg.get(CONF_GRID_ENTITY) or "",
        CONF_GRID_INVERT: bool(cfg.get(CONF_GRID_INVERT, False)),
        CONF_ZERO_IMPORT_DEVICE: cfg.get(CONF_ZERO_IMPORT_DEVICE) or "",
    }


def _payload(hass: HomeAssistant) -> dict:
    mgr, entry = _mgr(hass), _entry(hass)
    return {
        "devices": mgr.public_devices(),
        "schedules": list(mgr.schedules.values()),
        "settings": _settings(entry) if entry else {},
        "catalogue": catalogue(),
        "paused": mgr.paused,
        "live": mgr.coordinator.view() if mgr.coordinator else {"devices": {}, "events": []},
    }


@callback
def async_register(hass: HomeAssistant) -> None:
    for cmd in (ws_get, ws_subscribe, ws_save_device, ws_delete_device,
                ws_save_schedule, ws_delete_schedule, ws_save_settings, ws_pause,
                ws_override, ws_clear_override,
                ws_discover_anker, ws_discover_ecoflow, ws_probe, ws_control):
        websocket_api.async_register_command(hass, cmd)


@websocket_api.websocket_command({vol.Required("type"): "homepower/get"})
@websocket_api.require_admin
@callback
def ws_get(hass, connection, msg):
    connection.send_result(msg["id"], _payload(hass))


@websocket_api.websocket_command({vol.Required("type"): "homepower/subscribe"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_subscribe(hass, connection, msg):
    """Push the live view whenever the control loop finishes a cycle."""
    mgr = _mgr(hass)
    coord = mgr.coordinator if mgr else None

    @callback
    def push() -> None:
        connection.send_message(websocket_api.event_message(msg["id"], coord.view()))

    if coord:
        connection.subscriptions[msg["id"]] = coord.async_add_listener(push)
    connection.send_result(msg["id"])
    if coord:
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
    new = {k: v for k, v in msg["settings"].items() if k in SETTING_KEYS}
    for k in (CONF_IMPORT_LIMIT_W, CONF_COOLDOWN_S, CONF_CHARGE_CAP_W, CONF_DISCHARGE_CAP_W):
        if k not in new:
            continue  # partial update: leave the stored value alone
        try:
            new[k] = max(0, int(float(new.get(k) or 0)))
        except (TypeError, ValueError) as err:
            raise ValidationError(f"{k} must be a number") from err
    # Saving options reloads the entry (update listener), which rebuilds the coordinator.
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
    vol.Optional("power_w"): vol.Any(None, vol.All(int, vol.Range(min=0, max=50000))),
    vol.Optional("target_soc"): vol.Any(None, vol.All(int, vol.Range(min=0, max=100))),
    vol.Required("minutes"): vol.All(int, vol.Range(min=1, max=1440)),
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
