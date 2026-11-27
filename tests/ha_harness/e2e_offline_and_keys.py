"""A battery going offline and coming back (sensors, log), and EcoFlow refusing the account keys (re-authentication)."""
import _home  # noqa: F401 - sets up the paths first
import asyncio, logging
from types import SimpleNamespace
from homeassistant.helpers import entity_platform  # noqa: F401
from custom_components.homepower import config_flow, sensor
from custom_components.homepower.core.models import Actual
from custom_components.homepower.core.serde import ValidationError


async def sensors_and_log():
    h = await _home.Home().start(batteries=("a", "b"))
    coord = h.coord
    made = []
    h.entry.runtime_data.coordinator = coord
    h.entry.async_on_unload = lambda f: None
    await sensor.async_setup_entry(h.hass, h.entry, made.extend)
    by = {e.unique_id.replace("e1_", ""): e for e in made}
    assert by["a_soc"].available and by["a_power"].available and by["battery_power"].available and by["stored_energy"].available

    states = {"a": coord._snap.devices["a"], "b": coord._snap.devices["b"]}

    def offline(*ids):
        for did in ids:
            st = states[did]
            st.actual, st.link = Actual(online=False), "offline"

    offline("a")
    assert not by["a_soc"].available and not by["a_power"].available and not by["a_solar"].available
    assert by["a_cycles"].available and by["a_target"].available and by["home_battery"].available      # HomePower's own figures stay
    assert by["b_soc"].available and by["battery_power"].available
    offline("b")
    assert not by["battery_power"].available and not by["stored_energy"].available
    print("sensors  : an offline battery's readings are unavailable, its records are not")

    # the target sensor says when HomePower is paused or only watching
    target = by["a_target"]
    await h.mgr.set_paused(True)
    assert target.native_value == "paused"
    await h.mgr.set_paused(False)
    coord.adapters[0].config.extra["control_enabled"] = False
    assert target.native_value == "monitor_only"
    assert "reason" in target.extra_state_attributes
    for e in made:                      # every sensor can be read without an error, with batteries offline
        e.native_value
        e.native_unit_of_measurement

    # an Anker X1's own solar inputs, and a rename following into the sensor names
    cfg = coord.adapters[0].config
    cfg.extra["house_meter"] = True
    again = []
    await sensor.async_setup_entry(h.hass, h.entry, again.extend)
    pv = {e.unique_id.replace("e1_a_", ""): e for e in again if "_a_pv" in e.unique_id}
    assert len(pv) == 25 and pv["pv1_power"].entity_registry_enabled_default and not pv["pv5_power"].entity_registry_enabled_default
    states["a"].actual, states["a"].link = Actual(online=True, soc=50, pv_total_w=900.4, pv_strings=((100.0, 2.0),)), "online"
    assert pv["pv_total"].native_value == 900 and pv["pv1_power"].native_value == 200 and pv["pv2_power"].native_value is None
    print("x1       : PV input sensors")

    # ---- lost and found is logged once each, for a driver that does not say so itself
    lines = []

    class Keep(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    log = logging.getLogger("custom_components.homepower.coordinator")
    log.addHandler(Keep())
    log.setLevel(logging.INFO)
    quiet = SimpleNamespace(config=SimpleNamespace(device_id="g"), driver=object())
    for was, now in (("online", "stale"), ("stale", "offline"), ("offline", "offline"), ("offline", "online"), ("online", "online")):
        coord._log_link(quiet, was, now)
    coord._log_link(coord.adapters[0], "online", "offline")                      # the Anker driver logs its own
    assert lines == ["g: cannot be read: shown offline", "g: can be read again"], lines
    print("log      : once when lost, once when back")
    await h.stop()


async def keys():
    h = await _home.Home().start()
    coord, mgr = h.coord, h.mgr
    asked = []
    coord.config_entry = SimpleNamespace(async_start_reauth=lambda hass: asked.append(1))
    refused = SimpleNamespace(driver=SimpleNamespace(auth_failed=True))
    for adapters in ([refused], [refused], [], [refused]):
        coord._check_keys(adapters)
    assert len(asked) == 2, asked                                   # once while it lasts, again if it happens again

    def flow():
        f = config_flow.HomePowerConfigFlow()
        f.hass, f.flow_id, f.handler, f.context = h.hass, "f", "homepower", {"source": "reauth", "entry_id": "e1"}
        return f

    saved = []
    mgr.devices["eco"] = {"device_id": "eco", "driver": "ecoflow_stream", "connection": {"region": "eu", "sn": "SN1", "access_key": "old"}}

    async def discover(region, access_key, secret_key):
        if secret_key == "bad":
            raise ValidationError("signature is wrong")
        return [{"sn": "SN1" if access_key == "mine" else "OTHER"}]

    async def save(raw, new=False):
        saved.append(raw)

    mgr.discover_ecoflow, real_save, mgr.save_device = discover, mgr.save_device, save
    r = await flow().async_step_reauth({})
    assert r["type"] == "form" and r["step_id"] == "reauth_confirm", r
    r = await flow().async_step_reauth_confirm({"access_key": "mine", "secret_key": "bad"})
    assert r["errors"] == {"base": "cannot_connect"} and "signature" in r["description_placeholders"]["error"], r
    r = await flow().async_step_reauth_confirm({"access_key": "someone-else", "secret_key": "s"})
    assert r["errors"] == {"base": "no_match"} and not saved, r
    r = await flow().async_step_reauth_confirm({"access_key": " mine ", "secret_key": " new "})
    assert r["type"] == "abort" and r["reason"] == "reauth_successful", r
    assert saved[0]["connection"] == {"region": "eu", "sn": "SN1", "access_key": "mine", "secret_key": "new"}, saved
    # two batteries in different regions: the keys are tried where each battery is
    mgr.devices["far"] = {"device_id": "far", "driver": "ecoflow_stream", "connection": {"region": "america", "sn": "SN2"}}

    async def by_region(region, access_key, secret_key):
        if region == "eu":
            raise ValidationError("not an account here")
        return [{"sn": "SN2"}]

    mgr.discover_ecoflow = by_region
    saved.clear()
    r = await flow().async_step_reauth_confirm({"access_key": "k", "secret_key": "s"})
    assert r["reason"] == "reauth_successful" and [d["device_id"] for d in saved] == ["far"], (r, saved)
    del mgr.devices["eco"], mgr.devices["far"]
    coord.config_entry = h.entry
    mgr.save_device, mgr.coordinator = real_save, None
    h.entry.runtime_data = None
    import custom_components.homepower.config_flow as cf
    real, cf.running_manager = cf.running_manager, lambda hass: None
    r = await flow().async_step_reauth_confirm()
    assert r["reason"] == "not_loaded", r
    cf.running_manager, mgr.coordinator = real, coord
    print("keys     : refused keys ask for new ones; only matching keys are saved")
    await h.stop()


async def main():
    await sensors_and_log()
    await keys()
    print("E2E OFFLINE OK")


asyncio.run(main())
