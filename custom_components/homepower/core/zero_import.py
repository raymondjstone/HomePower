"""Zero-import battery: discharge exactly enough to hold grid import at 0 W."""
from __future__ import annotations

from .models import Capabilities, Desired, Mode


def setpoint(
    grid_import_w: float,
    own_discharge_w: float,
    other_charge_w: float,
    max_discharge_w: int,
    current_set_w: int | None = None,
    deadband_w: int = 50,
    min_w: int = 30,
) -> int:
    """Discharge power (W) the zero-import battery should be set to.

    grid_import_w   : + importing, - exporting (already sign-normalised)
    own_discharge_w : what this battery is delivering now (>= 0)
    other_charge_w  : power other batteries are charging at. That import is deliberate
                      (e.g. cheap-rate schedule), so it must not be chased.
    The deadband stops the setpoint hunting on small load changes.
    """
    raw = own_discharge_w + grid_import_w - other_charge_w
    raw = max(0.0, min(raw, float(max_discharge_w)))
    if raw < min_w:
        return 0
    if current_set_w and abs(raw - current_set_w) < deadband_w:
        return current_set_w
    return int(round(raw))


def desired_for(
    sp_w: int | None,
    caps: Capabilities,
    soc: float | None,
    reserve_soc: int | None = None,
) -> Desired:
    """Neutral command for the zero-import battery. Unknown grid reading => HOLD (fail safe)."""
    floor = max(caps.min_soc, reserve_soc or 0)
    if sp_w is None or sp_w <= 0 or (soc is not None and soc <= floor):
        return Desired(Mode.HOLD, reserve_soc=reserve_soc)
    return Desired(Mode.DISCHARGE, power_w=sp_w, reserve_soc=reserve_soc)
