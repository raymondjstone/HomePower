"""Zero-import battery: follow the grid meter so import (and export) stay at ~0 W.

Signed convention for the setpoint: + discharge, - charge.
Importing => discharge to cover it. Exporting (solar surplus) => charge with the surplus.
Charging only ever happens against export, never from the grid: if the battery's own
charging causes import, the same formula winds the charge rate back down.
"""
from __future__ import annotations

from .models import Capabilities, Desired, Mode


def setpoint(
    grid_import_w: float,
    own_net_discharge_w: float,
    other_charge_w: float,
    max_discharge_w: int,
    max_charge_w: int,
    current_set_w: int | None = None,
    deadband_w: int = 50,
    min_w: int = 30,
) -> int:
    """Signed target (+discharge / -charge) in W.

    grid_import_w       : + importing, - exporting (already sign-normalised)
    own_net_discharge_w : this battery now; + discharging, - charging
    other_charge_w      : power other batteries are charging at. That import is deliberate
                          (e.g. cheap-rate schedule), so it must not be chased.
    min_w is a dead zone around 0 so the battery doesn't flip charge<->discharge on noise;
    deadband_w stops small load changes from re-sending setpoints.
    """
    raw = own_net_discharge_w + grid_import_w - other_charge_w
    raw = max(-float(max_charge_w), min(raw, float(max_discharge_w)))
    if abs(raw) < min_w:
        return 0
    if current_set_w and (raw > 0) == (current_set_w > 0) and abs(raw - current_set_w) < deadband_w:
        return current_set_w
    return int(round(raw))


def desired_for(
    sp_w: int | None,
    caps: Capabilities,
    soc: float | None,
    reserve_soc: int | None = None,
    target_soc: int = 100,
) -> Desired:
    """Neutral command for the zero-import battery. Unknown grid reading => HOLD (fail safe)."""
    floor = max(caps.min_soc, reserve_soc or 0)
    if sp_w is None or sp_w == 0:
        return Desired(Mode.HOLD, reserve_soc=reserve_soc)
    if sp_w > 0:
        if soc is not None and soc <= floor:
            return Desired(Mode.HOLD, reserve_soc=reserve_soc)
        return Desired(Mode.DISCHARGE, power_w=sp_w, reserve_soc=reserve_soc)
    if soc is not None and soc >= target_soc:  # full: surplus exports instead
        return Desired(Mode.HOLD, reserve_soc=reserve_soc)
    return Desired(Mode.CHARGE, power_w=-sp_w, target_soc=target_soc, reserve_soc=reserve_soc)
