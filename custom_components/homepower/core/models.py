"""Common data model shared by all device adapters."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Mode(StrEnum):
    """Manufacturer-neutral operating modes."""

    AUTO = "auto"            # device's own self-consumption logic
    CHARGE = "charge"        # force charge (from grid/solar)
    DISCHARGE = "discharge"  # force discharge
    HOLD = "hold"            # idle: neither charge nor discharge


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Per-device limits. Never assume two devices share these."""

    capacity_wh: int
    max_charge_w: int
    max_discharge_w: int
    min_soc: int = 0
    supported_modes: frozenset[Mode] = frozenset(Mode)


@dataclass(frozen=True, slots=True)
class Desired:
    """What the controller wants a device to be doing."""

    mode: Mode = Mode.AUTO
    power_w: int | None = None      # charge/discharge setpoint
    target_soc: int | None = None   # stop charging at this SOC
    reserve_soc: int | None = None  # don't discharge below this


@dataclass(frozen=True, slots=True)
class Actual:
    """Read-back state from a device."""

    online: bool
    soc: float | None = None
    power_w: float | None = None    # + charging, - discharging
    mode: Mode | None = None
    set_power_w: int | None = None
    target_soc: int | None = None
    reserve_soc: int | None = None
    protected: bool = False         # manufacturer protection/calibration active


@dataclass(slots=True)
class DeviceConfig:
    device_id: str
    name: str
    kind: str                       # anker_x1 | anker_e5000 | ecoflow_ultra_x | ecoflow_es5000
    caps: Capabilities
    priority: int = 0               # lower = preferred discharge first
    extra: dict = field(default_factory=dict)
