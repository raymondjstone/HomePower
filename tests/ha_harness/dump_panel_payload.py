"""Run HomePower in real Home Assistant with a simulated battery and write the websocket payloads the panel receives.

Usage: python tests/ha_harness/dump_panel_payload.py tests/panel_dom/payload.json"""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]
import asyncio, json, tempfile
from types import SimpleNamespace
import conftest  # noqa: F401
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
    dev = FakeAnker(); srv = await asyncio.start_server(dev.handle, "127.0.0.1", 0); port = srv.sockets[0].getsockname()[1]
    entry = ConfigEntry(data={}, disabled_by=None, discovery_keys={}, domain="homepower", entry_id="e1", minor_version=1, options={},
                        pref_disable_new_entities=False, pref_disable_polling=False, source="user", subentries_data=[],
                        title="HomePower", unique_id="homepower", version=1)
    mgr = HomePowerManager(hass); await mgr.async_load()
    coord = HomePowerCoordinator(hass, entry, mgr, *mgr.build()); mgr.coordinator = coord
    hass.data["homepower"] = {"manager": mgr}
    hass.config_entries = SimpleNamespace(async_get_known_entry=lambda i: entry, async_entries=lambda d: [entry],
                                          async_update_entry=lambda e, options=None, **kw: coord.apply_settings(options) or True)
    hass.data.setdefault("websocket_api", {})
    H.async_register(hass)
    await mgr.save_device({"device_id": "a", "name": "Anker", "kind": "anker_e5000_pro", "connection": {"host": "127.0.0.1", "port": port}, "control_enabled": True})
    await mgr.save_schedule({"id": "Night", "start": "23:30", "end": "07:30", "mode": "charge", "power_w": 2000})
    await mgr.save_goal({"id": "G", "target_soc": 90, "by": "07:00", "days": [0, 1, 2, 3, 4, 5, 6]})
    for _ in range(3):
        await coord.async_refresh()
    coord.plan_log.add(dt_util.now(), "a", 1500.0, 1400.0)
    coord._observe_temp(dt_util.now())
    payload = {
        "get": H._payload(hass),
        "diag": {"devices": coord.diagnostics(), "events": [list(e) for e in list(coord._snap.events)[-100:]],
                 "alerts": sorted(coord.alerts.active()), "notes": coord.last_result.notes, "decisions": list(reversed(coord.why))[:40]},
        "preview": coord.preview_view(24),
        "tariffs": coord.tariff_comparison(30),
    }
    try:
        payload["whatif"] = coord.whatif(30, {"reserve_pct": 20, "cheap_pct": 80}, {"reserve_pct": 10, "cheap_pct": 60})
    except ValueError as err:
        payload["whatif_error"] = str(err)
    open(OUT, "w", encoding="utf8").write(json.dumps(payload, default=str))
    print("wrote", OUT, len(json.dumps(payload, default=str)), "bytes; keys:", sorted(payload["get"].keys()))
    await coord.async_close()


asyncio.run(main())
