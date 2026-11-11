"""Scenes, backup/restore, heads-up events, learned usage, combined battery and the new sensors, through the real coordinator."""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

import asyncio, json, tempfile, datetime as dtm
from types import SimpleNamespace
import conftest  # noqa: F401
from test_drivers import FakeAnker
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util
from custom_components.homepower.manager import HomePowerManager
from custom_components.homepower.coordinator import HomePowerCoordinator
from custom_components.homepower.core.serde import ValidationError
import custom_components.homepower.sensor as S


async def main():
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/London"))
    tmp = tempfile.mkdtemp()
    dev = FakeAnker(); srv = await asyncio.start_server(dev.handle, "127.0.0.1", 0); port = srv.sockets[0].getsockname()[1]
    hass = HomeAssistant(tmp); hass.config.config_dir = tmp
    entry = ConfigEntry(data={}, disabled_by=None, discovery_keys={}, domain="homepower", entry_id="e1", minor_version=1, options={},
                        pref_disable_new_entities=False, pref_disable_polling=False, source="user", subentries_data=[],
                        title="HomePower", unique_id="homepower", version=1)
    mgr = HomePowerManager(hass); await mgr.async_load()
    coord = HomePowerCoordinator(hass, entry, mgr, *mgr.build()); mgr.coordinator = coord

    def update_entry(e, options=None, **kw):
        object.__setattr__(e, "options", dict(options))   # ConfigEntry forbids plain assignment
        coord.apply_settings({**e.data, **e.options})
    hass.config_entries = SimpleNamespace(async_get_known_entry=lambda i: entry, async_update_entry=update_entry)
    sent = []
    hass.services.async_register("persistent_notification", "create", lambda call: sent.append(("create", call.data["notification_id"])))
    hass.services.async_register("persistent_notification", "dismiss", lambda call: sent.append(("dismiss", call.data["notification_id"])))

    await mgr.save_device({"device_id": "a", "name": "A", "kind": "anker_e5000_pro", "grid_meter": "yes", "connection": {"host": "127.0.0.1", "port": port}, "control_enabled": False})
    await coord._async_update_data()

    # --- combined battery + daily + health in the live view
    v = coord.view()
    c = v["combined"]
    print("combined :", c)
    assert c["online"] == 1 and c["soc"] is not None and c["capacity_kwh"] and "self_consumption" in v["daily"] and "a" in v["health"]
    # --- the new sensors
    hb = S.HomeBattery(coord, entry)
    assert hb.native_value == c["soc"] and hb.extra_state_attributes["homepower"] is True and hb.extra_state_attributes["batteries"][0]["name"] == "A"
    assert S.DailyShare(coord, entry, "self_consumption", "x").native_value is None     # no solar yet: unknown, not 0

    # --- scenes: defaults exist, apply sets flags, a scene with limits changes settings
    assert {"Normal", "Holiday", "Storm coming", "Hands off"} <= set(mgr.scenes)
    await mgr.apply_scene("Storm coming"); assert mgr.backup and not mgr.away and not mgr.paused
    await mgr.apply_scene("Holiday"); assert mgr.away and not mgr.backup
    await mgr.save_scene({"name": "EV night", "away": False, "settings": {"import_limit_w": 4500, "peak_shaving": True}})
    await mgr.apply_scene("EV night")
    assert coord.settings.engine.import_limit_w == 4500 and coord.settings.engine.peak_shaving is True and not mgr.away
    try:
        await mgr.apply_scene("nope"); assert False
    except ValidationError:
        pass
    print("scenes   : ok")

    # --- backup / restore round trip (no secrets in the file by default)
    await mgr.save_schedule({"id": "N", "start": "01:00", "end": "02:00", "mode": "charge", "power_w": 1000})
    await mgr.save_goal({"id": "G", "target_soc": 80, "by": "07:00", "auto_target": True})
    dump = mgr.export_all({"import_limit_w": 4500}, include_secrets=False)
    import json; json.dumps(dump)                                    # must be JSON-safe for the websocket
    assert dump["homepower_export"] == 1 and any(s["id"] == "N" for s in dump["schedules"]) and any(g["auto_target"] for g in dump["goals"])
    await mgr.delete_schedule("N"); await mgr.delete_goal("G"); await mgr.delete_scene("EV night")
    dump["schedules"].append({"id": "", "mode": "charge"})          # a bad row must be reported, not break the rest
    res = await mgr.import_all(dump, entry)
    print("restore  :", res)
    assert res["added"]["schedules"] == 1 and res["added"]["goals"] == 1 and "EV night" in mgr.scenes and len(res["skipped"]) == 1
    assert "N" in mgr.schedules and mgr.goals["G"]["auto_target"] is True
    try:
        await mgr.import_all({"hello": 1}, entry); assert False
    except ValidationError:
        pass

    # --- heads-up events: low battery fires once and clears
    soc = coord.view()["combined"]["soc"]
    coord.apply_settings({"notify_low_pct": int(soc) + 5, "notify_full": False})
    sent.clear()
    await coord._async_update_data(); await coord._async_update_data(); await asyncio.sleep(0.2)
    print("events   :", sent)
    assert sent.count(("create", "homepower_alert_event_low")) == 1, "raised exactly once"
    coord.apply_settings({"notify_low_pct": max(1, int(soc) - 20)})
    await coord._async_update_data(); await asyncio.sleep(0.2)
    assert ("dismiss", "homepower_alert_event_low") in sent

    # --- learned usage feeds an auto goal (until learned it falls back to the target)
    now = dt_util.now()
    for back in (7, 14, 21):                       # the same weekday on three earlier weeks
        for hh in range(48):
            t = (now - dtm.timedelta(days=back)).replace(hour=hh // 2, minute=(hh % 2) * 30, second=0)
            for k in range(8):
                coord.profile.record(t + dtm.timedelta(seconds=10 * k), 400.0)
    assert coord.profile.known(now)
    coord.apply_settings({"price_peak": 30.0, "price_offpeak": 7.5, "offpeak_start": "23:30", "offpeak_end": "07:30", "notify_low_pct": 0})
    await mgr.delete_schedule("N")
    await mgr.save_goal({"id": "Auto", "target_soc": 100, "by": (now + dtm.timedelta(hours=2)).strftime("%H:%M"), "auto_target": True})
    await coord._async_update_data()
    notes = coord.last_result.notes
    print("auto goal:", [n for n in notes if "auto" in n or "learning" in n])
    assert any("auto target" in n or "still learning" in n for n in notes)
    # --- where the grid reading comes from: the battery's own meter wins automatically; a slow sensor is flagged and not steered by
    await coord._async_update_data()
    assert coord.grid_source == "A (its own meter reading)" and coord.grid_age_s is None, coord.grid_source
    assert {i["key"]: i for i in coord.view()["setup"]}["grid_fresh"]["ok"]
    hass.states.async_set("sensor.my_grid", "450", {"unit_of_measurement": "W"})
    coord.apply_settings({"grid_import_entity": "sensor.my_grid"})
    await coord._async_update_data()
    assert coord.last_grid == 450 and coord.grid_source == "sensor.my_grid" and coord.grid_age_s < 60      # the user's choice wins
    coord._entity_age = lambda ent: 900.0                                                                   # ...but it has stopped updating
    await coord._async_update_data()
    item = {i["key"]: i for i in coord.view()["setup"]}["grid_fresh"]
    print("grid     :", coord.grid_source, coord.grid_age_s, "|", item["detail"][:60])
    assert not item["ok"] and "15 min old" in item["detail"]
    del coord._entity_age
    # the order is the user's: battery first beats the chosen sensor; a source left out is never used
    coord.apply_settings({"grid_import_entity": "sensor.my_grid", "grid_order": "battery,sensor"})
    await coord._async_update_data()
    assert coord.grid_source == "A (its own meter reading)", coord.grid_source
    coord.apply_settings({"grid_import_entity": "sensor.my_grid", "grid_order": "glow,cloud"})
    await coord._async_update_data()
    assert coord.last_grid is None and coord.grid_source == ""
    # a battery that only measures itself never supplies the house reading (the default for this plug-in model)
    await mgr.save_device({**mgr.devices["a"], "grid_meter": "auto"})
    coord.apply_settings({})
    await coord._async_update_data()
    print("grid auto:", repr(coord.grid_source), coord.last_grid)
    assert coord.last_grid is None and not {i["key"]: i for i in coord.view()["setup"]}["grid"]["ok"]
    await mgr.save_device({**mgr.devices["a"], "grid_meter": "yes"})
    coord.apply_settings({})

    # --- setup check, price slots, several cheap windows
    coord.apply_settings({"price_peak": 30.0, "price_offpeak": 7.5, "offpeak_start": "04:00", "offpeak_end": "07:00",
                          "offpeak_extra": "13:00-16:00, 22:00-00:00"})
    await coord._async_update_data()
    chk = {i["key"]: i for i in coord.view()["setup"]}
    print("setup    :", {k: v["ok"] for k, v in chk.items()})
    assert chk["batteries"]["ok"] and chk["reading:a"]["ok"] and chk["prices"]["ok"] and chk["tariff"]["ok"] and not chk["control:a"]["ok"]
    pv = coord.preview_view(24)
    cheap = sum(1 for x in pv["prices"] if x["cheap"])
    assert len(pv["prices"]) == 48 and cheap == 16, (len(pv["prices"]), cheap)          # 3 h + 3 h + 2 h of half-hours
    json.dumps(pv)

    # --- reserve role is saved and reaches the engine; control test result is remembered
    await mgr.save_device({**mgr.devices["a"], "role": "reserve", "control_enabled": True})
    assert mgr.devices["a"]["role"] == "reserve" and coord.adapters[0].config.extra["role"] == "reserve"
    try:
        await mgr.save_device({**mgr.devices["a"], "role": "boss"}); assert False
    except ValidationError:
        pass
    mgr.tests["a"] = {"ok": True, "at": "x"}
    assert {i["key"]: i for i in coord.setup_check()}["tested:a"]["ok"]

    # --- save for peak: in a cheap window the battery is held, outside it is handed back
    now = dt_util.now()
    hhmm = lambda d: d.strftime("%H:%M")
    await mgr.save_device({**mgr.devices["a"], "role": "normal"})
    await mgr.delete_goal("Auto"); await mgr.delete_goal("G")
    coord.apply_settings({"price_peak": 30.0, "price_offpeak": 7.5, "save_for_peak": True,
                          "offpeak_start": hhmm(now - dtm.timedelta(hours=1)), "offpeak_end": hhmm(now + dtm.timedelta(hours=1))})
    dev.inp[10002], dev.inp[10003] = 0, 0          # no solar
    coord._snap.devices["a"].last_applied = None
    await coord._async_update_data()
    v = coord.view()["devices"]["a"]
    print("save peak:", v["desired_mode"], "|", v["reason"], "| mode reg", dev.hold[10064])
    assert v["desired_mode"] == "hold" and "peak" in v["reason"] and dev.hold[10064] == 3
    coord.apply_settings({"price_peak": 30.0, "price_offpeak": 7.5, "save_for_peak": True,
                          "offpeak_start": hhmm(now + dtm.timedelta(hours=2)), "offpeak_end": hhmm(now + dtm.timedelta(hours=3))})
    coord._snap.devices["a"].last_applied = None
    await coord._async_update_data()
    assert dev.hold[10064] == 0 and not mgr.forced, "handed back when the cheap period is over"

    # --- solar-surplus diverter: full battery + export -> the chosen switch goes on, then off when importing
    calls = []
    hass.services.async_register("homeassistant", "turn_on", lambda call: calls.append(("on", call.data["entity_id"])))
    hass.services.async_register("homeassistant", "turn_off", lambda call: calls.append(("off", call.data["entity_id"])))
    coord.apply_settings({"divert_entity": "switch.immersion", "divert_export_w": 500, "divert_min_soc": 50})
    acts = {"a": coord._snap.devices["a"].actual}
    await coord._divert(1000.0, acts, -900.0); await coord._divert(1130.0, acts, -900.0)
    await coord._divert(1200.0, acts, 400.0); await coord._divert(1330.0, acts, 400.0)
    await asyncio.sleep(0.2)
    print("divert   :", calls)
    assert calls == [("on", "switch.immersion"), ("off", "switch.immersion")]
    await coord._divert(2000.0, acts, -900.0); await coord._divert(2130.0, acts, -900.0)
    assert mgr.divert_on == "switch.immersion", "remembered, so a restart can still switch it off"
    c3 = HomePowerCoordinator(hass, entry, mgr, *mgr.build())                    # what a restart would build
    assert c3.diverter.on and c3._divert_entity == "switch.immersion"
    mgr.paused = True                                                             # pausing lets go of the load
    await coord._divert(2200.0, acts, -900.0); await coord._divert(2330.0, acts, -900.0); await asyncio.sleep(0.2)
    assert calls[-1] == ("off", "switch.immersion") and mgr.divert_on == ""
    mgr.paused = False
    await coord._divert(3000.0, acts, -900.0); await coord._divert(3130.0, acts, -900.0)
    coord.apply_settings({"divert_entity": ""})                                   # cleared while on: the load is released
    await coord._divert(2140.0, acts, -900.0); await asyncio.sleep(0.2)
    assert calls[-1] == ("off", "switch.immersion") and not coord.diverter.on

    # --- daily summary fires once, and heads-up state survives a restart
    sent.clear()
    coord.apply_settings({"daily_summary_time": hhmm(dt_util.now() - dtm.timedelta(minutes=2))})
    await coord._async_update_data(); await coord._async_update_data(); await asyncio.sleep(0.2)
    summ = [x for x in sent if x[1].startswith("homepower_alert_event_summary_")]
    print("summary  :", summ, "|", coord._daily_summary(dt_util.now(), coord._combined(acts))[:60])
    assert len(summ) == 1
    await coord.async_close()
    mgr2 = HomePowerManager(hass); await mgr2.async_load()
    assert any(k.startswith("summary_") for k in mgr2.event_state) and mgr2.tests["a"]["ok"]
    coord2 = HomePowerCoordinator(hass, entry, mgr2, *mgr2.build()); mgr2.coordinator = coord2
    sent.clear()
    await coord2._async_update_data(); await asyncio.sleep(0.2)
    assert not [x for x in sent if "summary" in x[1]], "not sent again after a restart"
    await coord2.async_close()
    srv.close(); await srv.wait_closed()
    print("E2E7 OK")

asyncio.run(main())
