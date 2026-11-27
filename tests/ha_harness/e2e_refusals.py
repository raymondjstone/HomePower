"""What happens when something is refused or broken: the Configure forms, the manager, restore, and removal."""
import _home  # noqa: F401 - sets up the paths first
import asyncio, tempfile
from pathlib import Path
from types import SimpleNamespace
import custom_components.homepower as hp
from custom_components.homepower import options_flow
from custom_components.homepower.core.serde import ValidationError


async def refused(coro, words=""):
    try:
        await coro
    except ValidationError as err:
        assert words in str(err), (words, str(err))
    else:
        raise AssertionError("not refused: " + words)


def raising(words):
    async def fail(*a, **kw):
        raise ValidationError(words)
    return fail


def flow(h, **data):
    f = options_flow.HomePowerOptionsFlow()
    f.hass, f.flow_id, f.handler, f._d = h.hass, "o", "e1", dict(data)
    return f


async def forms():
    h = await _home.Home().start()
    mgr = h.mgr
    await mgr.save_schedule({"id": "S", "start": "01:00", "end": "02:00", "mode": "charge", "power_w": 500})
    await mgr.save_goal({"id": "G", "target_soc": 80, "by": "07:00"})
    assert "remove_goal" in (await flow(h).async_step_init())["menu_options"]
    real = {n: getattr(mgr, n) for n in ("probe", "discover_ecoflow", "save_device", "save_schedule", "save_goal", "delete_device")}

    def broken(name):
        setattr(mgr, name, raising("no good"))

    def invalid(r):
        assert r["type"] == "abort" and r["reason"] == "invalid" and r["description_placeholders"]["error"], r

    # a battery that cannot be reached keeps the form open and says why
    broken("probe")
    for step, data, answer in (("async_step_ecoflow_local", {"kind": "ecoflow_stream_5000_local"}, {"host": "10.0.0.9"}),
                               ("async_step_modbus_map", {"kind": "generic_modbus"}, {"host": "10.0.0.9", "map": "{}"})):
        if data["kind"] not in options_flow.PROFILE_BY_ID:
            data["kind"] = next(p["id"] for p in options_flow.PROFILES if p["driver"] == "modbus_generic")
        r = await getattr(flow(h, **data), step)(answer)
        assert r["type"] == "form" and r["errors"] == {"base": "cannot_connect"}, r

    async def none(**kw):
        return []

    mgr.discover_ecoflow = none
    r = await flow(h, kind="ecoflow_stream_ultra_x").async_step_ecoflow_account({"region": "eu", "access_key": "a", "secret_key": "b"})
    assert "no devices" in r["description_placeholders"]["error"], r
    f = flow(h, kind="ecoflow_stream_ultra_x", connection={})
    f._eco_devices = [{"sn": "SN1", "name": "One", "online": False}]
    r = await f.async_step_ecoflow_device({"sn": "SN1"})
    assert r["errors"] == {"base": "cannot_connect"}, r

    broken("save_device")
    invalid(await flow(h, name="A", kind="anker_e5000_pro")._save_battery())          # (the id "a" is taken: it becomes a_2)
    invalid(await flow(h).async_step_control_battery({"device": "gone", "enabled": True}))
    broken("save_schedule")
    times = {"mode": "charge", "start": "01:00:00", "end": "02:00:00"}
    invalid(await flow(h).async_step_add_schedule({"name": "S", **times}))             # already exists
    invalid(await flow(h).async_step_add_schedule({"name": "New", **times}))
    f = flow(h)
    invalid(await f.async_step_edit_schedule_form())                                  # nothing chosen / deleted meanwhile
    f._edit_id = "S"
    invalid(await f.async_step_edit_schedule_form(times))
    broken("save_goal")
    goal = {"target_soc": 80, "by": "07:00:00"}
    invalid(await flow(h).async_step_add_goal({"name": "G", **goal}))
    invalid(await flow(h).async_step_add_goal({"name": "New", **goal}))
    broken("delete_device")
    r = await flow(h).async_step_remove_battery({"device": "a"})
    assert r["errors"] == {"base": "remove_failed"}, r
    entry_options = SimpleNamespace(options={})
    f = flow(h)
    type(f).config_entry = property(lambda self: entry_options)
    invalid(await f.async_step_money({"wear_cost": -5}))
    assert options_flow._readings({}) == ""
    gone, options_flow.running_manager = options_flow.running_manager, lambda hass: None
    assert (await flow(h).async_step_init())["reason"] == "not_loaded"
    options_flow.running_manager = gone
    for name, fn in real.items():
        setattr(mgr, name, fn)
    print("forms    : refusals are shown, nothing is half saved")
    await h.stop()


