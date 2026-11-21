"""An ES5000 over local Modbus through the real coordinator/manager: schedules charge and discharge in watts, the keep-alive keeps
flowing, Auto hands the unit back, a restart while held still hands it back, and Test control notices a reversed power sign."""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

import asyncio, struct, time as real_time, tempfile
import conftest  # noqa: F401
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util
import custom_components.homepower.coordinator as C
import custom_components.homepower.drivers.ecoflow_modbus as EM
import custom_components.homepower.manager as M
from custom_components.homepower.manager import HomePowerManager
from custom_components.homepower.coordinator import HomePowerCoordinator
from custom_components.homepower.core.serde import device_from_dict, device_to_dict

OFF = EM.ADDRESS_OFFSET


class Clock:
    now = 4_000_000.0
    def monotonic(self): return self.now
    def time(self): return self.now
    def strftime(self, fmt): return real_time.strftime(fmt, real_time.localtime(self.now))


CLOCK = Clock()
C._time = CLOCK
for _m in ("coord_prices", "coord_forecast", "coord_reports"):   # the coordinator's parts read the same clock
    sys.modules[f"custom_components.homepower.{_m}"]._time = CLOCK
EM.time = CLOCK
M.async_get_clientsession = lambda hass: None
_real_sleep = asyncio.sleep


async def _fast_sleep(s, *a, **k):
    await _real_sleep(0)
asyncio.sleep = _fast_sleep


def f32(x):
    return list(struct.unpack(">HH", struct.pack(">f", x)))


class Fake:
    """Follows EcoFlow's map. flip=True makes the unit read a positive target as DISCHARGE (the opposite of our assumption)."""

    def __init__(self, flip=False):
        self.soc, self.flip, self.regs, self.writes = 40.0, flip, {}, []
        self.regs.update({0x1610: 0, 0x1611: 0, 0x1612: 0, 0x1613: 0, 0x1614: 0, 0x25F: 0})
        self.regs.update({0x227: 0, 0x228: 5000, 0x221: 0, 0x222: 2300, 0x223: 0, 0x224: 3000, 0x2200: 10})
        self.power = -150.0

    def tick(self, s):
        if self.regs[0x1610] == 1:
            t = struct.unpack(">h", struct.pack(">H", self.regs[0x1611]))[0]
            self.power = float(-t if self.flip else t)
        else:
            self.power = -150.0
        self.soc = max(5.0, min(100.0, self.soc + self.power * s / 3600 / 5000 * 100))

    async def read_holding(self, addr, count):
        off = addr - OFF
        if off == 0x206:
            self.tick(0)                                  # the power follows the register as soon as it is read
            return f32(450.0) + f32(100.0) + f32(0.0) + f32(self.power) + [int(round(self.soc))]
        if off == 0x3:
            return list(struct.unpack(">8H", b"ES21ZABC12345678"))
        if any(off + i not in self.regs for i in range(count)):
            from custom_components.homepower.drivers.modbus import ModbusError
            raise ModbusError("exception", code=2)
        return [self.regs[off + i] for i in range(count)]

    async def write_register(self, addr, value):
        self.writes.append((addr - OFF, value & 0xFFFF))
        self.regs[addr - OFF] = value & 0xFFFF

    async def close(self):
        pass


RAW = {"device_id": "es", "name": "ES5000", "kind": "ecoflow_stream_5000_local", "control_enabled": True, "capacity_wh": 5000,
       "max_charge_w": 3000, "max_discharge_w": 2300, "min_soc": 10, "connection": {"host": "10.0.0.213"}}


def new_entry():
    return ConfigEntry(data={}, disabled_by=None, discovery_keys={}, domain="homepower", entry_id="e1", minor_version=1, options={},
                       pref_disable_new_entities=False, pref_disable_polling=False, source="user", subentries_data=[],
                       title="HomePower", unique_id="homepower", version=1)


async def boot(hass, fake, entry, add=False):
    mgr = HomePowerManager(hass); await mgr.async_load()
    if add:
        mgr.devices["es"] = device_to_dict(device_from_dict(RAW)); mgr.confirmed.add("es")
    coord = HomePowerCoordinator(hass, entry, mgr, *mgr.build()); mgr.coordinator = coord
    coord.set_config(*mgr.build())
    for a in coord.adapters:
        a.driver.client = fake
    return mgr, coord


async def cycles(coord, fake, n, step=10.0):
    for _ in range(n):
        CLOCK.now += step
        fake.tick(step)
        await coord.async_refresh()


async def main():
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/London"))
    tmp = tempfile.mkdtemp(); hass = HomeAssistant(tmp); hass.config.config_dir = tmp
    entry, fake = new_entry(), Fake()
    mgr, coord = await boot(hass, fake, entry, add=True)

    await mgr.save_schedule({"id": "Cheap", "start": "00:00", "end": "23:59", "mode": "charge", "power_w": 2000, "target_soc": 80})
    await cycles(coord, fake, 20)
    assert fake.regs[0x1610] == 1 and fake.regs[0x1611] == 2000, (fake.regs[0x1610], fake.regs[0x1611])
    assert fake.soc > 41 and fake.power == 2000.0
    beats = [w for w in fake.writes if w[0] == 0x25F]
    assert 1 <= len(beats) <= 20, len(beats)
    print("1 charge   : control point 1, target 2000 W, soc", round(fake.soc, 1), "| keep-alives", len(beats))

    await mgr.delete_schedule("Cheap")
    await cycles(coord, fake, 20)
    assert fake.regs[0x1610] == 0 and fake.regs[0x1611] == 0, (fake.regs[0x1610], fake.regs[0x1611])
    print("2 hand back: control point 0, target 0")

    await mgr.save_schedule({"id": "Peak", "start": "00:00", "end": "23:59", "mode": "discharge", "power_w": 1500})
    await cycles(coord, fake, 20)
    assert fake.regs[0x1610] == 1 and fake.regs[0x1611] == (-1500) & 0xFFFF and fake.power == -1500.0, (fake.regs[0x1611], fake.power)
    print("3 discharge: target -1500 W, battery power", fake.power)

    await mgr.save_runtime()
    mgr2, coord2 = await boot(hass, fake, entry)           # Home Assistant restarts while the battery is held
    assert "es" in mgr2.forced
    await mgr2.delete_schedule("Peak")
    await cycles(coord2, fake, 30)
    assert fake.regs[0x1610] == 0, fake.regs[0x1610]
    print("4 restart  : still handed back after a restart")

    # Test control, normal sign: passes with a right-way power step
    res = await coord2.test_control("es")
    assert res["ok"], res
    step = next(s for s in res["steps"] if s["name"] == "power moves the right way")
    assert step["ok"], step
    assert fake.regs[0x1610] == 0
    print("5 test     : ok |", step["detail"])

    # a unit whose sign is the opposite of what we assume: Test control says so and tells the user what to tick
    fake2 = Fake(flip=True)
    mgr3, coord3 = await boot(hass, fake2, new_entry(), add=False)
    coord3.set_config(*mgr3.build())
    for a in coord3.adapters:
        a.driver.client = fake2
    res = await coord3.test_control("es")
    step = next(s for s in res["steps"] if s["name"] == "power moves the right way")
    assert step["ok"] is False and "Reverse the charge/discharge sign" in step["detail"], step
    assert fake2.regs[0x1610] == 0
    print("6 reversed : flagged ->", step["detail"][:90])
    await coord.async_close(); await coord2.async_close(); await coord3.async_close()
    print("E2E_ECOFLOW_MODBUS OK")


asyncio.run(main())
