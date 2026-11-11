"""A goal: be at X% by a deadline, charging in the cheapest slots."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

ALL_DAYS = frozenset(range(7))


@dataclass(frozen=True, slots=True)
class Goal:
    id: str
    device_ids: frozenset[str]            # empty = all batteries
    target_soc: int
    by: time
    days: frozenset[int] = ALL_DAYS       # weekday the deadline falls on (Monday=0)
    use_forecast: bool = False            # charge less when tomorrow's solar forecast is good
    max_power_w: int | None = None        # cap on total charge power for this goal
    enabled: bool = True
    source: str = "grid"                  # "grid" (cheapest slots) | "solar" (surplus only) | "both" (solar first, grid tops up)
    auto_target: bool = False             # target_soc becomes a ceiling; the real target follows the household's usual use

    def covers(self, device_id: str) -> bool:
        return not self.device_ids or device_id in self.device_ids


def next_deadline(goal: Goal, now: datetime, horizon_h: int = 36) -> datetime | None:
    """The next deadline within the horizon on an allowed weekday, else None."""
    for add in range(0, 8):
        cand = datetime.combine(now.date() + timedelta(days=add), goal.by, tzinfo=now.tzinfo)
        if cand > now and cand.weekday() in goal.days:
            return cand if cand - now <= timedelta(hours=horizon_h) else None
    return None
