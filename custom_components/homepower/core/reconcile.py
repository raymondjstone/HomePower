"""Desired-vs-actual comparison and safe re-application policy."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .models import Actual, Desired, Mode


class Decision(StrEnum):
    OK = "ok"                          # matches
    APPLY = "apply"                    # mismatch, safe to (re)apply
    WAIT = "wait"                      # mismatch, but in cooldown / settling
    SKIP_PROTECTED = "skip_protected"  # manufacturer protection takes precedence
    OFFLINE = "offline"                # cannot read or write


@dataclass(frozen=True, slots=True)
class Diff:
    field: str
    desired: object
    actual: object


def diff(desired: Desired, actual: Actual) -> list[Diff]:
    """Managed fields that differ. Unmanaged (None) desired fields are ignored."""
    out: list[Diff] = []
    if actual.mode is not None and desired.mode != actual.mode:
        out.append(Diff("mode", desired.mode, actual.mode))
    for name, want, have in (
        ("power_w", desired.power_w, actual.set_power_w),
        ("target_soc", desired.target_soc, actual.target_soc),
        ("reserve_soc", desired.reserve_soc, actual.reserve_soc),
    ):
        if want is not None and have is not None and want != have:
            out.append(Diff(name, want, have))
    return out


def decide(
    desired: Desired,
    actual: Actual,
    *,
    now: float,
    last_apply: float | None,
    cooldown_s: float = 120.0,
) -> tuple[Decision, list[Diff]]:
    """Never blindly overwrite: protection wins, and re-applies are rate-limited."""
    if not actual.online:
        return Decision.OFFLINE, []
    diffs = diff(desired, actual)
    if not diffs:
        return Decision.OK, []
    if actual.protected:
        return Decision.SKIP_PROTECTED, diffs
    if last_apply is not None and now - last_apply < cooldown_s:
        return Decision.WAIT, diffs
    return Decision.APPLY, diffs


def clamp(desired: Desired, *, max_charge_w: int, max_discharge_w: int, min_soc: int) -> Desired:
    """Enforce device limits before anything is sent."""
    power = desired.power_w
    if power is not None:
        limit = max_charge_w if desired.mode is Mode.CHARGE else max_discharge_w
        power = max(0, min(power, limit))
    reserve = desired.reserve_soc
    if reserve is not None:
        reserve = max(reserve, min_soc)
    return Desired(desired.mode, power, desired.target_soc, reserve)
