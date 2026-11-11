"""Every websocket command the panel uses, through HA's real websocket_api decorators, with JSON checks."""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

import asyncio, json, sys, tempfile
from types import SimpleNamespace
import conftest  # noqa: F401
from test_drivers import FakeAnker
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util
from homeassistant.components import websocket_api as wa
from custom_components.homepower.manager import HomePowerManager
from custom_components.homepower.coordinator import HomePowerCoordinator
from custom_components.homepower import websocket_api as H


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
    store = {}
    def update_entry(e, options=None, **kw):
        store["options"] = options; coord.apply_settings(options); return True
    hass.config_entries = SimpleNamespace(async_get_known_entry=lambda i: entry, async_entries=lambda d: [entry], async_update_entry=update_entry)

    hass.data.setdefault("websocket_api", {})
    H.async_register(hass)
    cmds = hass.data["websocket_api"]
    print("registered", len(cmds), "commands")

    out = {}
    class Conn:
        user = SimpleNamespace(is_admin=True)
        def __init__(self): self.subscriptions = {}
        def send_result(self, i, result=None): out["r"] = ("ok", json.loads(json.dumps(result)))     # must be JSON serialisable
        def send_error(self, i, code, msg): out["r"] = ("err", code, msg)
        def send_message(self, m): out.setdefault("events", []).append(json.loads(json.dumps(m)))
        def async_handle_exception(self, msg, err): out["r"] = ("exc", repr(err))
    conn = Conn()
    n = [0]

    async def call(type_, **kw):
        n[0] += 1
        msg = {"id": n[0], "type": type_, **kw}
        handler, schema = cmds[type_]
        out.pop("r", None)
        handler(hass, conn, msg if schema is False else schema(msg))
        for _ in range(200):
            if "r" in out: break
            await asyncio.sleep(0.05)
        return out.get("r")

    async def must(type_, **kw):
        r = await call(type_, **kw)
        assert r and r[0] == "ok", (type_, r)
        return r[1]

    conn_cfg = {"host": "127.0.0.1", "port": port}
    probe = await must("homepower/probe", device={"kind": "anker_e5000_pro", "connection": conn_cfg})
    print("probe    :", {k: probe[k] for k in ("soc", "capacity_wh", "max_charge_w", "min_soc", "word_order")})
    assert probe["capacity_wh"] == 10000
    d = await must("homepower/device/save", device={"device_id": "a", "name": "A", "kind": "anker_e5000_pro", "connection": conn_cfg})
    assert d["capacity_wh"] == 10000 and d["control_enabled"] is False
    await must("homepower/device/control", device_id="a", enabled=True)
    await must("homepower/schedule/save", schedule={"id": "S1", "start": "23:30", "end": "07:30", "mode": "charge", "power_w": 2000, "presence": "any", "total_power_w": None})
    await must("homepower/schedule/save", schedule={"id": "S2", "start": "23:30", "end": "07:30", "mode": "discharge", "power_w": 900})
    await must("homepower/goal/save", goal={"id": "G", "target_soc": 90, "by": "07:00", "days": [0, 1, 2, 3, 4, 5, 6], "use_forecast": True})
    s = await must("homepower/settings/save", settings={"price_peak": "30", "price_offpeak": "7.5", "offpeak_start": "23:30", "offpeak_end": "05:30",
                                                          "peak_shaving": True, "load_rules": [{"entity": "sensor.ev", "above_w": "1000", "block": "discharge"}]})
    assert s["price_peak"] == 30.0 and coord.settings.engine.peak_shaving
    r = await call("homepower/settings/save", settings={"offpeak_start": "99:99"})
    assert r[0] == "err", r
    await coord._async_update_data()

    payload = await must("homepower/get")
    print("get      :", sorted(payload)[:9], "...")
    assert payload["devices"][0]["connection"].get("secret_key") in (None, ""), "secrets must not reach the browser"
    assert payload["goals"][0]["id"] == "G" and "price_peak" in payload["settings"] and payload["live"]["devices"]["a"]["online"]
    pv = await must("homepower/preview", hours=24)
    print("preview  :", len(pv["segments"]["a"]), "segments,", len(pv["conflicts"]), "conflict(s)")
    assert pv["conflicts"], "S1 and S2 tie at the same priority and clash"
    dg = await must("homepower/diagnostics")
    assert dg["devices"]["a"]["driver"] == "anker_modbus"
    await must("homepower/override", device_ids=["a"], mode="charge", power_w=1000, target_soc=100, minutes=10)
    await must("homepower/override/clear", device_ids=["a"])
    await must("homepower/flag", flag="backup", enabled=True); assert mgr.backup
    await must("homepower/flag", flag="away", enabled=True); assert mgr.away
    await must("homepower/flag", flag="backup", enabled=False); await must("homepower/flag", flag="away", enabled=False)
    await must("homepower/pause", paused=True); assert mgr.paused
    await must("homepower/pause", paused=False)
    t = await must("homepower/device/test", device_id="a")
    print("test     :", t["ok"], [x["name"] for x in t["steps"]])
    assert t["ok"]
    # Never scan the real network from a test: a real battery there accepts only one Modbus client.
    from custom_components.homepower.drivers import discovery as _disc
    _real_scan = _disc.scan
    _disc.scan = lambda hosts, **kw: _real_scan(["127.0.0.1"], port=port)
    r = await call("homepower/discover/anker")
    print("discover :", r[0], r[1])
    assert r[0] == "ok"
    await must("homepower/goal/delete", goal_id="G"); await must("homepower/schedule/delete", schedule_id="S1")
    await must("homepower/schedule/delete", schedule_id="S2")
    bad = await call("homepower/device/save", device={"device_id": "x", "name": "X", "kind": "anker_e5000_pro", "connection": {}})
    assert bad[0] == "err", bad
    # live subscription pushes JSON-serialisable events
    handler, schema = cmds["homepower/subscribe"]
    out.pop("r", None); handler(hass, conn, {"id": 999, "type": "homepower/subscribe"} if schema is False else schema({"id": 999, "type": "homepower/subscribe"}))
    await asyncio.sleep(0.3)
    print("subscribe:", out.get("events", [{}])[0].get("event", {}).get("devices", {}).keys())
    assert out["events"] and out["events"][0]["event"]["devices"]["a"]["soc"] == 62
    # testing an already-connected battery reuses the live connection (single-client devices)
    await must("homepower/device/save", device={"device_id": "a", "name": "A", "kind": "anker_e5000_pro", "connection": conn_cfg})
    live = mgr._adapters["a"][1].driver.client
    opened = []
    orig = asyncio.open_connection
    async def counting(*a, **k): opened.append(a); return await orig(*a, **k)
    asyncio.open_connection = counting
    try:
        again = await must("homepower/probe", device={"device_id": "a", "kind": "anker_e5000_pro", "connection": conn_cfg})
        n_live = len(opened)
        await must("homepower/probe", device={"kind": "anker_e5000_pro", "connection": conn_cfg})   # a new battery: its own connection
    finally:
        asyncio.open_connection = orig
    print("probe reuse: new connections opened for the live battery =", n_live, "(new battery:", len(opened) - n_live, ")")
    assert again["soc"] == 62 and n_live <= 1 and len(opened) - n_live == 1
    # scenes, backup and restore through the websocket (results must be JSON-safe)
    g = await must("homepower/get")
    assert any(x["name"] == "Holiday" for x in g["scenes"]) and "combined" in g["live"] and "health" in g["live"]
    await must("homepower/scene/save", scene={"name": "WS scene", "away": True, "settings": {"import_limit_w": 5000}})
    await must("homepower/scene/apply", name="WS scene")
    assert mgr.away and coord.settings.engine.import_limit_w == 5000
    r = await call("homepower/scene/apply", name="missing"); assert r[0] == "err", r
    r = await call("homepower/scene/save", scene={"name": ""}); assert r[0] == "err", r
    await must("homepower/scene/apply", name="Normal"); assert not mgr.away
    dump = await must("homepower/export")
    assert dump["homepower_export"] == 1 and dump["devices"] and not dump["includes_secrets"]
    await must("homepower/scene/delete", name="WS scene")
    res = await must("homepower/import", data=dump)
    assert "WS scene" in mgr.scenes and res["skipped"] == [], res
    r = await call("homepower/import", data={"nope": 1}); assert r[0] == "err", r
    await must("homepower/device/delete", device_id="a")
    assert not mgr.devices
    await coord.async_close(); srv.close(); await srv.wait_closed()
    print("E2E5 OK")

asyncio.run(main())
