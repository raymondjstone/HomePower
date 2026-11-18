"""Learning and bookkeeping that sits on top of the live readings (HA-free, tested without Home Assistant):
the household's usual load by time of day, daily self-consumption / cheap-rate shares, weekly battery health,
the combined "home battery" figures, and saved scenes."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import median

# ---------------------------------------------------------------- usual load by time of day


class LoadProfile:
    """Average house load for each half-hour of the day, learned across days.

    Readings arrive every few seconds, so each visit to a half-hour is first averaged on its own, and that visit average is
    then blended into the long-term figure (so a slot reflects the whole half-hour, not just its last minutes). Weekdays and
    weekends are kept apart. A slot is only trusted after MIN_VISITS days, so a fresh profile never invents a number."""

    ALPHA = 0.25            # weight of the newest day against the history
    MIN_VISITS = 2
    MIN_SAMPLES_PER_VISIT = 6   # ignore a half-hour that was barely observed (e.g. Home Assistant restarted in it)

    def __init__(self, data: dict | None = None) -> None:
        d = data or {}
        self.avg: dict[str, list[float]] = {k: list(d.get("avg", {}).get(k, [0.0] * 48)) for k in ("wd", "we")}
        self.n: dict[str, list[int]] = {k: list(d.get("n", {}).get(k, [0] * 48)) for k in ("wd", "we")}
        for k in ("wd", "we"):
            if len(self.avg[k]) != 48 or len(self.n[k]) != 48:
                self.avg[k], self.n[k] = [0.0] * 48, [0] * 48
        self._cur: list | None = None    # [(kind, slot, date), sum, count]

    @staticmethod
    def _slot(t: datetime) -> tuple[str, int]:
        return ("we" if t.weekday() >= 5 else "wd"), t.hour * 2 + t.minute // 30

    def _blend(self, avg: dict, n: dict, cur: list | None) -> None:
        """Blend a finished visit into `avg`/`n` (the long-term figures)."""
        if cur is None:
            return
        (kind, i, _), total, count = cur
        if count < self.MIN_SAMPLES_PER_VISIT:
            return
        mean = total / count
        avg[kind][i] = mean if n[kind][i] == 0 else avg[kind][i] + self.ALPHA * (mean - avg[kind][i])
        n[kind][i] = min(n[kind][i] + 1, 10**6)

    def _fold(self) -> None:
        cur, self._cur = self._cur, None
        self._blend(self.avg, self.n, cur)

    def record(self, t: datetime, load_w: float | None) -> None:
        if load_w is None or load_w < 0 or load_w > 50000:
            return
        kind, i = self._slot(t)
        key = (kind, i, t.date())
        if self._cur is None or self._cur[0] != key:
            self._fold()
            self._cur = [key, 0.0, 0]
        self._cur[1] += load_w
        self._cur[2] += 1

    def known(self, t: datetime) -> bool:
        kind, i = self._slot(t)
        return self.n[kind][i] >= self.MIN_VISITS

    def expected_wh(self, start: datetime, hours: float) -> float | None:
        """Energy the house is expected to use from `start` for `hours`; None if any half-hour has not been learned yet."""
        tz = start.tzinfo
        utc = timezone.utc
        total, t, end = 0.0, start.astimezone(utc), start.astimezone(utc) + timedelta(hours=hours)      # real time: clock-change days keep their length
        while t < end:
            local = t.astimezone(tz) if tz else t
            if not self.known(local):
                return None
            kind, i = self._slot(local)
            nxt = min(end, t + timedelta(minutes=30 - local.minute % 30, seconds=-local.second, microseconds=-local.microsecond))
            total += self.avg[kind][i] * (nxt - t).total_seconds() / 3600
            t = nxt
        return total

    def dump(self) -> dict:
        # A visit in progress counts too once it has enough samples, but only in the copy that is saved: folding it for real on
        # every save (every few minutes) would turn one half-hour into many "visits" and make a new day look like days of history.
        avg, n = {k: list(v) for k, v in self.avg.items()}, {k: list(v) for k, v in self.n.items()}
        self._blend(avg, n, self._cur)
        return {"avg": {k: [round(x, 1) for x in v] for k, v in avg.items()}, "n": n}


def auto_target_soc(usage_wh: float | None, capacity_wh: float, floor_pct: float, margin: float = 1.15,
                    lo: int = 20, hi: int = 100) -> int | None:
    """The charge level that covers `usage_wh` of expected use on top of the battery's own floor."""
    if usage_wh is None or capacity_wh <= 0:
        return None
    pct = floor_pct + 100 * usage_wh * margin / capacity_wh
    return int(max(lo, min(hi, round(pct + 0.4999))))


