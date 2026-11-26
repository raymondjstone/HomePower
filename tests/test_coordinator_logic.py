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
    dt.DEFAULT_TIME_ZONE = timezone.utc
    mods["homeassistant.util"].dt = dt
    sys.modules.update(mods)


_stub_homeassistant()
coordinator = pytest.importorskip("hp.coordinator")

from hp.core.models import Actual, Capabilities, Desired, DeviceConfig, Mode  # noqa: E402
from hp.core.insights import PlanLog  # noqa: E402
from hp.core.pv_health import PvHealth  # noqa: E402
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
    c.pv_health = PvHealth()
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
        forced = []

        async def release_all(force=False):
            forced.append(force)
        c.release_all = release_all
        await c._on_action(types.SimpleNamespace(data={"action": coordinator.ACTION_PAUSE}))
        assert c.manager.paused
        c.manager.paused = False
        await c._on_action(types.SimpleNamespace(data={"action": coordinator.ACTION_HAND_BACK}))
        assert c.manager.paused                                       # handed back, and stays that way
        assert forced == [True]                                       # even if HomePower was already paused
        await c._on_action(types.SimpleNamespace(data={"action": "SOMETHING_ELSE"}))   # ignored
    asyncio.run(go())


def test_plan_is_recorded_for_the_chart():
    c = bare()
    from hp.core.reconcile import Decision
    st = coordinator.DeviceStatus(desired=Desired(Mode.CHARGE, 1500))
    st.decision = Decision.OK                                     # it was told (sent, or already doing it)
    c._snap.devices["e"] = st
    watched = coordinator.DeviceStatus(desired=Desired(Mode.CHARGE, 1500))    # monitor-only / paused: decision None, sent nothing
    c._snap.devices["m"] = watched
    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    c._record_plan(now, {"e": Actual(online=True, power_w=1400), "m": Actual(online=True, power_w=0)})
    st.desired = Desired(Mode.AUTO)
    c._record_plan(now.replace(minute=30), {"e": Actual(online=True, power_w=-200)})
    rows = {r[0][11:16]: r[1:] for r in c.plan_log.view("e", now.replace(minute=40))["rows"]}
    assert rows["12:00"] == [1500, 1400] and rows["12:30"] == [None, -200]
    mrows = {r[0][11:16]: r[1:] for r in c.plan_log.view("m", now.replace(minute=40))["rows"]}
    assert mrows["12:00"] == [None, 0]                            # not scored against a plan it never got


def test_tariff_drift_alerts_only_when_the_history_disagrees_for_long_enough():
    from hp.core.alerts import AlertTracker
    from datetime import time as _t
    sent = []

    async def go():
        c = bare()
        c.alerts = AlertTracker()
        c.settings = types.SimpleNamespace(offpeak_start=_t(23, 30), offpeak_end=_t(7, 30))
        c._tariff = types.SimpleNamespace(start=_t(0, 30), end=_t(5, 30), days=6)

        async def notify(key, msg, raised):
            sent.append((key, raised))
        c._notify = notify
        await c._check_tariff_drift(0)
        assert sent == []                                                # just noticed: wait
        await c._check_tariff_drift(7 * 3600)
        assert sent == [("tariffdrift", True)]
        c._tariff = types.SimpleNamespace(start=_t(23, 30), end=_t(7, 30), days=6)
        await c._check_tariff_drift(8 * 3600)
        assert sent[-1] == ("tariffdrift", False)                        # resolved when it matches again
    asyncio.run(go())


def test_startup_note_says_what_was_restored_and_what_is_unreachable():
    c = bare()
    c.manager.ecoflow_orig, c.manager.forced = {"e": {}}, set()
    c.rate_learner.rates["e"] = 2800
    c.adapters = [FakeAdapter(eco_cfg(), None)]
    c._startup_check({"e": Actual(online=False)})
    assert "1 saved hand-back settings" in c.startup_note and "Could not reach: Eco" in c.startup_note


