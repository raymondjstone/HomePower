"""Look ahead: what will each battery do over the next hours, and where do schedules clash?"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .models import Mode
from .planner import Alloc
from .schedule import ScheduleEntry, resolve_entry


@dataclass
class Segment:
    start: datetime
    end: datetime
    mode: str
    power_w: int | None
    source: str
    shared_w: int | None = None    # a total the schedule shares between its batteries (each one's part depends on charge then)


def _steps(start: datetime, hours: int, step_min: int):
    """(from, to) pieces of the look-ahead, cut on the clock (quarter hours) and not counted from the minute it was asked
    for: otherwise a 23:30 start asked about at 08:08 would show as 23:38. Counted in real time and given back on the home's
    clock, so the hour the clocks repeat is walked twice and the one they skip not at all."""
    zone = start.tzinfo
    t = start.astimezone(timezone.utc) if zone else start
    end = t + timedelta(hours=hours)
    while t < end:
        nxt = t - timedelta(minutes=t.minute % step_min, seconds=t.second, microseconds=t.microsecond) + timedelta(minutes=step_min)
        nxt = min(nxt, end)
        yield (t.astimezone(zone), nxt.astimezone(zone)) if zone else (t, nxt)
        t = nxt


def preview(
    schedules: list[ScheduleEntry], device_ids: list[str], start: datetime, hours: int = 24, step_min: int = 15,
    condition_ok=None, away: bool = False, allocs: list[Alloc] | None = None,
) -> dict[str, list[Segment]]:
    """Per battery, merged segments of schedule (or planned goal) behaviour. Gaps mean 'no instruction'."""
    out: dict[str, list[Segment]] = {d: [] for d in device_ids}
    for t, nxt in _steps(start, hours, step_min):
        for did in device_ids:
            e = resolve_entry(schedules, did, t, condition_ok, away)
            if e is not None:
                shared = e.total_power_w if e.desired.mode in (Mode.CHARGE, Mode.DISCHARGE) else None
                key = (e.desired.mode.value, None if shared else e.desired.power_w, f"schedule {e.id}", shared or None)
            else:
                p = sum(a.power_w for a in (allocs or []) if a.device_id == did and a.start <= t < a.end)
                key = (Mode.CHARGE.value, p, "goal plan", None) if p else None
            segs = out[did]
            if key is None:
                continue
            if segs and segs[-1].end == t and (segs[-1].mode, segs[-1].power_w, segs[-1].source, segs[-1].shared_w) == key:
                segs[-1].end = nxt
            else:
                segs.append(Segment(t, nxt, *key))
    return out


def find_conflicts(
    schedules: list[ScheduleEntry], device_ids: list[str], start: datetime, hours: int = 48, step_min: int = 15,
    condition_ok=None, away: bool = False,
) -> list[dict]:
    """Times where two enabled schedules tie for the top priority but ask for different things."""
    found: dict[tuple, dict] = {}
    for t, nxt in _steps(start, hours, step_min):
        for did in device_ids:
            hits = [
                e for e in schedules
                if e.applies(did, t) and (e.presence == "any" or (e.presence == "away") == away)
                and (not e.condition_entity or condition_ok is None or condition_ok(e.condition_entity))
            ]
            if len(hits) < 2:
                continue
            top = max((e.priority, e.on_date is not None) for e in hits)
            tied = [e for e in hits if (e.priority, e.on_date is not None) == top]
            if len({(e.desired.mode, e.desired.power_w) for e in tied}) > 1:
                ids = tuple(sorted(e.id for e in tied))
                key = (did, ids)
                if key in found and found[key]["end"] == t:
                    found[key]["end"] = nxt
                else:
                    found[(did, ids, t)] = {"device_id": did, "schedules": list(ids), "start": t, "end": nxt}
                    found[key] = found[(did, ids, t)]
    return sorted({id(v): v for v in found.values()}.values(), key=lambda c: c["start"])
