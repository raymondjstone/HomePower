"""Second audit, state and numbers: NaN/inf never poison saved totals, junk saved data never stops start-up, a save does not
change what was learned, removed batteries leave nothing behind."""
import json
from datetime import datetime, timedelta, timezone

from hp.core.energy import EnergyMeter, Ledger
from hp.core.insights import DailyStats, Health, LoadProfile, UsageHistory
from hp.core.meter import MeterReader
from hp.core.models import Actual
from hp.core.strategies import num

UTC = timezone.utc


def test_nan_and_inf_readings_are_no_reading():
    assert num("nan") is None and num("inf") is None and num("-inf") is None and num("12.5") == 12.5
    m = MeterReader()
    assert m.read("a", "nan", "W", 0.0) is None and m.read("a", "inf", "kW", 1.0) is None and m.read("a", "300", "W", 2.0) == 300.0
    e = EnergyMeter()
    e.add("grid", 0.0, 100.0)
    for bad in (float("nan"), float("inf"), float("-inf")):
        assert e.add("grid", 10.0, bad) == (0.0, 0.0)
    assert all(v == v and abs(v) != float("inf") for v in e.totals.values())


def test_saved_nulls_and_nans_load_as_zero_not_as_a_crash():
    saved = json.loads(json.dumps({"grid_in": float("nan"), "x_out": None, "y_in": 2.5}))     # what a JSON store does to NaN
    assert EnergyMeter(saved).totals == {"grid_in": 0.0, "x_out": 0.0, "y_in": 2.5}
    lg = Ledger({"2026-11-01": {"saved": None, "bought": 1.5}, "bad": "x"}, {"a": [None, 0.3], "b": "junk", "c": [1.0, 2.0]})
    assert lg.days == {"2026-11-01": {"saved": 0.0, "bought": 1.5}} and lg.stores == {"a": [0.0, 0.3], "c": [1.0, 2.0]}
    assert DailyStats({"2026-11-01": {"pv": None, "export": 1.0}, "x": 5}).days == {"2026-11-01": {"pv": 0.0, "export": 1.0}}
    u = UsageHistory({"d1": {"imp": [None] * 48, "exp": [0.0] * 48}, "d2": {"imp": [0.0] * 48, "exp": [0.0] * 48}, "d3": "junk", "d4": {"imp": "x", "exp": 5}})
    assert list(u.days) == ["d2"]


def test_saving_does_not_change_what_was_learned():
    lp = LoadProfile()
    t0 = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    for day in range(2):                                    # two ordinary days at 2000 W: the slot is learned
        for s in range(10):
            lp.record(t0 + timedelta(days=day, seconds=s * 10), 2000.0)
    lp._fold()
    before = (list(lp.avg["wd"]), list(lp.n["wd"]))
    t = t0 + timedelta(days=2)
    for s in range(10):                                     # a third day at 1000 W, saved every few readings (as the 5-minute save does)
        lp.record(t + timedelta(seconds=s * 10), 1000.0)
        lp.dump()
    lp.record(t + timedelta(minutes=40), 1000.0)            # the half-hour ends: one visit is blended in, once
    i = 12 * 2
    assert lp.n["wd"][i] == before[1][i] + 1                # not one extra "visit" per save
    assert abs(lp.avg["wd"][i] - (before[0][i] + 0.25 * (1000.0 - before[0][i]))) < 1.0


def test_a_removed_battery_leaves_no_efficiency_history():
    h = Health()
    h.update("2026-W45", {"a": (10.0, 8.0), "b": (5.0, 4.0)})
    h.update("2026-W46", {"a": (20.0, 17.0), "b": (9.0, 8.0)})
    assert h.efficiency("a")
    h.forget("a")
    assert h.efficiency("a") == [] and "a" not in h.base and h.efficiency("b")