def test_release_all_when_paused_only_with_force():
    async def go():
        c = bare()
        c.manager.paused = True
        a = FakeAdapter(eco_cfg(), Actual(online=True, soc=40))
        c.adapters = [a]
        c._snap.devices["e"] = coordinator.DeviceStatus(release_pending=True)

        async def save():
            pass
        c.manager.save_runtime = save
        saved = []

        async def save2():
            saved.append(1)
        c.manager.save_runtime = save2
        await c.release_all()
        assert a.sent == [] and saved == [1]                          # paused: left alone, but the running totals are still saved
        await c.release_all(force=True)
        assert [w.mode for w in a.sent] == [Mode.AUTO]                # safe mode: handed back anyway
    asyncio.run(go())


def test_handing_back_ends_the_command_watch_so_a_short_charge_is_not_judged_a_failure():
    async def go():
        c = bare()
        c.manager.confirmed.add("e")
        a = FakeAdapter(eco_cfg(), Actual(online=True, soc=40, power_w=0, mode=Mode.AUTO))
        st = coordinator.DeviceStatus(actual=a._actual)
        await c._apply_and_verify(a, st, Desired(Mode.CHARGE, 2000, target_soc=80), [])
        assert "e" in c.traces._open
        await c._apply_and_verify(a, st, Desired(Mode.AUTO), [])
        assert "e" not in c.traces._open and c.traces.results.get("e", []) == []
    asyncio.run(go())


def test_stuck_and_grid_sign_alerts_clear_when_the_battery_goes_offline():
    async def go():
        from hp.core.alerts import AlertTracker
        from hp.core.sanity import ChargeWatch
        c = bare()
        c.alerts, c.charge_watch, c.testing, c._event_state = AlertTracker(), ChargeWatch(after_s=60), set(), {}
        c.health = types.SimpleNamespace(degraded=lambda d: None)
        c.grid_events = types.SimpleNamespace(observe=lambda *a: None)
        c.trend = types.SimpleNamespace(update=lambda *a: None, warning=lambda d: None)
        c._cover_text = lambda: ""
        c.last_grid = 900.0
        sent = []

        async def notify(key, msg, raised):
            sent.append((key, raised))
        c._notify = notify
        cfg = eco_cfg()
        cfg.extra["driver"] = "ecoflow_stream"
        a = FakeAdapter(cfg, None)
        c.adapters = [a]
        st = coordinator.DeviceStatus(desired=Desired(Mode.CHARGE, 2000, target_soc=90), reason="schedule x",
                                      actual=Actual(online=True, soc=40, power_w=0, grid_w=-800.0), decision=coordinator.Decision.OK)
        c._snap.devices["e"] = st
        c.manager.paused = True                                       # paused: nothing is sent, so nothing can be "stuck"
        await c._check_alerts(0, [a])
        await c._check_alerts(120, [a])
        assert ("stuck:e", True) not in sent
        c.manager.paused = False
        c.charge_watch.forget("e")
        await c._check_alerts(300, [a])
        await c._check_alerts(420, [a])
        assert ("stuck:e", True) in sent                              # asked to charge, nothing happened for two minutes
        st.actual = Actual(online=False)
        await c._check_alerts(540, [a])
        assert ("stuck:e", False) in sent and "stuck:e" not in c.alerts.active()
    asyncio.run(go())


def test_scrub_removes_keys_always_and_addresses_and_serials_when_strict():
    c = bare()
    cfg = eco_cfg()
    cfg.extra["connection"] = {"sn": "BKSERIAL0001", "access_key": "AKSECRET9876", "secret_key": "SKSECRET5432", "host": "nas.local"}
    c.adapters = [FakeAdapter(cfg, None)]
    c.manager.devices = {"old": {"connection": {"host": "10.9.9.9", "access_key": "OLDKEY12345"}}, "bad": "junk"}   # one that failed to build
    text = "err BKSERIAL0001 AKSECRET9876 SKSECRET5432 nas.local 10.1.2.3:80 OLDKEY12345 10.9.9.9 v1.2.3.4x"
    easy = c.scrub({"a": [text, ("t", text)]})
    flat = str(easy)
    assert "AKSECRET9876" not in flat and "SKSECRET5432" not in flat and "OLDKEY12345" not in flat    # keys: always
    assert "BKSERIAL0001" in flat and "10.1.2.3" in flat                                               # the user's own screen keeps these
    hard = c.scrub({"a": [text, ("t", text)]}, strict=True)
    flat = str(hard)
    for s in ("BKSERIAL0001", "AKSECRET9876", "SKSECRET5432", "nas.local", "10.1.2.3", "OLDKEY12345", "10.9.9.9"):
        assert s not in flat, s
    assert "v1.2.3.4x" in flat                                                                          # a version number is not an address
    assert c.scrub(5, strict=True) == 5 and c.scrub(None) is None


