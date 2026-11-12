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
    """Estimated money saved by the batteries, per day (prices in pounds/euros per kWh).

    Each battery keeps a running average of what the energy now stored in it cost: grid charging adds its price, solar
    charging adds nothing, and the energy lost going in and out raises the average (the same money buys fewer usable
    kWh). The money is counted when the energy is *used*: every kWh discharged saves the price of the grid energy it
    replaced (or earns the export price, when it went out to the grid) minus what that kWh cost to store. So a night of
    cheap charging does not show as a loss the next morning; it shows as savings through the day as the charge is used."""

    KEEP_DAYS = 400

    def __init__(self, data: dict | None = None, stores: dict | None = None) -> None:
        self.days: dict[str, dict[str, float]] = {k: dict(v) for k, v in (data or {}).items()}
        # battery -> [kWh stored, cost per stored kWh]
        self.stores: dict[str, list[float]] = {k: [float(v[0]), float(v[1])] for k, v in (stores or {}).items()}

    def add(self, day: str, device_id: str, charged_kwh: float, grid_share: float, discharged_kwh: float,
            export_share: float, price: float | None, export_price: float, stored_kwh: float | None,
            first_cost: float | None = None) -> None:
        """One step for one battery. `grid_share` = fraction of the charging that came from the grid (the rest was solar);
        `export_share` = fraction of the discharge that went out to the grid rather than into the house; `stored_kwh` =
        energy in the battery now, from its charge level (None if unknown); `first_cost` = what to assume the energy
        already in a battery cost when it is first seen (the cheapest rate around)."""
        st = self.stores.get(device_id)
        if price is None:
            if st is not None and stored_kwh is not None:
                st[0] = stored_kwh             # follow the level; the cost per kWh is unchanged
            return
        if st is None:
            st = self.stores[device_id] = [stored_kwh or 0.0, price if first_cost is None else first_cost]
        kwh, avg = st
        share, exp = max(0.0, min(1.0, grid_share)), max(0.0, min(1.0, export_share))
        pool = kwh * avg + charged_kwh * share * price - discharged_kwh * avg
        new_kwh = stored_kwh if stored_kwh is not None else max(0.0, kwh + charged_kwh - discharged_kwh)
        if new_kwh > 0.05:
            # bounded: a blend of the old average and today's price, raised at most by charging/discharging losses
            st[1] = min(max(0.0, pool) / new_kwh, max(avg, price * share) / 0.75)
        elif charged_kwh > 0 and new_kwh <= 0.05:
            st[1] = price * share              # empty battery: the next energy in sets the cost
        st[0] = new_kwh
        if charged_kwh <= 0 and discharged_kwh <= 0:
            return
        d = self.days.setdefault(day, {"charged": 0.0, "discharged": 0.0, "cost": 0.0, "value": 0.0})
        d["charged"] += charged_kwh
        d["discharged"] += discharged_kwh
        d["cost"] += discharged_kwh * avg                                         # what the energy used had cost to store
        d["value"] += discharged_kwh * ((1 - exp) * price + exp * export_price)   # the import it replaced, or export earnings
        d["bought"] = d.get("bought", 0.0) + charged_kwh * share * price          # spent on grid charging (shown, not netted)
        for old in sorted(self.days)[:-self.KEEP_DAYS]:
            del self.days[old]

    def forget(self, keep: set[str]) -> None:
        for did in [d for d in self.stores if d not in keep]:
            del self.stores[did]

    def stored_cost(self, device_id: str) -> float | None:
        st = self.stores.get(device_id)
        return None if st is None else st[1]

    def _sum(self, prefix: str, key: str) -> float:
        return sum(v[key] for k, v in self.days.items() if k.startswith(prefix))

    def saved(self, prefix: str) -> float:
        """Savings for a day ("2026-11-10") or a month ("2026-11")."""
        return self._sum(prefix, "value") - self._sum(prefix, "cost")

    def dump(self) -> dict:
        return self.days
