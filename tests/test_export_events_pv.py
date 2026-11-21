"""Paid export, grid events, battery wear, solar string health and big-load suggestions (HA-free)."""
from datetime import datetime, time, timedelta, timezone

from core.engine import EVENT_REASON, EXPORT_REASON, DevIn, Settings, World, compute, export_pays
from core.loads import big_load_sensors
from core.models import Actual, Capabilities, Desired, DeviceConfig, Mode
from core.pv_health import DEAD_AFTER_S, PvHealth
from core.sanity import cycling_margin
from core.schedule import ScheduleEntry
from core.settings import SettingsError, build, clean

import pytest

UTC = timezone.utc
NOW = datetime(2026, 11, 10, 17, 0, tzinfo=UTC)


def dev(did="a", soc=80.0, modes=None, power_control=True, release=False, controllable=True, pv=0.0, min_soc=10):
    caps = Capabilities(10000, 3000, 3000, min_soc, frozenset(modes) if modes else frozenset(Mode), power_control)
    cfg = DeviceConfig(did, did, "x", caps, extra={"control_enabled": controllable})
    return DevIn(cfg, Actual(online=True, soc=soc, power_w=0.0, mode=Mode.AUTO, pv_w=pv), release_pending=release, controllable=controllable)


def world(devs, **kw):
    st = kw.pop("settings", Settings(export_discharge=True))
    return World(now=kw.pop("now", NOW), devices=devs, settings=st, **kw)


# ------------------------------------------------------------------ paid export
def test_export_pays_needs_to_beat_buyback_losses_and_wear():
    assert export_pays(0.30, 0.07)                       # 0.30 > 0.07 / 0.85
    assert not export_pays(0.08, 0.07)                   # 0.08 < 0.0824
    assert not export_pays(0.30, 0.07, wear=0.25)        # wear eats the margin
    assert not export_pays(None, 0.07) and not export_pays(0.3, None)
    assert export_pays(0.05, -0.02)                      # paid to import later: buying back costs nothing


def test_paid_export_discharges_when_it_pays_and_stops_at_the_keep_level():
    r = compute(world([dev(soc=80)], export_price_now=0.30, buyback_price=0.07, export_keep_pct=40))
    assert r.wants["a"].mode is Mode.DISCHARGE and r.wants["a"].power_w == 3000 and r.wants["a"].reserve_soc == 40
    assert r.reasons["a"].startswith(EXPORT_REASON)
    r = compute(world([dev(soc=41)], export_price_now=0.30, buyback_price=0.07, export_keep_pct=40))
    assert "a" not in r.wants or r.wants["a"].mode is not Mode.DISCHARGE      # too close to the keep level
    r = compute(world([dev(soc=80)], export_price_now=0.08, buyback_price=0.07))
    assert "a" not in r.wants                                                     # does not pay
    off = compute(world([dev(soc=80)], settings=Settings(), export_price_now=0.30, buyback_price=0.07))
    assert "a" not in off.wants                                                   # switched off


def test_paid_export_skips_batteries_that_cannot_be_told_a_power_and_respects_load_rules_and_schedules():
    eco = dev("e", power_control=False)
    r = compute(world([eco], export_price_now=0.30, buyback_price=0.07))
    assert "e" not in r.wants
    r = compute(world([dev()], export_price_now=0.30, buyback_price=0.07, load_blocks={"discharge"}))
    assert "a" not in r.wants
    sched = ScheduleEntry("s", frozenset(), time(16), time(18), Desired(Mode.CHARGE, 2000))
    r = compute(world([dev(soc=50)], schedules=[sched], export_price_now=0.30, buyback_price=0.07))
    assert r.wants["a"].mode is Mode.CHARGE and r.reasons["a"].startswith("schedule")
    r = compute(world([dev(controllable=False)], export_price_now=0.30, buyback_price=0.07))
    assert "a" not in r.wants


def test_paid_export_is_trimmed_by_the_export_limit():
    st = Settings(export_discharge=True, export_limit_w=1000)
    d = dev(soc=80)
    d.actual = Actual(online=True, soc=80, power_w=-3000.0, mode=Mode.DISCHARGE, set_power_w=3000)
    r = compute(world([d], settings=st, grid_w=-2500.0, export_price_now=0.30, buyback_price=0.07))
    assert r.wants["a"].mode in (Mode.DISCHARGE, Mode.HOLD) and (r.wants["a"].power_w or 0) < 3000
    assert "trimmed for export limit" in r.reasons["a"]