def test_auto_price_unit_is_decided_by_the_whole_table():
    from datetime import datetime, timedelta, timezone
    from hp.core.rates import RateTable, Slot, to_main_units
    t0 = datetime(2026, 11, 18, 0, 0, tzinfo=timezone.utc)
    slots = [Slot(t0 + timedelta(minutes=30 * i), t0 + timedelta(minutes=30 * (i + 1)), p) for i, p in enumerate([3.5, 12.0, 30.0, 18.0])]   # pence, one slot under 5p
    me = types.SimpleNamespace(settings=types.SimpleNamespace(rates_unit="auto", export_price=4.1))
    unit = coordinator.HomePowerCoordinator._unit(me, RateTable(slots))
    assert unit == "pence" and to_main_units(3.5, unit) == 0.035 and abs(to_main_units(4.1, unit) - 0.041) < 1e-9
    pounds = [Slot(t0, t0 + timedelta(minutes=30), 0.035), Slot(t0 + timedelta(minutes=30), t0 + timedelta(minutes=60), 0.30)]
    assert coordinator.HomePowerCoordinator._unit(types.SimpleNamespace(settings=types.SimpleNamespace(rates_unit="auto", export_price=0.04)), RateTable(pounds)) == "main"
    assert coordinator.HomePowerCoordinator._unit(types.SimpleNamespace(settings=types.SimpleNamespace(rates_unit="pence", export_price=None)), None) == "pence"


def test_a_device_reading_with_nan_is_cleaned_before_the_engine_sees_it():
    a = coordinator.HomePowerCoordinator._finite(Actual(online=True, soc=50.0, power_w=float("inf"), grid_w=float("nan"), pv_w=100.0))
    assert a.soc == 50.0 and a.power_w is None and a.grid_w is None and a.pv_w == 100.0


def test_export_price_comes_from_the_export_sensor_then_the_fixed_price_and_counts_in_the_unit():
    from datetime import datetime, timedelta, timezone
    from hp.core.rates import RateTable, Slot
    t0 = datetime(2026, 11, 18, 16, 0, tzinfo=timezone.utc)
    c = bare()
    c.settings = types.SimpleNamespace(rates_unit="auto", export_price=15.0, wear_cost=6.0, export_keep_pct=20)
    imp = RateTable([Slot(t0 + timedelta(minutes=30 * i), t0 + timedelta(minutes=30 * (i + 1)), p) for i, p in enumerate([30.0, 30.0, 7.0, 7.0])])
    c._exp_table = None
    assert c._export_price_now(imp, t0) == 0.15                     # the fixed export price, pence -> pounds
    c._exp_table = RateTable([Slot(t0, t0 + timedelta(minutes=30), 28.0)])
    assert c._export_price_now(imp, t0) == 0.28                     # the export sensor's slot wins
    assert c._export_price_now(imp, t0 + timedelta(hours=1)) == 0.15  # outside its slots: the fixed price again
    assert abs(c._wear(imp) - 0.06) < 1e-9 and c._buyback_price(imp, t0) == 0.07
    c.settings.wear_cost = None
    assert c._wear(imp) == 0.0


