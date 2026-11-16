"""Plain checks that a battery is really doing what it was told, and that its readings agree with the house meter.

No Home Assistant imports, so they are unit-tested directly.
"""
from __future__ import annotations

CHARGING_W = 50          # below this a battery told to charge is not really charging
STUCK_AFTER_S = 20 * 60  # told to charge, but nothing happened for this long
SIGN_MIN_W = 300         # both readings must be clearly away from zero before their signs are compared


class ChargeWatch:
    """Notices a battery that was told to charge, accepted the command, and then did nothing (state bytes say Charge,
    power and charge level say otherwise). One entry per battery."""

    def __init__(self, after_s: float = STUCK_AFTER_S) -> None:
        self.after_s = after_s
        self._since: dict[str, tuple[float, float]] = {}      # id -> (when it started waiting, charge level then)

    def stuck(self, did: str, wanted: bool, soc: float | None, power_w: float | None, target: float | None, now_s: float) -> bool:
        if (not wanted or soc is None or (target is not None and soc >= target - 1)
                or (power_w is not None and power_w >= CHARGING_W)):
            self._since.pop(did, None)
            return False
        since, soc0 = self._since.setdefault(did, (now_s, soc))
        if soc >= soc0 + 1:                                   # the level is rising: it is charging, whatever the power says
            self._since[did] = (now_s, soc)
            return False
        return now_s - since >= self.after_s

    def forget(self, did: str) -> None:
        self._since.pop(did, None)


def grid_sign_mismatch(meter_w: float | None, device_w: float | None, min_w: float = SIGN_MIN_W) -> bool:
    """The house meter and a battery's own grid reading point opposite ways by a clear margin (import is positive for both)."""
    if meter_w is None or device_w is None or abs(meter_w) < min_w or abs(device_w) < min_w:
        return False
    return (meter_w > 0) != (device_w > 0)


def cycling_margin(buy: float, avoided: float, efficiency: float) -> dict:
    """What storing one kWh is worth: `buy` is what it costs to charge it, `avoided` what the energy would otherwise cost
    (or earn if exported) when it is used later, `efficiency` the battery's round trip (0-1). Prices in the same unit."""
    eff = min(1.0, max(0.05, efficiency))
    net = avoided * eff - buy
    return {"net_per_kwh": round(net, 4), "worth_it": net > 0, "break_even_price": round(buy / eff, 4),
            "efficiency_pct": round(eff * 100, 1)}
