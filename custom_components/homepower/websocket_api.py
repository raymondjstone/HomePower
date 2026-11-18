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


def _coord(hass: HomeAssistant):
    c = _mgr(hass).coordinator
    if c is None:
        raise ValidationError("HomePower is still starting; try again in a moment")
    return c


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
        "paused": mgr.paused, "safe": mgr.safe, "backup": mgr.backup, "away": mgr.away, "scenes": list(mgr.scenes.values()),
        "entity_ids": mgr.entity_ids(),
        "live": mgr.coordinator.view() if mgr.coordinator else {"devices": {}, "events": []},
    }


@callback
def async_register(hass: HomeAssistant) -> None:
    for cmd in (ws_get, ws_subscribe, ws_save_device, ws_delete_device, ws_reorder_devices, ws_scan_registers, ws_read_registers, ws_register_notes, ws_whatif, ws_backups, ws_backup_now, ws_restore_backup, ws_compare_tariffs,
                ws_save_schedule, ws_delete_schedule, ws_save_settings, ws_pause,
                ws_override, ws_clear_override,
                ws_discover_anker, ws_discover_ecoflow, ws_probe, ws_control, ws_confirm, ws_safe, ws_debug, ws_scan_ports,
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
    {vol.Required("type"): "homepower/device/reorder", vol.Required("device_ids"): [str]}
)
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_reorder_devices(hass, msg):
    await _mgr(hass).reorder_devices(msg["device_ids"])
    return {}


@websocket_api.websocket_command({vol.Required("type"): "homepower/device/scan", vol.Required("device_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_scan_registers(hass, msg):
    return await _mgr(hass).scan_registers(msg["device_id"])


@websocket_api.websocket_command({vol.Required("type"): "homepower/device/registers", vol.Required("device_id"): str,
                                  vol.Required("addresses"): vol.All([vol.All(int, vol.Range(min=0, max=65535))], vol.Length(min=1, max=64)),
                                  vol.Optional("names", default=False): bool})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_read_registers(hass, msg):
    return await _mgr(hass).read_registers(msg["device_id"], msg["addresses"], msg["names"])


@websocket_api.websocket_command({vol.Required("type"): "homepower/device/register_notes", vol.Required("device_id"): str,
                                  vol.Optional("address"): vol.All(int, vol.Range(min=0, max=65535)), vol.Optional("label"): str})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_register_notes(hass, msg):
    return await _mgr(hass).register_note(msg["device_id"], msg.get("address"), msg.get("label"))


_PCT = vol.All(vol.Coerce(float), vol.Range(min=0, max=100))
_SCENARIO = {vol.Required("reserve"): _PCT, vol.Required("cheap"): _PCT}


@websocket_api.websocket_command({vol.Required("type"): "homepower/whatif", vol.Optional("days", default=30): vol.All(int, vol.Range(min=3, max=200)),
                                  vol.Required("base"): _SCENARIO, vol.Required("alt"): _SCENARIO})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_whatif(hass, msg):
    c = _coord(hass)
    try:
        return c.whatif(msg["days"], {"reserve_pct": msg["base"]["reserve"], "cheap_pct": msg["base"]["cheap"]},
                        {"reserve_pct": msg["alt"]["reserve"], "cheap_pct": msg["alt"]["cheap"]})
    except ValueError as err:
        raise ValidationError(str(err)) from err


@websocket_api.websocket_command({vol.Required("type"): "homepower/backups"})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_backups(hass, msg):
    return {"backups": await _mgr(hass).list_backups()}


@websocket_api.websocket_command({vol.Required("type"): "homepower/backups/now"})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_backup_now(hass, msg):
    from . import _write_backup

    entry = _entry(hass)
    mgr = _mgr(hass)
    await hass.async_add_executor_job(_write_backup, mgr._backup_dir(), mgr.export_all(dict(entry.options) if entry else {}))
    return {"backups": await mgr.list_backups()}


@websocket_api.websocket_command({vol.Required("type"): "homepower/backups/restore", vol.Required("name"): str})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_restore_backup(hass, msg):
    return await _mgr(hass).restore_backup(msg["name"], _entry(hass))


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
    mgr = _mgr(hass)
    saved = await mgr.save_schedule(msg["schedule"])
    return {**saved, "warnings": mgr.warnings_for("schedule", saved)}


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

    coord = _coord(hass)
    coord.set_override(msg["device_ids"] or None,
                       Desired(Mode(msg["mode"]), msg.get("power_w"), msg.get("target_soc")), msg["minutes"])
    await coord.async_request_refresh()
    return {}


@websocket_api.websocket_command({vol.Required("type"): "homepower/override/clear", vol.Required("device_ids"): [str]})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_clear_override(hass, msg):
    coord = _coord(hass)
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


@websocket_api.websocket_command({vol.Required("type"): "homepower/scan_ports", vol.Required("host"): vol.All(str, vol.Length(min=1, max=253)),
                                  vol.Optional("wide", default=False): bool})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_scan_ports(hass, msg):
    """Which well-known ports a device on the local network is listening on (read-only: connects and listens, sends nothing)."""
    from .drivers.portscan import scan, scan_wide

    try:
        return await (scan_wide if msg["wide"] else scan)(msg["host"])
    except ValueError as err:
        raise ValidationError(str(err)) from err


@websocket_api.websocket_command({vol.Required("type"): "homepower/safe", vol.Required("enabled"): bool})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_safe(hass, msg):
    """Safe mode: hand every battery back and stop sending commands (or leave it)."""
    await _mgr(hass).set_safe(msg["enabled"])
    return {}


@websocket_api.websocket_command({vol.Required("type"): "homepower/debug"})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_debug(hass, msg):
    """Everything useful for a bug report in one file: readings, command results, plan against actual, recent events."""
    c = _coord(hass)
    return c.debug_bundle()


@websocket_api.websocket_command({vol.Required("type"): "homepower/device/confirm", vol.Required("device_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_confirm(hass, msg):
    """Allow the first real command to a battery whose control has not been tested yet."""
    await _mgr(hass).confirm_first_command(msg["device_id"])
    return {}


@websocket_api.websocket_command({vol.Required("type"): "homepower/goal/save", vol.Required("goal"): dict})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_save_goal(hass, msg):
    mgr = _mgr(hass)
    saved = await mgr.save_goal(msg["goal"])
    return {**saved, "warnings": mgr.warnings_for("goal", saved)}


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
    return _coord(hass).preview_view(msg["hours"])


@websocket_api.websocket_command({vol.Required("type"): "homepower/diagnostics"})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_diagnostics(hass, msg):
    c = _coord(hass)
    return c.scrub({"devices": c.diagnostics(), "events": [list(e) for e in list(c._snap.events)[-100:]],
                    "alerts": sorted(c.alerts.active()), "notes": c.last_result.notes,
                    "decisions": list(reversed(c.why))[:40]})


@websocket_api.websocket_command({vol.Required("type"): "homepower/tariffs/compare", vol.Optional("days", default=30): vol.All(int, vol.Range(min=1, max=90))})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_compare_tariffs(hass, msg):
    try:
        return _coord(hass).tariff_comparison(msg["days"])
    except ValueError as err:          # the tariffs typed in Settings (e.g. one saved before a check was added): say what is wrong
        raise ValidationError(str(err)) from err


@websocket_api.websocket_command({vol.Required("type"): "homepower/device/test", vol.Required("device_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_test(hass, msg):
    return await _coord(hass).test_control(msg["device_id"])


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
