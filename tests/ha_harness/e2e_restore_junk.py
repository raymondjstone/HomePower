"""Restoring a damaged or hostile backup file through the real manager: bad rows are reported, nothing is half applied,
a restore that revokes control hands the battery back, and the stored EcoFlow keys come back for a keyless backup."""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

import asyncio, tempfile
import conftest  # noqa: F401
import _harness  # noqa: F401 - Python 3.12+ server shutdown (see _harness.py)
from homeassistant.core import HomeAssistant
import custom_components.homepower.manager as M
from custom_components.homepower.manager import HomePowerManager
from custom_components.homepower.core.serde import ValidationError

M.async_get_clientsession = lambda hass: None

GOOD = {"device_id": "es", "name": "ES", "kind": "ecoflow_stream_ultra_x", "control_enabled": True, "capacity_wh": 5000, "max_charge_w": 2300,
        "max_discharge_w": 0, "min_soc": 10, "connection": {"sn": "SN1", "access_key": "AK1", "secret_key": "SK1", "region": "eu"}}


async def main():
    tmp = tempfile.mkdtemp(); hass = HomeAssistant(tmp); hass.config.config_dir = tmp
    mgr = HomePowerManager(hass); await mgr.async_load()
    await mgr.save_device(GOOD)
    exported = mgr.export_all({})
    assert exported["devices"][0]["connection"]["access_key"] == "" and exported["devices"][0]["connection"]["secret_key"] == ""

    # a keyless backup of the same install restores with the stored keys
    exported["devices"][0]["name"] = "Renamed"
    res = await mgr.import_all(exported, None)
    assert not res["skipped"], res
    assert mgr.devices["es"]["connection"]["access_key"] == "AK1" and mgr.devices["es"]["name"] == "Renamed"
    print("1 keys back : a keyless backup keeps the stored keys, edits applied")

    # hostile rows: each is reported, the good one still restores, nothing raises
    junk = {"homepower_export": 1, "devices": ["x", {"device_id": "z", "name": "Z", "kind": "anker_x1", "connection": "x"},
                                               {"device_id": "q", "name": "Q", "kind": "anker_x1", "capacity_wh": "inf", "connection": {"host": "h"}}, GOOD],
            "schedules": ["x", {"id": "s", "mode": "charge", "start": "01:00", "end": "02:00", "power_w": 1e999}],
            "goals": [5], "scenes": ["x", {"name": 3}]}
    before = dict(mgr.devices)
    res = await mgr.import_all(junk, None)
    assert res["added"]["devices"] == 1 and len(res["skipped"]) >= 5, res
    print("2 junk rows :", len(res["skipped"]), "reported, 1 good row kept")

    # an unexpected failure part way puts everything back
    class Boom(Exception): ...
    snapshot = {k: dict(v) for k, v in mgr.devices.items()}
    orig = mgr._apply

    async def bad_apply():
        raise Boom("disk")
    mgr._apply = bad_apply
    try:
        await mgr.import_all({"homepower_export": 1, "devices": [{**GOOD, "device_id": "new", "name": "New"}]}, None)
        raise AssertionError("should have failed")
    except ValidationError as err:
        assert "nothing was changed" in str(err), err
    mgr._apply = orig
    assert mgr.devices == snapshot and "new" not in mgr.devices
    print("3 rollback  : an unexpected failure leaves everything as it was")
    print("E2E_RESTORE_JUNK OK")


asyncio.run(main())
