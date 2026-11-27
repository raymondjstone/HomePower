"""Grid-facing policies: balancing, peak shaving, export limiting, load rules, backup reserve."""
from __future__ import annotations

from collections.abc import Sequence

from .models import Capabilities, Desired, Mode

PEAK_MARGIN = 0.95  # aim a little under the limit


def split_power(total_w: float, items: Sequence[tuple[str, float, float]]) -> dict[str, int]:
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


def peak_shave(grid_w: float, limit_w: float, candidates: Sequence[tuple[str, float, float, float]]) -> dict[str, int]:
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
    grid_w: float, limit_w: float, discharging: dict[str, float], chargers: Sequence[tuple[str, float, float, float]],
    commanded: dict[str, float] | None = None, held: set[str] | None = None,
) -> tuple[dict[str, int], dict[str, int]]:
    """Keep export under the limit: first trim scheduled discharge, then absorb the rest by charging.

    discharging: id -> discharge W now. commanded: id -> discharge W it is about to be told (None = the same).
    chargers: (id, current_charge_w, max_charge_w, weight) for batteries free to be commanded. held: those already
    charging because we told them to. Returns (new discharge W for trimmed batteries, charge W for chargers).

    The grid reading includes what the guard did last time, so both are worked out from it with that added back:
    otherwise a reading at the limit would look like "all clear", undo the guard, and switch it on again next time.
    """
    excess = (-grid_w) - limit_w * PEAK_MARGIN
    want = dict(discharging if commanded is None else commanded)
    cut = sum(want.values()) - max(0.0, sum(discharging.values()) - excess)     # asked for beyond what the limit has room for
    trims: dict[str, int] = {}
    for did, w in sorted(want.items(), key=lambda kv: -kv[1]):
        if cut < 1:
            break
        off = min(w, cut)
        trims[did] = int(round(w - off))
        cut -= off
    left = excess - sum(discharging.values())            # what stopping every discharge would not take care of
    if left > 0:
        pool, extra = chargers, left + sum(c[1] for c in chargers)
    else:                                                # under the limit: only what we are already charging may carry on
        pool = [c for c in chargers if c[0] in (held or set())]
        # from its whole charge now, own panels included: that is what the command sets
        extra = sum(c[1] for c in pool) + min(0.0, excess)
    charges: dict[str, int] = {}
    if extra > 0 and pool:
        res = split_power(extra, [(i, w, m) for i, _, m, w in pool])
        charges = {k: v for k, v in res.items() if v > 0}
    return trims, charges


def apply_load_rules(d: Desired, blocks: set[str]) -> Desired:
    """Never charge/discharge into a load the user said to protect (EV charger, heat pump...)."""
    if (d.mode is Mode.DISCHARGE and "discharge" in blocks) or (d.mode is Mode.CHARGE and "charge" in blocks):
        return Desired(Mode.HOLD)
    return d


BACKUP_MARGIN = 2   # points above the reserve where the battery is held, so it settles there instead of dipping under and being topped up again


def backup_desired(soc: float | None, pct: int, caps: Capabilities) -> Desired | None:
    """Backup mode: top the battery up to the reserve (charging allowed only if the model can), then keep it from falling
    below it: just above the reserve it is held, so the reserve stays instead of being used and bought back again."""
    if soc is None:
        return None
    if soc < pct and Mode.CHARGE in caps.supported_modes:
        return Desired(Mode.CHARGE, caps.max_charge_w, target_soc=pct)
    if soc < pct + BACKUP_MARGIN and Mode.HOLD in caps.supported_modes:
        # also below the reserve when it cannot be told to charge: left alone it would run itself down further
        return Desired(Mode.HOLD, reserve_soc=pct)
    return None
