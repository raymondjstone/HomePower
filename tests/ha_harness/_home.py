"""A running HomePower with one or more simulated Anker batteries, for the checks that look at one behaviour at a time."""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]
import asyncio, tempfile
from types import SimpleNamespace
import conftest  # noqa: F401
import _harness  # noqa: F401
from test_drivers import FakeAnker
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util
from custom_components.homepower.manager import HomePowerManager
from custom_components.homepower.coordinator import HomePowerCoordinator


def new_entry(options=None):
    return ConfigEntry(data={}, disabled_by=None, discovery_keys={}, domain="homepower", entry_id="e1", minor_version=1,
                       options=options or {}, pref_disable_new_entities=False, pref_disable_polling=False, source="user",
                       subentries_data=[], title="HomePower", unique_id="homepower", version=1)


class Home:
    async def start(self, batteries=("a",), kind="anker_e5000_pro", control=True, options=None):
        dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/London"))
        tmp = tempfile.mkdtemp()
        self.hass = hass = HomeAssistant(tmp)
        hass.config.config_dir = tmp
        self.entry = entry = new_entry(options)
        self.mgr = mgr = HomePowerManager(hass)
        await mgr.async_load()
        self.coord = coord = HomePowerCoordinator(hass, entry, mgr, *mgr.build())
        mgr.coordinator = coord
        _harness.attach(hass, entry, mgr)
        hass.config_entries = SimpleNamespace(
            async_get_known_entry=lambda i: entry, async_entries=lambda d=None: [entry],
            async_update_entry=lambda e, options=None, **kw: coord.apply_settings(options) or True)
        self.fakes, self.servers = {}, []
        for did in batteries:
            fake = FakeAnker()
            srv = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
            self.servers.append(srv)
            self.fakes[did] = fake
            await mgr.save_device({"device_id": did, "name": did.upper(), "kind": kind, "control_enabled": control,
                                   "connection": {"host": "127.0.0.1", "port": srv.sockets[0].getsockname()[1]}})
        await coord.async_refresh()
        return self

    async def stop(self):
        await self.coord.async_close()
        for srv in self.servers:
            srv.close()
