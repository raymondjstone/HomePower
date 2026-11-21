"""The debug file and the copy-debug text promise: no keys, serial numbers or addresses. Check it with a battery that fails,
because error messages are where addresses end up."""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

import asyncio, json, tempfile
import conftest  # noqa: F401
import _harness  # noqa: F401 - Python 3.12+ server shutdown (see _harness.py)
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util
from custom_components.homepower.manager import HomePowerManager
from custom_components.homepower.coordinator import HomePowerCoordinator

SECRETS = ["10.1.2.3", "AKSECRET9876", "SKSECRET5432", "BKSERIAL0001"]


async def main():
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/London"))
    tmp = tempfile.mkdtemp(); hass = HomeAssistant(tmp); hass.config.config_dir = tmp
    entry = ConfigEntry(data={}, disabled_by=None, discovery_keys={}, domain="homepower", entry_id="e1", minor_version=1, options={},
                        pref_disable_new_entities=False, pref_disable_polling=False, source="user", subentries_data=[],
                        title="HomePower", unique_id="homepower", version=1)
    mgr = HomePowerManager(hass); await mgr.async_load()
    coord = HomePowerCoordinator(hass, entry, mgr, *mgr.build()); mgr.coordinator = coord
    mgr.devices["anker"] = {"device_id": "anker", "name": "Anker", "kind": "anker_e5000_pro", "control_enabled": True, "capacity_wh": 10000,
                            "max_charge_w": 3000, "max_discharge_w": 3000, "min_soc": 10, "connection": {"host": "10.1.2.3", "port": 1, "unit_id": 1}}
    mgr.devices["eco"] = {"device_id": "eco", "name": "Eco", "kind": "ecoflow_ultra_x", "control_enabled": True, "capacity_wh": 5000,
                          "max_charge_w": 2300, "max_discharge_w": 0, "min_soc": 10,
                          "connection": {"sn": "BKSERIAL0001", "access_key": "AKSECRET9876", "secret_key": "SKSECRET5432", "region": "eu"}}
    coord.set_config(*mgr.build())
    # make errors that mention the secrets, the way a failing library or server might
    for a in coord.adapters:
        if a.config.device_id == "eco":
            async def boom(*_a, **_k):
                raise RuntimeError("cannot reach EcoFlow: sn=BKSERIAL0001 key=AKSECRET9876 secret=SKSECRET5432 at 10.1.2.3")
            a.driver.api.quota = boom
    for _ in range(2):
        await coord.async_refresh()
    coord._log("anker", "apply failed: Connection to 10.1.2.3:1 refused")
    for a in coord.adapters:
        st = coord._snap.devices.get(a.config.device_id)
        if st:
            st.last_error = "failed for BKSERIAL0001 at 10.1.2.3 with AKSECRET9876"

    bundle = json.dumps(coord.debug_bundle(), default=str)
    leaks = [s for s in SECRETS if s in bundle]
    import re
    for sec in SECRETS:
        for m in re.finditer(re.escape(sec), bundle):
            print("   LEAK", sec, "in:", bundle[max(0, m.start() - 70):m.end() + 30])
            break
    print("debug file leaks:", leaks or "none", "|", len(bundle), "bytes")
    # the on-screen diagnostics show the user's own address and serial (they are troubleshooting), but never keys
    diag = json.dumps(coord.scrub(coord.diagnostics()), default=str)
    leaks2 = [s for s in ("AKSECRET9876", "SKSECRET5432") if s in diag]
    print("diagnostics (on screen) leaks keys:", leaks2 or "none")
    assert not leaks and not leaks2, (leaks, leaks2)
    # a version number or a short dotted value is not an address
    assert "1.2.3.4x" in coord.scrub("v1.2.3.4x", strict=True)
    print("E2E10 OK")


asyncio.run(main())