# ---------------------------------------------------------------- daily self-consumption / cheap-rate share


class DailyStats:
    KEEP_DAYS = 120

    def __init__(self, data: dict | None = None) -> None:
        from .energy import finite
        self.days: dict[str, dict[str, float]] = {k: {kk: finite(vv) for kk, vv in v.items()} for k, v in (data or {}).items() if isinstance(v, dict)}

    def add(self, day: str, pv_kwh: float = 0.0, export_kwh: float = 0.0, import_kwh: float = 0.0, cheap: bool | None = None) -> None:
        if pv_kwh <= 0 and export_kwh <= 0 and import_kwh <= 0:
            return
        d = self.days.setdefault(day, {"pv": 0.0, "export": 0.0, "import": 0.0, "import_cheap": 0.0, "import_known": 0.0})
        d["pv"] += pv_kwh
        d["export"] += export_kwh
        d["import"] += import_kwh
        if cheap is not None:
            d["import_known"] += import_kwh
            if cheap:
                d["import_cheap"] += import_kwh
        for old in sorted(self.days)[:-self.KEEP_DAYS]:
            del self.days[old]

    def self_consumption(self, day: str) -> float | None:
        """Share (0-100) of the solar generated that was used in the house or stored rather than exported."""
        d = self.days.get(day)
        if not d or d["pv"] < 0.2:
            return None
        return round(100 * (1 - min(d["export"], d["pv"]) / d["pv"]), 1)

    def cheap_share(self, day: str) -> float | None:
        d = self.days.get(day)
        if not d or d["import_known"] < 0.2:
            return None
        return round(100 * d["import_cheap"] / d["import_known"], 1)

    def dump(self) -> dict:
        return {k: {kk: round(vv, 4) for kk, vv in v.items()} for k, v in self.days.items()}


class UsageHistory:
    """Grid import / export per half-hour of each day (kWh), kept for the tariff comparison."""

    KEEP_DAYS = 200

    def __init__(self, data: dict | None = None) -> None:
        self.days: dict[str, dict[str, list[float]]] = {}
        def num(x) -> float:
            f = float(x)
            if f != f or f in (float("inf"), float("-inf")):
                raise ValueError("not finite")
            return f

        for k, v in (data or {}).items():
            if not isinstance(v, dict) or not isinstance(v.get("imp"), list) or not isinstance(v.get("exp"), list):
                continue
            if len(v["imp"]) == 48 and len(v["exp"]) == 48:
                try:
                    self.days[k] = {"imp": [num(x) for x in v["imp"]], "exp": [num(x) for x in v["exp"]]}
                except (TypeError, ValueError):
                    continue                                                           # damaged day: dropped
                try:
                    if isinstance(v.get("load"), list) and isinstance(v.get("pv"), list) and len(v["load"]) == 48 and len(v["pv"]) == 48:      # days recorded before house load was kept have neither
                        self.days[k]["load"], self.days[k]["pv"] = [num(x) for x in v["load"]], [num(x) for x in v["pv"]]
                except (TypeError, ValueError):
                    pass                                                               # damaged: that day simply has no load figures

    def add(self, t: datetime, import_kwh: float, export_kwh: float, load_kwh: float = 0.0, pv_kwh: float = 0.0) -> None:
        if import_kwh <= 0 and export_kwh <= 0 and load_kwh <= 0 and pv_kwh <= 0:
            return
        d = self.days.setdefault(t.strftime("%Y-%m-%d"), {"imp": [0.0] * 48, "exp": [0.0] * 48, "load": [0.0] * 48, "pv": [0.0] * 48})
        i = t.hour * 2 + t.minute // 30
        d["imp"][i] += import_kwh
        d["exp"][i] += export_kwh
        if "load" in d:              # a day that began before the upgrade stays without load figures: they would be half a day
            d["load"][i] += load_kwh
            d["pv"][i] += pv_kwh
        for old in sorted(self.days)[:-self.KEEP_DAYS]:
            del self.days[old]

    def dump(self) -> dict:
        return {k: {n: [round(x, 4) for x in v[n]] for n in ("imp", "exp", "load", "pv") if n in v} for k, v in self.days.items()}


