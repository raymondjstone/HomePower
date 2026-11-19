"""A battery that is being forced must always be handed back: when control is switched off while the first hand-back fails, when it is
deleted, when a test run (dry run) is turned on, and the stale held-list entry after a clean hand-back. (Real HA, simulated Anker.)"""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]
import asyncio, tempfile
from types import SimpleNamespace
import conftest  # noqa
from test_drivers import FakeAnker
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util
from custom_components.homepower.manager import HomePowerManager
from custom_components.homepower.coordinator import HomePowerCoordinator
from custom_components.homepower.core.models import Desired, Mode
from hp.drivers.modbus import to_i32, ModbusError


def mk_entry(opts=None):
    return ConfigEntry(data={}, disabled_by=None, discovery_keys={}, domain="homepower", entry_id="e1", minor_version=1, options=opts or {},
                       pref_disable_new_entities=False, pref_disable_polling=False, source="user", subentries_data=[],
                       title="HomePower", unique_id="homepower", version=1)


async def setup(tmp, dev, port, opts=None):
    hass = HomeAssistant(tmp); hass.config.config_dir = tmp
    entry = mk_entry(opts)
    mgr = HomePowerManager(hass); await mgr.async_load()
    coord = HomePowerCoordinator(hass, entry, mgr, *mgr.build()); mgr.coordinator = coord
    hass.data["homepower"] = {"manager": mgr}
    hass.config_entries = SimpleNamespace(async_get_known_entry=lambda i: entry)
    return hass, mgr, coord


async def main():
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/London"))
    dev = FakeAnker(); srv = await asyncio.start_server(dev.handle, "127.0.0.1", 0); port = srv.sockets[0].getsockname()[1]
    tmp = tempfile.mkdtemp()
    hass, mgr, coord = await setup(tmp, dev, port)
    conn = {"host": "127.0.0.1", "port": port}
    await mgr.save_device({"device_id": "a", "name": "A", "kind": "anker_e5000_pro", "connection": conn, "control_enabled": True})
    coord.set_override(["a"], Desired(Mode.CHARGE, 1500), 30)
    await coord._async_update_data()
    print("A forced:", dev.hold[10064], "forced", sorted(mgr.forced))

    # --- 1: revoke control while the hand-back write fails once (Modbus timeout)
    drv = mgr._adapters["a"][1].driver
    real = drv.client.write_register
    async def boom(*a, **k): raise ModbusError("timeout")
    drv.client.write_register = boom
    real_wm = drv.client.write_multiple
    drv.client.write_multiple = boom
    await mgr.save_device({**mgr.devices["a"], "control_enabled": False})
    # the connection recovers; the loop runs many cycles
    drv = mgr._adapters["a"][1].driver
    drv.client.write_register = real; drv.client.write_multiple = real_wm
    coord.clear_override()
    for _ in range(5):
        coord._snap.devices["a"].last_apply = None
        await coord._async_update_data()
    assert dev.hold[10064] != 3 and to_i32(dev.hold[10071], dev.hold[10072], "big") == 0, ("revoked control, hand-back failed once: still forced", dev.hold[10064])
    print("1 revoke + failed hand-back: the loop retried and handed it back")

    # --- 2: stale forced id after a successful revoke hand-back
    await mgr.save_device({**mgr.devices["a"], "control_enabled": True})
    for _ in range(3):
        coord._snap.devices["a"].last_apply = None
        await coord._async_update_data()
    print("   re-enabled -> handed back:", dev.hold[10064], "forced", sorted(mgr.forced))
    coord.set_override(["a"], Desired(Mode.CHARGE, 1500), 30)
    coord._snap.devices["a"].last_applied = None
    await coord._async_update_data()
    await mgr.save_device({**mgr.devices["a"], "control_enabled": False})   # succeeds now
    assert sorted(mgr.forced) == [] and sorted(coord._forced_ids) == [], (sorted(mgr.forced), sorted(coord._forced_ids))
    print("2 stale id   : a clean hand-back leaves no stale held id")

    # --- 3: delete while hand-back fails
    await mgr.save_device({**mgr.devices["a"], "control_enabled": True})
    coord.set_override(["a"], Desired(Mode.CHARGE, 1500), 30)
    coord._snap.devices["a"].last_applied = None
    await coord._async_update_data()
    drv = mgr._adapters["a"][1].driver
    drv.client.write_register = boom; drv.client.write_multiple = boom
    from custom_components.homepower.core.serde import ValidationError
    try:
        await mgr.delete_device("a")
        raise AssertionError("deleting a held battery whose hand-back failed must be refused")
    except ValidationError:
        pass
    assert "a" in mgr.devices
    drv.client.write_register = real; drv.client.write_multiple = real_wm
    await mgr.delete_device("a")                                 # the link is back: now it is handed back and deleted
    for _ in range(3):
        await coord._async_update_data()
    await coord.release_all()
    assert dev.hold[10064] != 3 and "a" not in mgr.devices, dev.hold[10064]
    print("3 delete     : refused while the hand-back failed; handed back and deleted once it worked")
    await coord.async_close()

    # --- 4: dry run switched on while forced
    dev2 = FakeAnker(); srv2 = await asyncio.start_server(dev2.handle, "127.0.0.1", 0); p2 = srv2.sockets[0].getsockname()[1]
    await mgr.save_device({"device_id": "b", "name": "B", "kind": "anker_e5000_pro", "connection": {"host": "127.0.0.1", "port": p2}, "control_enabled": True})
    coord2 = HomePowerCoordinator(hass, mk_entry(), mgr, *mgr.build()); mgr.coordinator = coord2
    coord2.set_override(["b"], Desired(Mode.CHARGE, 1500), 30)
    await coord2._async_update_data()
    print("4) B forced:", dev2.hold[10064])
    coord2.apply_settings({"dry_run": True})
    coord2.clear_override()
    for _ in range(3):
        coord2._snap.devices["b"].last_apply = None
        await coord2._async_update_data()
    assert dev2.hold[10064] != 3 and to_i32(dev2.hold[10071], dev2.hold[10072], "big") == 0, dev2.hold[10064]
    print("4 dry run    : a forced battery is still handed back")
    print("E2E_HANDBACK OK")
    await coord2.async_close()
    await mgr.async_close()
    srv.close(); srv2.close()
    await hass.async_stop(force=True)

asyncio.run(main())
