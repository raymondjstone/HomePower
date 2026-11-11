"""Electricity price slots: parse them from an HA entity's attributes, or from a simple fixed tariff."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from statistics import median
from typing import Any

SLOT = timedelta(minutes=30)
_START = ("start", "valid_from", "from", "start_time")
_END = ("end", "valid_to", "to", "end_time")
_PRICE = ("value_inc_vat", "value", "price", "value_exc_vat", "rate")


@dataclass(frozen=True, slots=True)
class Slot:
    start: datetime
    end: datetime
    price: float

    def clipped(self, lo: datetime, hi: datetime) -> "Slot | None":
        s, e = max(self.start, lo), min(self.end, hi)
        return Slot(s, e, self.price) if e > s else None


def _dt(v: Any, tz=None) -> datetime | None:
    """Parse a timestamp; one without a timezone is read in `tz` (local time), else UTC.
    Always returns an aware datetime so it can be compared with 'now'."""
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(v, datetime):
        return v if v.tzinfo is not None else v.replace(tzinfo=tz or timezone.utc)
    return None


def _first(d: dict, keys: tuple) -> Any:
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return None


def parse_rates(attrs: dict | None, tz=None) -> list[Slot]:
    """Understands the common shapes: Octopus `rates`, Nord Pool style `raw_today`/`raw_tomorrow`,
    and `today`/`tomorrow` lists of {start, end, value}."""
    attrs = attrs or {}
    lists: list[list] = []
    if isinstance(attrs.get("rates"), list):
        lists.append(attrs["rates"])
    for a, b in (("raw_today", "raw_tomorrow"), ("today", "tomorrow")):
        if isinstance(attrs.get(a), list) and attrs[a] and isinstance(attrs[a][0], dict):
            lists.append(attrs[a] + (attrs.get(b) if isinstance(attrs.get(b), list) else []))
            break
    out: list[Slot] = []
    for items in lists:
        for it in items:
            if not isinstance(it, dict):
                continue
            s, e, p = _dt(_first(it, _START), tz), _dt(_first(it, _END), tz), _first(it, _PRICE)
            if s is None or p is None:
                continue
            try:
                price = float(p)
            except (TypeError, ValueError):
                continue
            out.append(Slot(s, e if e is not None and e > s else s + SLOT, price))
    out.sort(key=lambda x: x.start)
    return out


class RateTable:
    """Prices from parsed slots, with an optional fixed two-rate tariff as the fallback."""

    def __init__(self, slots: list[Slot] | None = None, peak: float | None = None, offpeak: float | None = None,
                 offpeak_start: time | None = None, offpeak_end: time | None = None) -> None:
        self.slots = slots or []
        self.peak, self.offpeak = peak, offpeak
        self.offpeak_start, self.offpeak_end = offpeak_start, offpeak_end

    def _fixed(self, t: datetime) -> float | None:
        if self.peak is None and self.offpeak is None:
            return None
        if self.offpeak is not None and self.offpeak_start and self.offpeak_end:
            tt, a, b = t.time(), self.offpeak_start, self.offpeak_end
            inside = (a <= tt < b) if a < b else (tt >= a or tt < b)
            if inside:
                return self.offpeak
        return self.peak if self.peak is not None else self.offpeak

    def price_at(self, t: datetime) -> float | None:
        for s in self.slots:
            if s.start <= t < s.end:
                return s.price
        return self._fixed(t)

    def slots_between(self, lo: datetime, hi: datetime) -> list[Slot]:
        """Price slots covering [lo, hi): parsed ones where known, fixed-tariff half-hours elsewhere."""
        out = [c for s in self.slots if (c := s.clipped(lo, hi))]
        if self.peak is None and self.offpeak is None:
            return out
        covered = sorted((s.start, s.end) for s in out)
        t = lo
        while t < hi:
            end = min(hi, (t.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=30 * (t.minute // 30 + 1))))
            if not any(a <= t < b for a, b in covered):
                p = self._fixed(t)
                if p is not None:
                    out.append(Slot(t, end, p))
            t = end
        return sorted(out, key=lambda x: x.start)


def to_main_units(price: float, unit: str = "auto") -> float:
    """Convert a rate to pounds/euros per kWh. "auto" treats values of 5 or more as pence/cents
    (UK rates are 5-70p; the same in pounds is 0.05-0.70, so the two never overlap)."""
    if unit == "pence" or (unit == "auto" and abs(price) >= 5):
        return price / 100
    return price


@dataclass(frozen=True, slots=True)
class InferredTariff:
    start: time
    end: time          # end <= start means the cheap window crosses midnight
    offpeak: float
    peak: float
    days: float        # how much history the guess is based on


def _bucket(t: datetime) -> int:
    return t.hour * 2 + t.minute // 30


def infer_offpeak(samples: list[tuple[datetime, float]], until: datetime, tz=None, min_days: float = 1.0,
                  min_spread: float = 0.2) -> InferredTariff | None:
    """Find the recurring cheap (night) window from a price sensor's history.

    `samples` are (changed_at, price) pairs; each price holds until the next change (the last until `until`).
    The history is read half-hour by half-hour in local time, the median price per time of day is taken, and the
    longest run (it may wrap midnight) of below-midpoint half-hours is the cheap window. Returns None for a
    flat tariff, too little history, or prices that do not repeat as one block (e.g. a fully dynamic tariff)."""
    pts = sorted(((_dt(t, tz), p) for t, p in samples if _dt(t, tz) is not None and p is not None), key=lambda x: x[0])
    if len(pts) < 2:
        return None
    span = until - pts[0][0]
    if span < timedelta(days=min_days):
        return None
    if tz is not None:
        pts = [(t.astimezone(tz), p) for t, p in pts]
        until = until.astimezone(tz)
    per: list[list[float]] = [[] for _ in range(48)]
    t = pts[0][0].replace(minute=(pts[0][0].minute // 30) * 30, second=0, microsecond=0)
    i = 0
    while t < until:
        while i + 1 < len(pts) and pts[i + 1][0] <= t:
            i += 1
        if pts[i][0] <= t:
            per[_bucket(t)].append(pts[i][1])
        t += SLOT
    if any(not b for b in per):
        return None
    med = [median(b) for b in per]
    lo, hi = min(med), max(med)
    if hi <= 0 or (hi - lo) / abs(hi) < min_spread:
        return None
    mid = (lo + hi) / 2
    cheap = [m < mid for m in med]
    for k in range(48):                           # a single odd half-hour inside a block is noise
        if not cheap[k] and cheap[k - 1] and cheap[(k + 1) % 48]:
            cheap[k] = True
    if all(cheap):
        return None
    # longest circular run of cheap half-hours
    first_off = next(k for k in range(48) if not cheap[k])
    best, best_start, run, run_start = 0, 0, 0, 0
    for n in range(1, 49):
        k = (first_off + n) % 48
        if cheap[k]:
            if run == 0:
                run_start = k
            run += 1
            if run > best:
                best, best_start = run, run_start
        else:
            run = 0
    if best == 0 or best < 0.6 * sum(cheap):      # cheap half-hours scattered about: not a fixed window
        return None
    idx = {(best_start + n) % 48 for n in range(best)}
    off = median([med[k] for k in idx])
    on = median([med[k] for k in range(48) if k not in idx and not cheap[k]] or [hi])
    end_b = (best_start + best) % 48
    return InferredTariff(time(best_start // 2, (best_start % 2) * 30), time(end_b // 2, (end_b % 2) * 30),
                          float(off), float(on), span.total_seconds() / 86400)