# ---------------------------------------------------------------- weekly battery health


class Health:
    """Weekly charged/discharged energy per battery (from the lifetime counters) -> round-trip efficiency trend."""

    KEEP_WEEKS = 26

    def __init__(self, data: dict | None = None) -> None:
        d = data or {}
        self.weeks: dict[str, dict[str, dict[str, float]]] = {w: {k: dict(v) for k, v in x.items()} for w, x in d.get("weeks", {}).items()}
        self.base: dict[str, dict[str, float]] = {k: dict(v) for k, v in d.get("base", {}).items()}
        self.base_week: str = d.get("base_week", "")

    def update(self, week: str, lifetime: dict[str, tuple[float, float]]) -> None:
        """`lifetime` = {device_id: (charged_kwh_total, discharged_kwh_total)} as of now."""
        if week != self.base_week:           # a new week: close the previous one against its starting totals
            if self.base_week:
                done = self.weeks.setdefault(self.base_week, {})
                for did, (c, d_) in lifetime.items():
                    b = self.base.get(did)
                    if b is not None:
                        done[did] = {"in": max(0.0, c - b["in"]), "out": max(0.0, d_ - b["out"])}
            self.base = {did: {"in": c, "out": d_} for did, (c, d_) in lifetime.items()}
            self.base_week = week
            for old in sorted(self.weeks)[:-self.KEEP_WEEKS]:
                del self.weeks[old]
        else:
            for did, (c, d_) in lifetime.items():
                self.base.setdefault(did, {"in": c, "out": d_})

    def forget(self, did: str) -> None:
        """A battery was removed: a new one with the same name must not inherit its efficiency history."""
        self.base.pop(did, None)
        for w in self.weeks.values():
            w.pop(did, None)

    def efficiency(self, did: str) -> list[tuple[str, float]]:
        out = []
        for w in sorted(self.weeks):
            r = self.weeks[w].get(did)
            if r and r["in"] >= 1.0:
                out.append((w, round(100 * r["out"] / r["in"], 1)))
        return out

    def degraded(self, did: str, drop: float = 10.0) -> str | None:
        """A message if the latest full week's efficiency fell well below the weeks before it."""
        eff = self.efficiency(did)
        if len(eff) < 3:
            return None
        *before, (week, latest) = eff[-5:]
        base = median(v for _, v in before)
        if base - latest >= drop:
            return f"round-trip efficiency fell to {latest:g}% in week {week} (it was about {base:g}%)"
        return None

    def dump(self) -> dict:
        return {"weeks": self.weeks, "base": self.base, "base_week": self.base_week}


