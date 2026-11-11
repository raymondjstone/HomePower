"""Scenes, backup/restore, heads-up events, learned usage, combined battery and the new sensors, through the real coordinator."""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

import asyncio, tempfile, datetime as dtm
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

    await mgr.save_device({"device_id": "a", "name": "A", "kind": "anker_e5000_pro", "connection": {"host": "127.0.0.1", "port": port}, "control_enabled": False})
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
    await coord.async_close()
    srv.close(); await srv.wait_closed()
    print("E2E7 OK")

asyncio.run(main())
