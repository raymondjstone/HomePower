"""Saved records that are damaged or old, input that is refused, and the plain-language "why is it idle"."""
from datetime import datetime, time, timedelta, timezone

import pytest
from core.insights import DailyStats, GridEvents, Health, HealthTrend, UsageHistory
from core.sanity import explain_idle
from core.serde import ValidationError, device_from_dict, goal_from_dict, schedule_from_dict
from core.tariffs import parse_tariffs

NOON = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


def test_a_damaged_day_of_usage_is_dropped_or_kept_without_what_is_broken():
    good = [0.0] * 48
    usage = UsageHistory({"a": {"imp": good, "exp": good, "load": good, "pv": ["x"] * 48},
                          "b": {"imp": ["nan"] * 48, "exp": good}, "c": {"imp": "no", "exp": good},
                          "d": {"imp": good, "exp": good}})
    assert sorted(usage.days) == ["a", "d"] and "pv" not in usage.days["a"]
    usage.add(datetime(2026, 1, 1, 12), 0.0, 0.0)                      # nothing to add
    usage.add(datetime(2027, 1, 1, 12), 1.0, 0.0)
    assert "2027-01-01" in usage.days


def test_old_days_and_weeks_are_dropped_from_the_records():
    daily, usage = DailyStats(), UsageHistory()
    for i in range(UsageHistory.KEEP_DAYS + 3):
        day = datetime(2026, 1, 1, 12) + timedelta(days=i)
        daily.add(day.strftime("%Y-%m-%d"), pv_kwh=1.0)
        usage.add(day, 1.0, 0.0)
    assert len(daily.days) == DailyStats.KEEP_DAYS and len(usage.days) == UsageHistory.KEEP_DAYS
    health = Health()
    for i in range(Health.KEEP_WEEKS + 5):
        health.update(f"2026-W{i + 1:02d}", {"a": (10.0 * i, 8.0 * i)})
    assert len(health.weeks) == Health.KEEP_WEEKS
    trend = HealthTrend()
    for i in range(HealthTrend.KEEP_DAYS + 3):
        trend.update((datetime(2026, 1, 1) + timedelta(days=i)).strftime("%Y-%m-%d"), "a", 25.0, (50.0, 51.0))
    assert len(trend.days["a"]) == HealthTrend.KEEP_DAYS


def test_grid_outages_keep_only_the_recent_ones_and_survive_a_bad_row():
    events = GridEvents([{"did": "a", "start": "not a date"}, {"did": "a"}, "junk"])
    assert events.outages_since("", NOON) == (0, 0.0)
    events.observe("a", "A", None, NOON, 50)                           # cannot tell: nothing recorded
    for i in range(GridEvents.KEEP + 5):
        events.observe("b", "B", True, NOON + timedelta(minutes=2 * i), 50)
        events.observe("b", "B", False, NOON + timedelta(minutes=2 * i + 1), 49)
    assert len(events.dump()) == GridEvents.KEEP


@pytest.mark.parametrize("given, words", [
    ({"presence": "elsewhere"}, "presence"),
    ({"source": "wind"}, "source"),
])
def test_a_schedule_with_an_unknown_choice_is_refused(given, words):
    with pytest.raises(ValidationError, match=words):
        schedule_from_dict({"id": "s", "start": "01:00", "end": "02:00", "mode": "hold", **given})


@pytest.mark.parametrize("given, words", [
    ({"id": "x" * 121}, "too long"),
    ({"days": ["monday"]}, "weekdays"),
    ({"days": []}, "at least one"),
])
def test_a_goal_that_makes_no_sense_is_refused(given, words):
    with pytest.raises(ValidationError, match=words):
        goal_from_dict({"id": "g", "target_soc": 80, "by": "07:00", **given})


def test_tariff_lines_that_are_wrong_say_what_is_wrong():
    for text, words in (("A; peak=0.3\nA; peak=0.2", "twice"), ("A; peak", "should look like"),
                        ("A; peak=0.3; cheap=0.1", "both"), ("\n".join(f"T{i}; peak=0.3" for i in range(20)), "at most")):
        with pytest.raises(ValueError, match=words):
            parse_tariffs(text)


def test_why_a_battery_is_idle_names_the_first_thing_in_the_way():
    base = dict(control_enabled=True, paused=False, dry_run=False, online=True, off_grid=False, protected=False,
                last_error=None, guard_held=False, unconfirmed=0)
    assert explain_idle(**base) is None
    for change, words in (({"online": False, "hint": "Check Wi-Fi."}, "Offline: HomePower cannot read it. Check Wi-Fi."),
                          ({"off_grid": True}, "grid is down"), ({"control_enabled": False}, "Monitor only"), ({"paused": True}, "paused"),
                          ({"dry_run": True}, "Test run"), ({"awaiting_confirm": True}, "allow the first command"),
                          ({"protected": True}, "protection"), ({"last_error": "boom"}, "boom"), ({"guard_held": True}, "charge limit"),
                          ({"unconfirmed": 2}, "not confirmed")):
        assert words in explain_idle(**{**base, **change}), change