def test_wear_cost_stops_paid_export_in_the_engine():
    r = compute(world([dev()], settings=Settings(export_discharge=True, wear_cost=0.3), export_price_now=0.30, buyback_price=0.07))
    assert "a" not in r.wants


# ------------------------------------------------------------------ grid events
def test_grid_event_discharges_everything_that_can_and_overrides_schedules():
    sched = ScheduleEntry("s", frozenset(), time(16), time(18), Desired(Mode.CHARGE, 2000))
    eco = dev("e", power_control=False, release=True, modes={Mode.AUTO, Mode.HOLD, Mode.CHARGE})
    eco2 = dev("f", power_control=False, modes={Mode.AUTO, Mode.HOLD, Mode.CHARGE})
    r = compute(world([dev(soc=60), eco, eco2], settings=Settings(), schedules=[sched], grid_event=True))
    assert r.wants["a"].mode is Mode.DISCHARGE and r.reasons["a"] == EVENT_REASON and r.wants["a"].reserve_soc == 10
    assert r.wants["e"].mode is Mode.AUTO and r.reasons["e"].startswith(EVENT_REASON)    # handed back: its own logic powers the house
    assert "f" not in r.wants                                                            # not grid-charged by the schedule either


def test_grid_event_respects_overrides_backup_floor_and_does_not_hold_back():
    st = Settings(save_for_peak=True, backup_pct=50)
    r = compute(world([dev(soc=60)], settings=st, grid_event=True, overrides={"a": Desired(Mode.HOLD)}))
    assert r.wants["a"].mode is Mode.HOLD and r.reasons["a"] == "manual override"
    r = compute(world([dev(soc=11)], settings=st, grid_event=True, cheap_now=True))
    assert "a" not in r.wants                                       # at its floor: nothing to give, and no 'saving for peak' hold
    r = compute(world([dev(soc=40)], settings=st, grid_event=True, backup_active=True))
    assert r.reasons["a"] == "backup reserve"


# ------------------------------------------------------------------ wear in the 'is storing worth it' figure
def test_cycling_margin_counts_wear():
    a = cycling_margin(0.07, 0.30, 0.85)
    b = cycling_margin(0.07, 0.30, 0.85, wear=0.2)
    assert a["worth_it"] and not b["worth_it"]
    assert b["break_even_price"] == round(0.27 / 0.85, 4) and b["wear_per_kwh"] == 0.2


# ------------------------------------------------------------------ settings
def test_new_settings_validate_and_build():
    out = clean({"wear_cost": "6.7", "export_keep_pct": "30", "export_discharge": "on", "event_entity": " binary_sensor.saving ",
                 "export_rates_entity": "sensor.agile_export"})
    assert out == {"wear_cost": 6.7, "export_keep_pct": 30, "export_discharge": True, "event_entity": "binary_sensor.saving",
                   "export_rates_entity": "sensor.agile_export"}
    for bad in ({"wear_cost": -1}, {"export_keep_pct": 120}, {"wear_cost": "nan"}):
        with pytest.raises(SettingsError):
            clean(bad)
    s = build({"export_discharge": True, "wear_cost": 5, "export_keep_pct": 25})
    assert s.engine.export_discharge and s.wear_cost == 5 and s.export_keep_pct == 25
    d = build({})
    assert not d.engine.export_discharge and d.wear_cost is None and d.event_entity == ""


# ------------------------------------------------------------------ solar strings
def _strings(*watts, v=300.0):
    out = [(v, w / v) for w in watts] + [(0.0, 0.0)] * (8 - len(watts))
    return tuple(out)


def _sunny_day(pv, did, day, watts, t0):
    t = t0
    for _ in range(int(6 * 3600 / 60)):              # six hours of readings a minute apart
        pv.observe(did, day, _strings(*watts), t)
        t += 60
    return t


def test_pv_health_learns_used_strings_and_usual_shares():
    pv, t = PvHealth(), 0.0
    for d in range(1, 5):
        t = _sunny_day(pv, "x1", f"2026-06-0{d}", (1000, 1000, 500), t + 3600)
    st = pv.status("x1", "2026-06-05", t)
    assert [s["state"] for s in st[:4]] == ["ok", "ok", "ok", "unused"]
    assert st[0]["usual_share"] == 40.0 and st[2]["usual_share"] == 20.0
    assert pv.warnings("x1", "2026-06-05", t) == {}


