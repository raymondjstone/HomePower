import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from core.insights import BatteryFigures, DailyStats, Health, LoadProfile, SceneError, clean_scene, combine


def test_combine_never_crashes_and_stays_sane():
    rnd = random.Random(7)
    for _ in range(3000):
        items = [BatteryFigures(rnd.choice([0, 1000, 5000, 10000]), rnd.choice([None, 0, 50, 100, 120, -5]), rnd.random() < 0.7,
                                rnd.choice([None, -3000, 0, 2500]), rnd.choice([0, 10, 100])) for _ in range(rnd.randint(0, 4))]
        c = combine(items, rnd.choice([None, 0, 800]))
        assert c["online"] <= c["total"]
        for k in ("runtime_h", "full_in_h", "usable_kwh", "stored_kwh"):
            assert c[k] is None or c[k] >= 0, (k, c)


def test_profile_across_dst_and_midnight_terminates_and_survives_dump():
    tz = ZoneInfo("Europe/London")
    p = LoadProfile()
    t = datetime(2026, 10, 24, 0, 0, tzinfo=tz)
    for _ in range(3 * 24 * 60):                 # three days over the clocks going back, a sample a minute
        p.record(t, 500.0)
        t += timedelta(minutes=1)
    q = LoadProfile(p.dump())
    wh = q.expected_wh(datetime(2026, 10, 27, 7, 30, tzinfo=tz), 30)
    assert wh is None or wh > 0
    q.expected_wh(datetime(2026, 10, 25, 0, 0, tzinfo=tz), 24)      # across the repeated hour: must terminate


def test_stats_and_scenes_reject_junk():
    d, h = DailyStats({"x": {"pv": 1.0, "export": 0.0, "import": 0.0, "import_cheap": 0.0, "import_known": 0.0}}), Health({"weeks": {}})
    d.add("2026-01-01", pv_kwh=-1, export_kwh=-1, import_kwh=-1)
    h.update("2026-W01", {})
    for bad in ({"name": 5, "settings": 3}, {"name": "ok", "settings": {"import_limit_w": [1]}}):
        try:
            clean_scene(bad)
        except SceneError:
            pass
