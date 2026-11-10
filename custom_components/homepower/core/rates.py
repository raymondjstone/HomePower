"""Electricity price slots: parse them from an HA entity's attributes, or from a simple fixed tariff."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
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
