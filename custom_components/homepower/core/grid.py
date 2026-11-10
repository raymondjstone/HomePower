"""Grid-facing policies: balancing, peak shaving, export limiting, load rules, backup reserve."""
from __future__ import annotations

from .models import Capabilities, Desired, Mode

PEAK_MARGIN = 0.95  # aim a little under the limit


def split_power(total_w: float, items: list[tuple[str, float, float]]) -> dict[str, int]:
    """Share `total_w` across batteries in proportion to weight, never above each one's max (water-filling).

    items: (id, weight, max_w). Anything that can't be placed is simply not allocated.
    """
    alloc = {i: 0.0 for i, _, _ in items}
    active = [(i, max(w, 0.0), m) for i, w, m in items if m > 0]
    left = max(0.0, float(total_w))
    while active and left > 1e-6:
        wsum = sum(w for _, w, _ in active)
        spent, saturated = 0.0, set()
        for i, w, m in active:
            share = left * (w / wsum if wsum > 0 else 1 / len(active))
            give = min(share, m - alloc[i])
            alloc[i] += give
            spent += give
            if alloc[i] >= m - 1e-6:
                saturated.add(i)
        left -= spent
        active = [a for a in active if a[0] not in saturated]
        if spent < 1e-6:
            break
    return {i: int(round(v)) for i, v in alloc.items()}


def peak_shave(grid_w: float, limit_w: float, candidates: list[tuple[str, float, float, float]]) -> dict[str, int]:
    """Discharge to keep whole-house import under the limit.

    candidates: (id, current_discharge_w, max_discharge_w, weight) for batteries free to be commanded.
    The grid reading already includes their current discharge, so the new total is current + excess.
    """
    if not candidates:
        return {}
    target = limit_w * PEAK_MARGIN
    cur = sum(c[1] for c in candidates)
    want = cur + (grid_w - target)
    if want <= 0:
        return {}
    res = split_power(want, [(i, w, m) for i, _, m, w in candidates])
    return {k: v for k, v in res.items() if v > 0}


def export_guard(
    grid_w: float, limit_w: float, discharging: dict[str, float], chargers: list[tuple[str, float, float, float]]
) -> tuple[dict[str, int], dict[str, int]]:
    """Keep export under the limit: first trim scheduled discharge, then absorb the rest by charging.

    discharging: id -> commanded discharge W (can be reduced). chargers: (id, current_charge_w,
    max_charge_w, weight) for batteries free to be commanded. Returns (new discharge W for trimmed
    batteries, charge W for chargers).
    """
    excess = (-grid_w) - limit_w * PEAK_MARGIN
    if excess <= 0:
        return {}, {}
    trims: dict[str, int] = {}
    for did, w in sorted(discharging.items(), key=lambda kv: -kv[1]):
        if excess <= 0:
            break
        cut = min(w, excess)
        trims[did] = int(round(w - cut))
        excess -= cut
    charges: dict[str, int] = {}
    if excess > 0 and chargers:
        cur = sum(c[1] for c in chargers)
        res = split_power(cur + excess, [(i, w, m) for i, _, m, w in chargers])
        charges = {k: v for k, v in res.items() if v > 0}
    return trims, charges


def apply_load_rules(d: Desired, blocks: set[str]) -> Desired:
    """Never charge/discharge into a load the user said to protect (EV charger, heat pump...)."""
    if (d.mode is Mode.DISCHARGE and "discharge" in blocks) or (d.mode is Mode.CHARGE and "charge" in blocks):
        return Desired(Mode.HOLD)
    return d


def backup_desired(soc: float | None, pct: int, caps: Capabilities) -> Desired | None:
    """Backup mode: top the battery up to the reserve (charging allowed only if the model can)."""
    if soc is None or soc >= pct or Mode.CHARGE not in caps.supported_modes:
        return None
    return Desired(Mode.CHARGE, caps.max_charge_w, target_soc=pct)
