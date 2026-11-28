"""Common data model shared by all device adapters."""
from __future__ import annotations
from typing import Any

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
    power_control: bool = True   # False: Charge is on/off only (the battery picks its own rate), so surplus-tracking is not possible


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
    grid_w: float | None = None     # house grid power if the device can see it (+ importing)
    pv_w: float | None = None
    load_w: float | None = None
    limits: tuple[Any, ...] | None = None     # the battery's own (charge limit %, discharge limit %, backup reserve %, backup on) - Solarbank
    firmware: str | None = None     # software version the device reports (X1)
    temp_c: float | None = None     # internal temperature (X1)
    alarms: tuple[str, ...] = ()              # active alarms the device reports, as plain text (X1)
    off_grid: bool | None = None    # the device reports the grid is lost (None = it cannot tell)
    pv_strings: tuple[tuple[float, float], ...] = ()          # X1: (volts, amps) of each of PV1-PV8 (registers 10167-10182); () = not read yet
    pv_total_w: float | None = None  # X1: the inverter's own total PV power (register 10183)
    pack_v: tuple[Any, ...] = ()              # X1: raw voltage reading of each battery pack (for the health trend)
    gone: bool = False              # not read, and known to be off the network (not just a refused or unanswered request)


@dataclass(slots=True)
class DeviceConfig:
    device_id: str
    name: str
    kind: str                       # anker_x1 | anker_e5000 | ecoflow_ultra_x | ecoflow_es5000
    caps: Capabilities
    priority: int = 0               # lower = preferred discharge first
    extra: dict[str, Any] = field(default_factory=dict)