def test_export_keep_level_covers_the_usual_use_until_the_cheapest_price():
    from datetime import datetime, timedelta, timezone
    from hp.core.rates import RateTable, Slot
    t0 = datetime(2026, 11, 18, 16, 0, tzinfo=timezone.utc)
    c = bare()
    c.settings = types.SimpleNamespace(rates_unit="main", export_price=None, export_keep_pct=20)
    c.adapters = [FakeAdapter(eco_cfg(power_control=True), None)]          # 5 kWh, minimum 10 %
    rates = RateTable([Slot(t0 + timedelta(hours=i), t0 + timedelta(hours=i + 1), 0.07 if i == 4 else 0.3) for i in range(6)])
    c.profile = types.SimpleNamespace(expected_wh=lambda start, hours: 500.0 * hours)   # 0.5 kW until the cheap slot 4 h away
    assert c._export_keep_pct(rates, t0) == 10 + 40                        # 2 kWh of 5 kWh, above the battery's minimum
    c.profile = types.SimpleNamespace(expected_wh=lambda start, hours: None)          # not learned yet: the setting holds
    assert c._export_keep_pct(rates, t0) == 20
    assert c._export_keep_pct(None, t0) == 20


def test_lasting_problems_get_stable_repair_ids_and_fall_back_to_notifications_without_repairs():
    a, b = coordinator.repair_id("offline:garage"), coordinator.repair_id("offline:garage_2")
    assert a != b and a == coordinator.repair_id("offline:garage") and a.startswith("offline_garage_")
    assert set(coordinator.repair_id("alarm:x1:Meter 1 comms failure (A6)")) <= set("abcdefghijklmnopqrstuvwxyz0123456789_")
    c = bare()
    c.hass = None
    assert c._repair("offline:garage", "Garage is offline", True) is False        # Repairs not available here: notify instead
    assert c._repair("summary_20261118", "daily summary", True) is False           # not a lasting problem


def test_load_rules_read_kilowatt_sensors_in_watts():
    c = bare()
    st = {"sensor.ohme_power": types.SimpleNamespace(state="7.2", attributes={"unit_of_measurement": "kW"}),
          "sensor.heat_pump_power": types.SimpleNamespace(state="900", attributes={"unit_of_measurement": "W"}),
          "sensor.broken": types.SimpleNamespace(state="unavailable", attributes={"unit_of_measurement": "kW"}),
          "sensor.nan": types.SimpleNamespace(state="nan", attributes={})}
    c.hass = types.SimpleNamespace(states=types.SimpleNamespace(get=st.get))
    c.settings = types.SimpleNamespace(load_rules=[{"entity": "sensor.ohme_power", "block": "discharge", "above_w": 1000.0},
                                                   {"entity": "sensor.heat_pump_power", "block": "charge", "above_w": 1000.0},
                                                   {"entity": "sensor.broken", "block": "both", "above_w": 1.0},
                                                   {"entity": "sensor.nan", "block": "both", "above_w": 1.0}])
    assert c._load_blocks() == {"discharge"}                 # 7.2 kW is busy; 900 W is not; unreadable ones are not


def test_grid_event_ignores_an_all_day_calendar_entry():
    c = bare()
    st = {"calendar.saving": types.SimpleNamespace(state="on", attributes={"all_day": True, "start_time": "2026-03-01 00:00:00", "end_time": "2026-03-02 00:00:00"}),
          "binary_sensor.octo": types.SimpleNamespace(state="off", attributes={})}
    c.hass = types.SimpleNamespace(states=types.SimpleNamespace(get=st.get))
    c.settings = types.SimpleNamespace(event_entity="calendar.saving, binary_sensor.octo")
    assert c._grid_event_on() is False
    st["calendar.saving"] = types.SimpleNamespace(state="on", attributes={"start_time": "2026-03-01 11:30:00", "end_time": "2026-03-01 12:30:00"})
    assert c._grid_event_on() is True
    st["calendar.saving"] = types.SimpleNamespace(state="off", attributes={})
    st["binary_sensor.octo"] = types.SimpleNamespace(state="on", attributes={})
    assert c._grid_event_on() is True


