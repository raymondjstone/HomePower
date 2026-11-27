"""Clock-change days (Europe/London: 2026-03-29 is 23 hours long, 2026-10-25 is 25) must keep their real length."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from core.goals import Goal, next_deadline
from core.insights import LoadProfile
from core.rates import RateTable, infer_offpeak

LON = ZoneInfo("Europe/London")
UTC = timezone.utc


def cheap_hours(day_start_local: datetime, hours: float, **tariff) -> float:
    rt = RateTable(**tariff)
    lo = day_start_local
    hi = (lo.astimezone(UTC) + timedelta(hours=hours)).astimezone(LON)
    return sum((s.end - s.start).total_seconds() for s in rt.slots_between(lo, hi) if s.price == tariff["offpeak"]) / 3600


GO = dict(peak=30.0, offpeak=7.5, offpeak_start=time(0, 30), offpeak_end=time(4, 30))


def test_fixed_tariff_cheap_window_is_five_real_hours_on_the_25_hour_day():
    # 00:30-04:30 local on 2026-10-25 is 5 real hours (the 01:00-02:00 hour happens twice)
    assert cheap_hours(datetime(2026, 10, 25, 0, 0, tzinfo=LON), 25, **GO) == 5.0


def test_fixed_tariff_cheap_window_is_three_real_hours_on_the_23_hour_day():
    assert cheap_hours(datetime(2026, 3, 29, 0, 0, tzinfo=LON), 23, **GO) == 3.0


def test_an_ordinary_day_is_unchanged():
    assert cheap_hours(datetime(2026, 11, 10, 0, 0, tzinfo=LON), 24, **GO) == 4.0


def test_slots_are_back_to_back_and_half_an_hour_each_on_clock_change_days():
    for day, hours in ((datetime(2026, 10, 25, 0, 0, tzinfo=LON), 25), (datetime(2026, 3, 29, 0, 0, tzinfo=LON), 23)):
        hi = (day.astimezone(UTC) + timedelta(hours=hours)).astimezone(LON)
        slots = RateTable(**GO).slots_between(day, hi)
        assert sum((s.end - s.start).total_seconds() for s in slots) / 3600 == hours
        assert all(a.end == b.start for a, b in zip(slots, slots[1:]))
        assert all(timedelta(0) < s.end - s.start <= timedelta(minutes=30) for s in slots)


def test_load_profile_energy_over_a_25_and_a_23_hour_day():
    lp = LoadProfile()
    for kind in ("wd", "we"):
        lp.avg[kind] = [1000.0] * 48
        lp.n[kind] = [lp.MIN_VISITS] * 48
    assert lp.expected_wh(datetime(2026, 10, 25, 0, 0, tzinfo=LON), 25) == 25000.0       # one W for each real hour
    assert lp.expected_wh(datetime(2026, 3, 29, 0, 0, tzinfo=LON), 23) == 23000.0


def test_learnt_cheap_window_across_the_autumn_change():
    # a Go-style tariff, history spanning the clock change: still found as exactly 00:30-04:30
    samples = []
    day = datetime(2026, 10, 20, 0, 0, tzinfo=LON)
    for d in range(10):
        base = (day + timedelta(days=d))
        for hh, price in ((0, 30.0), (0.5, 7.5), (4.5, 30.0)):
            t = datetime.combine(base.date(), time(int(hh), int((hh % 1) * 60)), tzinfo=LON)
            samples.append((t, price))
    got = infer_offpeak(samples, datetime(2026, 10, 30, 0, 0, tzinfo=LON), LON)
    assert got is not None and (got.start, got.end) == (time(0, 30), time(4, 30))


def test_goal_deadline_across_a_clock_change_is_the_right_morning():
    g = Goal("g", frozenset(), 80, time(7, 0))
    now = datetime(2026, 10, 24, 20, 0, tzinfo=LON)                          # the evening before the 25-hour night
    d = next_deadline(g, now)
    assert d is not None and d.date().isoformat() == "2026-10-25" and d.hour == 7


def test_preview_walks_real_time_over_both_clock_changes():
    from datetime import datetime as DT, time as T, timedelta as D, timezone as Z
    from zoneinfo import ZoneInfo
    from core.models import Desired, Mode
    from core.preview import preview
    from core.schedule import ScheduleEntry

    tz = ZoneInfo("Europe/London")
    night = [ScheduleEntry("n", frozenset(), T(0, 30), T(2, 30), Desired(Mode.CHARGE, 1000))]
    for asked, minutes in ((DT(2026, 10, 24, 12, 0, tzinfo=tz), 180), (DT(2026, 3, 28, 12, 0, tzinfo=tz), 60)):
        segs = preview(night, ["a"], asked, hours=24)["a"]      # 00:30-02:30 on the clock: 3 real hours in autumn, 1 in spring
        assert sum((s.end.astimezone(Z.utc) - s.start.astimezone(Z.utc) for s in segs), D()) == D(minutes=minutes), segs
