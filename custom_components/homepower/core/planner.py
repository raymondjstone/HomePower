"""Goal planner: reach a target charge by a deadline using the cheapest price slots."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .rates import Slot

MIN_POWER_W = 50


@dataclass(frozen=True, slots=True)
class Need:
    device_id: str
    soc: float
    capacity_wh: int
    max_charge_w: int
    target_soc: float


@dataclass(frozen=True, slots=True)
class Alloc:
    device_id: str
    start: datetime
    end: datetime
    power_w: int
    price: float


def adjust_target_for_forecast(target: int, forecast_kwh: float | None, capacity_kwh: float,
                               share: float = 0.5, floor: int = 20) -> int:
    """Charge less from the grid when tomorrow's solar will refill the battery anyway.

    `share` is the fraction of the forecast generation expected to end up in the battery.
    """
    if forecast_kwh is None or forecast_kwh <= 0 or capacity_kwh <= 0:
        return target
    reduction = forecast_kwh * share / capacity_kwh * 100
    return int(max(min(floor, target), round(target - reduction)))


def plan_charge(now: datetime, deadline: datetime, needs: list[Need], slots: list[Slot],
                ceiling_w: int | None = None, efficiency: float = 0.92) -> tuple[list[Alloc], dict[str, float]]:
    """Fill the cheapest slots first. Returns (allocations, unmet energy in Wh per device)."""
    remaining = {n.device_id: max(0.0, (n.target_soc - n.soc) / 100 * n.capacity_wh) / efficiency for n in needs}
    maxw = {n.device_id: n.max_charge_w for n in needs}
    usable = [c for s in slots if (c := s.clipped(now, deadline))]
    allocs: list[Alloc] = []
    for slot in sorted(usable, key=lambda s: (s.price, s.start)):
        hours = (slot.end - slot.start).total_seconds() / 3600
        if hours <= 0:
            continue
        budget = ceiling_w if ceiling_w is not None else sum(maxw.values())
        for did in sorted(remaining, key=lambda k: -remaining[k]):
            if remaining[did] <= 0 or budget <= 0:
                continue
            p = int(min(maxw[did], remaining[did] / hours, budget))
            if p < MIN_POWER_W:
                continue
            allocs.append(Alloc(did, slot.start, slot.end, p, slot.price))
            remaining[did] -= p * hours
            budget -= p
    return sorted(allocs, key=lambda a: (a.start, a.device_id)), {k: v for k, v in remaining.items() if v > 1}


def power_now(allocs: list[Alloc], device_id: str, now: datetime) -> int:
    return sum(a.power_w for a in allocs if a.device_id == device_id and a.start <= now < a.end)
