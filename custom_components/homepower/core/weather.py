"""Cold-weather planning: how much more the house uses on a cold day, and what that means for the overnight target.

Tomorrow's temperature comes from a Home Assistant weather entity, a sensor, or (only if switched on) Open-Meteo. The
house's own response to cold is learned from its recorded daily use against the daily temperature. No Home Assistant
imports, so everything here is unit-tested directly.
"""
from __future__ import annotations
from typing import Any

from datetime import date, datetime, tzinfo
from math import isfinite

BASE_C = 18.0           # below this the house is assumed to need heating
MIN_DAYS = 7            # days of use and temperature needed before the link is trusted
MIN_SPREAD = 3.0        # the days seen must differ by at least this many degree-days, or there is nothing to learn from
MIN_R = 0.3             # a weaker link than this is noise
MAX_POINTS = 25        # the target is never raised by more than this many points


def to_celsius(value: float, unit: str | None) -> float:
    u = (unit or "").upper().replace("°", "").strip()
    return (value - 32) * 5 / 9 if u == "F" else value


def degree_days(temp_c: float) -> float:
    """How far below the heating threshold the day's mean temperature is (0 on a warm day)."""
    return max(0.0, BASE_C - temp_c)


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and isfinite(v) else None


def pick_tomorrow(forecast: list[Any], tomorrow: date, tz: tzinfo | None, unit: str | None = "C") -> float | None:
    """Tomorrow's mean temperature (°C) from a weather entity's forecast list (daily entries, or hourly ones)."""
    temps: list[float] = []
    for item in forecast if isinstance(forecast, (list, tuple)) else []:
        if not isinstance(item, dict):
            continue
        try:
            when = datetime.fromisoformat(str(item.get("datetime")).replace("Z", "+00:00"))
        except ValueError:
            continue
        local = when.astimezone(tz).date() if tz is not None and when.tzinfo else when.date()
        if local != tomorrow:
            continue
        hi, lo = _num(item.get("temperature")), _num(item.get("templow"))
        if hi is not None and lo is not None:
            temps.append((hi + lo) / 2)            # a daily entry with a high and a low
        elif hi is not None:
            temps.append(hi)                        # an hourly entry (or a daily one with only one figure)
    return round(to_celsius(sum(temps) / len(temps), unit), 1) if temps else None


def parse_open_meteo(data: dict[str, Any], tomorrow: date) -> float | None:
    """Tomorrow's mean of the daily high and low from an Open-Meteo reply (°C)."""
    try:
        d = data["daily"]
        i = d["time"].index(tomorrow.isoformat())
        hi, lo = _num(d["temperature_2m_max"][i]), _num(d["temperature_2m_min"][i])
    except (KeyError, ValueError, IndexError, TypeError, AttributeError):
        return None
    return round((hi + lo) / 2, 1) if hi is not None and lo is not None else None


class TempLog:
    """Mean outdoor temperature per day (kept for a few weeks)."""

    KEEP = 60

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.days: dict[str, list[float]] = {}
        for k, v in (data.items() if isinstance(data, dict) else ()):
            if isinstance(v, list) and len(v) == 2 and all(isinstance(x, (int, float)) and isfinite(x) for x in v):
                self.days[k] = [float(v[0]), float(v[1])]

    def add(self, day: str, temp_c: float) -> None:
        d = self.days.setdefault(day, [0.0, 0.0])
        d[0] += temp_c
        d[1] += 1
        for old in sorted(self.days)[:-self.KEEP]:
            del self.days[old]

    def mean(self, day: str, min_samples: int = 360) -> float | None:     # about an hour of readings, so a day seen for minutes is not trusted
        d = self.days.get(day)
        return d[0] / d[1] if d and d[1] >= min_samples else None

    def dump(self) -> dict[str, Any]:
        return {k: [round(v[0], 2), int(v[1])] for k, v in self.days.items()}


class ColdModel:
    """Extra watt-hours the house uses per degree-day, fitted from its own recorded days."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        d = data if isinstance(data, dict) else {}
        self.k: float | None = float(d["k"]) if isinstance(d.get("k"), (int, float)) and isfinite(d["k"]) else None     # Wh per degree-day
        self.days: int = int(d.get("days", 0)) if isinstance(d.get("days"), (int, float)) else 0
        self.r: float | None = float(d["r"]) if isinstance(d.get("r"), (int, float)) else None

    def fit(self, temps: TempLog, usage_days: dict[str, Any], today: str) -> None:
        """Line through (degree-days, kWh used) for every finished day with both figures."""
        xs, ys = [], []
        for day, rec in usage_days.items():
            t = temps.mean(day)
            load = rec.get("load") if isinstance(rec, dict) else None
            if day == today or t is None or not load or len(load) != 48:
                continue
            kwh = sum(load)
            if kwh > 0.5 and sum(1 for x in load if x > 0) >= 40:     # a whole day: not one HomePower only saw part of
                xs.append(degree_days(t))
                ys.append(kwh)
        self.days = len(xs)
        if len(xs) < MIN_DAYS or max(xs) - min(xs) < MIN_SPREAD:
            self.k, self.r = None, None
            return
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        syy = sum((y - my) ** 2 for y in ys)
        sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        if sxx <= 0 or syy <= 0:
            self.k, self.r = None, None
            return
        r = sxy / (sxx * syy) ** 0.5
        slope = sxy / sxx
        self.r = round(r, 2)
        self.k = round(slope * 1000, 1) if slope > 0 and r >= MIN_R else None

    def dump(self) -> dict[str, Any]:
        return {"k": self.k, "days": self.days, "r": self.r}


def recent_degree_days(temps: TempLog, today: str, n: int = 7) -> float | None:
    """The average degree-days of the last few finished days (what the usual-use figure already reflects)."""
    days = [d for d in sorted(temps.days) if d < today][-n:]
    vals = [degree_days(m) for d in days if (m := temps.mean(d)) is not None]
    return sum(vals) / len(vals) if len(vals) >= 3 else None


def cold_extra_wh(k: float | None, tomorrow_c: float | None, recent_dd: float | None) -> float:
    """Extra energy tomorrow compared with the recent days, if tomorrow is colder (never negative)."""
    if k is None or tomorrow_c is None or recent_dd is None or not all(isfinite(x) for x in (k, tomorrow_c, recent_dd)):
        return 0.0
    return max(0.0, k * (degree_days(tomorrow_c) - recent_dd))


def boost_points(extra_wh: float, capacity_wh: float, max_points: int = MAX_POINTS) -> int:
    """The extra energy as percentage points of the batteries' capacity."""
    if not (extra_wh > 0 and capacity_wh > 0):          # also false for NaN
        return 0
    if not isfinite(extra_wh / capacity_wh):
        return max_points
    return int(min(max_points, round(extra_wh / capacity_wh * 100)))
