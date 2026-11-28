"""Battery discovery when things go wrong or are unusual, on the real config-flow class and a running HomePower with simulated
Anker batteries: a battery that does not report its capacity is asked for it; a save that is refused, a battery that cannot be
read, HomePower set up but not running, a pending battery that cannot be added at start-up; the serial kept (or not) through
edits; the serial learned over HomePower's own connection when a battery is offline or will not save; and the background scan's
timing (the first scan after start-up, the 12-hourly one, switched off, and a scan that fails never reaching control)."""
import _home  # noqa: F401 - sets up the paths first
import asyncio
from types import SimpleNamespace

from test_drivers import FakeAnker
from homeassistant.config_entries import ConfigEntryState
from custom_components.homepower import autodiscover, runtime
from custom_components.homepower import _add_discovered
from custom_components.homepower.config_flow import HomePowerConfigFlow
from custom_components.homepower.core.serde import ValidationError


def use_entries(hass, *entries):
    def async_entries(domain=None, include_ignore=True, include_disabled=True):
        return [e for e in entries if include_ignore or getattr(e, "source", "user") != "ignore"]
    hass.config_entries.async_entries = async_entries
    hass.config_entries.async_entry_for_domain_unique_id = lambda domain, uid: next((e for e in entries if e.unique_id == uid), None)


def flow_for(hass, source="integration_discovery"):
    f = HomePowerConfigFlow()
    f.hass, f.handler, f.flow_id = hass, "homepower", "f1"
    f.context = {"source": source}
    return f


def found(port, serial, **kw):
    return {"vendor": "anker", "host": "127.0.0.1", "port": port, "serial": serial, "model": "A17C5", "x1": False, **kw}