def test_pv_health_flags_a_dead_string_after_half_an_hour():
    pv, t = PvHealth(), 0.0
    for d in range(1, 5):
        t = _sunny_day(pv, "x1", f"2026-06-0{d}", (1000, 1000, 500), t + 3600)
    t += 3600
    start = t
    while t - start < DEAD_AFTER_S + 120:
        pv.observe("x1", "2026-06-05", ((300.0, 1000 / 300), (300.0, 1000 / 300), (301.0, 0.0)) + ((0.0, 0.0),) * 5, t)
        t += 60
    w = pv.warnings("x1", "2026-06-05", t)
    assert list(w) == [3] and "PV3" in w[3]
    pv.observe("x1", "2026-06-05", _strings(1000, 1000, 500), t + 60)      # back again: clears at once
    assert pv.warnings("x1", "2026-06-05", t + 60) == {}


def test_pv_health_does_not_call_a_string_dead_in_poor_light_or_before_it_has_a_usual_share():
    pv, t = PvHealth(), 0.0
    for _ in range(60):                                  # never learned: no usual share
        pv.observe("x1", "2026-06-01", ((300.0, 3.0), (300.0, 0.0)) + ((0.0, 0.0),) * 6, t)
        t += 60
    assert pv.warnings("x1", "2026-06-01", t) == {}
    for d in range(2, 6):
        t = _sunny_day(pv, "x1", f"2026-06-0{d}", (1000, 1000), t + 3600)
    for _ in range(60):                                  # dusk: 150 W in all, under the threshold
        pv.observe("x1", "2026-06-06", ((300.0, 0.5), (300.0, 0.0)) + ((0.0, 0.0),) * 6, t)
        t += 60
    assert pv.warnings("x1", "2026-06-06", t) == {}


def test_pv_health_flags_a_string_weaker_than_usual_two_days_running():
    pv, t = PvHealth(), 0.0
    for d in range(1, 5):
        t = _sunny_day(pv, "x1", f"2026-06-0{d}", (1000, 1000), t + 3600)
    t = _sunny_day(pv, "x1", "2026-06-05", (1000, 400), t + 3600)
    assert pv.warnings("x1", "2026-06-06", t) == {}                 # one day is not enough
    t = _sunny_day(pv, "x1", "2026-06-06", (1000, 400), t + 3600)
    w = pv.warnings("x1", "2026-06-07", t)
    assert list(w) == [2] and "usual 50%" in w[2]


def test_pv_health_survives_damaged_saves_and_round_trips():
    pv = PvHealth({"x1": {"used": [True] * 8, "days": {"2026-06-01": [1.0] * 8, "bad": [None] * 8, "2026-06-02": ["nan"] * 8}},
                   "y": "junk", 5: {}, "z": {"used": [1, 2], "days": []}})
    assert pv.used == {"x1": [True] * 8} and list(pv.days["x1"]) == ["2026-06-01"]
    again = PvHealth(pv.dump())
    assert again.dump() == pv.dump()
    pv.forget("x1")
    assert pv.dump() == {}                               # "z" had nothing usable, so it was never kept
    pv.observe("x1", "2026-06-01", "junk", 0.0)
    pv.observe("x1", "2026-06-01", ((float("nan"), 1.0),) * 8, 0.0)
    assert "x1" not in pv.used


def test_pv_health_keeps_three_weeks():
    pv, t = PvHealth(), 0.0
    base = datetime(2026, 6, 1)
    for d in range(30):
        t = _sunny_day(pv, "x1", (base + timedelta(days=d)).strftime("%Y-%m-%d"), (500, 500), t + 3600) if d % 10 == 0 else t
        pv.days.setdefault("x1", {})[(base + timedelta(days=d)).strftime("%Y-%m-%d")] = [1000.0, 1000.0] + [0.0] * 6
    pv.observe("x1", "2026-07-01", _strings(500, 500), t + 3600)
    pv.observe("x1", "2026-07-01", _strings(500, 500), t + 3660)
    assert len(pv.days["x1"]) == 21


