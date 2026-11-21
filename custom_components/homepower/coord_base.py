"""What the coordinator and its parts share: timings, the per-battery status record, alert kinds and small helpers."""
from __future__ import annotations

import re
import logging
from collections import deque
from dataclasses import dataclass, field

from .core.models import Actual, Desired
from .core.reconcile import Decision

_LOGGER = logging.getLogger(__name__.rsplit(".", 1)[0] + ".coordinator")   # one logger for the coordinator and all its parts
READ_TIMEOUT_S = 20
HANDBACK_TIMEOUT_S = 20   # per battery at shutdown: Home Assistant is waiting
APPLY_TIMEOUT_S = 60   # a command that hangs (e.g. another integration's service call) must not stall control of every battery
PERSIST_EVERY_S = 300
TARIFF_REFRESH_S = 6 * 3600
TARIFF_HISTORY_DAYS = 14
FORECAST_PLATFORMS = {   # solar forecast integrations found automatically: platform -> (today, tomorrow) unique_id endings
    "forecast_solar": ("energy_production_today", "energy_production_tomorrow"),
    "solcast_solar": ("forecast_today", "forecast_tomorrow"),
}
GLOW_DOMAIN = "hildebrandglow_dcc"  # ha-hildebrandglow-dcc (raymondjstone fork and HandyHat original)
KEEPALIVE_EVERY_S = 60
OFFLINE_ALERT_AFTER_S = 180
PROTECT_ALERT_AFTER_S = 60
UNCONFIRMED_ALERT_AFTER = 3
GRID_SLOW_AFTER_S = 120     # setup check: warn that the grid reading is not live
GRID_STALE_AFTER_S = 300    # control: too old to steer by (solar surplus tracking waits instead)
ACTION_PAUSE, ACTION_HAND_BACK = "HOMEPOWER_PAUSE", "HOMEPOWER_HAND_BACK"   # buttons on phone notifications
MONEY_SAVED, MONEY_EXPORT = "money:saved", "money:export"   # lifetime money counters, kept with the energy counters
FORCING_REASONS = ("schedule", "goal", "manual override", "backup reserve")
REPAIR_KINDS = {   # alert kinds shown under Settings > Repairs (until fixed), with the title shown there
    "offline": "battery offline", "error": "command failed", "unconfirmed": "battery not following commands",
    "grid": "grid down", "protect": "battery protection active", "stuck": "battery not charging", "gridsign": "grid readings disagree",
    "alarm": "battery alarm", "health": "battery health", "trend": "battery health trend", "pv": "solar string",
    "tariffdrift": "cheap-rate hours changed",
}


def repair_id(key: str) -> str:
    """A Repairs issue id for an alert key: lower-case letters, digits and _ only, unique per key."""
    import hashlib

    slug = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")[:60]
    return f"{slug}_{hashlib.sha1(key.encode()).hexdigest()[:8]}"


@dataclass(slots=True)
class DeviceStatus:
    desired: Desired | None = None
    actual: Actual | None = None
    decision: Decision | None = None
    reason: str = ""
    last_apply: float | None = None
    last_error: str | None = None
    release_pending: bool = False   # we forced this device; hand it back when no longer managed
    guard_held: bool = False        # software SOC limit is currently holding this device
    last_read_ok: float | None = None
    unconfirmed: int = 0
    awaiting_confirm: bool = False  # the first command to an unproven battery is waiting for the user
    waited: int = 0                 # cycles a slow (cloud) battery has been given to show a command's effect
    last_keepalive: float = 0.0
    last_command: dict | None = None
    last_applied: Desired | None = None   # what we last sent, to tell a new instruction from a retry


@dataclass(slots=True)
class Snapshot:
    devices: dict[str, DeviceStatus] = field(default_factory=dict)
    events: deque = field(default_factory=lambda: deque(maxlen=200))  # diagnostics log


_PRIVATE_KEY = re.compile(r"ssid|wifi|pass|mac|^sn$|serial|lat|lon|addr|ip$|token", re.I)


def _real(v) -> float:
    """A sensor's number, or ValueError. "nan" and "inf" parse as floats but are no reading: let one through and a forecast,
    price or temperature turns the plan into NaN, which stops the decision engine every cycle."""
    f = float(v)
    if f != f or f in (float("inf"), float("-inf")):
        raise ValueError("not a finite number")
    return f
