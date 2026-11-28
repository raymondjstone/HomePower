"""Solar forecast against what really came (HA-free).

The forecast comes from Forecast.Solar or Solcast: their "today" sensors give the day's total, and their energy platform
(the one Home Assistant's Energy dashboard uses) gives the expected energy for each hour. This module turns those hourly
figures into kWh per local hour, keeps a short history of each day's forecast, and lines it up with the solar HomePower
measured, by day and by hour.

The forecast for a day is kept twice: the total first seen that day ("first": what the day was planned with, before the
weather could prove it right or wrong) and the hourly profile ("hours"). Each hour's figure is refreshed until that hour
starts and then kept: it is the last forecast made before the hour. (After an hour has passed, Solcast's energy data
replaces its forecast with its own estimate of what came, and a forecast can be revised: neither is a forecast any more.)
"""
from __future__ import annotations
from typing import Any, cast

from datetime import datetime, timedelta, tzinfo

KEEP_DAYS = 120


def _num(x: Any) -> float | None:
    try:
        f = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if f == f and abs(f) != float("inf") and f >= 0 else None


def hours_by_day(wh_hours: Any, tz: tzinfo | None, keyed_by_end: bool = False) -> dict[str, list[float]]:
    """{"2026-11-25T08:00:00+00:00": Wh, ...} (each the energy expected in the hour starting then) -> {local day: [kWh x 24]}.
    With `keyed_by_end` each key is the END of its hour instead (Forecast.Solar before Home Assistant moved its keys to
    the period start), so it is moved back an hour. Entries that are not a time and a finite, non-negative number are
    skipped. On the autumn clock change the repeated hour's energy is added together, so the day's total stays right."""
    out: dict[str, list[float]] = {}
    for ts, wh in (wh_hours or {}).items() if isinstance(wh_hours, dict) else []:
        v = _num(wh)
        try:
            t = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))
        except ValueError:
            continue
        if v is None or t.tzinfo is None:
            continue
        if keyed_by_end:
            t -= timedelta(hours=1)
        local = t.astimezone(tz)
        out.setdefault(local.date().isoformat(), [0.0] * 24)[local.hour] += v / 1000
    return {d: [round(x, 4) for x in h] for d, h in out.items()}


def remaining_kwh(hours: list[float] | None, now: datetime) -> float | None:
    """What is still to come today: the hours after this one, and the rest of this one."""
    if not hours or len(hours) != 24:
        return None
    part = 1 - (now.minute * 60 + now.second) / 3600
    return round(sum(hours[now.hour + 1:]) + hours[now.hour] * part, 2)


class SolarForecastLog:
    """Per local day: {"first": kWh first seen that day, "hours": [kWh x 24] latest hourly forecast}."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.days: dict[str, dict[str, Any]] = {}
        for day, row in (data.items() if isinstance(data, dict) else []):       # a damaged save never stops start-up
            if not isinstance(day, str) or len(day) != 10 or not isinstance(row, dict):
                continue
            first = _num(row.get("first"))
            hours = row.get("hours")
            hours = [(_num(x) or 0.0) for x in hours] if isinstance(hours, list) and len(hours) == 24 else None
            if first is not None or hours is not None:
                self.days[day] = {"first": first, "hours": hours}

    def note_total(self, day: str, kwh: float | None) -> None:
        v = _num(kwh)
        if v is None:
            return
        row = self.days.setdefault(day, {"first": None, "hours": None})
        if row["first"] is None:
            row["first"] = round(v, 2)

    def note_hours(self, day: str, hours: list[float], from_hour: int = 0) -> None:
        """The latest hourly forecast for a day. Hours before `from_hour` (those already started, today) keep what was
        noted before they began; the first time a day is seen they are taken as given, being the best there is."""
        if not (isinstance(hours, list) and len(hours) == 24):
            return
        row = self.days.setdefault(day, {"first": None, "hours": None})
        new = [round(_num(x) or 0.0, 4) for x in hours]
        if row["hours"] is not None and from_hour > 0:
            new[:from_hour] = row["hours"][:from_hour]
        row["hours"] = new

    def prune(self, today: str) -> None:
        keep = sorted(d for d in self.days if d <= today)[-KEEP_DAYS:] + [d for d in self.days if d > today][:2]
        self.days = {d: self.days[d] for d in sorted(set(keep))}

    def forecast_total(self, day: str) -> float | None:
        """The day's forecast as first seen, or (if HomePower only saw the hourly profile) the sum of that."""
        row = self.days.get(day)
        if not row:
            return None
        if row["first"] is not None:
            return cast(float, row["first"])
        return round(sum(row["hours"]), 2) if row["hours"] else None

    def dump(self) -> dict[str, Any]:
        return {d: dict(r) for d, r in self.days.items()}


def compare_days(log: SolarForecastLog, actual: dict[str, Any], today: str, n: int = 30) -> list[dict[str, Any]]:
    """The last n days up to today: the forecast and the solar measured (None where either is not known)."""
    end = datetime.fromisoformat(today).date()
    out = []
    for i in range(n - 1, -1, -1):
        day = (end - timedelta(days=i)).isoformat()
        a = _num(actual.get(day)) if isinstance(actual, dict) else None
        out.append({"day": day, "forecast": log.forecast_total(day), "actual": None if a is None else round(a, 2)})
    while out and out[0]["forecast"] is None and out[0]["actual"] is None:
        out.pop(0)                    # days before anything was recorded are left off the chart
    return out


def compare_hours(forecast: list[float] | None, actual_half_hours: list[float] | None) -> list[dict[str, Any]]:
    """Today hour by hour: the latest forecast and the solar measured (half-hours added into hours)."""
    fc = forecast if isinstance(forecast, list) and len(forecast) == 24 else None
    act = actual_half_hours if isinstance(actual_half_hours, list) and len(actual_half_hours) == 48 else None
    return [{"hour": h, "forecast": None if fc is None else round(fc[h], 3),
             "actual": None if act is None else round((_num(act[2 * h]) or 0.0) + (_num(act[2 * h + 1]) or 0.0), 3)} for h in range(24)]
