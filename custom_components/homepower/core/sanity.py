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


# ---------------------------------------------------------------- batteries that cannot be told a power


class RateLearner:
    """Learns the power a battery really charges at when it picks its own rate (EcoFlow): the median of recent readings
    taken while it was told to charge. Until it has enough samples the per-model estimate is used."""

    MIN_SAMPLES = 6
    KEEP = 20
    MIN_W = 500          # slower than this is a battery tapering or being throttled, not its rate

    def __init__(self, data: dict | None = None) -> None:
        self.rates: dict[str, int] = {k: int(v) for k, v in (data.items() if isinstance(data, dict) else ())
                                      if isinstance(v, (int, float)) and 0 < v < 1e6}
        self._samples: dict[str, list[float]] = {}

    def observe(self, did: str, charging_wanted: bool, power_w: float | None, soc: float | None) -> bool:
        """Returns True when the learned rate for this battery changed (so the caller can save it)."""
        if not charging_wanted or power_w is None or power_w < self.MIN_W or (soc is not None and soc >= 97):
            return False
        s = self._samples.setdefault(did, [])
        s.append(float(power_w))
        del s[:-self.KEEP]
        if len(s) < self.MIN_SAMPLES:
            return False
        med = sorted(s)[len(s) // 2]
        old = self.rates.get(did)
        if old is None or abs(med - old) / old >= 0.10:
            self.rates[did] = int(round(med, -1))
            return True
        return False

    def forget(self, did: str) -> None:
        self.rates.pop(did, None)
        self._samples.pop(did, None)


def next_unconfirmed(unconfirmed: int, waited: int, remaining: bool, slow: bool, grace: int = 1) -> tuple[int, int]:
    """After a command the battery still disagrees. A slow (cloud) battery gets one cycle's grace before that counts as
    'not following commands'. Returns the new (unconfirmed, waited)."""
    if not remaining:
        return 0, 0
    if slow and waited < grace:
        return unconfirmed, waited + 1
    return unconfirmed + 1, waited


class CommandBudget:
    """At most `limit` non-Auto commands per battery per day, so a flapping plan cannot hammer a rate-limited cloud."""

    def __init__(self, limit: int = 150) -> None:
        self.limit = limit
        self._n: dict[str, tuple[str, int]] = {}

    def allow(self, did: str, day: str, is_auto: bool) -> bool:
        d, n = self._n.get(did, (day, 0))
        if d != day:
            d, n = day, 0
        if is_auto:
            return True                    # handing a battery back is always allowed
        if n >= self.limit:
            self._n[did] = (d, n)
            return False
        self._n[did] = (d, n + 1)
        return True


# Anker's own words (issues 76, 81 and 124 of anker-charging/ha-anker-solix-official): a Solarbank's Modbus server stops while the battery
# cannot reach Anker's cloud, and returns when it can again. Confirmed by Anker as a firmware defect, for these two models.
_CLOUD_DEPENDENT = ("anker_e5000_pro", "anker_solarbank_max_ac")
SOLARBANK_CLOUD_HINT = ("The Solarbank 4 and Max AC stop answering on your network while they cannot reach Anker's cloud, even if the app showed them online "
                        "a moment ago. Opening the Anker app, or the battery getting its internet connection back, brings it back. Anker has confirmed this as a "
                        "firmware defect and is rolling out a fix; it is not a HomePower setting.")


def offline_hint(kind: str | None) -> str | None:
    """Why a battery of this kind may be unreachable even though it is working, when that is known; else None."""
    return SOLARBANK_CLOUD_HINT if kind in _CLOUD_DEPENDENT else None


def explain_idle(*, control_enabled: bool, paused: bool, dry_run: bool, online: bool, off_grid: bool, protected: bool,
                 last_error: str | None, guard_held: bool, unconfirmed: int, awaiting_confirm: bool = False, hint: str | None = None) -> str | None:
    """The main reason a battery is not being driven right now, in plain words (None = nothing is in the way)."""
    if not online:
        return "Offline: HomePower cannot read it." + (" " + hint if hint else "")
    if off_grid:
        return "The grid is down: the battery runs the house itself and gets no commands."
    if not control_enabled:
        return "Monitor only: 'Let HomePower send commands' is off for this battery."
    if paused:
        return "HomePower is paused."
    if dry_run:
        return "Test run is on: plans are worked out but nothing is sent."
    if awaiting_confirm:
        return "Waiting for you to allow the first command to this battery (button on its card)."
    if protected:
        return "The battery's own protection is active; HomePower does not override it."
    if last_error:
        return last_error
    if guard_held:
        return "Held by the software charge limit."
    if unconfirmed:
        return "The last command is not confirmed by the battery yet."
    return None
