"""Where the energy already stored is worth most (HA-free): running the house in the dearest half-hours, or exporting in the
best-paid ones, between now and the cheapest coming import price (when refilling is cheap again).

Every half-hour offers two uses for stored energy: covering the house's usual load there (worth that half-hour's import
price) and, beyond the load, exporting at the battery's full power (worth the export price). The uses are taken best-paid
first while energy lasts, and only while a use is worth more than buying the energy back later (the cheapest coming price
divided by the round trip, plus battery wear). Exporting in a half-hour also uses the battery for the house there first,
because a battery always covers the house before anything reaches the grid.

What it decides for now:
  * export: the power to send out now (0 = none: a better-paid half-hour is coming, or it does not pay);
  * hold: keep the stored energy now (the house is better run from the grid at this price: the energy earns more later).
Charging stays with goals and schedules; this plans the other half of the day."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .rates import Slot

ROUND_TRIP = 0.85
DEFAULT_LOAD_W = 400.0        # the house's draw where its usual use has not been learned yet
MIN_CHUNK_WH = 10.0


@dataclass(slots=True)
class PlanSlot:
    start: datetime
    end: datetime
    import_price: float
    export_price: float | None
    load_wh: float
    house_wh: float = 0.0      # stored energy planned to run the house here
    export_wh: float = 0.0     # stored energy planned to go out to the grid here

    def as_dict(self) -> dict[str, Any]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "import": self.import_price, "export": self.export_price,
                "load_wh": round(self.load_wh), "house_wh": round(self.house_wh), "export_wh": round(self.export_wh)}


@dataclass(slots=True)
class DayPlan:
    slots: list[PlanSlot] = field(default_factory=list)
    until: datetime | None = None      # the cheapest coming import slot: the plan covers the time up to it
    budget_wh: float = 0.0
    threshold: float = 0.0             # a kWh must be worth more than this to be used (buying it back, losses, wear)
    export_now_w: int = 0
    hold_now: bool = False

    @property
    def used_wh(self) -> float:
        return sum(s.house_wh + s.export_wh for s in self.slots)

    def as_dict(self) -> dict[str, Any]:
        return {"until": self.until.isoformat() if self.until else None, "budget_wh": round(self.budget_wh), "used_wh": round(self.used_wh),
                "threshold": round(self.threshold, 4), "export_now_w": self.export_now_w, "hold_now": self.hold_now,
                "slots": [s.as_dict() for s in self.slots]}


def plan_day(now: datetime, import_slots: list[Slot], export_price_at: Callable[[datetime], float | None] | None,
             usage_wh: Callable[[datetime, float], float | None] | None, budget_wh: float, export_max_w: float,
             wear: float = 0.0, horizon_h: float = 24.0, discharge_max_w: float | None = None) -> DayPlan | None:
    """`import_slots`: price slots (main units) covering at least now..now+horizon. `export_max_w`: the discharge power of the
    batteries that can be told to export; `discharge_max_w`: of all the batteries (what can run the house; None = no limit).
    Returns None without enough prices to plan."""
    house_cap_w = float("inf") if discharge_max_w is None else max(0.0, discharge_max_w)
    now_u = now.astimezone(timezone.utc)
    ahead = sorted((s for s in import_slots if s.end > now_u and s.start < now_u + timedelta(hours=horizon_h)), key=lambda s: s.start)
    if len(ahead) < 2:
        return None
    cheapest = min(ahead, key=lambda s: (s.price, s.start))
    plan = DayPlan(until=cheapest.start if cheapest.start > now_u else now_u, budget_wh=max(0.0, budget_wh),
                   threshold=max(0.0, cheapest.price) / ROUND_TRIP + max(0.0, wear))
    for s in ahead:
        if s is cheapest:
            break                                  # from here refilling is cheapest: the plan covers the time before it
        lo, hi = max(s.start, now_u), s.end
        hours = (hi - lo).total_seconds() / 3600
        if hours <= 0:
            continue
        local = lo.astimezone(now.tzinfo) if now.tzinfo else lo
        load = usage_wh(local, hours) if usage_wh else None
        load = DEFAULT_LOAD_W * hours if load is None or load != load or load < 0 else load
        exp = export_price_at(lo) if export_price_at else None
        plan.slots.append(PlanSlot(lo, hi, s.price, exp, load))
    if not plan.slots:
        return plan
    # the uses, best-paid first: (value per kWh, slot index, kind)
    uses = []
    for i, p in enumerate(plan.slots):
        uses.append((p.import_price, i, "house"))
        if p.export_price is not None and export_max_w > 0:
            uses.append((p.export_price, i, "export"))
    uses.sort(key=lambda u: (-u[0], u[1], u[2] != "house"))
    left = plan.budget_wh
    for value, i, kind in uses:
        if left < MIN_CHUNK_WH or value <= plan.threshold:
            break
        p = plan.slots[i]
        hours = (p.end - p.start).total_seconds() / 3600
        house_room = max(0.0, min(p.load_wh, house_cap_w * hours) - p.house_wh)   # the batteries cannot run more than their power
        if kind == "house":
            take = min(left, house_room)
            p.house_wh += take
            left -= take
        else:
            take = min(left, house_room)             # the house is covered first: exporting here means running it too
            p.house_wh += take
            left -= take
            room = max(0.0, export_max_w * hours - p.house_wh - p.export_wh)
            take = min(left, room)
            if take > 1e-6:
                p.export_wh += take
                left -= take
    # What to do now is read as shares of the current half-hour, not amounts, so the last seconds of a half-hour (tiny amounts)
    # decide the same as the rest of it and nothing is switched off and on again at every half-hour.
    first = plan.slots[0]
    hours = (first.end - first.start).total_seconds() / 3600
    if first.export_wh > 1e-6 and hours > 0:
        plan.export_now_w = int(round(min(export_max_w, (first.house_wh + first.export_wh) / hours)))
    coverable = min(first.load_wh, house_cap_w * hours)           # what the batteries could run here at most
    plan.hold_now = coverable > 0 and first.house_wh < 0.5 * coverable and plan.used_wh >= MIN_CHUNK_WH
    return plan
