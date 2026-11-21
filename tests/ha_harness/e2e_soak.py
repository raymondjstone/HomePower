"""A simulated day of control cycles (every 10 s) with two EcoFlow batteries and schedules switching on and off:
nothing may grow without bound, the saved state must stay small, and a cycle must stay fast."""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

import asyncio, json, time as real_time, tempfile
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

CYCLES = int(os.environ.get("SOAK_CYCLES", "8640"))


class Clock:
    now = 3_000_000.0
    def monotonic(self): return self.now
    def time(self): return self.now
    def strftime(self, fmt): return real_time.strftime(fmt, real_time.localtime(self.now))


CLOCK = Clock()
C._time = CLOCK
for _m in ("coord_prices", "coord_forecast", "coord_reports", "coord_inputs", "coord_accounting"):   # the coordinator's parts read the same clock
    sys.modules[f"custom_components.homepower.{_m}"]._time = CLOCK
E.time = CLOCK
M.async_get_clientsession = lambda hass: None


class Fake:
    def __init__(self, soc, reserve):
        self.soc, self.reserve, self.power = float(soc), reserve, 0.0

    def tick(self, s, hour):
        base = -400.0 if 7 <= hour < 22 else -80.0           # the house draws from the battery by day
        if self.reserve > self.soc + 1:
            self.power = 2800.0
            self.soc = min(float(self.reserve), self.soc + self.power * s / 3600 / 5000 * 100)
        else:
            self.power = base
            self.soc = max(5.0, self.soc + self.power * s / 3600 / 5000 * 100)

    async def quota(self, sn):
        return {"soc": round(self.soc, 1), "cmsBattSoc": 0.0, "backupReverseSoc": self.reserve, "powGetBpCms": self.power,
                "powGetSysGrid": 300.0, "powGetPvSum": 0.0, "powGetSysLoad": 500.0}

    async def command(self, sn, params, cmd_id=17):
        if "cfgBackupReverseSoc" in params:
            self.reserve = int(params["cfgBackupReverseSoc"])


async def main():
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/London"))
    tmp = tempfile.mkdtemp(); hass = HomeAssistant(tmp); hass.config.config_dir = tmp
    entry = ConfigEntry(data={}, disabled_by=None, discovery_keys={}, domain="homepower", entry_id="e1", minor_version=1, options={},
                        pref_disable_new_entities=False, pref_disable_polling=False, source="user", subentries_data=[],
                        title="HomePower", unique_id="homepower", version=1)
    mgr = HomePowerManager(hass); await mgr.async_load()
    coord = HomePowerCoordinator(hass, entry, mgr, *mgr.build()); mgr.coordinator = coord
    fakes = {"a": Fake(50, 30), "b": Fake(30, 20)}
    for did in fakes:
        raw = {"device_id": did, "name": did.upper(), "kind": "ecoflow_stream_ultra_x", "control_enabled": True, "capacity_wh": 5000,
               "max_charge_w": 2300, "max_discharge_w": 0, "min_soc": 10,
               "connection": {"sn": did.upper() + "SN01", "access_key": "ak" + did, "secret_key": "sk" + did, "region": "eu"}}
        mgr.devices[did] = device_to_dict(device_from_dict(raw))
        mgr.confirmed.add(did)
    coord.set_config(*mgr.build())
    for a in coord.adapters:
        a.driver.api = fakes[a.config.device_id]

    cycle_times, view_times = [], []
    for i in range(CYCLES):
        CLOCK.now += 10.0
        hour = (CLOCK.now / 3600) % 24
        for f in fakes.values():
            f.tick(10.0, hour)
        if i % 360 == 0:                                       # every simulated hour: a schedule starts or stops
            if (i // 360) % 2 == 0:
                await mgr.save_schedule({"id": "Cheap", "start": "00:00", "end": "23:59", "mode": "charge", "power_w": 2000, "target_soc": 70})
            else:
                await mgr.delete_schedule("Cheap")
        t0 = real_time.perf_counter()
        await coord.async_refresh()
        cycle_times.append(real_time.perf_counter() - t0)
        if i % 6 == 0:
            t0 = real_time.perf_counter()
            json.dumps(coord.view(), default=str)
            view_times.append(real_time.perf_counter() - t0)
    await mgr.save_runtime()
    saved = await mgr._store.async_load()
    size = len(json.dumps(saved, default=str))

    ct = sorted(cycle_times)
    print(f"cycles   : {CYCLES} | median {ct[len(ct) // 2] * 1000:.1f} ms | 99th {ct[int(len(ct) * .99)] * 1000:.1f} ms | slowest {ct[-1] * 1000:.1f} ms")
    print(f"view     : median {sorted(view_times)[len(view_times) // 2] * 1000:.1f} ms")
    print(f"saved    : {size / 1024:.1f} KB |", {k: len(json.dumps(v, default=str)) for k, v in saved.items() if len(json.dumps(v, default=str)) > 2000})
    print(f"bounded  : events {len(coord._snap.events)} | decisions {len(coord.why)} | traces {[len(v) for v in coord.traces.results.values()]} | "
          f"plan days {len(coord.plan_log.days)} | temps {len(coord.temp_log.days)}")
    assert len(coord._snap.events) <= 200 and len(coord.why) <= 80
    assert all(len(v) <= 30 for v in coord.traces.results.values()) and len(coord.plan_log.days) <= 8
    assert size < 400_000, size                                # a day of running must not bloat the saved state
    assert ct[int(len(ct) * .99)] < 0.25, "a control cycle became slow"
    # the batteries are in a sane state at the end
    for did, f in fakes.items():
        assert 3 <= f.reserve <= 95 and 5 <= f.soc <= 100, (did, f.reserve, f.soc)
    await coord.async_close()
    print("E2E13 OK")


asyncio.run(main())
