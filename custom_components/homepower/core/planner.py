"""Goal planner: reach a target charge by a deadline using the cheapest price slots."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .grid import split_power
from .rates import Slot

MIN_POWER_W = 50
MIN_REMAINING_WH = 25     # a need smaller than this is met: it is not worth a command, and in a short clipped slot it would become a huge power


@dataclass(frozen=True, slots=True)
class Need:
    device_id: str
    soc: float
    capacity_wh: int
    max_charge_w: int
    target_soc: float
    fixed_rate: bool = False   # the battery picks its own charge rate (cannot be set): it is either charging flat out or not
    efficiency: float | None = None   # this battery's charging efficiency (None = use the default)


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
    """Fill the cheapest slots first. Returns (allocations, unmet energy in Wh per device).

    When the batteries share a house limit, the most efficient one takes the cheapest slots. If that leaves a battery short of its
    target that a fair split would have reached, the energy is shared out in proportion to what each still needs instead."""
    plan = _plan(now, deadline, needs, slots, ceiling_w, efficiency, share=False)
    if plan[1] and ceiling_w is not None and len(needs) > 1:
        fair = _plan(now, deadline, needs, slots, ceiling_w, efficiency, share=True)
        if sum(fair[1].values()) < sum(plan[1].values()) - 1:
            return fair
    return plan


def _plan(now: datetime, deadline: datetime, needs: list[Need], slots: list[Slot], ceiling_w: int | None,
          efficiency: float, share: bool) -> tuple[list[Alloc], dict[str, float]]:
    eff = {n.device_id: n.efficiency or efficiency for n in needs}
    remaining = {n.device_id: max(0.0, (n.target_soc - n.soc) / 100 * n.capacity_wh) / eff[n.device_id] for n in needs}
    maxw = {n.device_id: n.max_charge_w for n in needs}
    fixed = {n.device_id for n in needs if n.fixed_rate}
    usable = [c for s in slots if (c := s.clipped(now, deadline))]
    allocs: list[Alloc] = []
    for slot in sorted(usable, key=lambda s: (s.price, s.start)):
        hours = (slot.end - slot.start).total_seconds() / 3600
        if hours <= 0:
            continue
        budget = ceiling_w if ceiling_w is not None else sum(maxw.values())
        if share:
            # fixed-rate batteries first (whole slots, most efficient first), then the rest share what is left in proportion to need
            for did in sorted((k for k in remaining if k in fixed), key=lambda k: (-eff[k], -remaining[k])):
                if remaining[did] < MIN_REMAINING_WH or budget <= 0 or maxw[did] > budget or maxw[did] < MIN_POWER_W:
                    continue
                allocs.append(Alloc(did, slot.start, slot.end, int(maxw[did]), slot.price))
                remaining[did] -= maxw[did] * hours
                budget -= int(maxw[did])
            want = {d: min(maxw[d], remaining[d] / hours) for d in remaining if d not in fixed and remaining[d] >= MIN_REMAINING_WH}
            if want and budget > 0:
                grant = {d: int(p) for d, p in want.items()} if sum(want.values()) <= budget else                     split_power(budget, [(d, remaining[d], want[d]) for d in want])
                for did in sorted(grant, key=lambda k: (-eff[k], -remaining[k])):
                    if grant[did] >= MIN_POWER_W:
                        allocs.append(Alloc(did, slot.start, slot.end, int(grant[did]), slot.price))
                        remaining[did] -= grant[did] * hours
            continue
        # when the house limit cannot feed everyone at once, the most efficient battery gets the cheapest slots
        for did in sorted(remaining, key=lambda k: (-eff[k], -remaining[k])):
            if remaining[did] < MIN_REMAINING_WH or budget <= 0:
                continue
            if did in fixed:
                # A battery that cannot be told a power charges at its own full rate for as long as it is on: give it whole
                # slots at that rate (it stops at its target), and only when the house limit has room for all of it.
                if maxw[did] > budget or maxw[did] < MIN_POWER_W:
                    continue
                p = int(maxw[did])
            else:
                p = int(min(maxw[did], remaining[did] / hours, budget))
            if p < MIN_POWER_W:
                continue
            allocs.append(Alloc(did, slot.start, slot.end, p, slot.price))
            remaining[did] -= p * hours
            budget -= p
    return sorted(allocs, key=lambda a: (a.start, a.device_id)), {k: v for k, v in remaining.items() if v > MIN_REMAINING_WH}


def power_now(allocs: list[Alloc], device_id: str, now: datetime) -> int:
    return sum(a.power_w for a in allocs if a.device_id == device_id and a.start <= now < a.end)
