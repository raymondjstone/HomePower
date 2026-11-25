"""Removing HomePower takes the X1 import/export limits it wrote back off, putting back what each slot had before (here an
installer's 6000 W export limit that HomePower had tightened to 3000 W), and forgets them in the saved state. A limit
someone changed since is left alone. Runs the real removal code (_release_x1_limits_on_remove) against a simulated X1."""
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

import asyncio, tempfile
import conftest  # noqa: F401
import _harness  # noqa: F401 - Python 3.12+ server shutdown (see _harness.py)
from test_drivers import FakeAnker
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import custom_components.homepower as HP
from custom_components.homepower.manager import HomePowerManager


def export(dev):
    return dev.hold[10074], dev.hold[10075] << 16 | dev.hold[10076]


def imp(dev):
    return dev.hold[10077], dev.hold[10078] << 16 | dev.hold[10079]


async def main():
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/London"))
    tmp = tempfile.mkdtemp(); hass = HomeAssistant(tmp); hass.config.config_dir = tmp
    dev = FakeAnker(); srv = await asyncio.start_server(dev.handle, "127.0.0.1", 0); port = srv.sockets[0].getsockname()[1]

    # Saved state as HomePower leaves it: an X1 whose export slot HomePower tightened from the installer's 6000 W to 3000 W,
    # and whose import slot HomePower wrote (500 W) but someone has since changed in the app (to 800 W).
    m0 = HomePowerManager(hass); await m0.async_load(); m0.coordinator = None
    await m0.save_device({"device_id": "x1", "name": "X1", "kind": "anker_x1", "connection": {"host": "127.0.0.1", "port": port},
                          "control_enabled": True})
    dev.hold[10074], dev.hold[10075], dev.hold[10076] = 2, 0, 3000
    dev.hold[10077], dev.hold[10078], dev.hold[10079] = 2, 0, 800
    m0.event_state.update({"x1lim:x1:export:3000": True, "x1orig:x1:export:2:6000": True,
                           "x1lim:x1:import:500": True, "x1orig:x1:import:0:0": True, "unrelated_flag": True})
    await m0._save()

    await HP._release_x1_limits_on_remove(hass)
    print("export   :", export(dev), "| import", imp(dev))
    assert export(dev) == (2, 6000), "the installer's export limit is back"
    assert imp(dev) == (2, 800), "a limit changed since is not HomePower's: left alone"

    m1 = HomePowerManager(hass); await m1.async_load()
    left = sorted(k for k in m1.event_state if k.startswith(("x1lim:", "x1orig:")))
    print("saved    :", left, "| other flags kept:", "unrelated_flag" in m1.event_state, "| devices kept:", list(m1.devices))
    assert left == [] and "unrelated_flag" in m1.event_state and list(m1.devices) == ["x1"]

    # The X1 cannot be reached at removal: nothing is lost from the saved state, so it is still known to be HomePower's.
    m1.event_state.update({"x1lim:x1:export:3000": True, "x1orig:x1:export:2:6000": True})
    dev.hold[10076] = 3000
    await m1._save()
    srv.close(); await srv.wait_closed()
    await HP._release_x1_limits_on_remove(hass)
    m2 = HomePowerManager(hass); await m2.async_load()
    print("offline  :", sorted(k for k in m2.event_state if k.startswith(("x1lim:", "x1orig:"))))
    assert {"x1lim:x1:export:3000", "x1orig:x1:export:2:6000"} <= set(m2.event_state)
    await hass.async_stop(force=True)
    print("E2E_X1_RELEASE OK")

asyncio.run(main())
