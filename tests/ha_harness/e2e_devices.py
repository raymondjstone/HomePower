"""One device per battery, on the real registries and entity platform: an upgrade keeps every entity id and name, a rename
follows, a deleted battery's device goes, and only a left-over device may be deleted by hand."""
import _home  # noqa: F401 - sets up the paths first
import asyncio, logging
from datetime import timedelta
from homeassistant.config_entries import ConfigEntries
from homeassistant.helpers import device_registry as dr, entity_registry as er, frame
from homeassistant.helpers.entity_platform import EntityPlatform
import custom_components.homepower as hp
from custom_components.homepower import sensor
from custom_components.homepower.entity import find_device


async def main():
    h = await _home.Home().start(batteries=("a", "b"))
    hass, entry, mgr, coord = h.hass, h.entry, h.mgr, h.coord
    real_entries = hass.config_entries
    if hasattr(frame, "async_setup"):
        frame.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()                          # the registries wait for this
    hass.config_entries._entries[entry.entry_id] = entry
    for registry in (dr, er):
        if hasattr(registry, "async_setup"):
            registry.async_setup(hass)
        if hasattr(registry, "DATA_REGISTRY") and registry.DATA_REGISTRY in hass.data:     # older releases: already made
            await registry.async_get(hass).async_load()
        else:
            await registry.async_load(hass)
    devices, entities = dr.async_get(hass), er.async_get(hass)

    # as the previous version left things: every sensor on the one HomePower device
    old = devices.async_get_or_create(config_entry_id="e1", identifiers={("homepower", "e1")}, name="HomePower")
    before = {}
    for uid, object_id, name in (("e1_a_soc", "homepower_a_charge", "A charge"), ("e1_energy_bat_a_in", "homepower_a_energy_charged", "A energy charged")):
        before[uid] = entities.async_get_or_create("sensor", "homepower", uid, config_entry=entry, device_id=old.id,
                                                   suggested_object_id=object_id, original_name=name, has_entity_name=True).entity_id
    gone = devices.async_get_or_create(config_entry_id="e1", identifiers={("homepower", "e1_deleted")}, name="HomePower Deleted")

    hp.prepare_devices(hass, entry, mgr.devices)
    assert devices.async_get(gone.id) is None, "a battery deleted while not loaded keeps no device"
    platform = EntityPlatform(hass=hass, logger=logging.getLogger("t"), domain="sensor", platform_name="homepower", platform=None,
                              scan_interval=timedelta(seconds=30), entity_namespace=None)
    platform.config_entry = entry
    made = []
    entry.runtime_data.coordinator = coord
    entry.async_on_unload = lambda f: None
    await sensor.async_setup_entry(hass, entry, made.extend)
    await platform.async_add_entities(made)
    await hass.async_block_till_done()

    battery = find_device(devices, ("homepower", "e1_a"), "e1")
    assert battery and battery.name == "HomePower A" and battery.via_device_id == old.id and battery.manufacturer == "Anker", battery
    assert find_device(devices, ("homepower", "e1_b"), "e1")
    for uid, entity_id in before.items():
        row = entities.async_get(entity_id)
        assert row and row.unique_id == uid and row.device_id == battery.id, (uid, row)
    names = {s: hass.states.get(before[s]).attributes["friendly_name"] for s in before}
    assert names == {"e1_a_soc": "HomePower A charge", "e1_energy_bat_a_in": "HomePower A energy charged"}, names
    fresh = entities.async_get_entity_id("sensor", "homepower", "e1_b_soc")
    assert fresh == "sensor.homepower_b_charge", fresh                     # new batteries are named as before too
    system = entities.async_get(entities.async_get_entity_id("sensor", "homepower", "e1_battery_power"))
    assert system.device_id == old.id
    print("upgrade  : entity ids and names kept, each battery on its own device")

    await mgr.save_device({**mgr.devices["a"], "name": "Garage"})
    coord.async_update_listeners()
    await hass.async_block_till_done()
    assert devices.async_get(battery.id).name == "HomePower Garage"
    assert entities.async_get(before["e1_a_soc"]), "the entity id stays"
    assert hass.states.get(before["e1_a_soc"]) is not None
    print("rename   :", devices.async_get(battery.id).name)

    assert not await hp.async_remove_config_entry_device(hass, entry, devices.async_get(battery.id))
    assert not await hp.async_remove_config_entry_device(hass, entry, devices.async_get(old.id))
    stray = devices.async_get_or_create(config_entry_id="e1", identifiers={("homepower", "e1_stray")}, name="stray")
    assert await hp.async_remove_config_entry_device(hass, entry, stray)

    entry.runtime_data.coordinator.config_entry = entry
    await mgr.delete_device("a")
    await hass.async_block_till_done()
    assert devices.async_get(battery.id) is None and entities.async_get(before["e1_a_soc"]) is None
    assert find_device(devices, ("homepower", "e1_b"), "e1")
    print("delete   : the battery's device and sensors are gone")
    hp.prepare_devices(None, entry, mgr.devices)                          # a registry that is not there stops nothing
    hass.config_entries = real_entries
    await h.stop()
    print("E2E DEVICES OK")


asyncio.run(main())
