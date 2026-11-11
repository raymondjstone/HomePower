from datetime import datetime, time, timedelta, timezone

from core.meter import MeterReader
from core.rates import infer_offpeak

UTC = timezone.utc


def test_power_units():
    m = MeterReader()
    assert m.read("g", "1.5", "kW", 0) == 1500
    assert m.read("g", "800", "W", 0) == 800
    assert m.read("g", "800", None, 0) == 800
    assert m.read("g", "unavailable", "W", 0) is None


def test_energy_counter_becomes_power():
    m = MeterReader()
    assert m.read("g", "100.000", "kWh", 0) is None          # first sight
    w = m.read("g", "100.010", "kWh", 30)                    # 10 Wh in 30 s = 1200 W
    assert round(w) == 1200
    assert round(m.read("g", "100.010", "kWh", 50)) == 1200   # held between counter updates
    assert m.read("g", "100.010", "kWh", 200) == 0.0          # still for >2 steps: no power
    assert m.read("g", "5.0", "kWh", 230) is None             # counter reset
    assert round(m.read("g", "5.005", "kWh", 260)) == 600


def _go(days=7, tz=UTC):
    """Octopus-Go-like: 7p from 00:30 to 05:30, 30p otherwise."""
    start = datetime(2026, 11, 1, 0, 0, tzinfo=tz)
    out = []
    for d in range(days):
        day = start + timedelta(days=d)
        out += [(day + timedelta(minutes=30), 7.0), (day + timedelta(hours=5, minutes=30), 30.0)]
    return [(start, 30.0)] + out, start + timedelta(days=days)


def test_infer_night_window():
    s, until = _go()
    t = infer_offpeak(s, until)
    assert t and t.start == time(0, 30) and t.end == time(5, 30)
    assert t.offpeak == 7.0 and t.peak == 30.0


def test_infer_window_across_midnight():
    start = datetime(2026, 11, 1, tzinfo=UTC)
    s = [(start, 30.0)]
    for d in range(5):
        day = start + timedelta(days=d)
        s += [(day + timedelta(hours=23, minutes=30), 8.0), (day + timedelta(days=1, hours=5, minutes=30), 30.0)]
    t = infer_offpeak(s, start + timedelta(days=5))
    assert t.start == time(23, 30) and t.end == time(5, 30)


def test_infer_rejects_flat_short_and_dynamic():
    start = datetime(2026, 11, 1, tzinfo=UTC)
    assert infer_offpeak([(start, 30.0), (start + timedelta(hours=3), 30.0)], start + timedelta(days=3)) is None   # flat
    s, until = _go(days=1)
    assert infer_offpeak(s, start + timedelta(hours=10)) is None                                                    # too short
    import random
    rng = random.Random(1)
    dyn = [(start + timedelta(minutes=30 * i), rng.uniform(5, 40)) for i in range(48 * 5)]
    assert infer_offpeak(dyn, start + timedelta(days=5)) is None                                                    # scattered
