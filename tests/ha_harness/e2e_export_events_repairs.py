"""Paid export, grid events, Repairs, the hardware report and X1 solar strings, through real Home Assistant and a simulated Anker battery."""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

import asyncio, json, tempfile
from datetime import timedelta
from types import SimpleNamespace
import conftest  # noqa: F401
import _harness  # noqa: F401 - Python 3.12+ server shutdown (see _harness.py)
from test_drivers import FakeAnker
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from custom_components.homepower.manager import BATTERY_SENSOR_SUFFIXES, HomePowerManager
from custom_components.homepower.coordinator import HomePowerCoordinator
from custom_components.homepower import websocket_api as H


async def main():
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/London"))
    tmp = tempfile.mkdtemp(); hass = HomeAssistant(tmp); hass.config.config_dir = tmp
    await ir.async_load(hass)
    bat, x1 = FakeAnker(), FakeAnker()
    x1.inp[10167], x1.inp[10168] = 3652, 512            # PV1: 365.2 V, 5.12 A
    x1.inp[10169], x1.inp[10170] = 3600, 500            # PV2: 360.0 V, 5.00 A
    s1 = await asyncio.start_server(bat.handle, "127.0.0.1", 0); p1 = s1.sockets[0].getsockname()[1]
    s2 = await asyncio.start_server(x1.handle, "127.0.0.1", 0); p2 = s2.sockets[0].getsockname()[1]
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
    cmds = hass.data["websocket_api"]
    out, n = {}, [0]

    class Conn:
        user = SimpleNamespace(is_admin=True)
        def __init__(self): self.subscriptions = {}
        def send_result(self, i, result=None): out["r"] = ("ok", json.loads(json.dumps(result)))
        def send_error(self, i, code, msg): out["r"] = ("err", code, msg)
        def send_message(self, m): pass
        def async_handle_exception(self, msg, err): out["r"] = ("exc", repr(err))
    conn = Conn()

    async def must(type_, **kw):
        n[0] += 1
        msg = {"id": n[0], "type": type_, **kw}
        handler, schema = cmds[type_]
        out.pop("r", None)
        handler(hass, conn, msg if schema is False else schema(msg))
        for _ in range(300):
            if "r" in out: break
            await asyncio.sleep(0.05)
        r = out.get("r")
        assert r and r[0] == "ok", (type_, r)
        return r[1]

    await must("homepower/device/save", device={"device_id": "a", "name": "A", "kind": "anker_e5000_pro", "connection": {"host": "127.0.0.1", "port": p1}})
    await must("homepower/device/control", device_id="a", enabled=True)
    await must("homepower/device/save", device={"device_id": "x", "name": "X", "kind": "anker_x1", "connection": {"host": "127.0.0.1", "port": p2}})
    mgr.tests["a"] = {"ok": True, "at": "2026-01-01T00:00:00", "steps": [{"name": "sent charge 100 W", "ok": True, "detail": "host 127.0.0.1"}]}
    mgr.confirmed.add("a")
    # Fixed tariff: 30p, 7p at night (buy back at 7p); export pays 30p from a plain export price sensor
    hass.states.async_set("sensor.export_price", "30")
    cheap_from = dt_util.now() + timedelta(hours=6)            # the cheap window is always ahead, whatever time this runs
    win = (cheap_from.strftime("%H:00"), (cheap_from + timedelta(hours=2)).strftime("%H:00"))
    await must("homepower/settings/save", settings={"price_peak": 30, "price_offpeak": 7, "offpeak_start": win[0], "offpeak_end": win[1],
                                                     "export_rates_entity": "sensor.export_price", "export_discharge": True, "export_keep_pct": 30})
    await coord.async_refresh()
    st = coord._snap.devices["a"]
    assert st.desired and st.desired.mode.value == "discharge" and st.reason.startswith("paid export"), (st.desired, st.reason)
    live = json.loads(json.dumps(coord.view()))
    assert live["export"]["pays"] and live["export"]["price_now"] == 0.3 and live["export"]["buyback"] == 0.07, live["export"]
    print("export   :", st.reason)

    # wear cost too high: paid export stops, and the battery is handed back
    await must("homepower/settings/save", settings={"wear_cost": 30})
    await coord.async_refresh()
    assert not (coord._snap.devices["a"].reason or "").startswith("paid export"), coord._snap.devices["a"].reason
    await must("homepower/settings/save", settings={"wear_cost": ""})
    print("wear     : stops paid export")

    # a saving session: grid event discharges, then ends
    hass.states.async_set("binary_sensor.saving_session", "on")
    await must("homepower/settings/save", settings={"event_entity": "binary_sensor.saving_session", "export_discharge": False})
    await coord.async_refresh()
    st = coord._snap.devices["a"]
    assert st.reason == "grid event" and st.desired.mode.value == "discharge", st.reason
    assert coord.view()["grid_event"] is True
    hass.states.async_set("binary_sensor.saving_session", "off")
    await coord.async_refresh()
    assert coord._snap.devices["a"].reason != "grid event" and coord.view()["grid_event"] is False
    print("event    : discharged during the session, stopped after")

    # X1 solar strings: live values in the view and the details sheet
    pv = coord.view()["devices"]["x"]["pv"]
    assert pv["strings"][0]["volts"] == 365.2 and pv["strings"][0]["watts"] == round(365.2 * 5.12), pv
    assert pv["strings"][0]["state"] == "learning" and pv["strings"][2]["state"] == "unused"
    det = await must("homepower/device/details", device_id="x")
    assert det["pv"]["strings"][1]["volts"] == 360.0
    assert coord.view()["devices"]["a"]["pv"] is None
    assert "pv1_voltage" in BATTERY_SENSOR_SUFFIXES and "pv_total" in BATTERY_SENSOR_SUFFIXES and "solar" in BATTERY_SENSOR_SUFFIXES
    print("strings  :", [(s["string"], s.get("watts"), s["state"]) for s in pv["strings"][:3]])

    # Repairs: a lasting problem (the alert path's raise and clear) is filed under Repairs, a heads-up is not
    def mine():
        return {iid: i for (dom, iid), i in ir.async_get(hass).issues.items() if dom == "homepower"}
    await coord._notify("offline:a", "A is offline: HomePower cannot read it.", True)
    await coord._notify("pv:x:2", "X: PV2 has given almost nothing for 31 min while the other strings produce.", True)
    await coord._notify("event_full", "The home battery is full (96%).", True)
    got = mine()
    assert sorted(k.split("_")[0] for k in got) == ["offline", "pv"], got
    issue = next(i for k, i in got.items() if k.startswith("offline_a_"))
    assert issue.translation_key == "alert" and issue.translation_placeholders == {"title": "battery offline", "message": "A is offline: HomePower cannot read it."}
    assert issue.severity == ir.IssueSeverity.ERROR and not issue.is_fixable
    await coord._notify("offline:a", "A is offline: HomePower cannot read it.", False)
    assert [k.split("_")[0] for k in mine()] == ["pv"]
    await coord._notify("pv:x:2", "", False)
    assert mine() == {}
    print("repairs  : raised and cleared; heads-ups stay notifications")

    # a new run starts with no stale Repairs
    ir.async_create_issue(hass, "homepower", "stale_x", is_fixable=False, severity=ir.IssueSeverity.WARNING, translation_key="alert",
                          translation_placeholders={"title": "t", "message": "m"})
    coord2 = HomePowerCoordinator(hass, entry, mgr, *mgr.build())
    assert not [i for (d, i) in ir.async_get(hass).issues if d == "homepower"]
    await coord2.async_close() if hasattr(coord2, "async_close") else None
    coord2.stop_listening()

    # hardware report: JSON-safe, no names, addresses or serials
    rep = await must("homepower/hardware_report")
    text = json.dumps(rep)
    assert "127.0.0.1" not in text and '"A"' not in text and rep["batteries"][0]["battery"] == "battery 1", text[:400]
    b = rep["batteries"][0]
    assert b["kind"] == "anker_e5000_pro" and b["control_test"]["passed"] is True and b["limits"]["sets_watts"] is True
    print("report   :", {k: b[k] for k in ("kind", "commands_watched", "followed_plan_pct", "proven")})

    # ---- the day plan through real rate sensors: export 15p now but 30p in two hours, so the energy is kept for then
    now = dt_util.now()
    lo = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)

    def table(prices):
        return [{"start": (lo + timedelta(minutes=30 * i)).isoformat(), "end": (lo + timedelta(minutes=30 * (i + 1))).isoformat(),
                 "value_inc_vat": p} for i, p in enumerate(prices)]
    hass.states.async_set("sensor.agile", "20", {"rates": table([20, 20, 20, 20, 35, 35, 20, 20, 7, 7, 20, 20] * 4)})
    hass.states.async_set("sensor.export_price", "15", {"rates": table([15, 15, 15, 15, 30, 30, 15, 15, 15, 15, 15, 15] * 4)})
    await must("homepower/settings/save", settings={"rates_entity": "sensor.agile", "price_peak": "", "price_offpeak": "", "offpeak_start": "",
                                                     "offpeak_end": "", "offpeak_auto": False, "export_discharge": True, "hold_for_dearer": True,
                                                     "event_entity": "", "export_keep_pct": 30, "export_rates_entity": "sensor.export_price"})
    # (this harness's settings save keeps only what each call sends, so everything needed is sent together)
    await coord.async_refresh()
    st = coord._snap.devices["a"]
    plan = coord.day_plan
    assert plan is not None and plan.export_now_w == 0 and plan.hold_now, plan.as_dict()
    assert st.desired.mode.value == "hold" and "(planned)" in st.reason, (st.desired, st.reason)
    pv2 = await must("homepower/preview")
    peak = [s for s in pv2["day_plan"]["slots"] if s["export_wh"] > 0]
    assert peak and all(s["export"] == 0.3 for s in peak), pv2["day_plan"]
    print("day plan :", st.reason, "| export planned at", [s["start"][11:16] for s in peak])

    # ---- charging up before a saving session on a calendar (3 h from now)
    start = lo + timedelta(hours=3)
    hass.states.async_set("calendar.saving", "off", {"message": "Saving session", "start_time": start.strftime("%Y-%m-%d %H:%M:%S"),
                                                      "end_time": (start + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")})
    await must("homepower/settings/save", settings={"event_entity": "calendar.saving", "event_charge_pct": 95, "export_discharge": False, "hold_for_dearer": False,
                                                     "rates_entity": "sensor.agile", "offpeak_auto": False, "export_rates_entity": "sensor.export_price"})
    await coord.async_refresh()
    assert any("before grid event" in n for n in coord.last_result.notes) or coord.last_result.allocs, coord.last_result.notes
    assert all(a.end <= start.astimezone(a.end.tzinfo) for a in coord.last_result.allocs)
    pv3 = await must("homepower/preview")
    assert len(pv3["events"]) == 1 and pv3["events"][0]["start"].startswith(start.strftime("%Y-%m-%dT%H:%M")), pv3["events"]
    print("event    : charge planned before", start.strftime("%H:%M"), "in", len(coord.last_result.allocs), "half-hours")

    # ---- money totals: export earnings at the export price, kept as a 'total' Home Assistant keeps statistics of
    before = coord.meter.totals.get("money:export", 0.0)
    coord._account_savings(dt_util.now(), {}, -1000.0, coord._rates(), {}, 2.0)
    assert abs(coord.meter.totals["money:export"] - before - 2.0 * 0.15) < 1e-9, coord.meter.totals
    from custom_components.homepower.sensor import MoneyTotal
    m = MoneyTotal(coord, entry, "money:export", "Export earnings", "mdi:cash-plus")
    assert m.native_value == round(coord.meter.totals["money:export"], 4) and m.native_unit_of_measurement == hass.config.currency
    assert m.state_class == "total" and m.device_class == "monetary"
    print("money    :", m.native_value, m.native_unit_of_measurement)

    # ---- the Configure form for prices, export and events
    from custom_components.homepower.options_flow import HomePowerOptionsFlow
    f = HomePowerOptionsFlow()
    f.hass, f.flow_id, f.handler = hass, "x", "e1"
    hass.config_entries.async_get_known_entry = lambda i: entry
    form = await f.async_step_money()
    assert form["type"] == "form" and "event_entity" in str(form["data_schema"].schema)
    res = await f.async_step_money({"export_discharge": True, "wear_cost": 4.5, "event_entity": ["calendar.saving", "binary_sensor.saving_session"],
                                    "event_charge_pct": 90, "export_keep_pct": 20})
    assert res["type"] == "create_entry" and res["data"]["event_entity"] == "calendar.saving, binary_sensor.saving_session", res
    assert res["data"]["wear_cost"] == 4.5 and res["data"]["hold_for_dearer"] is False and res["data"]["export_price"] is None
    bad = await f.async_step_money({"event_charge_pct": 150})
    assert bad["type"] == "abort", bad
    print("configure: prices, export and events form saves")

    # ---- a fix from Repairs: 'not following commands' -> switch the battery to monitor-only
    assert coord.alerts.observe("unconfirmed:a", True, 0.0) == "raised"
    await coord._notify("unconfirmed:a", "A is not following HomePower's commands.", True)
    iss = next(i for (d, k), i in ir.async_get(hass).issues.items() if d == "homepower" and k.startswith("unconfirmed_a_"))
    assert iss.is_fixable and iss.data["device_id"] == "a" and iss.translation_key == "alert_fix_battery", iss
    from custom_components.homepower import repairs
    flow = await repairs.async_create_fix_flow(hass, iss.issue_id, iss.data)
    flow.hass = hass
    menu = await flow.async_step_init()
    assert menu["type"] == "menu" and menu["menu_options"] == ["test", "monitor"]
    done = await flow.async_step_monitor()
    assert done["type"] == "create_entry" and mgr.devices["a"]["control_enabled"] is False
    # Home Assistant removes the issue when the fix finishes: the alert is forgotten too, so it is raised again if it carries on
    assert "unconfirmed:a" not in mgr.coordinator.alerts.active(), mgr.coordinator.alerts.active()
    assert mgr.coordinator.alerts.observe("unconfirmed:a", True, 1.0) == "raised"
    tflow = await repairs.async_create_fix_flow(hass, "t", {"fix": "tariff", "message": "m"})
    tflow.hass = hass
    entry_opts = {}
    hass.config_entries.async_update_entry = lambda e, options=None, **kw: entry_opts.update(options or {}) or True
    assert (await tflow.async_step_init())["type"] == "form"
    assert (await tflow.async_step_init({}))["type"] == "create_entry" and entry_opts["offpeak_start"] == "" and entry_opts["offpeak_auto"] is True
    print("repairs  : fix flows switch a battery to monitor-only and reset typed cheap hours")

    # string history is saved and loaded again
    await mgr.save_runtime()
    mgr2 = HomePowerManager(hass); await mgr2.async_load()
    assert "x" in mgr2.pv_health_data and mgr2.pv_health_data["x"]["used"][:3] == [True, True, False], mgr2.pv_health_data
    print("saved    : string health survives a restart")

    coord.stop_listening()
    await coord.async_close()
    s1.close(); s2.close()
    await hass.async_stop(force=True)
    print("OK")


asyncio.run(main())
