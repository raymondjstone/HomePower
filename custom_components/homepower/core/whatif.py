"""'What if I had set the battery differently?' (HA-free): replays recorded house load and solar, half-hour by half-hour,
through a simple battery model, and prices the grid import and export on a tariff (see core/tariffs.py).

The model: solar feeds the house first, any surplus charges the battery (then is exported), any shortfall is met from the
battery down to the reserve (then imported). In cheap-rate periods the battery is also topped up from the grid to
`cheap_pct`. It is an estimate: a fixed 95% one-way efficiency, one battery made of all of yours, and none of HomePower's
other rules (limits, forecast, goals)."""
from __future__ import annotations

from .tariffs import _in_window

ONE_WAY = 0.95


def simulate(days: dict[str, dict[str, list[float]]], tariff: dict, capacity_kwh: float, max_kw: float,
             reserve_pct: float = 10, cheap_pct: float = 0) -> dict:
    cap, step = float(capacity_kwh), float(max_kw) * 0.5          # kWh the battery can move in a half-hour
    floor = cap * max(0.0, min(100.0, reserve_pct)) / 100
    target = cap * max(0.0, min(100.0, cheap_pct)) / 100
    soc = floor
    buy = earn = imp_total = exp_total = 0.0
    for k in sorted(days):
        d = days[k]
        for i in range(48):
            load, pv = d["load"][i], d["pv"][i]
            cheap = tariff["cheap"] is not None and _in_window(i, tariff["windows"])
            rate = tariff["cheap"] if cheap else tariff["peak"]
            net = load - pv
            imp = exp = 0.0
            if net < 0:                                              # surplus solar: battery first, then the grid
                put = min(-net, step, (cap - soc) / ONE_WAY)
                soc += put * ONE_WAY
                exp = -net - put
            else:                                                    # shortfall: battery down to the reserve, then the grid
                take = min(net, step, max(0.0, soc - floor) * ONE_WAY)
                soc -= take / ONE_WAY
                imp = net - take
            if cheap and soc < target:                               # cheap-rate top-up from the grid
                add = min(step, (target - soc) / ONE_WAY, (cap - soc) / ONE_WAY)
                soc += add * ONE_WAY
                imp += add
            buy += imp * rate
            earn += exp * tariff["export"]
            imp_total += imp
            exp_total += exp
    standing = tariff["standing"] * len(days)
    return {"cost": round(buy - earn + standing, 2), "import_kwh": round(imp_total, 1), "export_kwh": round(exp_total, 1)}