# ------------------------------------------------------------------ big loads
def test_big_load_sensors_finds_uncovered_chargers_and_heat_pumps():
    ents = [("sensor.zappi_power", "power", "W"), ("sensor.ohme_charger_power", None, "kW"), ("sensor.heat_pump_power", "power", "W"),
            ("sensor.zappi_energy_today", "energy", "kWh"), ("sensor.kitchen_power", "power", "W"), ("switch.zappi", None, None),
            ("sensor.evcc_charge_power", "power", "W"), ("sensor.evcc_charge_power_limit", "power", "W"), ("sensor.zappiish_power", "power", "W")]
    found = big_load_sensors(ents, [])
    assert found == ["sensor.evcc_charge_power", "sensor.heat_pump_power", "sensor.ohme_charger_power", "sensor.zappi_power"]
    covered = big_load_sensors(ents, [{"entity": "sensor.zappi_power", "block": "discharge"}, {"entity": "binary_sensor.heat_pump_running"}])
    assert covered == ["sensor.evcc_charge_power", "sensor.ohme_charger_power"]          # a rule on the same device counts
    assert big_load_sensors([], None) == [] and big_load_sensors([(None, None, None)], [{"x": 1}, "junk"]) == []


def test_ecoflow_solar_inputs_are_read_as_strings():
    from hp.drivers.ecoflow import EcoFlowStreamDriver
    q = {"plugInInfoPvVol": 61.2, "plugInInfoPvAmp": 8.1, "plugInInfoPv2Vol": "58.0", "plugInInfoPv2Amp": "7.5",
         "plugInInfoPv3Vol": "nan", "plugInInfoPv3Amp": 1.0, "plugInInfoPv4Vol": -3, "plugInInfoPv4Amp": 0}
    s = EcoFlowStreamDriver._strings(q)
    assert len(s) == 8 and s[0] == (61.2, 8.1) and s[1] == (58.0, 7.5) and s[2] == (0.0, 0.0) and s[3] == (0.0, 0.0)
    assert EcoFlowStreamDriver._strings({"soc": 50}) == ()
    pv = PvHealth()
    pv.observe("eco", "2026-06-01", s, 0.0)
    assert pv.used["eco"][:3] == [True, True, False]


def test_a_dead_string_stays_flagged_through_a_cloud_and_clears_only_when_it_produces():
    pv, t = PvHealth(), 0.0
    for d in range(1, 5):
        t = _sunny_day(pv, "x1", f"2026-06-0{d}", (1000, 1000, 500), t + 3600)
    t += 3600
    dead = ((300.0, 1000 / 300), (300.0, 1000 / 300), (301.0, 0.0)) + ((0.0, 0.0),) * 5
    for _ in range(35):
        pv.observe("x1", "2026-06-05", dead, t)
        t += 60
    assert list(pv.warnings("x1", "2026-06-05", t)) == [3]
    dull = ((300.0, 0.3), (300.0, 0.3), (301.0, 0.0)) + ((0.0, 0.0),) * 5        # 180 W in all: too dark to judge
    for _ in range(10):
        pv.observe("x1", "2026-06-05", dull, t)
        t += 60
    assert list(pv.warnings("x1", "2026-06-05", t)) == [3]                     # still flagged, not cleared by the cloud
    pv.observe("x1", "2026-06-05", _strings(1000, 1000, 500), t)
    assert pv.warnings("x1", "2026-06-05", t) == {}


def test_a_string_quiet_at_dusk_is_not_called_dead_through_the_night():
    """Only quiet time in good light counts: ten minutes of nothing at dusk, then darkness all night, is not 'dead'."""
    pv, t = PvHealth(), 0.0
    for d in range(1, 5):
        t = _sunny_day(pv, "x1", f"2026-06-0{d}", (1000, 1000, 500), t + 3600)
    t += 3600
    quiet = ((300.0, 1000 / 300), (300.0, 1000 / 300), (301.0, 0.0)) + ((0.0, 0.0),) * 5
    for _ in range(10):
        pv.observe("x1", "2026-06-05", quiet, t)
        t += 60
    dark = ((0.0, 0.0),) * 8
    for _ in range(600):                                   # ten hours of night
        pv.observe("x1", "2026-06-05", dark, t)
        t += 60
    assert pv.warnings("x1", "2026-06-05", t) == {}
    for _ in range(15):                                    # quiet again next morning: 9 + 14 counted minutes, still under half an hour
        pv.observe("x1", "2026-06-06", quiet, t)
        t += 60
    assert pv.warnings("x1", "2026-06-06", t) == {}
    for _ in range(10):
        pv.observe("x1", "2026-06-06", quiet, t)
        t += 60
    assert list(pv.warnings("x1", "2026-06-06", t)) == [3]
    assert "for 33 min" in pv.warnings("x1", "2026-06-06", t)[3]
