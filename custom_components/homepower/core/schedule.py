"""Schedule resolution: time windows, weekday/weekend, seasonal, overrides."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .models import Desired

WEEKDAYS = frozenset(range(0, 5))
WEEKEND = frozenset(range(5, 7))
ALL_DAYS = frozenset(range(7))


@dataclass(frozen=True, slots=True)
class ScheduleEntry:
    id: str
    device_ids: frozenset[str]       # empty = all devices
    start: time
    end: time                        # end <= start means window crosses midnight
    desired: Desired
    days: frozenset[int] = ALL_DAYS  # Monday=0; day the window *starts* on
    priority: int = 0                # higher wins
    season_start: tuple[int, int] | None = None  # (month, day) inclusive
    season_end: tuple[int, int] | None = None
    on_date: date | None = None      # date-specific override (start date)
    enabled: bool = True
    all_day: bool = False               # ignore start/end (use with a condition entity)
    condition_entity: str | None = None  # only active while this HA entity is 'on' (tariff window etc.)

    def _in_season(self, d: date) -> bool:
        if not (self.season_start and self.season_end):
            return True
        md, s, e = (d.month, d.day), self.season_start, self.season_end
        return s <= md <= e if s <= e else (md >= s or md <= e)  # wraps year end

    def _day_ok(self, d: date) -> bool:
        if self.on_date is not None:
            return d == self.on_date
        return d.weekday() in self.days and self._in_season(d)

    def applies(self, device_id: str, now: datetime) -> bool:
        if not self.enabled or (self.device_ids and device_id not in self.device_ids):
            return False
        if self.all_day:
            return self._day_ok(now.date())
        t = now.time()
        if self.end > self.start:
            return self._day_ok(now.date()) and self.start <= t < self.end
        # Crosses midnight: evening part belongs to the start day, morning part to the day before.
        if t >= self.start:
            return self._day_ok(now.date())
        if t < self.end:
            return self._day_ok(now.date() - timedelta(days=1))
        return False


def resolve(entries: list[ScheduleEntry], device_id: str, now: datetime, condition_ok=None) -> Desired | None:
    """Highest-priority matching entry wins; a date-specific entry beats recurring on ties."""
    hits = [
        e for e in entries
        if e.applies(device_id, now)
        and (not e.condition_entity or condition_ok is None or condition_ok(e.condition_entity))
    ]
    if not hits:
        return None
    return max(hits, key=lambda e: (e.priority, e.on_date is not None)).desired