def test_a_failed_read_shows_stale_readings_until_the_offline_after_time():
    """Another Modbus client holding the battery for a moment must not make it look offline: the last reading is shown,
    marked stale, until reads have failed for the offline-after time (or the network says the battery is not there)."""
    c = bare()
    c.settings.offline_after_min = 5
    c._began = 0.0
    st = c._snap.devices["e"] = coordinator.DeviceStatus()
    good = Actual(online=True, soc=55.0, power_w=-300.0)
    c._note_link(st, good, 1000.0)
    st.actual = good
    assert c.link("e") == "online" and c.shown_actual("e") is good

    st.actual = Actual(online=False)
    c._note_link(st, st.actual, 1000.0 + 299)
    assert c.link("e") == "stale" and c.shown_actual("e") is good and c.shown_actuals()["e"] is good
    assert st.ok_mono == 1000.0                           # a failed read does not move the last good read on

    c._note_link(st, st.actual, 1000.0 + 300)
    assert c.link("e") == "offline" and c.shown_actual("e") is None

    c._note_link(st, good, 1400.0)                        # answers again
    assert c.link("e") == "online"
    st.actual = Actual(online=False, gone=True)           # the network says it is not there: offline at once
    c._note_link(st, st.actual, 1401.0)
    assert c.link("e") == "offline"
    st.actual = Actual(online=False)                      # next cycle a time-out instead: still offline, not back to stale
    c._note_link(st, st.actual, 1402.0)
    assert c.link("e") == "offline"


def test_a_battery_never_read_since_start_up_waits_before_showing_offline():
    c = bare()
    c.settings.offline_after_min = 1
    c._began = 500.0
    st = c._snap.devices["e"] = coordinator.DeviceStatus(actual=Actual(online=False))
    c._note_link(st, st.actual, 530.0)
    assert c.link("e") == "stale" and c.shown_actual("e") is None    # nothing to show yet, but not called offline
    c._note_link(st, st.actual, 560.0)
    assert c.link("e") == "offline"


def test_a_battery_added_but_not_read_yet_is_waiting_not_offline():
    c = bare()
    c._snap.devices["e"] = coordinator.DeviceStatus()
    assert c.link("e") == "stale" and c.shown_actual("e") is None
    assert c.link("missing") == "offline"


def test_x1_limit_state_round_trips_and_drops_originals_once_the_limit_is_gone():
    from hp.coord_base import x1_limits_keep, x1_limits_take
    state = {"x1lim:x1:export:3000": True, "x1orig:x1:export:2:6000": True, "x1lim:other:import:5": True, "unrelated": True}
    mine, orig = x1_limits_take(state, "x1")
    assert mine == {"export": 3000} and orig == {"export": [2, 6000]}
    assert set(state) == {"x1lim:other:import:5", "unrelated"}              # another battery's are left alone
    x1_limits_keep(state, "x1", mine, orig)
    assert x1_limits_take(dict(state), "x1") == (mine, orig)
    x1_limits_take(state, "x1")
    x1_limits_keep(state, "x1", {}, {"export": [2, 6000]})                    # HomePower's limit gone: no stray original kept
    assert not any(k.startswith(("x1lim:x1:", "x1orig:x1:")) for k in state)


def test_x1_limits_are_taken_off_when_the_option_is_switched_off():
    c = bare()
    calls = []

    class Drv:
        async def set_grid_limits(self, imp, exp, mine, orig):
            calls.append((imp, exp, dict(mine), dict(orig)))
            mine.clear(); orig.clear()
            return ["export limit back to 6000 W"]

    cfg = DeviceConfig("x1", "X1", "anker_x1", Capabilities(10000, 3000, 3000, 10), extra={"house_meter": True, "control_enabled": True})
    a = types.SimpleNamespace(config=cfg, driver=Drv())
    c._snap.devices["x1"] = coordinator.DeviceStatus(actual=Actual(online=True, soc=50))
    c._event_state = {"x1lim:x1:export:3000": True, "x1orig:x1:export:2:6000": True}
    c._last_limit_sync = -1e9                                                 # (not paused: FakeManager)
    c.settings = types.SimpleNamespace(x1_grid_limits=False, dry_run=False, engine=types.SimpleNamespace(import_limit_w=None, export_limit_w=3000))
    asyncio.run(c._sync_x1_limits(1000.0, [a]))
    assert calls == [(None, None, {"export": 3000}, {"export": [2, 6000]})]  # option off: HomePower's limit taken off
    assert not any(k.startswith(("x1lim:x1", "x1orig:x1")) for k in c._event_state)
    calls.clear()
    c._last_limit_sync = -1e9
    asyncio.run(c._sync_x1_limits(2000.0, [a]))
    assert calls == []                                                        # nothing of HomePower's left: nothing done


