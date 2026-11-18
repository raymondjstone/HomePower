"""Three days through the real coordinator across the autumn clock change (Europe/London, 2026-10-25, a 25-hour day): a Go-style cheap
window 00:30-04:30, a goal "90 % by 07:00", a house load and a battery that obeys power commands. Checks the battery is charged in the cheap
window and not outside it, reaches the goal by 07:00 every morning, the command count stays small, and nothing raises."""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

import asyncio, logging, tempfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import conftest  # noqa: F401
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util
import custom_components.homepower.coordinator as C
import custom_components.homepower.manager as M
from custom_components.homepower.manager import HomePowerManager
from custom_components.homepower.coordinator import HomePowerCoordinator
from custom_components.homepower.core.models import Actual, Mode
from custom_components.homepower.core.serde import device_from_dict, device_to_dict

LON = ZoneInfo("Europe/London")
UTC = timezone.utc
STEP_S = 30
M.async_get_clientsession = lambda hass: None


class Sim:
    now_utc = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)

    def local(self):
        return self.now_utc.astimezone(LON)


SIM = Sim()
dt_util.now = lambda time_zone=None: SIM.local()
dt_util.utcnow = lambda: SIM.now_utc


class Clock:
    def monotonic(self): return (SIM.now_utc - datetime(2026, 1, 1, tzinfo=UTC)).total_seconds()
    def time(self): return self.monotonic()
    def strftime(self, fmt): return SIM.local().strftime(fmt)


C._time = Clock()


class Battery:
    """10 kWh, 3 kW, 92 % one-way efficiency, floor 10 %. Obeys the last command."""

    def __init__(self):
        self.soc, self.mode, self.set_w, self.power, self.commands = 30.0, Mode.AUTO, 0, 0.0, 0
        self.charged_by_hour: dict[int, float] = {}
        self.config = None

    def step(self, load_w, dt):
        if self.mode is Mode.CHARGE:
            self.power = float(min(self.set_w, 3000))
        elif self.mode is Mode.DISCHARGE:
            self.power = -float(min(self.set_w, 3000))
        elif self.mode is Mode.HOLD:
            self.power = 0.0
        else:
            self.power = -float(min(load_w, 3000)) if self.soc > 10 else 0.0          # own logic: cover the house
        if self.power > 0 and self.soc >= 100:
            self.power = 0.0
        if self.power < 0 and self.soc <= 10:
            self.power = 0.0
        e = self.power * dt / 3600
        self.soc = max(0.0, min(100.0, self.soc + (e * 0.92 if e > 0 else e / 0.92) / 10000 * 100))
        if self.power > 0:
            h = SIM.local().hour
            self.charged_by_hour[h] = self.charged_by_hour.get(h, 0.0) + e

    async def read(self):
        return Actual(online=True, soc=round(self.soc, 1), power_w=self.power, mode=self.mode, set_power_w=self.set_w if self.mode in (Mode.CHARGE, Mode.DISCHARGE) else None,
                      grid_w=HOUSE_LOAD + self.power, load_w=HOUSE_LOAD)

    async def apply(self, desired):
        self.commands += 1
        self.mode = desired.mode
        self.set_w = int(desired.power_w or 0) if desired.mode in (Mode.CHARGE, Mode.DISCHARGE) else 0

    async def close(self):
        pass


HOUSE_LOAD = 450.0


async def main():
    dt_util.set_default_time_zone(LON)
    tmp = tempfile.mkdtemp(); hass = HomeAssistant(tmp); hass.config.config_dir = tmp
    entry = ConfigEntry(data={}, disabled_by=None, discovery_keys={}, domain="homepower", entry_id="e1", minor_version=1, options={},
                        pref_disable_new_entities=False, pref_disable_polling=False, source="user", subentries_data=[],
                        title="HomePower", unique_id="homepower", version=1)
    mgr = HomePowerManager(hass); await mgr.async_load()
    coord = HomePowerCoordinator(hass, entry, mgr, *mgr.build()); mgr.coordinator = coord
    raw = {"device_id": "b", "name": "B", "kind": "anker_e5000_pro", "control_enabled": True, "capacity_wh": 10000, "max_charge_w": 3000,
           "max_discharge_w": 3000, "min_soc": 10, "connection": {"host": "127.0.0.1", "port": 1}}
    mgr.devices["b"] = device_to_dict(device_from_dict(raw))
    mgr.confirmed.add("b")
    coord.set_config(*mgr.build())
    bat = Battery()
    coord.adapters[0].driver = bat
    coord.apply_settings({"price_peak": 0.30, "price_offpeak": 0.075, "offpeak_start": "00:30", "offpeak_end": "04:30", "offpeak_auto": False})
    await mgr.save_goal({"id": "morning", "target_soc": 90, "by": "07:00", "days": [0, 1, 2, 3, 4, 5, 6], "enabled": True, "device_ids": []})

    errors = []
    class H(logging.Handler):
        def emit(self, rec):
            if rec.levelno >= logging.ERROR:
                errors.append(rec.getMessage())
    logging.getLogger().addHandler(H())

    morning = {}
    end = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
    cycles = 0
    while SIM.now_utc < end:
        SIM.now_utc += timedelta(seconds=STEP_S)
        bat.step(HOUSE_LOAD, STEP_S)
        await coord.async_refresh()
        cycles += 1
        loc = SIM.local()
        if os.environ.get("TRACE") and loc.date().isoformat() == "2026-10-26" and (loc.hour, loc.minute) in ((4, 0), (4, 29), (4, 31), (5, 0), (6, 0), (6, 59)) and loc.second < STEP_S:
            st = coord._snap.devices["b"]
            print(loc.strftime("%H:%M"), "soc", round(bat.soc, 1), "mode", bat.mode.value, bat.set_w, "| desired", st.desired, "|", st.reason, "|", coord.last_result.notes[:2])
        if loc.hour == 7 and loc.minute == 0 and loc.second < STEP_S:
            morning[loc.date().isoformat()] = round(bat.soc, 1)

    print("cycles", cycles, "| commands", bat.commands)
    print("SOC at 07:00:", morning)
    print("energy charged by clock hour (kWh):", {h: round(v / 1000, 2) for h, v in sorted(bat.charged_by_hour.items())})
    outside = sum(v for h, v in bat.charged_by_hour.items() if not (0 <= h < 5))
    inside = sum(v for h, v in bat.charged_by_hour.items() if 0 <= h < 5)
    print(f"charged inside the cheap window {inside / 1000:.1f} kWh, outside {outside / 1000:.2f} kWh")
    assert len(morning) == 3, morning
    assert all(v >= 88.4 for v in morning.values()), ("the goal was missed", morning)
    assert outside < 2500, ("charging outside the cheap window", outside)   # only the small top-ups that hold it at the target while the house draws on it
    assert bat.commands < 250, ("too many commands", bat.commands)
    assert not errors, errors[:3]
    await coord.async_close()
    print("E2E_MULTIDAY OK")


asyncio.run(main())
