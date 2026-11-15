"""Leave room for tomorrow's solar (HA-free, tested without Home Assistant).

For a battery with panels, the highest charge to reach before the cheap rate ends such that the coming day's solar
never pushes it over 100%. From the end of charging (t0) the battery's energy follows

    E(t) = E0 + solar(t0..t) - base load(t0..t, outside cheap-rate periods)

Solar is the day's forecast spread over daylight as a half-sine (nothing at sunrise and sunset, most at midday). The
house's base load is taken by solar first; what solar cannot cover comes from the battery outside cheap-rate periods
(the grid covers it during them). So the base load between the end of charging and sunrise, and on into the morning
while solar is weak, empties room that the afternoon solar then fills. A battery cannot go below its minimum charge;
once there the grid supplies the house, and the battery only rises again.

The answer is found by bisection on E0: a higher start can only ever give a higher peak.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

STEP = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class SolarDay:
    sunrise: datetime
    sunset: datetime
    forecast_kwh: float            # this battery's share of the day's forecast generation

    def energy(self, a: datetime, b: datetime) -> float:
        """kWh produced between a and b (half-sine over daylight)."""
        length = (self.sunset - self.sunrise).total_seconds()
        if length <= 0 or self.forecast_kwh <= 0 or b <= a:
            return 0.0

        def pos(t: datetime) -> float:
            return min(max((t - self.sunrise).total_seconds() / length, 0.0), 1.0)

        return self.forecast_kwh / 2 * (math.cos(math.pi * pos(a)) - math.cos(math.pi * pos(b)))


def peak_energy(e0: float, t0: datetime, day: SolarDay, base_kw: float, battery_pays: Callable[[datetime], bool],
                floor_kwh: float) -> float:
    """Highest energy the battery reaches between t0 and sunset when it starts at e0."""
    e, top, t = e0, e0, t0
    while t < day.sunset:
        nxt = min(t + STEP, day.sunset)
        net = day.energy(t, nxt) - base_kw * (nxt - t).total_seconds() / 3600
        if net < 0 and not battery_pays(t):
            net = 0.0                    # cheap-rate period: the grid supplies what solar cannot
        e = max(floor_kwh, e + net) if e >= floor_kwh else e + max(net, 0.0)
        top = max(top, e)
        t = nxt
    return top


def max_start_kwh(capacity_kwh: float, floor_kwh: float, t0: datetime, day: SolarDay, base_kw: float,
                  battery_pays: Callable[[datetime], bool]) -> float:
    """The most energy the battery can hold at t0 without the day's solar taking it over capacity."""
    if day.forecast_kwh <= 0 or t0 >= day.sunset or capacity_kwh <= 0:
        return capacity_kwh
    if peak_energy(capacity_kwh, t0, day, base_kw, battery_pays, floor_kwh) <= capacity_kwh + 1e-6:
        return capacity_kwh
    lo, hi = 0.0, capacity_kwh
    if peak_energy(lo, t0, day, base_kw, battery_pays, floor_kwh) > capacity_kwh:
        return floor_kwh                 # even empty it would overflow: charge nothing beyond the minimum
    for _ in range(30):
        mid = (lo + hi) / 2
        if peak_energy(mid, t0, day, base_kw, battery_pays, floor_kwh) <= capacity_kwh:
            lo = mid
        else:
            hi = mid
    return max(lo, floor_kwh)


def charge_ceiling_pct(capacity_kwh: float, min_soc: float, t0: datetime, day: SolarDay, base_kw: float,
                       battery_pays: Callable[[datetime], bool]) -> int:
    """max_start_kwh as a whole-percent charge level (rounded down, never below the battery's minimum)."""
    floor = capacity_kwh * min_soc / 100
    e0 = max_start_kwh(capacity_kwh, floor, t0, day, base_kw, battery_pays)
    return max(int(min_soc), min(100, math.floor(e0 / capacity_kwh * 100 + 1e-4))) if capacity_kwh > 0 else 100


def split_by_capacity(total: float, capacities: dict[str, float]) -> dict[str, float]:
    """Share a total out in proportion to capacity: 10 kWh and 5 kWh batteries get 2/3 and 1/3."""
    whole = sum(c for c in capacities.values() if c > 0)
    return {k: (total * c / whole if whole > 0 and c > 0 else 0.0) for k, c in capacities.items()}
