"""Energy counters (for the HA Energy dashboard) and the savings ledger."""
from __future__ import annotations

MAX_GAP_S = 300  # don't integrate across gaps longer than this (restart, outage)


class EnergyMeter:
    """Integrates power into lifetime kWh totals. Positive watts -> `<key>_in`, negative -> `<key>_out`."""

    def __init__(self, totals: dict | None = None) -> None:
        self.totals: dict[str, float] = {k: float(v) for k, v in (totals or {}).items()}
        self._last: dict[str, tuple[float, float]] = {}

    def add(self, key: str, now_s: float, watts: float | None) -> tuple[float, float]:
        """Record a reading; returns the (in, out) kWh added by this step."""
        if watts is None:
            self._last.pop(key, None)
            return 0.0, 0.0
        last = self._last.get(key)
        self._last[key] = (now_s, watts)
        if last is None:
            return 0.0, 0.0
        dt = now_s - last[0]
        if dt <= 0 or dt > MAX_GAP_S:
            return 0.0, 0.0
        avg = (last[1] + watts) / 2
        kwh = abs(avg) * dt / 3.6e6
        d_in, d_out = (kwh, 0.0) if avg > 0 else (0.0, kwh)
        self.totals[f"{key}_in"] = self.totals.get(f"{key}_in", 0.0) + d_in
        self.totals[f"{key}_out"] = self.totals.get(f"{key}_out", 0.0) + d_out
        return d_in, d_out

    def get(self, key: str) -> float:
        return self.totals.get(key, 0.0)


class Ledger:
    """Estimated money saved by the batteries, per day. Prices are in whatever unit the rates use."""

    KEEP_DAYS = 400

    def __init__(self, data: dict | None = None) -> None:
        self.days: dict[str, dict[str, float]] = {k: dict(v) for k, v in (data or {}).items()}

    def add(self, day: str, charged_kwh: float, grid_share: float, discharged_kwh: float, price: float | None) -> None:
        """`grid_share` = fraction of the charging that came from the grid (the rest was free solar)."""
        if price is None or (charged_kwh <= 0 and discharged_kwh <= 0):
            return
        d = self.days.setdefault(day, {"charged": 0.0, "discharged": 0.0, "cost": 0.0, "value": 0.0})
        d["charged"] += charged_kwh
        d["discharged"] += discharged_kwh
        d["cost"] += charged_kwh * max(0.0, min(1.0, grid_share)) * price
        d["value"] += discharged_kwh * price
        for old in sorted(self.days)[:-self.KEEP_DAYS]:
            del self.days[old]

    def _sum(self, prefix: str, key: str) -> float:
        return sum(v[key] for k, v in self.days.items() if k.startswith(prefix))

    def saved(self, prefix: str) -> float:
        """Savings for a day ("2026-11-10") or a month ("2026-11")."""
        return self._sum(prefix, "value") - self._sum(prefix, "cost")

    def dump(self) -> dict:
        return self.days
