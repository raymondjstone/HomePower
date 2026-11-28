"""Batteries found on the network, through the real config-flow class against a running HomePower and simulated Anker batteries:
a new one is offered and added monitor-only, a known one that moved gets its new address, one saved without its serial gets it,
EcoFlow's zeroconf announcement is offered, Ignore and the "find automatically" switch are respected, a discovery before
HomePower is set up sets it up with the battery, and the background scan offers new batteries and repairs moved ones."""
import _home  # noqa: F401 - sets up the paths first
import asyncio
from types import SimpleNamespace

from test_drivers import FakeAnker
from custom_components.homepower import autodiscover
from custom_components.homepower import _add_discovered
from custom_components.homepower.config_flow import HomePowerConfigFlow


def put_text(fake, addr, text, regs=8):
    raw = text.encode().ljust(regs * 2, b"\0")
    for i in range(regs):
        fake.inp[addr + i] = (raw[2 * i] << 8) | raw[2 * i + 1]


def use_entries(hass, *entries):
    """The config entries Home Assistant has for HomePower (its own, and any battery the user chose to ignore)."""
    def async_entries(domain=None, include_ignore=True, include_disabled=True):
        return [e for e in entries if include_ignore or getattr(e, "source", "user") != "ignore"]
    hass.config_entries.async_entries = async_entries
    hass.config_entries.async_entry_for_domain_unique_id = lambda domain, uid: next((e for e in entries if e.unique_id == uid), None)


async def step(coro):
    """A flow step's result, as Home Assistant's flow manager hands it on (an AbortFlow raised in the step is an abort)."""
    from homeassistant.data_entry_flow import AbortFlow
    try:
        return await coro
    except AbortFlow as err:
        return {"type": "abort", "reason": err.reason}


def flow_for(hass, source):
    f = HomePowerConfigFlow()
    f.hass, f.handler, f.flow_id = hass, "homepower", "f1"
    f.context = {"source": source}
    return f


