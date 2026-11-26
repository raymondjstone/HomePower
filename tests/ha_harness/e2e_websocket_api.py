"""Every websocket command the panel uses, through HA's real websocket_api decorators, with JSON checks."""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

import asyncio, json, sys, tempfile
from types import SimpleNamespace
import conftest  # noqa: F401
import _harness  # noqa: F401 - Python 3.12+ server shutdown (see _harness.py)
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
    _harness.attach(hass, entry, mgr)
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
    # adding (new) a schedule or goal under a name in use is refused; the existing one is kept
    dup = await call("homepower/schedule/save", new=True, schedule={"id": "S1", "start": "01:00", "end": "02:00", "mode": "hold"})
    assert dup[0] == "err" and "already exists" in str(dup) and mgr.schedules["S1"]["mode"] == "charge", dup
    dup = await call("homepower/goal/save", new=True, goal={"id": "G", "target_soc": 50, "by": "06:00"})
    assert dup[0] == "err" and "already exists" in str(dup) and mgr.goals["G"]["target_soc"] == 90, dup
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
    # the battery details sheet and changing a setting, through the real websocket machinery
    dev.hold.update({10064: 0, 60000: 100, 60001: 10, 60002: 20, 60003: 1}); dev.inp[32774], dev.inp[32775] = 0b1111011, 0b1111
    det = await must("homepower/device/details", device_id="a")
    assert det["actual"]["soc"] == 62 and any(r["name"] == "SOC" and r["value"] == "62" and r["unit"] == "%" for r in det["registers"]), det["actual"]
    by = {s["key"]: s for s in det["settings"]}
    assert {"work_mode", "charge_limit", "discharge_limit", "backup_reserve"} <= set(by), sorted(by)
    assert by["work_mode"]["locked"] and not by["charge_limit"]["locked"], "HomePower drives the mode, not the limits"
    # opening the sheet and changing a setting must reuse the battery's one connection (these batteries serve a single client), and leave it online
    _opened = []
    _real_open = asyncio.open_connection
    async def _counting_open(*a, **k):
        _opened.append(a); return await _real_open(*a, **k)
    asyncio.open_connection = _counting_open
    try:
        await must("homepower/device/details", device_id="a"); await must("homepower/device/details", device_id="a")
        await must("homepower/device/setting", device_id="a", key="charge_limit", value=95)
        await must("homepower/device/setting", device_id="a", key="charge_limit", value=100)
        await coord._async_update_data()
    finally:
        asyncio.open_connection = _real_open
    assert not _opened, f"the sheet or a change opened {len(_opened)} new connection(s) to the battery"
    assert coord.devices_view()["a"]["online"], "the battery went offline after the sheet was opened"
    r = await call("homepower/device/details", device_id="nope"); assert r[0] == "err", r
    r = await call("homepower/device/setting", device_id="a", key="work_mode", value=1)
    assert r[0] == "err" and "managing" in r[2], r
    r = await must("homepower/device/setting", device_id="a", key="charge_limit", value=90)
    assert dev.hold[60000] == 90 and not r["pending"] and {s["key"]: s for s in r["settings"]}["charge_limit"]["value"] == 90, r
    r = await call("homepower/device/setting", device_id="a", key="charge_limit", value=70)
    assert r[0] == "err" and "between 80 and 100" in r[2] and dev.hold[60000] == 90, r
    r = await call("homepower/device/setting", device_id="nope", key="charge_limit", value=90); assert r[0] == "err", r
    try:
        await call("homepower/device/setting", device_id="a", key="x" * 41, value=1)          # the schema refuses an over-long key before anything runs
        raise AssertionError("an over-long key was accepted")
    except Exception as err:
        import voluptuous as _vol
        assert isinstance(err, _vol.Invalid), err
    await must("homepower/pause", paused=True)                  # paused: HomePower sends nothing, so the mode is the user's to change
    await must("homepower/device/setting", device_id="a", key="work_mode", value=1)
    assert dev.hold[10064] == 1
    await must("homepower/pause", paused=False)
    # a Solarbank 4 that has gone silent says why, and when it was last read
    from custom_components.homepower.core.models import Actual
    live_actual = coord._snap.devices["a"].actual
    coord._snap.devices["a"].actual = Actual(online=False)
    v = coord.devices_view()["a"]
    assert v["offline_hint"] and "Anker's cloud" in v["why"] and v["last_read_ok"], v
    coord._snap.devices["a"].actual = live_actual
    assert coord.devices_view()["a"]["offline_hint"] is None
    print("details/setting: ok")

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
    # adding (new) a battery whose id is taken is refused: the existing one is not overwritten
    name_before = mgr.devices["a"]["name"]
    dup = await call("homepower/device/save", new=True, device={"device_id": "a", "name": "Other", "kind": "anker_e5000_pro", "connection": conn_cfg})
    assert dup[0] == "err" and "already exists" in str(dup) and mgr.devices["a"]["name"] == name_before, dup
    # live subscription pushes JSON-serialisable events
    handler, schema = cmds["homepower/subscribe"]
    out.pop("r", None); handler(hass, conn, {"id": 999, "type": "homepower/subscribe"} if schema is False else schema({"id": 999, "type": "homepower/subscribe"}))
    await asyncio.sleep(0.3)
    print("subscribe:", out.get("events", [{}])[0].get("event", {}).get("devices", {}).keys())
    assert out["events"] and out["events"][0]["event"]["devices"]["a"]["soc"] == 62
    from homeassistant.helpers.dispatcher import async_dispatcher_send
    from custom_components.homepower.const import SIGNAL_UPDATE
    had = len(out["events"]); async_dispatcher_send(hass, SIGNAL_UPDATE); await asyncio.sleep(0.1)
    assert len(out["events"]) > had, "the live feed did not follow the update signal"
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
    r = await call("homepower/scene/save", new=True, scene={"name": "WS scene", "backup": True})
    assert r[0] == "err" and "already exists" in str(r) and mgr.scenes["WS scene"].get("away") is True, r
    await must("homepower/scene/apply", name="Normal"); assert not mgr.away
    dump = await must("homepower/export")
    assert dump["homepower_export"] == 1 and dump["devices"] and not dump["includes_secrets"]
    await must("homepower/scene/delete", name="WS scene")
    res = await must("homepower/import", data=dump)
    assert "WS scene" in mgr.scenes and res["skipped"] == [], res
    r = await call("homepower/import", data={"nope": 1}); assert r[0] == "err", r
    # ---- the register watch, labels, what-if, readiness, decisions and the saved copies
    rr = await must("homepower/device/registers", device_id="a", addresses=[10014, 10015, 10064], names=True)
    assert rr["values"]["10014"] == 62 and "names" in rr, rr
    n1 = await must("homepower/device/register_notes", device_id="a", address=10080, label="Grid fail sensitivity")
    assert n1["notes"] == {"10080": "Grid fail sensitivity"} and (await must("homepower/device/register_notes", device_id="a"))["notes"] == n1["notes"]
    assert (await must("homepower/device/register_notes", device_id="a", address=10080, label=""))["notes"] == {}
    try:
        await call("homepower/device/registers", device_id="a", addresses=[]); raise SystemExit("an empty register list must be refused")
    except Exception as e:  # noqa: BLE001 - the schema refuses it before the handler runs
        assert "length of value" in str(e), e
    r = await call("homepower/whatif", base={"reserve": 10, "cheap": 0}, alt={"reserve": 10, "cheap": 80}); assert r[0] == "err" and "tariff" in r[2].lower(), r
    await coord.async_refresh()
    v = (await must("homepower/get"))["live"]
    assert v["readiness"] is None or "usable_kwh" in v["readiness"]
    assert "grid_events" in v and "trend" in v and "forecast_reserve" in v
    dg = await must("homepower/diagnostics")
    assert dg["decisions"] and dg["decisions"][0]["name"] and "inputs" in dg["decisions"][0], dg.get("decisions")
    b = await must("homepower/backups/now"); assert b["backups"], b
    name = b["backups"][0]["name"]
    res = await must("homepower/backups/restore", name=name); assert res["skipped"] == [], res
    r = await call("homepower/backups/restore", name="../../etc/passwd"); assert r[0] == "err", r
    coord.apply_settings({"compare_tariffs": "Go; peak=0.30; cheap=0.10; window=00:30-05:30; export=0.05"})
    for i in range(1, 6):
        coord.usage.days[f"2026-11-0{i}"] = {"imp": [0.1] * 48, "exp": [0.0] * 48, "load": [0.4] * 48, "pv": [0.0] * 48}
    wi = await must("homepower/whatif", days=30, base={"reserve": 10, "cheap": 0}, alt={"reserve": 10, "cheap": 100})
    assert wi["days"] == 5 and wi["alt"]["cost"] < wi["base"]["cost"] and wi["tariff"] == "Go", wi
    json.dumps(wi)
    coord.grid_events.observe("a", "A", True, dt_util.now(), 80); coord.grid_events.observe("a", "A", False, dt_util.now(), 70)
    dig = coord._weekly_digest(dt_util.now())
    assert "Last 7 days" in dig and "1 grid outage" in dig, dig
    rd = coord.readiness()
    assert rd is None or (rd["usable_kwh"] > 0 and rd["basis"]), rd
    print("new      : registers, labels, what-if, readiness, decisions, backups OK |", dig[:60])
    await must("homepower/device/delete", device_id="a")
    assert not mgr.devices
    await coord.async_close(); srv.close(); await srv.wait_closed()
    print("E2E5 OK")

asyncio.run(main())
