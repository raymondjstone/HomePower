"""async_setup_entry / async_unload_entry on a bare Home Assistant with NO http / frontend: control must still work."""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

import asyncio, sys, tempfile
from types import SimpleNamespace
import conftest  # noqa: F401
from test_drivers import FakeAnker
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.util import dt as dt_util
import custom_components.homepower as HP
from custom_components.homepower.const import DOMAIN


async def main():
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/London"))
    tmp = tempfile.mkdtemp(); hass = HomeAssistant(tmp); hass.config.config_dir = tmp
    dev = FakeAnker(); srv = await asyncio.start_server(dev.handle, "127.0.0.1", 0); port = srv.sockets[0].getsockname()[1]
    entry = ConfigEntry(data={}, disabled_by=None, discovery_keys={}, domain=DOMAIN, entry_id="e1", minor_version=1, options={},
                        pref_disable_new_entities=False, pref_disable_polling=False, source="user", subentries_data=[],
                        title="HomePower", unique_id="homepower", version=1)
    forwarded, unloaded = [], []
    async def fwd(e, platforms): forwarded.append(list(platforms))
    async def unl(e, platforms): unloaded.append(list(platforms)); return True
    hass.config_entries = SimpleNamespace(async_forward_entry_setups=fwd, async_unload_platforms=unl, async_get_known_entry=lambda i: entry,
                                          async_entries=lambda d: [entry])
    # a battery saved earlier by the user, so setup has something to read
    from custom_components.homepower.manager import HomePowerManager
    m0 = HomePowerManager(hass); await m0.async_load()
    m0.coordinator = None
    await m0.save_device({"device_id": "a", "name": "A", "kind": "anker_e5000_pro", "grid_meter": "yes", "connection": {"host": "127.0.0.1", "port": port}, "control_enabled": True})

    entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
    ok = await HP.async_setup_entry(hass, entry)          # no hass.http, no frontend, no panel_custom
    print("setup    :", ok, "| platforms", forwarded, "| services", sorted(hass.services.async_services().get(DOMAIN, {})))
    assert ok is True and forwarded == [["sensor", "switch"]]
    assert {"override", "pause", "backup", "away", "test_control"} <= set(hass.services.async_services()[DOMAIN])
    mgr, coord = hass.data[DOMAIN]["manager"], hass.data[DOMAIN]["e1"]
    assert coord.view()["devices"]["a"]["soc"] == 62

    # the registered services work
    await hass.services.async_call(DOMAIN, "override", {"mode": "charge", "power_w": 1200.0, "minutes": 10.0, "device_ids": ["a"]}, blocking=True)
    await asyncio.sleep(0.2)
    from hp.drivers.modbus import to_i32
    print("service  : setpoint", to_i32(dev.hold[10071], dev.hold[10072], "big"))
    assert to_i32(dev.hold[10071], dev.hold[10072], "big") == -1200
    await hass.services.async_call(DOMAIN, "clear_override", {}, blocking=True)
    await hass.services.async_call(DOMAIN, "pause", {}, blocking=True); assert mgr.paused
    await hass.services.async_call(DOMAIN, "resume", {}, blocking=True); assert not mgr.paused
    res = await hass.services.async_call(DOMAIN, "test_control", {"device_id": "a"}, blocking=True, return_response=True)
    print("test svc :", res["ok"] if isinstance(res, dict) and "ok" in res else res)

    # options update is applied in place (no reload)
    await HP._options_updated(hass, entry)

    # graceful shutdown hands back a forced battery
    coord.set_override(["a"], __import__("hp.core.models", fromlist=["Desired"]).Desired(__import__("hp.core.models", fromlist=["Mode"]).Mode.CHARGE, 900), 30)
    await coord._async_update_data()
    assert dev.hold[10064] == 3
    await coord.release_all()
    print("shutdown : mode reg", dev.hold[10064])
    assert dev.hold[10064] == 0

    # an unknown scene is a clear service error, not a stack trace
    from homeassistant.exceptions import ServiceValidationError
    try:
        await hass.services.async_call(DOMAIN, "apply_scene", {"name": "no such scene"}, blocking=True)
        raise AssertionError("unknown scene accepted")
    except ServiceValidationError as err:
        print("bad scene:", err)

    # every control cycle sends the signal the panel's live feed follows (so the feed survives a reload)
    from homeassistant.helpers.dispatcher import async_dispatcher_connect
    from custom_components.homepower.const import SIGNAL_UPDATE
    seen = []
    off = async_dispatcher_connect(hass, SIGNAL_UPDATE, lambda: seen.append(1))
    unsub = coord.async_add_listener(lambda: None)
    await coord.async_refresh(); await asyncio.sleep(0.1)
    unsub(); off()
    assert seen, "no update signal after a control cycle"

    # unloading (reload / disable / remove) hands a forced battery back too
    from hp.core.models import Desired, Mode
    coord.set_override(["a"], Desired(Mode.CHARGE, 700), 30)      # a new instruction, so it is sent at once
    await coord._async_update_data()
    assert dev.hold[10064] == 3
    ok = await HP.async_unload_entry(hass, entry)
    print("unload   : mode reg", dev.hold[10064])
    assert dev.hold[10064] == 0
    print("unload   :", ok, unloaded, "| manager gone:", "manager" not in hass.data[DOMAIN])
    assert ok and unloaded == [["sensor", "switch"]] and "manager" not in hass.data[DOMAIN]
    srv.close(); await srv.wait_closed()
    print("E2E6 OK")

asyncio.run(main())
