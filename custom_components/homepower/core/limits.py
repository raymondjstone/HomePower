"""Whole-system caps: total charge / discharge power across ALL schedules combined."""
from __future__ import annotations

from .models import Capabilities, Desired, Mode


def apply_totals(
    items: dict[str, tuple[Desired, Capabilities, int]],
    charge_cap_w: int | None,
    discharge_cap_w: int | None,
) -> dict[str, Desired]:
    """Trim commanded power so the sums stay within the caps.

    items: device_id -> (desired, caps, priority). Lower priority number is served
    first (primary battery keeps its power, secondary ones are cut). A device cut to
    zero is put on HOLD. Devices in AUTO are the device's own business and cannot be
    capped here. Returns a new desired per device (unchanged where within the cap).
    """
    out = {k: v[0] for k, v in items.items()}
    for mode, cap in ((Mode.CHARGE, charge_cap_w), (Mode.DISCHARGE, discharge_cap_w)):
        if cap is None:
            continue
        group = sorted(
            (k for k, (d, _, _) in items.items() if out[k].mode is mode),
            key=lambda k: items[k][2],
        )
        remaining = max(0, cap)
        for k in group:
            d, caps, _ = items[k]
            max_w = caps.max_charge_w if mode is Mode.CHARGE else caps.max_discharge_w
            want = d.power_w if d.power_w is not None else max_w
            want = min(want, max_w)
            give = min(want, remaining)
            remaining -= give
            if give == 0:
                out[k] = Desired(Mode.HOLD, None, d.target_soc, d.reserve_soc)
            elif give < want or d.power_w is None:
                out[k] = Desired(mode, give, d.target_soc, d.reserve_soc)
    return out


def soc_guard(
    d: Desired, soc: float | None, caps: Capabilities, held: bool = False, margin: float = 2.0
) -> Desired:
    """Enforce SOC limits in software, for batteries with no target/reserve entity.

    Charge stops (HOLD) once SOC reaches the target; discharge stops at the reserve or the
    battery's minimum SOC. Once stopped (`held`), it only resumes after SOC moves `margin`
    points back inside the limit, so it does not flip every cooldown at the boundary.
    Unknown SOC => command unchanged.
    """
    if soc is None:
        return d
    if d.mode is Mode.CHARGE and d.target_soc is not None:
        if soc >= d.target_soc or (held and soc > d.target_soc - margin):
            return Desired(Mode.HOLD)
    if d.mode is Mode.DISCHARGE:
        floor = max(caps.min_soc, d.reserve_soc or 0)
        if soc <= floor or (held and soc < floor + margin):
            return Desired(Mode.HOLD)
    return d


def charge_ceiling(
    user_cap_w: int | None, import_limit_w: int | None, grid_import_w: float | None, battery_net_w: float
) -> int | None:
    """Effective cap on total charging power.

    The house import limit bounds charging too: headroom = limit - (import without batteries),
    where import without batteries = grid import - net battery charging. Without a grid reading
    the limit itself is used (conservative). Returns None when nothing limits charging.
    """
    caps = [c for c in (user_cap_w,) if c]
    if import_limit_w:
        if grid_import_w is None:
            caps.append(import_limit_w)
        else:
            caps.append(max(0, int(import_limit_w - (grid_import_w - battery_net_w))))
    return min(caps) if caps else None