async def main():
    h = await _home.Home().start(batteries=("a",), options={"auto_discover": True})   # the network scan itself is stubbed below
    hass, entry, mgr = h.hass, h.entry, h.mgr
    port_a = mgr.devices["a"]["connection"]["port"]
    use_entries(hass, entry)
    hass.config_entries.flow = SimpleNamespace(async_progress_by_handler=lambda *a, **k: [])

    # ---- one HomePower only (the manifest's single_config_entry is gone, so that discovered batteries reach the flow)
    r = await flow_for(hass, "user").async_step_user()
    assert r["type"] == "abort" and r["reason"] == "single_instance_allowed", r

    # ---- a new Anker battery found by the scan: offered, then added monitor-only, with its serial kept
    fake = FakeAnker()
    put_text(fake, 10100, "AZV1SHED")
    srv = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
    port_b = srv.sockets[0].getsockname()[1]
    f = flow_for(hass, "integration_discovery")
    r = await f.async_step_integration_discovery({"vendor": "anker", "host": "127.0.0.1", "port": port_b, "serial": "azv1shed",
                                                  "model": "A17C5", "x1": False})
    assert r["type"] == "form" and r["step_id"] == "discovery_confirm", r
    assert f.unique_id == "anker_AZV1SHED" and f.context["title_placeholders"]["name"] == "A17C5 SHED"
    assert "kind" in {str(k) for k in r["data_schema"].schema}, "a Solarbank's model is chosen by the user"
    r = await f.async_step_discovery_confirm({"name": "Shed", "kind": "anker_e5000_pro", "control_enabled": False})
    assert r["type"] == "abort" and r["reason"] == "battery_added", r     # the battery reported its capacity and limits: no more questions
    shed = mgr.devices["shed"]
    assert shed["connection"] == {"host": "127.0.0.1", "port": port_b, "unit_id": 1, "serial": "AZV1SHED"}, shed["connection"]
    assert shed["control_enabled"] is False and shed["capacity_wh"] == 10000
    print("new      : offered, added monitor-only with its serial")

    # ---- the same battery at a new address (a new DHCP lease): the address is put right, nothing is offered
    r = await flow_for(hass, "integration_discovery").async_step_integration_discovery(
        {"vendor": "anker", "host": "127.0.0.1", "port": port_b + 1, "serial": "AZV1SHED", "model": "A17C5", "x1": False})
    assert r["type"] == "abort" and r["reason"] == "battery_known", r
    assert mgr.devices["shed"]["connection"]["port"] == port_b + 1 and mgr.devices["shed"]["connection"]["serial"] == "AZV1SHED"
    print("moved    : address updated")

    # ---- a battery saved before serials were kept, seen at its own address: it gets its serial
    r = await flow_for(hass, "integration_discovery").async_step_integration_discovery(
        {"vendor": "anker", "host": "127.0.0.1", "port": port_a, "serial": "AZV1AAAA", "model": "A17C5", "x1": False})
    assert r["reason"] == "battery_known" and mgr.devices["a"]["connection"]["serial"] == "AZV1AAAA", mgr.devices["a"]["connection"]
    # an edit in the panel (whose form has no serial field) keeps it while the address stays
    await mgr.save_device({**mgr.devices["a"], "connection": {"host": "127.0.0.1", "port": port_a}})
    assert mgr.devices["a"]["connection"]["serial"] == "AZV1AAAA"
    print("serial   : learned, and kept through an edit")

    # ---- an X1 says it is one: its model is chosen already
    r = await flow_for(hass, "integration_discovery").async_step_integration_discovery(
        {"vendor": "anker", "host": "127.0.0.1", "port": 1, "serial": "X1SERIAL01", "model": "X1-H6K", "x1": True})
    kind = next(k for k in r["data_schema"].schema if str(k) == "kind")
    assert kind.default() == "anker_x1", kind

    # ---- EcoFlow announces itself (zeroconf): offered with no model to choose; an unreadable one says so and is not added
    info = SimpleNamespace(host="127.0.0.1", port=1, properties={"model": "STREAM AC 5000", "serial": "ES21XYZ987"})
    f = flow_for(hass, "zeroconf")
    r = await f.async_step_zeroconf(info)
    assert r["step_id"] == "discovery_confirm" and "kind" not in {str(k) for k in r["data_schema"].schema}, r
    assert f.unique_id == "ecoflow_ES21XYZ987"
    r = await f.async_step_discovery_confirm({"name": "Garage", "control_enabled": False})
    assert r["type"] == "form" and r["errors"] == {"base": "cannot_connect_battery"} and r["description_placeholders"]["error"], r
    assert "garage" not in mgr.devices
    print("zeroconf : offered; an unreachable one is refused with the reason")

    # ---- Ignore: an ignored battery is not offered again
    from homeassistant.config_entries import ConfigEntryState
    ignored = SimpleNamespace(source="ignore", unique_id="ecoflow_ES21XYZ987", domain="homepower", entry_id="ig", options={},
                              data={}, state=ConfigEntryState.NOT_LOADED)
    use_entries(hass, entry, ignored)
    r = await step(flow_for(hass, "zeroconf").async_step_zeroconf(info))
    assert r["type"] == "abort" and r["reason"] == "already_configured", r
    use_entries(hass, entry)

    # ---- "Find batteries automatically" off: nothing is offered
    off = SimpleNamespace(source="user", unique_id="homepower", domain="homepower", entry_id="e1", options={"auto_discover": False})
    use_entries(hass, off)
    r = await flow_for(hass, "zeroconf").async_step_zeroconf(info)
    assert r["type"] == "abort" and r["reason"] == "discovery_off", r
    use_entries(hass, entry)
    print("respects : Ignore and the off switch")

    # ---- a battery saved without its serial learns it over HomePower's own connection (no second Modbus client)
    put_text(h.fakes["a"], 10100, "AZV1OWN1")
    await mgr.save_device({**mgr.devices["a"], "connection": {"host": "127.0.0.1", "port": port_a + 0, "unit_id": 1}})
    mgr.devices["a"]["connection"].pop("serial", None)
    await autodiscover.learn_serials(mgr)
    assert mgr.devices["a"]["connection"]["serial"] == "AZV1OWN1", mgr.devices["a"]["connection"]
    print("own read : serial read over HomePower's own connection")

    # ---- the background scan: a new battery becomes a discovery, a moved one is put right, known addresses are not contacted
    made, asked = [], {}
    autodiscover.discovery_flow.async_create_flow = lambda hass_, domain, context, data: made.append((context["source"], data))

    async def discover_anker(skip=frozenset()):
        asked["skip"] = set(skip)
        return [{"host": "10.0.0.9", "port": 502, "soc": 50, "model": "A17C5", "serial": "AZV1SHED", "x1": False},     # Shed, moved
                {"host": "10.0.0.7", "port": 502, "soc": 40, "model": "X1-H6K", "serial": "X1NEW", "x1": True}]        # a new one
    mgr.discover_anker = discover_anker
    await autodiscover.scan_once(hass, entry, mgr)
    assert asked["skip"] == {"127.0.0.1"}, asked
    assert mgr.devices["shed"]["connection"]["host"] == "10.0.0.9"
    assert made == [("integration_discovery", {"vendor": "anker", "host": "10.0.0.7", "port": 502, "serial": "X1NEW",
                                                "model": "X1-H6K", "x1": True})], made
    print("scan     : moved one updated, new one offered, own batteries skipped")

    # ---- a discovery before HomePower is set up: confirming sets HomePower up, and the battery is added when it starts
    from custom_components.homepower import runtime
    use_entries(hass)
    real, runtime.running = runtime.running, lambda h_: None
    try:
        f = flow_for(hass, "integration_discovery")
        r = await f.async_step_integration_discovery({"vendor": "anker", "host": "127.0.0.1", "port": port_b, "serial": "AZV1NEW1",
                                                      "model": "A17C5", "x1": False})
        r = await f.async_step_discovery_confirm({"name": "Loft", "kind": "anker_e5000_pro", "control_enabled": False})
    finally:
        runtime.running = real
    assert r["type"] == "create_entry" and r["title"] == "HomePower" and f.unique_id == "homepower", r
    pending = r["data"]["discovered"]
    assert pending[0]["name"] == "Loft" and pending[0]["connection"]["serial"] == "AZV1NEW1"
    updated = {}
    new_entry = SimpleNamespace(data={"discovered": pending, "kept": 1})
    hass.config_entries.async_update_entry = lambda e, data=None, **kw: updated.update(data=data)
    await _add_discovered(hass, new_entry, mgr)
    assert "loft" in mgr.devices and mgr.devices["loft"]["control_enabled"] is False
    assert updated["data"] == {"kept": 1}, "added once: forgotten from the entry"
    print("not set up: HomePower set up with the battery, added at start")

    srv.close()
    await h.stop()
    print("E2E DISCOVERY OK")


asyncio.run(main())
