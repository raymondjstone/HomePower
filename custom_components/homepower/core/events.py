"""Demand-flexibility events (saving sessions) from Home Assistant entities: when the next one starts and ends (HA-free).

Understands a calendar entity (its attributes hold the current or next event's start_time / end_time), the Octopus Energy
integration's saving-session entities (a list of joined events, each with start and end) and any entity with a list of
{start, end} under `events`. An entity that only says on / off gives no times: it can still run the event, but nothing can
be charged up for it beforehand."""
from __future__ import annotations
from typing import Any

from datetime import datetime, timezone, tzinfo

from .goals import ALL_DAYS, Goal
from .rates import _dt

_LISTS = ("joined_events", "events", "upcoming_events")
_PAIRS = (("start_time", "end_time"),                                       # a calendar: the current or next event
          ("current_joined_event_start", "current_joined_event_end"),       # the Octopus Energy saving-session binary sensor
          ("next_joined_event_start", "next_joined_event_end"))


def all_day(attrs: dict[str, Any] | None) -> bool:
    """A calendar entity showing an all-day entry (its state is on all day: that is not a session)."""
    return isinstance(attrs, dict) and attrs.get("all_day") is True
GOAL_ID = "before grid event"


def event_windows(attrs: dict[str, Any] | None, tz: tzinfo | None = None) -> list[tuple[datetime, datetime]]:
    """(start, end) of each event the entity describes, as aware datetimes, sorted, junk skipped."""
    attrs = attrs if isinstance(attrs, dict) else {}
    out: list[tuple[datetime, datetime]] = []
    if all_day(attrs):
        return out                  # a calendar's all-day entry is a note, not a session: it would discharge the batteries all day
    for a, b in _PAIRS:
        s, e = _dt(attrs.get(a), tz), _dt(attrs.get(b), tz)
        if s is not None and e is not None and e > s:
            out.append((s, e))
    for key in _LISTS:
        items = attrs.get(key)
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            s, e = _dt(it.get("start") or it.get("start_time"), tz), _dt(it.get("end") or it.get("end_time"), tz)
            if s is not None and e is not None and e > s:
                out.append((s, e))
    return sorted(set(out), key=lambda w: w[0])


def next_event_start(windows: list[tuple[datetime, datetime]], now: datetime, within_h: float = 24.0) -> datetime | None:
    """The start of the next event that has not begun yet and starts within `within_h` hours."""
    now_u = now.astimezone(timezone.utc)
    starts = [s for s, _ in windows if 0 < (s.astimezone(timezone.utc) - now_u).total_seconds() <= within_h * 3600]
    return min(starts, default=None)


def charge_goal(start: datetime | None, target_pct: int, now: datetime) -> Goal | None:
    """A goal that has the batteries charged to `target_pct` by the time the next event starts (cheapest slots first)."""
    if start is None or not 0 < target_pct <= 100:
        return None
    local = start.astimezone(now.tzinfo) if now.tzinfo else start
    return Goal(GOAL_ID, frozenset(), int(target_pct), local.time().replace(second=0, microsecond=0),
                days=frozenset({local.weekday()}) & ALL_DAYS, source="grid")