async def manager():
    h = await _home.Home().start()
    mgr, coord, hass = h.mgr, h.coord, h.hass

    # EcoFlow live feed: one hub per account; its state for the panel
    assert mgr.live_status() is None
    cfg = SimpleNamespace(extra={"connection": {"access_key": "a", "secret_key": "b", "region": "eu"}})
    from custom_components.homepower import manager as M
    real_session, M.async_get_clientsession = M.async_get_clientsession, lambda hass: None
    hub = mgr._live_hub(cfg)
    assert hub is mgr._live_hub(cfg) and mgr._live_hub(SimpleNamespace(extra={"connection": {"region": "x"}})) is None
    mgr.set_live(True)
    assert mgr.live_status() == {"on": True, "connected": False, "messages": 0, "problem": None}
    mgr.set_live(False)

    # stored rows that no longer make sense are skipped, not fatal
    mgr.schedules["bad"], mgr.goals["bad"] = {"id": "bad"}, {"id": "bad"}
    adapters, schedules, goals = mgr.build()
    assert len(adapters) == 1 and not [s for s in schedules if s.id == "bad"] and not [g for g in goals if g.id == "bad"]
    del mgr.schedules["bad"], mgr.goals["bad"]

    class Api:
        def __init__(self, *a):
            pass

        async def devices(self):
            return [{"sn": "SN1", "deviceName": "One", "online": 1}, {"nothing": True}]

    real_api, M.EcoFlowApi = M.EcoFlowApi, Api
    assert await mgr.discover_ecoflow("eu", "a", "b") == [{"sn": "SN1", "name": "One", "online": True, "product": None}]

    class Refused(Api):
        async def devices(self):
            raise M.EcoFlowError("keys refused")

    M.EcoFlowApi = Refused
    await refused(mgr.discover_ecoflow("eu", "a", "b"), "keys refused")
    M.EcoFlowApi, M.async_get_clientsession = real_api, real_session

    from homeassistant.components import network
    real_adapters = network.async_get_adapters

    async def no_network(hass):
        raise OSError("no network")

    network.async_get_adapters = no_network
    assert await mgr.discover_anker() == []
    network.async_get_adapters = real_adapters

    await refused(mgr.probe({"kind": "anker_e5000_pro", "connection": {"host": "127.0.0.1", "port": 1}}), "could not read")
    assert M.HomePowerManager._map_controls({"kind": "x", "connection": {"map": "not a map"}}) is False

    # deleting a battery takes it out of schedules and goals; one left with nobody is switched off, not widened to everybody
    await mgr.save_schedule({"id": "S", "start": "01:00", "end": "02:00", "mode": "charge", "power_w": 500, "device_ids": ["a"]})
    await mgr.reorder_devices(["nope", "a"])
    assert list(mgr.devices) == ["a"]
    await mgr.confirm_first_command("a")
    await refused(mgr.confirm_first_command("nope"), "unknown battery")
    await refused(mgr.apply_scene("nope"), "no scene")
    mgr.scenes["broken"] = {"name": "broken", "settings": {"import_limit_w": "lots"}}
    await refused(mgr.apply_scene("broken"), "cannot be used")
    del mgr.scenes["broken"]

    # ---- restore
    Path(mgr._backup_dir()).mkdir(parents=True, exist_ok=True)
    (Path(mgr._backup_dir()) / "homepower-2026-01-01.json").write_text("{not json", encoding="utf8")
    await refused(mgr.restore_backup("homepower-2026-01-01.json", h.entry), "could not read")
    await refused(mgr.restore_backup("../other.json", h.entry), "not one of")
    await refused(mgr.import_all({"nonsense": 1}, h.entry), "not a HomePower export")

    async def explode():
        raise RuntimeError("disk")

    real_apply, mgr._apply = mgr._apply, explode
    before = dict(mgr.schedules)
    await refused(mgr.import_all({"homepower_export": 1, "schedules": [
        {"id": "T", "start": "03:00", "end": "04:00", "mode": "hold"}]}, h.entry), "nothing was changed")
    assert mgr.schedules == before
    mgr._apply = real_apply
    out = await mgr.import_all({"homepower_export": 1, "devices": [{**mgr.devices["a"], "control_enabled": False}, 5],
                                "settings": {"import_limit_w": "lots"}}, h.entry)
    assert out["added"]["devices"] == 1 and any("settings" in s for s in out["skipped"]) and len(out["skipped"]) == 2, out
    mgr.coordinator = None
    assert mgr.entity_ids() == {}
    mgr.coordinator = coord
    await mgr.delete_device("a")
    assert mgr.schedules["S"]["enabled"] is False and mgr.schedules["S"]["device_ids"] == []
    print("manager  : refusals, restore all-or-nothing, delete")
    await h.stop()


async def removal():
    tmp = Path(tempfile.mkdtemp()) / "homepower_backups"
    hp._erase_backups(str(tmp))                                      # nothing there
    tmp.mkdir()
    (tmp / "homepower-2026-01-01.json").write_text("{}", encoding="utf8")
    hp._erase_backups(str(tmp))
    assert not tmp.exists()
    hp._write_backup(str(tmp), {"a": 1}, "2026-01-02")
    hp._write_backup(str(tmp), {"a": 2}, "2026-01-02", True)                 # today's is already there: left alone
    assert '"a": 1' in (tmp / "homepower-2026-01-02.json").read_text(encoding="utf8")
    for day in range(3, 12):
        hp._write_backup(str(tmp), {}, f"2026-01-{day:02d}")
    assert len(hp.backup_files(tmp)) == hp.BACKUPS_KEPT

    h = await _home.Home().start()
    hass = h.hass
    real = hp._release_x1_limits_on_remove

    async def explode(hass):
        raise RuntimeError("x")

    hp._release_x1_limits_on_remove, real_erase = explode, hp._erase_backups

    def cannot(folder):
        raise OSError("read only")

    hp._erase_backups = cannot
    await hp.async_remove_entry(hass, SimpleNamespace(options={}))               # kept
    await hp.async_remove_entry(hass, SimpleNamespace(options={"erase_on_remove": True}))
    hp._release_x1_limits_on_remove, hp._erase_backups = real, real_erase

    async def fails(*a):
        raise OSError("no disk")

    real_job, hass.async_add_executor_job = hass.async_add_executor_job, fails
    await hp._backup_if_missing(hass, h.mgr, h.entry)                         # a failed safety copy never stops anything
    hass.async_add_executor_job = real_job
    hp._notify(None, "k", "m")
    print("removal  : survives a battery or disk that will not cooperate")
    await h.stop()


async def main():
    await forms()
    await manager()
    await removal()
    print("E2E REFUSALS OK")


asyncio.run(main())
