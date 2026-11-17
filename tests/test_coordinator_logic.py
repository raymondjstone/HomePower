"""The coordinator's own logic, tested without Home Assistant by stubbing the few HA names it imports."""
import asyncio
import sys
import types
from datetime import datetime, timezone
from typing import Generic, TypeVar

import pytest


def _stub_homeassistant():
    if "homeassistant" in sys.modules:
        return
    T = TypeVar("T")
    mods = {n: types.ModuleType(n) for n in ("homeassistant", "homeassistant.core", "homeassistant.helpers",
                                             "homeassistant.helpers.update_coordinator", "homeassistant.util", "homeassistant.util.dt")}
    mods["homeassistant.core"].HomeAssistant = object

    class DataUpdateCoordinator(Generic[T]):
        def __init__(self, *a, **k):
            pass

    mods["homeassistant.helpers.update_coordinator"].DataUpdateCoordinator = DataUpdateCoordinator
    dt = mods["homeassistant.util.dt"]
    dt.now = lambda: datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    dt.UTC = timezone.utc
    mods["homeassistant.util"].dt = dt
    sys.modules.update(mods)


_stub_homeassistant()
coordinator = pytest.importorskip("hp.coordinator")

from hp.core.models import Actual, Capabilities, Desired, DeviceConfig, Mode  # noqa: E402
from hp.core.insights import PlanLog  # noqa: E402
from hp.core.sanity import CommandBudget, RateLearner  # noqa: E402
from hp.core.traces import CommandTrace  # noqa: E402

Coord = coordinator.HomePowerCoordinator


class FakeManager:
    def __init__(self):
        self.confirmed, self.tests, self.paused = set(), {}, False

    async def set_paused(self, on):
        self.paused = on


class FakeAdapter:
    def __init__(self, cfg, actual):
        self.config, self._actual, self.sent = cfg, actual, []

    async def apply(self, want):
        self.sent.append(want)

    async def read(self):
        return self._actual


def eco_cfg(power_control=False):
    caps = Capabilities(5000, 3000, 0, 10, frozenset({Mode.AUTO, Mode.HOLD, Mode.CHARGE}), power_control)
    return DeviceConfig("e", "Eco", "ecoflow", caps, extra={"control_enabled": True})


def bare(manager=None):
    c = Coord.__new__(Coord)
    c.manager = manager or FakeManager()
    c.rate_learner, c.traces, c.budget = RateLearner(), CommandTrace(), CommandBudget()
    c.plan_log = PlanLog()
    c._snap = coordinator.Snapshot()
    c.last_grid = 120.0
    c._log = lambda *a, **k: None
    c._set_release = lambda *a, **k: None
    c._rates = lambda: None
    c.settings = types.SimpleNamespace(dry_run=False, cooldown_s=120, rates_unit="main")
    return c


def test_effective_config_uses_the_measured_rate_and_drops_charge_that_does_not_work():
    c = bare()
    cfg = eco_cfg()
    assert c._effective_config(cfg) is cfg
    c.rate_learner.rates["e"] = 2700
    assert c._effective_config(cfg).caps.max_charge_w == 2700
    for _ in range(3):
        c.traces.start("e", "charge", 0, "t", 40, 0)
        c.traces.observe("e", 700, 40, 0, "hold")                     # never charged
    got = c._effective_config(cfg).caps
    assert Mode.CHARGE not in got.supported_modes and Mode.HOLD in got.supported_modes
    c.traces.reset("e")
    assert Mode.CHARGE in c._effective_config(cfg).caps.supported_modes
    anker = eco_cfg(power_control=True)
    assert c._effective_config(anker) is anker                       # batteries with a power setpoint are left alone


def test_the_first_command_to_an_unproven_battery_waits_for_the_user():
    async def go():
        c = bare()
        cfg = eco_cfg()
        a = FakeAdapter(cfg, Actual(online=True, soc=40, power_w=0, mode=Mode.CHARGE))
        st = coordinator.DeviceStatus(actual=a._actual, reason="schedule x")
        want = Desired(Mode.CHARGE, 2000, target_soc=80)
        await c._apply_and_verify(a, st, want, [])
        assert a.sent == [] and st.awaiting_confirm and st.last_error is None      # nothing sent, and not reported as a failure
        c.manager.confirmed.add("e")
        await c._apply_and_verify(a, st, want, [])
        assert a.sent == [want] and not st.awaiting_confirm
        assert st.last_command["why"] == "schedule x" and st.last_command["soc"] == 40   # the inputs behind it are kept
        assert "e" in c.traces._open                                                     # and its effect is being watched
    asyncio.run(go())


def test_proven_battery_needs_no_confirmation_and_handback_is_never_held_up():
    async def go():
        c = bare()
        c.manager.tests["e"] = {"ok": True}
        a = FakeAdapter(eco_cfg(), Actual(online=True, soc=40, power_w=0, mode=Mode.AUTO))
        st = coordinator.DeviceStatus(actual=a._actual)
        await c._apply_and_verify(a, st, Desired(Mode.HOLD), [])
        await c._apply_and_verify(a, st, Desired(Mode.AUTO), [])
        assert [w.mode for w in a.sent] == [Mode.HOLD, Mode.AUTO]
    asyncio.run(go())


def test_phone_notification_buttons_pause_and_hand_back():
    async def go():
        c = bare()
        c.release_all = lambda: asyncio.sleep(0)
        await c._on_action(types.SimpleNamespace(data={"action": coordinator.ACTION_PAUSE}))
        assert c.manager.paused
        c.manager.paused = False
        await c._on_action(types.SimpleNamespace(data={"action": coordinator.ACTION_HAND_BACK}))
        assert c.manager.paused                                       # handed back, and stays that way
        await c._on_action(types.SimpleNamespace(data={"action": "SOMETHING_ELSE"}))   # ignored
    asyncio.run(go())


def test_plan_is_recorded_for_the_chart():
    c = bare()
    st = coordinator.DeviceStatus(desired=Desired(Mode.CHARGE, 1500))
    c._snap.devices["e"] = st
    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    c._record_plan(now, {"e": Actual(online=True, power_w=1400)})
    st.desired = Desired(Mode.AUTO)
    c._record_plan(now.replace(minute=30), {"e": Actual(online=True, power_w=-200)})
    rows = {r[0][11:16]: r[1:] for r in c.plan_log.view("e", now.replace(minute=40))["rows"]}
    assert rows["12:00"] == [1500, 1400] and rows["12:30"] == [None, -200]