def test_deleting_an_x1_takes_its_limits_off_and_forgets_them_even_if_unreachable():
    c = bare()
    c.hass = object()
    calls = []

    class Drv:
        def __init__(self, fail):
            self.fail = fail

        async def set_grid_limits(self, imp, exp, mine, orig):
            calls.append((imp, exp, dict(mine), dict(orig)))
            if self.fail:
                raise OSError("no route to host")
            mine.clear(); orig.clear()
            return ["export limit back to 6000 W"]

    cfg = DeviceConfig("x1", "X1", "anker_x1", Capabilities(10000, 3000, 3000, 10), extra={"house_meter": True})
    for fail in (False, True):
        calls.clear()
        c.adapters = [types.SimpleNamespace(config=cfg, driver=Drv(fail))]
        c._event_state = {"x1lim:x1:export:3000": True, "x1orig:x1:export:2:6000": True, "x1lim:other:import:5": True}
        shown = []
        fake = types.ModuleType("homeassistant.components.persistent_notification")
        fake.async_create = lambda hass, message, **k: shown.append((message, k.get("notification_id")))
        comps = types.ModuleType("homeassistant.components")
        comps.persistent_notification = fake
        saved = {m: sys.modules.get(m) for m in ("homeassistant", "homeassistant.components", "homeassistant.components.persistent_notification")}
        sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
        sys.modules["homeassistant.components"], sys.modules["homeassistant.components.persistent_notification"] = comps, fake
        try:
            asyncio.run(c.release_x1_limits("x1"))
        finally:
            for m, v in saved.items():
                if v is None:
                    sys.modules.pop(m, None)
                else:
                    sys.modules[m] = v
        assert calls == [(None, None, {"export": 3000}, {"export": [2, 6000]})]
        assert set(c._event_state) == {"x1lim:other:import:5"}       # the deleted battery's records go; another's stay
        # unreachable: nothing will retry once it is deleted, so the user is told (not only the log)
        assert [n for _m, n in shown] == (["homepower_x1_limits_x1"] if fail else []) and all("Anker app" in m for m, _n in shown)


def test_a_lasting_failure_in_the_control_loop_is_logged_once_not_every_cycle(caplog):
    import logging
    c = bare()
    c._logged_failures = {}
    caplog.set_level(logging.DEBUG)

    def fail(msg):
        try:
            raise ValueError(msg)
        except ValueError:
            c._log_failure("engine", "decision engine failed")
    for _ in range(5):
        fail("boom")
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and errors[0].exc_info                       # once, with its traceback; the rest at debug
    fail("a different problem")
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 2   # a new failure is logged at once


def test_one_battery_that_cannot_be_shown_does_not_blank_the_view_for_the_others():
    c = bare()
    c._logged_failures = {}
    caps = Capabilities(5000, 3000, 3000, 10)
    good, bad = (types.SimpleNamespace(config=DeviceConfig(d, d.upper(), "anker_e5000_pro", caps, extra={})) for d in ("good", "bad"))
    c.adapters, c.schedules, c.overrides = [good, bad], [], {}
    c._snap.devices["good"] = coordinator.DeviceStatus(actual=Actual(online=True, soc=55.0))
    c._snap.devices["bad"] = coordinator.DeviceStatus(actual=Actual(online=True, soc=40.0))
    c.settings = types.SimpleNamespace(dry_run=False, offline_after_min=5)
    c.manager.pv_seen = set()
    c._has_solar = lambda cfg: False
    c._condition_ok = lambda e: True

    def pv_view(did):
        if did == "bad":
            raise ValueError("odd solar string data")
        return None
    c.pv_view = pv_view
    out = c.devices_view()
    assert out["good"]["online"] and out["good"]["soc"] == 55.0 and "view_error" not in out["good"]
    assert out["bad"] == {**out["bad"], "online": False, "view_error": True}