class GridEvents:
    """When each battery reported the grid down: start, end, and the charge level at both."""

    KEEP = 50

    def __init__(self, data: list | None = None) -> None:
        self.events: list[dict] = [dict(e) for e in (data or []) if isinstance(e, dict) and isinstance(e.get("start"), str) and e.get("start") and e.get("did")][-self.KEEP:]

    def observe(self, did: str, name: str, off_grid: bool | None, now: datetime, soc: float | None) -> None:
        if off_grid is None:
            return
        open_ev = next((e for e in reversed(self.events) if e["did"] == did and e.get("end") is None), None)
        if off_grid and open_ev is None:
            self.events.append({"did": did, "name": name, "start": now.isoformat(timespec="seconds"), "end": None, "soc_start": soc, "soc_end": None})
            del self.events[:-self.KEEP]
        elif not off_grid and open_ev is not None:
            open_ev["end"], open_ev["soc_end"] = now.isoformat(timespec="seconds"), soc

    def forget(self, did: str, now: datetime) -> None:
        """A battery was removed: an outage still open for it ends now, so the list does not show it down for ever."""
        for e in self.events:
            if e["did"] == did and e.get("end") is None:
                e["end"] = now.isoformat(timespec="seconds")

    def dump(self) -> list[dict]:
        return self.events

    def outages_since(self, cutoff_iso: str, now: datetime) -> tuple[int, float]:
        """(number of outages, total minutes) since `cutoff_iso`, with batteries that saw the same outage counted once:
        overlapping reports are merged, so two batteries do not double the figures."""
        spans = []
        for e in self.events:
            if e.get("start", "") < cutoff_iso:
                continue
            try:
                a = datetime.fromisoformat(e["start"])
                b = datetime.fromisoformat(e["end"]) if e.get("end") else now
            except (ValueError, TypeError):
                continue
            spans.append((a, max(a, b)))
        spans.sort()
        merged: list[list] = []
        for a, b in spans:
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        return len(merged), sum((b - a).total_seconds() for a, b in merged) / 60

    def view(self, n: int = 20) -> list[dict]:
        return list(reversed(self.events[-n:]))


class HealthTrend:
    """Daily highest battery temperature and how far apart the battery packs' voltages are (percent of their mean).
    A pack drifting away from the others, or running hot, shows here well before it becomes a fault."""

    KEEP_DAYS = 60
    MIN_DAYS = 5          # days of history needed before the drift check speaks
    ABS_SPREAD = 1.5      # percentage points: a spread under this is never worth a warning
    HOT_C = 65.0          # an inverter's internal temperature runs well above the air around it

    def __init__(self, data: dict | None = None) -> None:
        self.days: dict[str, dict[str, dict]] = {}
        for did, rows in (data or {}).items():
            if isinstance(rows, dict):
                self.days[did] = {d: {"tmax": v.get("tmax"), "spread": v.get("spread")} for d, v in rows.items() if isinstance(v, dict)}

    def update(self, day: str, did: str, temp_c: float | None, pack_v: tuple) -> None:
        rec = self.days.setdefault(did, {}).setdefault(day, {"tmax": None, "spread": None})
        if temp_c is not None:
            rec["tmax"] = temp_c if rec["tmax"] is None else max(rec["tmax"], temp_c)
        if len(pack_v) >= 2 and (mean := sum(pack_v) / len(pack_v)) > 0:
            sp = round(100 * (max(pack_v) - min(pack_v)) / mean, 2)
            rec["spread"] = sp if rec["spread"] is None else max(rec["spread"], sp)
        for old in sorted(self.days[did])[:-self.KEEP_DAYS]:
            del self.days[did][old]

    def series(self, did: str, n: int = 14) -> list[list]:
        d = self.days.get(did, {})
        return [[k, d[k]["tmax"], d[k]["spread"]] for k in sorted(d)[-n:]]

    def warning(self, did: str) -> str | None:
        d = self.days.get(did, {})
        keys = sorted(d)
        if not keys:
            return None
        last = d[keys[-1]]
        if last["tmax"] is not None and last["tmax"] >= self.HOT_C:
            return f"it reached {last['tmax']:g} °C today"
        before = [d[k]["spread"] for k in keys[:-1] if d[k]["spread"] is not None]
        if last["spread"] is not None and len(before) >= self.MIN_DAYS:
            base = median(before)
            if last["spread"] >= max(self.ABS_SPREAD, 2 * base):
                return f"its battery packs have drifted apart ({last['spread']:g}% voltage spread, usually {base:g}%)"
        return None

    def dump(self) -> dict:
        return self.days


