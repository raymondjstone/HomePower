"""Spread cheap-rate charging across batteries within a whole-house import limit."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChargeRequest:
    device_id: str
    soc: float
    target_soc: float
    capacity_wh: int
    max_charge_w: int
    priority: int = 0  # lower = served first when power is short


def energy_needed_wh(r: ChargeRequest) -> float:
    return max(0.0, (r.target_soc - r.soc) / 100.0 * r.capacity_wh)


def allocate(
    requests: list[ChargeRequest],
    window_hours: float,
    import_limit_w: float,
    base_load_w: float = 0.0,
) -> dict[str, int]:
    """Per-device charge power (W) so every target is met by window end if possible.

    Each device wants energy/hours (its just-in-time rate, capped by its max).
    If the sum exceeds headroom under the import limit, higher-priority devices
    are filled first. Returns 0 for devices already at target.
    """
    out = {r.device_id: 0 for r in requests}
    if window_hours <= 0:
        return out
    headroom = max(0.0, import_limit_w - base_load_w)
    wants = {
        r.device_id: min(r.max_charge_w, energy_needed_wh(r) / window_hours)
        for r in requests
    }
    if sum(wants.values()) <= headroom:
        return {k: int(round(v)) for k, v in wants.items()}
    remaining = headroom
    for r in sorted(requests, key=lambda r: r.priority):
        give = min(wants[r.device_id], remaining)
        out[r.device_id] = int(give)
        remaining -= give
    return out
