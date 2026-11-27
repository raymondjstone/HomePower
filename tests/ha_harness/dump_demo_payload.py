"""A made-up home (two simulated batteries, a tariff, schedules, a goal) run through the real backend, for the README screenshots.

Usage: python tests/ha_harness/dump_demo_payload.py tests/panel_dom/demo.json  (then node tests/panel_dom/readme_shots.mjs)"""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]
import asyncio, json, tempfile
from types import SimpleNamespace
import conftest  # noqa: F401
import _harness  # noqa: F401 - Python 3.12+ server shutdown (see _harness.py)
from test_drivers import FakeAnker
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util
from custom_components.homepower.manager import HomePowerManager
from custom_components.homepower.coordinator import HomePowerCoordinator
from custom_components.homepower import websocket_api as H

OUT = sys.argv[1]


async def main():
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/London"))
    tmp = tempfile.mkdtemp(); hass = HomeAssistant(tmp); hass.config.config_dir = tmp
    fakes, ports = [FakeAnker(), FakeAnker()], []
    for fake, soc, power, grid in zip(fakes, (74, 48), (-900, -600), (120, 120)):       # battery power: + discharging in the register
        fake.inp[10014] = soc
        fake.put(10008, -power)
        fake.put(10012, grid)
        srv = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
        ports.append(srv.sockets[0].getsockname()[1])
    entry = ConfigEntry(data={}, disabled_by=None, discovery_keys={}, domain="homepower", entry_id="e1", minor_version=1, options={},
                        pref_disable_new_entities=False, pref_disable_polling=False, source="user", subentries_data=[],
                        title="HomePower", unique_id="homepower", version=1)
    mgr = HomePowerManager(hass); await mgr.async_load()
    coord = HomePowerCoordinator(hass, entry, mgr, *mgr.build()); mgr.coordinator = coord
    _harness.attach(hass, entry, mgr)
    hass.config_entries = SimpleNamespace(async_get_known_entry=lambda i: entry, async_entries=lambda d: [entry],
                                          async_update_entry=lambda e, options=None, **kw: coord.apply_settings(options) or True)
    hass.data.setdefault("websocket_api", {})
    H.async_register(hass)
    for i, (did, name) in enumerate((("garage", "Garage"), ("utility", "Utility room"))):
        await mgr.save_device({"device_id": did, "name": name, "kind": "anker_e5000_pro", "connection": {"host": "127.0.0.1", "port": ports[i]},
                               "control_enabled": True, "priority": i, "grid_meter": "yes" if i == 0 else "no"})
        mgr.tests[did] = {"ok": True, "at": dt_util.now().isoformat()}
    coord.apply_settings({"price_peak": 0.285, "price_offpeak": 0.07, "export_price": 0.15, "offpeak_start": "23:30", "offpeak_end": "05:30",
                          "import_limit_w": 14000, "peak_shaving": True})
    await mgr.save_schedule({"id": "Cheap night charge", "start": "23:30", "end": "05:30", "mode": "charge", "power_w": 2000})
    await mgr.save_schedule({"id": "Evening peak", "start": "16:00", "end": "19:00", "mode": "discharge", "power_w": 1500})
    await mgr.save_goal({"id": "Ready for the morning", "target_soc": 90, "by": "07:00", "days": [0, 1, 2, 3, 4, 5, 6]})
    for _ in range(3):
        await coord.async_refresh()
    payload = {
        "get": H._payload(hass),
        "diag": {"devices": coord.diagnostics(), "events": [], "alerts": [], "notes": [], "decisions": []},
        "preview": coord.preview_view(24),
        "tariffs": coord.tariff_comparison(30),
    }
    open(OUT, "w", encoding="utf8").write(json.dumps(payload, default=str))
    print("wrote", OUT)
    await coord.async_close()


asyncio.run(main())
