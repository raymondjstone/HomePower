"""Edges of the core modules that no other test reaches: the savings ledger's day pruning and an emptied battery's cost, a goal
with no allowed day, and the cheap-window guess refusing histories that cannot show one."""
from datetime import datetime, time, timedelta, timezone

from hp.core.energy import Ledger
from hp.core.goals import Goal, next_deadline
from hp.core.rates import infer_offpeak

UTC = timezone.utc


def test_ledger_keeps_the_newest_days_and_drops_the_oldest():
    lg = Ledger()
    start = datetime(2025, 1, 1)
    days = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(Ledger.KEEP_DAYS + 5)]
    for d in days:
        lg.add(d, "a", 1.0, 1.0, 0.5, 0.0, 0.10, 0.0, 5.0)
    assert len(lg.days) == Ledger.KEEP_DAYS
    assert min(lg.days) == days[5] and max(lg.days) == days[-1], "the oldest go, never the most recent"


def test_an_emptied_battery_takes_the_cost_of_the_next_energy_in():
    lg = Ledger(stores={"a": [0.0, 0.30]})          # it was charged at 30p, then emptied
    lg.add("2026-11-28", "a", 0.04, 1.0, 0.0, 0.0, 0.07, 0.0, 0.0)
    assert lg.stores["a"] == [0.0, 0.07], "the old 30p no longer applies: the energy now going in cost 7p"
    lg = Ledger(stores={"b": [0.0, 0.30]})
    lg.add("2026-11-28", "b", 0.04, 0.0, 0.0, 0.0, 0.07, 0.0, 0.0)   # from solar: free
    assert lg.stores["b"][1] == 0.0


def test_a_goal_with_no_allowed_day_never_has_a_deadline():
    now = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)
    assert next_deadline(Goal("g", frozenset(), 90, time(7, 0), days=frozenset()), now) is None
    sunday_only = Goal("g", frozenset(), 90, time(7, 0), days=frozenset({6}))      # 2026-11-28 is a Saturday
    assert next_deadline(sunday_only, now) == datetime(2026, 11, 29, 7, 0, tzinfo=UTC)
    assert next_deadline(sunday_only, now, horizon_h=12) is None, "beyond the horizon"


def _hourly(start, days, price_at):
    return [(start + timedelta(minutes=30 * i), price_at(start + timedelta(minutes=30 * i))) for i in range(int(days * 48))]


def test_the_cheap_window_guess_refuses_what_cannot_show_one():
    start = datetime(2026, 11, 1, tzinfo=UTC)
    until = start + timedelta(days=3)
    assert infer_offpeak([(start, 0.25)], until) is None, "one price change is not a history"
    # every half-hour cheap but one dear spike: smoothed into all cheap, which is no window
    spike = _hourly(start, 3, lambda t: 0.40 if (t.hour, t.minute) == (18, 0) else 0.10)
    assert infer_offpeak(spike, until) is None
    # a history that stops short of a whole day has half-hours it never saw
    gap =[(start, 0.25), (start + timedelta(hours=1), 0.07)]
    assert infer_offpeak(gap, start + timedelta(hours=20), min_days=0.5) is None, "half-hours never seen: no guess"
    # a real Economy-7-like history is still found
    e7 = _hourly(start, 3, lambda t: 0.08 if 0 <= t.hour < 7 else 0.30)
    found = infer_offpeak(e7, until)
    assert found is not None and (found.start, found.end) == (time(0, 0), time(7, 0)), found
