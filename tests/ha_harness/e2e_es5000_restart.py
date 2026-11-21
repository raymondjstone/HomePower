"""An ES5000 (STREAM 5000 series) held by HomePower while Home Assistant restarts: the unit's own operating mode and reserve
must still come back afterwards, through the real saved state."""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

import asyncio, time as real_time, tempfile
import conftest  # noqa: F401
import _harness  # noqa: F401 - Python 3.12+ server shutdown (see _harness.py)
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util
import custom_components.homepower.coordinator as C
import custom_components.homepower.drivers.ecoflow as E
import custom_components.homepower.manager as M
from custom_components.homepower.manager import HomePowerManager
from custom_components.homepower.coordinator import HomePowerCoordinator
from custom_components.homepower.core.serde import device_from_dict, device_to_dict


class Clock:
    now = 2_000_000.0
    def monotonic(self): return self.now
    def time(self): return self.now
    def strftime(self, fmt): return real_time.strftime(fmt, real_time.localtime(self.now))


CLOCK = Clock()
C._time = CLOCK
for _m in ("coord_prices", "coord_forecast", "coord_reports", "coord_inputs", "coord_accounting"):   # the coordinator's parts read the same clock
    sys.modules[f"custom_components.homepower.{_m}"]._time = CLOCK
E.time = CLOCK
M.async_get_clientsession = lambda hass: None


class FakeEs5000:
    """Starts the way a real ES5000 was reported: reserve switched off, in Timer-task mode. Charges from the grid only in
    self-consumption mode, with the reserve above its charge level."""

    def __init__(self):
        self.soc, self.on, self.level, self.mode, self.power, self.sent = 40.0, False, 40, "TIMER_TASK", 0.0, []

    def tick(self, s):
        if self.on and self.level > self.soc + 1 and self.mode == "SELF_CONSUMPTION":
            self.power = 3000.0
            self.soc = min(float(self.level), self.soc + self.power * s / 3600 / 5000 * 100)
        else:
            self.power = -200.0

    async def quota(self, sn):
        return {"bpSoc": round(self.soc, 1), "bpPwr": self.power, "gridPwr": 100.0, "loadPwr": 300.0, "pvPwr": 0.0,
                "backupSoc": self.level, "backupSocOnOff": self.on, "workMode": self.mode}

    async def command(self, sn, params, cmd_id=17):
        self.sent.append((cmd_id, params))
        assert cmd_id == 38, "the 5000 series uses command 38"
        if "bpBackupConfig" in params:
            self.on, self.level = bool(params["bpBackupConfig"]["backupSocOnOff"]), int(params["bpBackupConfig"]["backupSoc"])
        if "workMode" in params:
            self.mode = params["workMode"]


async def boot(hass, fake, entry):
    mgr = HomePowerManager(hass); await mgr.async_load()
    coord = HomePowerCoordinator(hass, entry, mgr, *mgr.build()); mgr.coordinator = coord
    for a in coord.adapters:
        a.driver.api = fake
    return mgr, coord


async def main():
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/London"))
    tmp = tempfile.mkdtemp(); hass = HomeAssistant(tmp); hass.config.config_dir = tmp
    entry = ConfigEntry(data={}, disabled_by=None, discovery_keys={}, domain="homepower", entry_id="e1", minor_version=1, options={},
                        pref_disable_new_entities=False, pref_disable_polling=False, source="user", subentries_data=[],
                        title="HomePower", unique_id="homepower", version=1)
    fake = FakeEs5000()

    mgr, coord = await boot(hass, fake, entry)
    raw = {"device_id": "es", "name": "ES5000", "kind": "ecoflow_stream_ac_5000", "control_enabled": True, "capacity_wh": 5000,
           "max_charge_w": 3000, "max_discharge_w": 0, "min_soc": 10,
           "connection": {"sn": "ES21SN01", "access_key": "ak", "secret_key": "sk", "region": "eu"}}
    mgr.devices["es"] = device_to_dict(device_from_dict(raw))
    mgr.confirmed.add("es")
    coord.set_config(*mgr.build())
    coord.adapters[0].driver.api = fake

    async def cycles(c, n, step=10.0):
        for _ in range(n):
            CLOCK.now += step
            fake.tick(step)
            await c.async_refresh()

    await mgr.save_schedule({"id": "Night", "start": "00:00", "end": "23:59", "mode": "charge", "power_w": 3000, "target_soc": 80})
    await cycles(coord, 16)
    sent = [p for _, p in fake.sent]
    assert sent[0] == {"workMode": "SELF_CONSUMPTION"} and {"bpBackupConfig": {"backupSocOnOff": True, "backupSoc": 80}} in sent, sent
    assert fake.soc > 41 and fake.mode == "SELF_CONSUMPTION"
    assert mgr.ecoflow_orig["es"]["mode"] == "TIMER_TASK" and mgr.ecoflow_orig["es"]["on"] is False, mgr.ecoflow_orig
    print("1 charging : unit put in self-consumption, reserve 80 %, originals saved:", mgr.ecoflow_orig["es"])

    await mgr.save_runtime()
    # ---- Home Assistant restarts: everything is rebuilt from the saved file
    mgr2, coord2 = await boot(hass, fake, entry)
    assert mgr2.ecoflow_orig.get("es", {}).get("mode") == "TIMER_TASK", mgr2.ecoflow_orig
    assert "es" in mgr2.forced, "the unit was held when HomePower stopped, so it must still be marked as held"
    drv = coord2.adapters[0].driver
    assert drv._orig and drv._orig["mode"] == "TIMER_TASK"
    print("2 restart  : the new HomePower still knows what to put back:", drv._orig)

    # nothing wants it any more: hand it back
    await mgr2.delete_schedule("Night")
    await cycles(coord2, 30)
    assert fake.mode == "TIMER_TASK", fake.mode
    assert fake.on is False, (fake.on, fake.level)
    assert "es" not in mgr2.ecoflow_orig and not coord2._snap.devices["es"].release_pending
    print("3 hand back: operating mode", fake.mode, "| reserve switched off | nothing left to remember")
    print("E2E12 OK")


asyncio.run(main())
