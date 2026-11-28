"""'What would the same usage have cost on another tariff?' (HA-free).

Tariffs are typed one per line in Settings, in main currency units per kWh:
    Name; peak=0.28; cheap=0.07; window=00:30-05:30, 13:00-16:00; export=0.15; standing=0.45
`cheap` and `window` are optional (a flat tariff), `export` and `standing` (per day) default to 0."""
from __future__ import annotations
from typing import Any

from datetime import time
from math import isfinite

MAX_TARIFFS = 8


def _t(v: str) -> time:
    h, m = v.strip().split(":")[:2]
    return time(int(h), int(m))


def parse_tariffs(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        name, *parts = [p.strip() for p in line.split(";")]
        if not name or "=" in name:
            raise ValueError(f"'{line}': start the line with the tariff's name")
        if any(t["name"].lower() == name.lower() for t in out):
            raise ValueError(f"'{name}' is listed twice: give each tariff its own name")     # results are kept by name
        kv: dict[str, str] = {}
        for p in parts:
            if "=" not in p:
                raise ValueError(f"'{name}': '{p}' should look like peak=0.28")
            k, v = p.split("=", 1)
            kv[k.strip().lower()] = v.strip()
        unknown = set(kv) - {"peak", "cheap", "window", "export", "standing"}
        if unknown:
            raise ValueError(f"'{name}': unknown field {sorted(unknown)[0]} (use peak, cheap, window, export, standing)")
        if "peak" not in kv:
            raise ValueError(f"'{name}': peak=... is required")
        try:
            peak = float(kv["peak"])
            cheap = float(kv["cheap"]) if "cheap" in kv else None
            windows = []
            for w in kv.get("window", "").replace("–", "-").split(","):
                if w.strip():
                    a, b = w.split("-")
                    windows.append((_t(a), _t(b)))
            export, standing = float(kv.get("export", 0)), float(kv.get("standing", 0))
        except (ValueError, TypeError) as err:
            raise ValueError(f"'{name}': a number or a time (HH:MM-HH:MM) is not valid") from err
        if not all(isfinite(x) for x in (peak, export, standing) + ((cheap,) if cheap is not None else ())):
            raise ValueError(f"'{name}': a price must be a number")       # 'nan' or 'inf' parse as floats
        if (cheap is None) != (not windows):
            raise ValueError(f"'{name}': give both cheap=... and window=..., or neither")
        out.append({"name": name, "peak": peak, "cheap": cheap, "windows": windows, "export": export, "standing": standing})
    if len(out) > MAX_TARIFFS:
        raise ValueError(f"at most {MAX_TARIFFS} tariffs")
    return out


def _in_window(slot: int, windows: list[tuple[time, time]]) -> bool:
    t = time(slot // 2, 30 * (slot % 2))
    return any((a <= t < b) if a < b else (t >= a or t < b) for a, b in windows)


def compare(usage: dict[str, dict[str, list[float]]], tariffs: list[dict[str, Any]], days: int = 30) -> dict[str, Any]:
    """Cost of the recorded half-hourly import/export under each tariff, over the newest `days` days that have data."""
    keys = sorted(usage)[-days:]
    res = []
    for t in tariffs:
        buy = earn = 0.0
        for k in keys:
            for i in range(48):
                rate = t["cheap"] if t["cheap"] is not None and _in_window(i, t["windows"]) else t["peak"]
                buy += usage[k]["imp"][i] * rate
                earn += usage[k]["exp"][i] * t["export"]
        standing = t["standing"] * len(keys)
        res.append({"name": t["name"], "import_cost": round(buy, 2), "export_earned": round(earn, 2),
                    "standing": round(standing, 2), "total": round(buy - earn + standing, 2)})
    return {"days": len(keys), "import_kwh": round(sum(sum(usage[k]["imp"]) for k in keys), 1),
            "export_kwh": round(sum(sum(usage[k]["exp"]) for k in keys), 1), "tariffs": sorted(res, key=lambda r: r["total"]),
            "monthly": monthly(usage, tariffs)}


def monthly(usage: dict[str, dict[str, list[float]]], tariffs: list[dict[str, Any]]) -> dict[str, Any]:
    """Each tariff's cost month by month over all the recorded days, and which tariff has been cheaper than the first
    one listed (taken to be the one you are on) for the last few complete months running."""
    months: dict[str, list[str]] = {}
    for k in sorted(usage):
        months.setdefault(k[:7], []).append(k)
    names = sorted(months)
    try:
        lowest = 0 if not names or int(months[names[0]][0][8:10]) <= 3 else 1       # recording began part way through the first month
    except ValueError:
        lowest = 0
    totals: dict[str, list[float]] = {}
    for t in tariffs:
        row = []
        for m in names:
            buy = earn = 0.0
            for k in months[m]:
                for i in range(48):
                    rate = t["cheap"] if t["cheap"] is not None and _in_window(i, t["windows"]) else t["peak"]
                    buy += usage[k]["imp"][i] * rate
                    earn += usage[k]["exp"][i] * t["export"]
            row.append(round(buy - earn + t["standing"] * len(months[m]), 2))
        totals[t["name"]] = row
    flag = None
    if len(tariffs) > 1 and len(names) > 1:
        cur = tariffs[0]["name"]
        for t in tariffs[1:]:
            run, saved = 0, 0.0
            # the newest month is still in progress: leave it out; so is the first one when recording began part way through it
            for i in range(len(names) - 2, lowest - 1, -1):
                gain = totals[cur][i] - totals[t["name"]][i]
                if gain <= 0:
                    break
                run, saved = run + 1, saved + gain
            if run >= 3 and (flag is None or saved > flag["saved"]):
                flag = {"tariff": t["name"], "current": cur, "months": run, "saved": round(saved, 2)}
    return {"months": names, "totals": totals, "cheaper_than_current": flag}
