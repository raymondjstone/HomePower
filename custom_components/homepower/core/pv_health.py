"""Solar string health for inverters that report each PV input (the Anker X1's PV1-PV8): HA-free.

Two checks, both comparing a string with the other strings on the same inverter, so the weather cancels out:
  * dead: a string that normally produces gives (almost) nothing for half an hour while the others produce well
    (a blown fuse, a pulled connector, a failed optimiser or MPPT input);
  * weak: over two whole days in a row, a string's share of the day's solar is well below its usual share
    (new shading, soiling, a failing panel). Daily shares are used because strings facing different ways share the
    output differently through the day; over a whole day the share is steady.
"""
from __future__ import annotations

from statistics import median

KEEP_DAYS = 21          # daily energy per string kept for the usual share
MIN_TOTAL_W = 300       # only judge while the inverter as a whole sees real light
USED_V = 30.0           # a string seen above this voltage has panels on it
MIN_DAY_WH = 1000       # a day with less solar than this is too dull to judge shares by
MIN_SHARE = 0.05        # strings that normally give under 5 % are too small to judge
DEAD_SHARE = 0.01       # "nothing": under 1 % of the inverter's PV power
DEAD_AFTER_S = 1800
WEAK_RATIO = 0.6        # a day's share under 60 % of the usual share is weak
BASELINE_DAYS = 3       # days needed (before the two being judged) to know a string's usual share
MAX_STEP_S = 120        # a gap longer than this (restart, offline) is not filled in


def _num(x) -> float:
    f = float(x)
    if f != f or abs(f) == float("inf"):
        raise ValueError("not finite")
    return f


class PvHealth:
    """Per battery (inverter): which strings are used, energy per string per day, and the live "dead" timers."""

    def __init__(self, data: dict | None = None) -> None:
        self.used: dict[str, list[bool]] = {}
        self.days: dict[str, dict[str, list[float]]] = {}
        d = data if isinstance(data, dict) else {}
        for did, row in d.items():           # a damaged save starts that battery afresh, it must never stop start-up
            if not isinstance(did, str) or not isinstance(row, dict):
                continue
            used = row.get("used")
            if isinstance(used, list) and len(used) == 8:
                self.used[did] = [bool(u) for u in used]
            days = row.get("days")
            if isinstance(days, dict):
                good: dict[str, list[float]] = {}
                for day, vals in days.items():
                    try:
                        if isinstance(day, str) and isinstance(vals, list) and len(vals) == 8:
                            good[day] = [max(0.0, _num(v)) for v in vals]
                    except (TypeError, ValueError):
                        continue
                self.days[did] = good
        self._last: dict[str, float] = {}           # monotonic time of the last reading per battery
        self._quiet: dict[tuple[str, int], float] = {}   # (battery, string) -> since when it has given nothing

    def dump(self) -> dict:
        return {did: {"used": self.used.get(did, [False] * 8), "days": self.days.get(did, {})}
                for did in set(self.used) | set(self.days)}

    def forget(self, did: str) -> None:
        self.used.pop(did, None)
        self.days.pop(did, None)
        self._last.pop(did, None)
        for k in [k for k in self._quiet if k[0] == did]:
            del self._quiet[k]

    def observe(self, did: str, day: str, strings, now_s: float) -> None:
        """One reading: `strings` is ((volts, amps), ...) for PV1-PV8, `day` the local date (YYYY-MM-DD)."""
        try:
            pts = [(_num(v), _num(a)) for v, a in (strings or ())][:8]
        except (TypeError, ValueError):
            return
        if len(pts) < 8:
            return
        power = [max(0.0, v * a) for v, a in pts]
        total = sum(power)
        used = self.used.setdefault(did, [False] * 8)
        for i, (v, _) in enumerate(pts):
            if v >= USED_V:
                used[i] = True
        last, self._last[did] = self._last.get(did), now_s
        step = 0.0 if last is None else now_s - last
        if total >= MIN_TOTAL_W and 0 < step <= MAX_STEP_S:
            row = self.days.setdefault(did, {}).setdefault(day, [0.0] * 8)
            for i in range(8):
                row[i] += power[i] * step / 3600
            days = self.days[did]
            for old in sorted(days)[:-KEEP_DAYS]:
                del days[old]
        usual = self.usual_shares(did, day)
        for i in range(8):
            key = (did, i)
            if total >= MIN_TOTAL_W and used[i] and (usual[i] or 0) >= MIN_SHARE and power[i] < DEAD_SHARE * total:
                self._quiet.setdefault(key, now_s)
            else:
                self._quiet.pop(key, None)

    def _shares(self, row: list[float]) -> list[float] | None:
        tot = sum(row)
        return [x / tot for x in row] if tot >= MIN_DAY_WH else None

    def usual_shares(self, did: str, before: str, skip: int = 0) -> list[float | None]:
        """Each string's usual share of a day's solar: the median over the finished days before `before`, leaving out the
        last `skip` of them. None for a string until BASELINE_DAYS good days are known."""
        days = self.days.get(did, {})
        good = [s for d in sorted(days) if d < before and (s := self._shares(days[d])) is not None]
        good = good[:-skip] if skip else good
        if len(good) < BASELINE_DAYS:
            return [None] * 8
        return [median(s[i] for s in good) for i in range(8)]

    def status(self, did: str, today: str, now_s: float) -> list[dict]:
        """Per string: in use, today's energy and share, the usual share, and a state: ok / weak / dead / unused / learning."""
        used = self.used.get(did, [False] * 8)
        days = self.days.get(did, {})
        finished = [d for d in sorted(days) if d < today and self._shares(days[d]) is not None]
        usual = self.usual_shares(did, today)                 # shown, and what "dead" is judged by
        base = self.usual_shares(did, today, skip=2)          # the weak check compares the last two days with the days before them
        recent = [self._shares(days[d]) for d in finished[-2:]]
        now_row = days.get(today, [0.0] * 8)
        now_shares = self._shares(now_row)
        out = []
        for i in range(8):
            if not used[i]:
                out.append({"string": i + 1, "state": "unused"})
                continue
            item = {"string": i + 1, "today_wh": round(now_row[i]), "today_share": None if now_shares is None else round(now_shares[i] * 100, 1),
                    "usual_share": None if usual[i] is None else round(usual[i] * 100, 1), "state": "ok", "note": ""}
            since = self._quiet.get((did, i))
            if since is not None and now_s - since >= DEAD_AFTER_S:
                item["state"] = "dead"
                item["note"] = f"PV{i + 1} has given almost nothing for {int((now_s - since) // 60)} min while the other strings produce"
            elif usual[i] is None:
                item["state"] = "learning"
            elif (base[i] is not None and base[i] >= MIN_SHARE and len(recent) == 2
                  and all(r is not None and r[i] < WEAK_RATIO * base[i] for r in recent)):
                item["state"] = "weak"
                item["note"] = (f"PV{i + 1} gave {recent[0][i] * 100:.0f}% and {recent[1][i] * 100:.0f}% of the solar on the last two days, "
                                f"against its usual {base[i] * 100:.0f}% (new shading, dirt or a failing panel?)")
            out.append(item)
        return out

    def warnings(self, did: str, today: str, now_s: float) -> dict[int, str]:
        """String number -> what is wrong, for the alerts."""
        return {s["string"]: s["note"] for s in self.status(did, today, now_s) if s["state"] in ("dead", "weak")}
