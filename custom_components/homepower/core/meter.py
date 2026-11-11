"""Smart-meter readings: a sensor may report power (W/kW) or a cumulative energy counter (Wh/kWh)."""
from __future__ import annotations

_POWER = {"w": 1.0, "kw": 1000.0, "mw": 1e6}
_ENERGY_KWH = {"wh": 0.001, "kwh": 1.0, "mwh": 1000.0}
MAX_WINDOW_S = 900  # a counter that has not moved for this long means "no power", not "stale"


def is_energy_unit(unit: str | None) -> bool:
    return str(unit or "").strip().lower() in _ENERGY_KWH


class MeterReader:
    """Turns sensor states into watts. Power sensors convert units; energy counters are differentiated.

    Counters update in coarse steps, so the last derived power is held until the counter moves again,
    and falls to 0 once it has been still for twice the previous step (or MAX_WINDOW_S)."""

    def __init__(self) -> None:
        self._c: dict[str, dict] = {}

    def read(self, key: str, state: str | None, unit: str | None, now_s: float) -> float | None:
        try:
            v = float(state)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            self._c.pop(key, None)
            return None
        u = str(unit or "").strip().lower()
        if u in _ENERGY_KWH:
            return self._counter(key, v * _ENERGY_KWH[u], now_s)
        self._c.pop(key, None)
        return v * _POWER.get(u, 1.0)  # no unit: assume watts, as before

    def _counter(self, key: str, kwh: float, now: float) -> float | None:
        c = self._c.get(key)
        if c is None or kwh < c["kwh"]:           # first sight, or the counter reset
            self._c[key] = {"kwh": kwh, "t": now, "w": None, "step": None}
            return None
        if kwh > c["kwh"]:
            dt = now - c["t"]
            if 0 < dt <= MAX_WINDOW_S * 4:
                c["w"], c["step"] = (kwh - c["kwh"]) * 3.6e6 / dt, dt
            else:
                c["w"], c["step"] = None, None
            c["kwh"], c["t"] = kwh, now
            return c["w"]
        if c["w"] is None:
            return None
        if now - c["t"] > min(MAX_WINDOW_S, 2 * max(c["step"] or 60, 60)):
            return 0.0
        return c["w"]
