"""Learning and bookkeeping that sits on top of the live readings (HA-free, tested without Home Assistant):
the household's usual load by time of day, daily self-consumption / cheap-rate shares, weekly battery health,
the combined "home battery" figures, and saved scenes."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
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

    def _fold(self) -> None:
        if self._cur is None:
            return
        (kind, i, _), total, count = self._cur
        self._cur = None
        if count < self.MIN_SAMPLES_PER_VISIT:
            return
        mean = total / count
        self.avg[kind][i] = mean if self.n[kind][i] == 0 else self.avg[kind][i] + self.ALPHA * (mean - self.avg[kind][i])
        self.n[kind][i] = min(self.n[kind][i] + 1, 10**6)

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
        total, t, end = 0.0, start, start + timedelta(hours=hours)
        while t < end:
            if not self.known(t):
                return None
            kind, i = self._slot(t)
            nxt = min(end, (t.replace(minute=(t.minute // 30) * 30, second=0, microsecond=0)) + timedelta(minutes=30))
            total += self.avg[kind][i] * (nxt - t).total_seconds() / 3600
            t = nxt
        return total

    def dump(self) -> dict:
        self._fold()    # a visit in progress counts too once it has enough samples
        return {"avg": {k: [round(x, 1) for x in v] for k, v in self.avg.items()}, "n": self.n}


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
        self.days: dict[str, dict[str, float]] = {k: dict(v) for k, v in (data or {}).items()}

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
    on = [b for b in items if b.online and b.soc is not None and b.capacity_wh > 0]
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
    st = raw.get("settings") or {}
    out["settings"] = {}
    for k in SCENE_SETTINGS:
        if k in st and st[k] not in (None, ""):
            try:
                out["settings"][k] = bool(st[k]) if k == "peak_shaving" else max(0, int(float(st[k])))
            except (TypeError, ValueError) as err:
                raise SceneError(f"{k} is not a number") from err
    return out
