"""Solar forecast against what really came: hourly figures to local hours, the history, and the comparisons."""
from datetime import datetime
from zoneinfo import ZoneInfo

from hp.core.solar_forecast import SolarForecastLog, compare_days, compare_hours, hours_by_day, remaining_kwh

LONDON = ZoneInfo("Europe/London")


def test_hourly_wh_become_kwh_per_local_hour_and_junk_is_skipped():
    wh = {"2026-07-01T07:00:00+00:00": 500, "2026-07-01T12:00:00+00:00": 2000, "2026-07-02T11:00:00+00:00": 1000,
          "not a time": 50, "2026-07-01T13:00:00": 300, "2026-07-01T14:00:00+00:00": "nan", "2026-07-01T15:00:00+00:00": -4}
    h = hours_by_day(wh, LONDON)
    assert set(h) == {"2026-07-01", "2026-07-02"}
    assert h["2026-07-01"][8] == 0.5 and h["2026-07-01"][13] == 2.0        # BST: UTC 07:00 is 08:00 local
    assert sum(h["2026-07-01"]) == 2.5 and h["2026-07-02"][12] == 1.0
    assert hours_by_day(None, LONDON) == {} and hours_by_day("junk", LONDON) == {}


def test_the_repeated_hour_on_the_autumn_clock_change_is_added_not_lost():
    wh = {"2026-10-25T00:00:00+00:00": 100, "2026-10-25T01:00:00+00:00": 200}       # 01:00 BST, then 01:00 GMT
    assert hours_by_day(wh, LONDON)["2026-10-25"][1] == 0.3


def test_remaining_counts_the_rest_of_this_hour_and_the_hours_after():
    hours = [0.0] * 24
    hours[12], hours[13] = 2.0, 1.0
    assert remaining_kwh(hours, datetime(2026, 7, 1, 12, 30, tzinfo=LONDON)) == 2.0
    assert remaining_kwh(hours, datetime(2026, 7, 1, 14, 0, tzinfo=LONDON)) == 0.0
    assert remaining_kwh(None, datetime(2026, 7, 1, 12, tzinfo=LONDON)) is None


def test_the_day_keeps_its_first_total_and_its_latest_hours_and_survives_damage():
    log = SolarForecastLog()
    log.note_total("2026-07-01", 12.0)
    log.note_total("2026-07-01", 9.5)                                         # updated later in the day: the first is kept
    log.note_hours("2026-07-01", [0.5] * 24)
    log.note_hours("2026-07-01", [0.25] * 24)
    assert log.forecast_total("2026-07-01") == 12.0 and log.days["2026-07-01"]["hours"][0] == 0.25
    log.note_hours("2026-07-02", [0.1] * 24)                                  # only the hourly profile known: its sum
    assert log.forecast_total("2026-07-02") == 2.4
    again = SolarForecastLog(log.dump())
    assert again.dump() == log.dump()
    damaged = SolarForecastLog({"2026-07-01": {"first": "nan", "hours": [1] * 23}, "x": {"first": 1}, "2026-07-03": "junk",
                                "2026-07-04": {"first": 3, "hours": [None] * 24}})
    assert damaged.dump() == {"2026-07-04": {"first": 3.0, "hours": [0.0] * 24}}
    assert SolarForecastLog("junk").dump() == {}


def test_prune_keeps_the_last_120_days_and_tomorrow():
    log = SolarForecastLog({f"2026-{m:02d}-{d:02d}": {"first": 1} for m in range(1, 7) for d in range(1, 29)})
    log.note_total("2026-06-29", 5)
    log.prune("2026-06-28")
    days = sorted(log.days)
    assert len(days) == 121 and days[-1] == "2026-06-29" and days[-2] == "2026-06-28"


def test_daily_comparison_starts_where_records_start_and_marks_unknowns():
    log = SolarForecastLog()
    log.note_total("2026-07-03", 10)
    log.note_total("2026-07-05", 8)
    rows = compare_days(log, {"2026-07-04": 6.0, "2026-07-05": 9.0}, "2026-07-05", n=10)
    assert [r["day"] for r in rows] == ["2026-07-03", "2026-07-04", "2026-07-05"]
    assert rows[0] == {"day": "2026-07-03", "forecast": 10, "actual": None} and rows[1]["forecast"] is None and rows[2]["actual"] == 9.0
    assert compare_days(SolarForecastLog(), {}, "2026-07-05") == []


def test_hourly_comparison_adds_half_hours_and_copes_with_missing_data():
    fc = [0.0] * 24
    fc[12] = 1.5
    act = [0.0] * 48
    act[24], act[25] = 0.4, 0.6
    rows = compare_hours(fc, act)
    assert len(rows) == 24 and rows[12] == {"hour": 12, "forecast": 1.5, "actual": 1.0}
    assert compare_hours(None, None)[0] == {"hour": 0, "forecast": None, "actual": None}
    assert compare_hours([1] * 3, [1] * 5)[5] == {"hour": 5, "forecast": None, "actual": None}


def test_keys_at_the_end_of_each_hour_are_moved_back_an_hour():
    wh = {"2026-07-01T09:00:00+00:00": 400}                   # the hour 08:00-09:00 UTC (09:00-10:00 BST), keyed by its end
    assert hours_by_day(wh, LONDON, keyed_by_end=True)["2026-07-01"][9] == 0.4
    assert hours_by_day(wh, LONDON)["2026-07-01"][10] == 0.4


def test_an_hour_keeps_its_forecast_once_it_has_started():
    log = SolarForecastLog()
    log.note_hours("2026-07-01", [1.0] * 24)                                  # first seen: taken as given
    log.note_hours("2026-07-01", [2.0] * 24, from_hour=10)                    # at 10:xx: hours 0-9 have started
    h = log.days["2026-07-01"]["hours"]
    assert h[9] == 1.0 and h[10] == 2.0 and h[23] == 2.0
    log.note_hours("2026-07-01", [3.0] * 24, from_hour=14)                    # an estimate of what came for 10-13: not kept
    h = log.days["2026-07-01"]["hours"]
    assert h[:10] == [1.0] * 10 and h[10:14] == [2.0] * 4 and h[14:] == [3.0] * 10
    log.note_hours("2026-07-02", [5.0] * 24, from_hour=0)                     # tomorrow: all of it is still the forecast
    assert log.days["2026-07-02"]["hours"] == [5.0] * 24
