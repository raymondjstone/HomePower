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
