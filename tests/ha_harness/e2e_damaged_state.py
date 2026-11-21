"""HomePower must start whatever is in its saved state: old versions' files, and damaged values in the newer fields."""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

import asyncio, copy, json, tempfile
import conftest  # noqa: F401
import _harness  # noqa: F401 - Python 3.12+ server shutdown (see _harness.py)
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util
from custom_components.homepower.manager import HomePowerManager
from custom_components.homepower.coordinator import HomePowerCoordinator

GOOD_DEVICE = {"device_id": "e", "name": "Eco", "kind": "ecoflow_ultra_x", "control_enabled": False,
               "connection": {"sn": "BK61", "access_key": "a", "secret_key": "b", "region": "eu"}}

CASES = {
    "an old file with none of the newer fields": {"devices": [GOOD_DEVICE]},
    "newer fields with the wrong types": {"devices": [GOOD_DEVICE], "weather": {"temps": "x", "model": 5}, "traces": {"e": "junk", "f": [1, "x", {"mode": "charge"}]},
                                          "plan_log": {"2026-01-01": "junk", "2026-01-02": {"e": {"ps": [1]}}}, "charge_rates": {"e": "fast", "f": None},
                                          "ecoflow_orig": {"e": 5}, "confirmed": ["e", 7], "safe": "yes"},
    "newer fields empty": {"devices": [GOOD_DEVICE], "weather": {}, "traces": {}, "plan_log": {}, "charge_rates": {}, "ecoflow_orig": {}, "confirmed": [], "safe": False},
    "numbers that are not numbers": {"devices": [GOOD_DEVICE], "weather": {"temps": {"2026-01-01": [float("nan"), 5]}, "model": {"k": float("inf"), "days": "x", "r": None}},
                                     "charge_rates": {"e": float("nan")}},
}


async def start(saved):
    tmp = tempfile.mkdtemp(); hass = HomeAssistant(tmp); hass.config.config_dir = tmp
    entry = ConfigEntry(data={}, disabled_by=None, discovery_keys={}, domain="homepower", entry_id="e1", minor_version=1, options={},
                        pref_disable_new_entities=False, pref_disable_polling=False, source="user", subentries_data=[],
                        title="HomePower", unique_id="homepower", version=1)
    mgr = HomePowerManager(hass)

    async def load():
        return copy.deepcopy(saved)
    mgr._store.async_load = load
    await mgr.async_load()
    coord = HomePowerCoordinator(hass, entry, mgr, *mgr.build()); mgr.coordinator = coord
    mgr.public_devices(); json.dumps(coord.view(), default=str); coord.diagnostics(); coord.debug_bundle(); coord.setup_check()
    return mgr, coord


async def main():
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/London"))
    for name, saved in CASES.items():
        try:
            mgr, coord = await start(saved)
        except Exception as err:  # noqa: BLE001
            print("FAIL", name, "->", repr(err))
            raise
        print("ok  ", name, "| safe =", mgr.safe, "| rates =", coord.rate_learner.rates)
    print("E2E9 OK")


asyncio.run(main())