def test_one_battery_whose_alerts_fail_does_not_silence_the_others(monkeypatch):
    from hp.core.alerts import AlertTracker
    c = bare()
    c._logged_failures, c.testing, c.overrides = {}, set(), {}
    caps = Capabilities(5000, 3000, 3000, 10)
    good, bad = (types.SimpleNamespace(config=DeviceConfig(d, d.upper(), kind, caps, extra={}))
                 for d, kind in (("good", "anker_e5000_pro"), ("bad", "anker_x1")))
    for d in ("good", "bad"):
        c._snap.devices[d] = coordinator.DeviceStatus(actual=Actual(online=False), link="offline")
    c.alerts = AlertTracker()
    from hp.core.insights import GridEvents, Health, HealthTrend
    from hp.core.sanity import ChargeWatch
    c.charge_watch, c.health, c.grid_events, c.trend, c._event_state = ChargeWatch(), Health({}), GridEvents([]), HealthTrend({}), {}
    c._cover_text = lambda: ""
    c.settings = types.SimpleNamespace(dry_run=False)
    c.manager.pv_seen = set()
    sent = []

    async def notify(key, msg, on):
        sent.append(key)
    c._notify = notify
    import hp.coord_alerts as ca

    def hint(kind):
        if kind == "anker_x1":
            raise ValueError("a bug for this kind of battery")
        return None
    monkeypatch.setattr(ca, "offline_hint", hint)
    asyncio.run(c._check_alerts(10_000.0, [bad, good]))
    asyncio.run(c._check_alerts(10_070.0, [bad, good]))          # the offline alert waits a minute
    assert "offline:good" in c.alerts.active() and "offline:bad" not in c.alerts.active()
    assert set(c._logged_failures) == {"alerts bad"}                           # only the faulty battery failed


def test_batteries_are_handed_back_all_at_once_at_shutdown():
    import time as _t
    c = bare()
    c._retry_handback = set()
    caps = Capabilities(5000, 3000, 3000, 10)
    done = []

    class Slow:
        def __init__(self, did):
            self.config = DeviceConfig(did, did, "anker_e5000_pro", caps, extra={})

        async def apply(self, desired):
            await asyncio.sleep(0.3)                     # each battery takes a while to answer
            done.append(self.config.device_id)
    c.adapters = [Slow("a"), Slow("b")]
    for d in ("a", "b"):
        c._snap.devices[d] = coordinator.DeviceStatus(release_pending=True)

    async def save():
        pass
    c.manager.save_runtime = save
    t0 = _t.monotonic()
    asyncio.run(c.release_all(force=True))
    assert sorted(done) == ["a", "b"] and _t.monotonic() - t0 < 0.55      # together, not 0.3 s each one after the other


def test_hand_back_waits_for_a_command_already_going_out_and_closing_stops_later_ones():
    c = bare()
    c._retry_handback = set()
    caps = Capabilities(5000, 3000, 3000, 10)
    order = []

    class Bat:
        config = DeviceConfig("a", "a", "anker_e5000_pro", caps, extra={})

        async def apply(self, desired):
            order.append(desired.mode.name)
    c.adapters = [Bat()]
    c._snap.devices["a"] = coordinator.DeviceStatus(release_pending=True)

    async def save():
        pass
    c.manager.save_runtime = save

    async def cycle_sending():                     # a cycle's command phase, mid-way through a slow command
        async with c._send_lock():
            await asyncio.sleep(0.2)
            order.append("FORCE")

    async def go():
        t = asyncio.create_task(cycle_sending())
        await asyncio.sleep(0.05)
        await c.release_all(closing=True)
        await t
    asyncio.run(go())
    assert order == ["FORCE", "AUTO"], order          # the hand-back comes last, so the battery is left in its own control
    assert c._closing                                 # and no later cycle sends anything


