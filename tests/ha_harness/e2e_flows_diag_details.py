"""The set-up flow, the diagnostics download, the Repairs fixes' refusals and the one-battery-up-close calls, in real Home Assistant."""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]
import asyncio, json, tempfile
from types import SimpleNamespace
import conftest  # noqa: F401
import _harness  # noqa: F401 - Python 3.12+ server shutdown (see _harness.py)
from test_drivers import FakeAnker
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.config_entries import ConfigEntry
from custom_components.homepower import config_flow, diagnostics, repairs, services, switch
from custom_components.homepower.core import device_settings
from custom_components.homepower.core.serde import ValidationError
from custom_components.homepower.manager import HomePowerManager
from custom_components.homepower.coordinator import HomePowerCoordinator
from custom_components.homepower.options_flow import HomePowerOptionsFlow


async def refused(coro, words):
    try:
        await coro
    except ValidationError as err:
        assert words in str(err), (words, str(err))
    else:
        raise AssertionError("not refused: " + words)


async def set_up_flow(hass):
    existing = []
    hass.config_entries = SimpleNamespace(
        flow=SimpleNamespace(async_progress_by_handler=lambda *a, **kw: [], async_abort=lambda i: None),
        async_entry_for_domain_unique_id=lambda d, u: existing[0] if existing else None,
        async_entries=lambda d=None, include_ignore=True, include_disabled=True: list(existing))

    def flow():
        f = config_flow.HomePowerConfigFlow()
        f.hass, f.flow_id, f.handler, f.context = hass, "f", "homepower", {"source": "user"}
        return f

    r = await flow().async_step_user()
    assert r["type"] == "form" and r["step_id"] == "user", r
    r = await flow().async_step_user({})
    assert r["type"] == "create_entry" and r["title"] == "HomePower" and r["data"] == {}, r
    assert isinstance(config_flow.HomePowerConfigFlow.async_get_options_flow(None), HomePowerOptionsFlow)
    existing.append(SimpleNamespace(entry_id="e1", source="user", unique_id="homepower", data={}, options={}))
    r = await flow().async_step_user()          # one HomePower: checked in the flow (not the manifest, so batteries can be discovered)
    assert r["type"] == "abort" and r["reason"] == "single_instance_allowed", r
    print("set up   : one entry only")