# ---------------------------------------------------------------- the combined "home battery"


@dataclass
class BatteryFigures:
    capacity_wh: float
    soc: float | None
    online: bool
    power_w: float | None      # + charging
    min_soc: float
    load_w: float | None = None
    max_charge_w: float = 0.0


def combine(items: list[BatteryFigures], load_w: float | None = None) -> dict:
    """Totals across every online battery: charge level (capacity weighted), stored and usable energy, flow, and how long
    the battery would last at the current draw / take to fill."""
    on = [BatteryFigures(b.capacity_wh, max(0.0, min(100.0, b.soc)), b.online, b.power_w, b.min_soc, b.load_w, b.max_charge_w)
          for b in items if b.online and b.soc is not None and b.capacity_wh > 0]    # a bad reading cannot push totals out of range
    if not on:
        return {"online": 0, "total": len(items), "soc": None, "stored_kwh": None, "usable_kwh": None, "capacity_kwh": None,
                "flow_w": None, "load_w": load_w, "runtime_h": None, "full_in_h": None}
    cap = sum(b.capacity_wh for b in on)
    stored = sum(b.capacity_wh * b.soc / 100 for b in on)
    usable = sum(b.capacity_wh * max(0.0, b.soc - b.min_soc) / 100 for b in on)
    flow = sum(b.power_w or 0.0 for b in on)
    draw = max(0.0, -flow) if flow < -20 else (load_w if load_w and load_w > 20 else None)
    runtime = usable / draw if draw else None
    room = sum(b.capacity_wh * (100 - b.soc) / 100 for b in on)
    full = room / flow if flow > 20 else None
    return {"online": len(on), "total": len(items), "soc": round(100 * stored / cap, 1), "stored_kwh": round(stored / 1000, 2),
            "usable_kwh": round(usable / 1000, 2), "capacity_kwh": round(cap / 1000, 2), "flow_w": round(flow),
            "load_w": None if load_w is None else round(load_w),
            "runtime_h": None if runtime is None else round(runtime, 1), "full_in_h": None if full is None else round(full, 1)}


# ---------------------------------------------------------------- scenes

SCENE_FLAGS = ("backup", "away", "paused")
SCENE_SETTINGS = ("peak_shaving", "import_limit_w", "export_limit_w", "total_charge_limit_w", "total_discharge_limit_w")

DEFAULT_SCENES = [
    {"name": "Normal", "backup": False, "away": False, "paused": False, "settings": {}},
    {"name": "Holiday", "backup": False, "away": True, "paused": False, "settings": {}},
    {"name": "Storm coming", "backup": True, "away": False, "paused": False, "settings": {}},
    {"name": "Hands off", "backup": False, "away": False, "paused": True, "settings": {}},
]


class SceneError(ValueError):
    pass


def clean_scene(raw: dict) -> dict:
    name = str(raw.get("name") or "").strip()
    if not name:
        raise SceneError("give the scene a name")
    out = {"name": name[:40]}
    for k in SCENE_FLAGS:
        out[k] = bool(raw.get(k, False))
    st = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
    out["settings"] = {}
    for k in SCENE_SETTINGS:
        if k in st and st[k] not in (None, ""):
            try:
                out["settings"][k] = bool(st[k]) if k == "peak_shaving" else max(0, int(float(st[k])))
            except (TypeError, ValueError) as err:
                raise SceneError(f"{k} is not a number") from err
    return out


# ---------------- solar-surplus diverter