async def main():
    h = await _home.Home().start(batteries=("a",), options={"auto_discover": True})
    hass, entry, mgr = h.hass, h.entry, h.mgr
    port_a = mgr.devices["a"]["connection"]["port"]
    use_entries(hass, entry)
    hass.config_entries.flow = SimpleNamespace(async_progress_by_handler=lambda *a, **k: [])

    # ---- a battery that does not report its capacity or limits: asked for exactly those, then added with them
    fake = FakeAnker()
    fake.put(10250, 0)                     # no rated energy
    fake.put(10036, 0)
    fake.put(10038, 0)                     # no max charge / discharge power
    fake.put(10008, -300)                  # but it reports power, so its word order can be learned
    srv = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
    port_b = srv.sockets[0].getsockname()[1]
    f = flow_for(hass)
    await f.async_step_integration_discovery(found(port_b, "BLANK001"))
    r = await f.async_step_discovery_confirm({"name": "Blank", "kind": "anker_e5000_pro", "control_enabled": False})
    assert r["type"] == "form" and r["step_id"] == "discovery_details", r
    assert {str(k) for k in r["data_schema"].schema} == {"capacity_wh", "max_charge_w", "max_discharge_w"}, r["data_schema"].schema
    r = await f.async_step_discovery_details({"capacity_wh": 5120, "max_charge_w": 1200, "max_discharge_w": 1200})
    assert r["reason"] == "battery_added", r
    d = mgr.devices["blank"]
    assert (d["capacity_wh"], d["max_charge_w"], d["max_discharge_w"], d["min_soc"]) == (5120, 1200, 1200, 10), d
    print("details  : asked only for what the battery did not report")

    # ---- two found batteries given the same name do not overwrite each other
    f = flow_for(hass)
    await f.async_step_integration_discovery(found(port_b, "BLANK002"))
    await f.async_step_discovery_confirm({"name": "Blank", "kind": "anker_e5000_pro", "control_enabled": False})
    r = await f.async_step_discovery_details({"capacity_wh": 5120, "max_charge_w": 1200, "max_discharge_w": 1200})
    assert r["reason"] == "battery_added" and "blank_2" in mgr.devices and mgr.devices["blank"]["connection"]["serial"] == "BLANK001"
    await mgr.delete_device("blank_2")

    # ---- the save is refused (say, a battery of that name was added in the panel meanwhile): a clear abort, nothing half-added
    real_save = mgr.save_device

    async def refuse(raw, new=False):
        raise ValidationError("A battery with that name already exists. Give this one a different name.")
    f = flow_for(hass)
    await f.async_step_integration_discovery(found(port_a + 1000, "REFUSED1"))
    f._found["port"] = port_b                     # reachable, so the probe works
    await f.async_step_discovery_confirm({"name": "Refused", "kind": "anker_e5000_pro", "control_enabled": False})
    mgr.save_device = refuse
    r = await f.async_step_discovery_details({"capacity_wh": 5120, "max_charge_w": 1200, "max_discharge_w": 1200})
    mgr.save_device = real_save
    assert r["type"] == "abort" and r["reason"] == "invalid" and "already exists" in r["description_placeholders"]["error"], r
    assert "refused" not in mgr.devices
    print("refused  : a refused save aborts with the reason")

    # ---- a known battery that moved, whose new address cannot be saved: still "known", not offered as a new one
    mgr.save_device = refuse
    r = await flow_for(hass).async_step_integration_discovery(found(port_b + 7, "BLANK001"))
    mgr.save_device = real_save
    assert r["reason"] == "battery_known" and mgr.devices["blank"]["connection"]["port"] == port_b, r

    # ---- HomePower set up but not running (it failed to start): nothing is added behind its back
    real_running = runtime.running
    runtime.running = lambda h_: None
    try:
        f = flow_for(hass)
        r = await f.async_step_integration_discovery(found(port_b, "NOTRUN01"))
    finally:
        runtime.running = real_running
    assert r["type"] == "abort" and r["reason"] == "not_loaded", r
    print("not run  : HomePower set up but not running: not added")

    # ---- the serial is kept through an edit at the same address, and dropped when the battery is pointed elsewhere
    a = mgr.devices["a"]
    await mgr.save_device({**a, "connection": {**a["connection"], "serial": "KEEP0001"}})
    await mgr.save_device({**mgr.devices["a"], "connection": {"host": "127.0.0.1", "port": port_a}})
    assert mgr.devices["a"]["connection"].get("serial") == "KEEP0001"
    try:
        await mgr.save_device({**mgr.devices["a"], "connection": {"host": "127.0.0.1", "port": "not a port"}})
        raise AssertionError("a junk port was accepted")
    except ValidationError:
        pass                                       # refused by validation, with no crash on the way
    await mgr.save_device({**mgr.devices["a"], "connection": {"host": "127.0.0.1", "port": port_b}})
    assert "serial" not in mgr.devices["a"]["connection"], "pointed at another address: it may be another battery"
    await mgr.save_device({**mgr.devices["a"], "connection": {"host": "127.0.0.1", "port": port_a}})
    print("serial   : kept through an edit, dropped for another address")

    # ---- learning serials over HomePower's own connection: an offline battery or a refused save is tried again next time
    for did in ("a", "blank"):
        mgr.devices[did]["connection"].pop("serial", None)
    srv.close()
    await srv.wait_closed()                        # "blank" is now offline
    put_text = lambda fk, text: [fk.inp.__setitem__(10100 + i, (b << 8) | c)
                                 for i, (b, c) in enumerate(zip(*[iter(text.encode().ljust(16, b"\0"))] * 2))]
    put_text(h.fakes["a"], "OWNSERIAL")
    mgr.save_device = refuse
    await autodiscover.learn_serials(mgr)          # refused: nothing saved, no error
    mgr.save_device = real_save
    assert "serial" not in mgr.devices["a"]["connection"]
    await autodiscover.learn_serials(mgr)
    assert mgr.devices["a"]["connection"]["serial"] == "OWNSERIAL"
    assert "serial" not in mgr.devices["blank"]["connection"], "offline: tried again at the next scan"
    print("learn    : offline and refused batteries are left for the next scan")

    # ---- the background scan: off does nothing; a moved battery that will not save is logged; a failed scan is contained
    asked = []

    async def discover(skip=frozenset()):
        asked.append(set(skip))
        return [{"host": "10.0.0.9", "port": 502, "serial": "OWNSERIAL", "model": "A17C5", "x1": False}]
    mgr.discover_anker = discover
    entry_off = SimpleNamespace(options={"auto_discover": False})
    assert await autodiscover.scan_once(hass, entry_off, mgr) == [] and not asked, "switched off: nothing is scanned"
    mgr.save_device = refuse
    await autodiscover.scan_once(hass, entry, mgr)
    mgr.save_device = real_save
    assert mgr.devices["a"]["connection"]["host"] == "127.0.0.1", "the refused update left it as it was"

    tasks, unloads, timers = [], [], []
    fake_entry = SimpleNamespace(options={"auto_discover": True}, async_on_unload=unloads.append,
                                 async_create_background_task=lambda hass_, coro, name: tasks.append(coro))
    real_track, real_first = autodiscover.async_track_time_interval, autodiscover.FIRST_SCAN_AFTER_S
    autodiscover.async_track_time_interval = lambda hass_, fn, every: timers.append((fn, every)) or (lambda: None)
    autodiscover.FIRST_SCAN_AFTER_S = 0
    try:
        autodiscover.async_start(hass, fake_entry, mgr)
        assert len(tasks) == 1 and timers[0][1] == autodiscover.SCAN_EVERY and unloads, (tasks, timers)
        asked.clear()
        await tasks.pop()                          # the first scan, after start-up
        assert asked == [{"127.0.0.1"}], asked
        timers[0][0](None)                         # twelve hours later
        assert len(tasks) == 1
        mgr.discover_anker = lambda skip=frozenset(): (_ for _ in ()).throw(OSError("network down"))
        await tasks.pop()                          # a scan that fails is logged, never raised
        fake_entry.options["auto_discover"] = False
        autodiscover.async_start(hass, fake_entry, mgr)
        asked.clear()
        mgr.discover_anker = discover
        await tasks.pop()                          # off at start-up: no wait and no scan
        assert not asked
    finally:
        autodiscover.async_track_time_interval, autodiscover.FIRST_SCAN_AFTER_S = real_track, real_first
    print("scan     : first scan, 12-hourly, off, and a failure contained")

    # ---- a battery confirmed before HomePower was set up that cannot be added when it starts: said so, HomePower still starts
    notes, updated = [], {}
    import homeassistant.components.persistent_notification as pn
    real_create = pn.async_create
    pn.async_create = lambda hass_, message, title=None, notification_id=None: notes.append((notification_id, message))
    hass.config_entries.async_update_entry = lambda e, data=None, **kw: updated.update(data=data)
    try:
        pending = SimpleNamespace(data={"discovered": {"name": "Gone", "kind": "anker_e5000_pro",
                                                       "connection": {"host": "127.0.0.1", "port": 1}}})
        await _add_discovered(hass, pending, mgr)    # unreachable, so its capacity cannot be read and the save is refused
    finally:
        pn.async_create = real_create
    assert "gone" not in mgr.devices and updated["data"] == {}, (mgr.devices.keys(), updated)
    assert notes and notes[0][0] == "homepower_discovered" and "Gone" in notes[0][1], notes
    await _add_discovered(hass, SimpleNamespace(data={}), mgr)     # nothing pending: nothing happens
    print("pending  : a battery that cannot be added is reported; HomePower still starts")

    assert entry.state is ConfigEntryState.LOADED
    await h.stop()
    print("E2E DISCOVERY EDGES OK")


asyncio.run(main())