def test_hand_back_button_and_safe_mode_pause_before_handing_back():
    # paused first: a cycle running in between must not force a battery again after it was handed back
    c = bare()
    seen = []

    async def release_all(force=False, closing=False):
        seen.append(("release", c.manager.paused))
    c.release_all = release_all

    async def set_paused(p):
        c.manager.paused = p
        seen.append(("pause", p))
    c.manager.set_paused = set_paused
    c.manager.paused = False
    c._log = lambda *a, **k: None
    asyncio.run(c._on_action(types.SimpleNamespace(data={"action": coordinator.ACTION_HAND_BACK})))
    assert seen == [("pause", True), ("release", True)], seen


def test_manual_overrides_are_kept_over_a_restart_and_damaged_or_naive_ones_dropped():
    from datetime import timedelta
    from hp.core.models import Desired, Mode
    c = bare()
    until = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    c.overrides = {"a": (Desired(Mode.CHARGE, 1500, 80), until), "b": (Desired(Mode.HOLD), until + timedelta(hours=1))}
    saved = c.dump_overrides()
    import json
    back = coordinator.HomePowerCoordinator._load_overrides(json.loads(json.dumps(saved)))
    assert back == c.overrides
    damaged = {"x": {"mode": "fly"}, "y": "nonsense", "z": {"mode": "hold", "until": "2026-06-01T12:00:00"},   # no time zone
               "w": {"mode": "charge", "until": "soon"}, "v": {"mode": "charge", "power_w": "lots", "until": until.isoformat()}}
    assert coordinator.HomePowerCoordinator._load_overrides({**saved, **damaged}) == c.overrides
    assert coordinator.HomePowerCoordinator._load_overrides(None) == {} and coordinator.HomePowerCoordinator._load_overrides([1]) == {}


def test_an_override_naming_a_monitor_only_battery_is_refused_not_silently_ignored():
    c = bare()
    caps = Capabilities(5000, 3000, 3000, 10)
    c.adapters = [types.SimpleNamespace(config=DeviceConfig("a", "Garage", "anker_e5000_pro", caps, extra={})),
                  types.SimpleNamespace(config=DeviceConfig("b", "Shed", "anker_e5000_pro", caps, extra={"control_enabled": False}))]
    c.overrides, c._log = {}, (lambda *a, **k: None)
    with pytest.raises(ValueError, match="Shed is monitor-only"):
        c.set_override(["a", "b"], Desired(Mode.HOLD), 10)
    assert c.overrides == {}                                   # nothing half applied
    c.set_override(None, Desired(Mode.HOLD), 10)               # "all batteries" still works: the monitor-only one is just not sent anything
    c.set_override(["a"], Desired(Mode.HOLD), 10)
    assert set(c.overrides) == {"a", "b"}
    c.overrides = {}
    c.manager.safe = True
    with pytest.raises(ValueError, match="Safe mode is on"):
        c.set_override(["a"], Desired(Mode.CHARGE), 10)
    assert c.overrides == {}
    c.manager.safe = False


def test_a_forecast_with_a_plane_missing_is_not_taken_as_the_days_total():
    day, nxt = "2026-11-11", "2026-11-12"
    values = {"a_today": 4.0, "b_today": 6.0, "a_tom": 5.0, "b_tom": 7.0}
    me = types.SimpleNamespace(
        manager=types.SimpleNamespace(forecast_days={}), _forecast_kwh=lambda: None, _kwh=values.get,
        _forecast_entities=lambda: {"today": ["a_today", "b_today"], "tomorrow": ["a_tom", "b_tom"]})
    ask = lambda: coordinator.HomePowerCoordinator._forecast_as_given(me, datetime(2026, 11, 11, 10, tzinfo=timezone.utc))
    values["b_today"] = values["b_tom"] = None
    assert ask() == {} and me.manager.forecast_days == {}          # half a forecast is not remembered as the whole
    values.update(b_today=6.0, b_tom=7.0)
    assert ask() == {day: 10.0, nxt: 12.0}
    values["b_today"] = values["b_tom"] = None                     # one plane drops out: the last whole figures stand
    assert ask() == {day: 10.0, nxt: 12.0}