class Diverter:
    """Switch a chosen load on while the batteries are full and power is being exported; off again when it would
    need the grid. Both directions must hold for HOLD_S so a passing cloud or a kettle does not toggle it."""

    HOLD_S = 120

    def __init__(self) -> None:
        self.on = False
        self._since: float | None = None
        self._want: bool | None = None

    def decide(self, now_s: float, soc: float | None, grid_w: float | None, export_w: int, min_soc: int, cheap: bool = False) -> str | None:
        """Returns "on" / "off" when the switch should change, else None. grid_w: + importing, - exporting.
        `cheap`: it is a cheap-rate period, so the load may run (and keeps running) whatever the sun is doing."""
        if cheap and soc is not None:
            want = True if not self.on else None
        elif soc is None or grid_w is None:
            want = False if self.on else None          # no data: release the load rather than leave it running blind
        elif not self.on:
            want = True if (soc >= min_soc and -grid_w >= export_w) else None
        else:
            want = False if (grid_w > 100 or soc < min_soc - 5) else None
        if want is None or want == self.on:
            self._since = self._want = None
            return None
        if self._want != want:
            self._want, self._since = want, now_s
            return None
        if self._since is None or now_s - self._since < self.HOLD_S:
            return None
        self.on, self._since, self._want = want, None, None
        return "on" if want else "off"


# ---------------------------------------------------------------- plan versus actual


class PlanLog:
    """What each battery was told to do against what it really did, averaged per half hour, for the last few days.

    Power is signed like the readings: charging positive, discharging negative. A battery left to its own control has no
    planned figure for that slot (None), only an actual one."""

    KEEP_DAYS = 8

    def __init__(self, data: dict | None = None) -> None:
        self.days: dict[str, dict[str, dict[str, list[float]]]] = {}
        for day, devs in (data.items() if isinstance(data, dict) else ()):
            for did, r in (devs.items() if isinstance(devs, dict) else ()):
                try:
                    if all(len(r[k]) == 48 for k in ("ps", "pn", "as", "an")):
                        self.days.setdefault(day, {})[did] = {k: [float(x) for x in r[k]] for k in ("ps", "pn", "as", "an")}
                except (KeyError, TypeError, ValueError):
                    continue                                    # a damaged day is dropped, never fatal

    def add(self, t: datetime, did: str, planned_w: float | None, actual_w: float | None) -> None:
        if planned_w is None and actual_w is None:
            return
        r = self.days.setdefault(t.strftime("%Y-%m-%d"), {}).setdefault(did, {k: [0.0] * 48 for k in ("ps", "pn", "as", "an")})
        i = t.hour * 2 + t.minute // 30
        if planned_w is not None:
            r["ps"][i] += planned_w
            r["pn"][i] += 1
        if actual_w is not None:
            r["as"][i] += actual_w
            r["an"][i] += 1
        for old in sorted(self.days)[:-self.KEEP_DAYS]:
            del self.days[old]

    def view(self, did: str, now: datetime, hours: int = 24) -> dict:
        """The last `hours` hours as half-hour rows [start iso, planned W, actual W] (None = no reading), plus how closely
        the battery followed the plan over the slots that had both."""
        end = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0) + timedelta(minutes=30)
        end_utc, local = end.astimezone(timezone.utc), now.tzinfo
        rows, num, den = [], 0.0, 0.0
        for k in range(hours * 2):
            # step in real time and only then look at the local clock, so rows stay evenly spaced when the clocks change
            start = (end_utc - timedelta(minutes=30 * (hours * 2 - k))).astimezone(local)
            r = self.days.get(start.strftime("%Y-%m-%d"), {}).get(did)
            i = start.hour * 2 + start.minute // 30
            p = r["ps"][i] / r["pn"][i] if r and r["pn"][i] else None
            a = r["as"][i] / r["an"][i] if r and r["an"][i] else None
            rows.append([start.isoformat(), None if p is None else round(p), None if a is None else round(a)])
            if p is not None and a is not None:
                num += abs(p - a)
                den += max(abs(p), abs(a), 1.0)
        both = sum(1 for _, p, a in rows if p is not None and a is not None)
        return {"rows": rows, "match_pct": round(100 * (1 - num / den)) if both >= 4 and den > 0 else None}

    def dump(self) -> dict:
        return {d: {did: {k: [round(x, 1) for x in v] for k, v in r.items()} for did, r in devs.items()} for d, devs in self.days.items()}