async def main():
    tmp = tempfile.mkdtemp(); hass = HomeAssistant(tmp); hass.config.config_dir = tmp
    await set_up_flow(hass)

    dev = FakeAnker(); srv = await asyncio.start_server(dev.handle, "127.0.0.1", 0); port = srv.sockets[0].getsockname()[1]
    entry = ConfigEntry(data={}, disabled_by=None, discovery_keys={}, domain="homepower", entry_id="e1", minor_version=1,
                        options={"notify_service": "notify.somebody"}, pref_disable_new_entities=False, pref_disable_polling=False,
                        source="user", subentries_data=[], title="HomePower", unique_id="homepower", version=1)
    assert await diagnostics.async_get_config_entry_diagnostics(hass, entry) == {"loaded": False}
    for fix in ({"fix": "battery", "device_id": "a"}, {"fix": "tariff"}):        # nothing running yet: the fixes say so
        hass.config_entries = SimpleNamespace(async_entries=lambda d=None: [])
        f = await repairs.async_create_fix_flow(hass, "i", fix)
        f.hass, f.flow_id, f.handler = hass, "r", "homepower"
        first = await f.async_step_init()
        assert first["type"] in ("menu", "form"), first
        steps = (f.async_step_test, f.async_step_monitor) if fix["fix"] == "battery" else (lambda: f.async_step_init({}),)
        for step in steps:
            r = await step()
            assert r["type"] == "abort" and r["reason"] == "not_ready", r

    mgr = HomePowerManager(hass); await mgr.async_load()
    coord = HomePowerCoordinator(hass, entry, mgr, *mgr.build()); mgr.coordinator = coord
    _harness.attach(hass, entry, mgr)
    hass.config_entries = SimpleNamespace(async_get_known_entry=lambda i: entry, async_entries=lambda d=None: [entry],
                                          async_update_entry=lambda e, options=None, **kw: True)
    await mgr.save_device({"device_id": "a", "name": "Anker", "kind": "anker_e5000_pro", "connection": {"host": "127.0.0.1", "port": port},
                           "control_enabled": True})
    await coord.async_refresh()

    # ---- the diagnostics download has no address in it
    diag = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(diag, default=str)
    assert diag["devices"] and "127.0.0.1" not in text and "notify.somebody" not in text, text[:500]
    print("download : redacted,", len(text), "characters")

    # ---- registers: read-only tools, and their refusals
    out = await mgr.scan_registers("a")
    assert out["order"] and out["names"] and out["input"]
    got = await mgr.read_registers("a", [10014], names=True)
    assert got["values"]["10014"] == 62 and got["names"]
    await refused(mgr.scan_registers("nope"), "does not support a register scan")
    await refused(mgr.read_registers("nope", [1]), "does not support reading registers")
    driver = mgr._adapters["a"][1].driver
    real_scan, real_read = driver.scan, driver.read_registers

    async def broken(*a, **kw):
        raise OSError("gone")

    driver.scan, driver.read_registers = broken, broken
    await refused(mgr.scan_registers("a"), "could not scan")
    await refused(mgr.read_registers("a", [1]), "could not read")
    details = await mgr.device_details("a")
    assert "could not read the registers" in details["error"]
    driver.scan, driver.read_registers = real_scan, real_read
    details = await mgr.device_details("a")
    assert details["registers"] and details["actual"], details.keys()
    await refused(mgr.device_details("nope"), "unknown battery")

    # ---- the user's own register names
    assert (await mgr.register_note("a", 10014, "  charge level  "))["notes"] == {"10014": "charge level"}
    assert (await mgr.register_note("a", 10014, ""))["notes"] == {}
    mgr.register_notes["a"] = {str(i): "x" for i in range(500)}
    await refused(mgr.register_note("a", 600, "one too many"), "500 register labels")
    await refused(mgr.register_note("nope"), "unknown battery")

    # ---- changing a setting: refusals, and a change that went through
    await refused(mgr.set_device_setting("nope", "k", 1), "unknown battery")
    real_apply, real_settings = device_settings.apply_change, device_settings.read_settings
    result = {"raise": ValueError("out of range")}

    async def apply(drv, key, value, controlled):
        if result.get("raise"):
            raise result["raise"]
        return result["family"]

    async def settings(drv, controlled):
        if result.get("no_settings"):
            raise OSError("no answer")
        return "anker", [{"key": "k"}], None

    device_settings.apply_change, device_settings.read_settings = apply, settings
    await refused(mgr.set_device_setting("a", "k", 1), "out of range")
    result["raise"] = OSError("said no")
    await refused(mgr.set_device_setting("a", "k", 1), "did not accept it")
    result.update({"raise": None, "family": "anker"})
    assert await mgr.set_device_setting("a", "k", 1) == {"settings": [{"key": "k"}], "pending": False}
    result.update({"family": "ecoflow", "no_settings": True})
    driver._cache, driver._pushed = "old", 1.0
    assert await mgr.set_device_setting("a", "k", 1) == {"settings": [], "pending": True} and driver._cache is None
    details = await mgr.device_details("a")
    assert "could not read the settings" in details["settings_error"]
    result["no_settings"] = False

    # an EcoFlow-style battery: what it last sent is shown, without anything private
    driver.last_quota = {"bmsBattSoc": 55, "serialNumber": "SN123456", "wifiSsid": "secret-net", "token": "secret"}
    details = await mgr.device_details("a")
    assert details["raw"]["bmsBattSoc"] == 55 and "secret" not in json.dumps(details["raw"]), details["raw"]
    del driver.last_quota

    # a battery that is not answering is not sent a burst of reads, and nothing is changed
    coord._snap.devices["a"].actual = None
    details = await mgr.device_details("a")
    assert "not answering" in details["error"] and details["settings"] == []
    await refused(mgr.set_device_setting("a", "k", 1), "not answering")
    device_settings.apply_change, device_settings.read_settings = real_apply, real_settings
    print("details  : registers, labels, settings and their refusals")

    await coord.async_refresh()

    # ---- actions, as an automation calls them and as a user who is not an administrator would
    services.async_register(hass)

    async def call(name, data=None, **kw):
        return await hass.services.async_call("homepower", name, data or {}, blocking=True, **kw)

    async def rejects(name, data, error=ServiceValidationError, **kw):
        try:
            await call(name, data, **kw)
        except error:
            return
        raise AssertionError(f"{name} {data} was accepted")

    await call("pause")
    assert mgr.paused
    await call("resume")
    assert not mgr.paused
    for name in ("backup", "away"):
        await call(name, {"enabled": True})
        assert getattr(mgr, name)
        await call(name, {"enabled": False})
    await call("override", {"mode": "hold", "minutes": 5, "device_ids": "a"})
    assert coord.view()["devices"]["a"]["override"]
    await call("clear_override", {"device_ids": ["a"]})
    assert not coord.view()["devices"]["a"]["override"]
    await rejects("override", {"mode": "hold", "device_ids": ["typo"]})
    await rejects("clear_override", {"device_ids": ["typo"]})
    await rejects("apply_scene", {"name": "No such scene"})
    await rejects("test_control", {"device_id": "typo"})
    await call("apply_scene", {"name": "Normal"})

    async def get_user(uid):
        return SimpleNamespace(is_admin=uid == "boss")

    hass.auth = SimpleNamespace(async_get_user=get_user)
    await rejects("pause", {}, error=Unauthorized, context=Context(user_id="guest"))
    await call("resume", context=Context(user_id="boss"))
    print("actions  : work, and refuse typos and non-administrators")

    # ---- switches
    added = []
    await switch.async_setup_entry(hass, entry, added.extend)
    for sw in added:
        await sw.async_turn_on()
        assert sw.is_on, sw.translation_key
        await sw.async_turn_off()
        assert not sw.is_on, sw.translation_key
    print("switches :", [sw.translation_key for sw in added])

    # ---- Repairs: "test control" that passes, fails, and runs no steps
    for res, kind, reason in (({"ok": True, "steps": [{"name": "read", "ok": True}]}, "create_entry", None),
                              ({"ok": False, "steps": [{"name": "charge", "ok": False, "detail": "nothing happened"}]}, "abort", "test_failed"),
                              ({"ok": False}, "abort", "test_failed")):
        async def test(did, res=res):
            return res

        coord.test_control = test
        f = await repairs.async_create_fix_flow(hass, "i", {"fix": "battery", "device_id": "a"})
        f.hass, f.flow_id, f.handler = hass, "r", "homepower"
        r = await f.async_step_test()
        assert r["type"] == kind and r.get("reason") == reason, r
    print("repairs  : test control passes and fails")

    mgr.coordinator = coord
    await coord.async_close()
    srv.close(); await srv.wait_closed()
    print("E2E FLOWS OK")


asyncio.run(main())
