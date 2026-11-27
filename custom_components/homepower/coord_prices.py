"""Prices and tariffs: the rate table, the cheap window learnt from history, export prices, paid export and battery wear.

Part of HomePowerCoordinator (coordinator.py), kept in its own file so each file has one concern: the methods
run on the coordinator itself and use its state."""
from __future__ import annotations

import time as _time
from datetime import datetime, timedelta

from homeassistant.util import dt as dt_util

from .core.rates import SLOT, RateTable, Slot, infer_offpeak, parse_rates, to_main_units
from .core.report import window_drift
from .core.sanity import cycling_margin
from .coord_base import _LOGGER, TARIFF_REFRESH_S, TARIFF_HISTORY_DAYS, _real


class PricesMixin:
    def _rates_entity(self) -> str:
        return self.settings.rates_entity or self._glow().get("rate", "")

    def history_window(self) -> tuple | None:
        """The cheap period found in the price history, as (start, end) to plan with; None until one has been found.
        The window is read from when the sensor's value changed, and such sensors (smart-meter/DCC data in particular) lag
        the real switch by up to an hour. A late *start* only wastes cheap time, but a late *end* would plan charging into
        peak-rate time, so the end is pulled back one half-hour."""
        inf = self._tariff
        if inf is None:
            return None
        end = (datetime.combine(dt_util.now().date(), inf.end) - SLOT).time()
        return inf.start, inf.end if end == inf.start else end    # a one-slot window: pulling back would leave it empty (= all day)

    def _rates(self) -> RateTable | None:
        s = self.settings
        slots = []
        ent = self._rates_entity()
        st = self.hass.states.get(ent) if ent else None
        if st is not None:
            slots = parse_rates(dict(st.attributes), dt_util.DEFAULT_TIME_ZONE)
            if not slots:  # a plain price sensor: its state is the price right now
                try:
                    now = dt_util.now()
                    lo = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)
                    slots = [Slot(lo, lo + SLOT, _real(st.state))]
                except (TypeError, ValueError):
                    pass
        peak, off, a, b = s.price_peak, s.price_offpeak, s.offpeak_start, s.offpeak_end
        inf = self._tariff if s.offpeak_auto and ent else None
        if inf is not None:   # anything the user typed in wins; history only fills the blanks
            if (a is None or b is None) and not s.offpeak_extra:   # typed-in extra periods mean the user knows the hours
                a, b = self.history_window()
            peak = inf.peak if peak is None else peak
            off = inf.offpeak if off is None else off
        if not slots and peak is None and off is None:
            return None
        if (a is None or b is None) and s.offpeak_extra:      # only the extra windows were entered: the first is the main one
            (a, b), rest = s.offpeak_extra[0], s.offpeak_extra[1:]
            return RateTable(slots, peak, off, a, b, rest)
        return RateTable(slots, peak, off, a, b, s.offpeak_extra)

    async def _refresh_tariff(self) -> None:
        """Learn the cheap (night) window from the rates sensor's recorded price history (every few hours)."""
        s, ent = self.settings, self._rates_entity()
        key = (ent, s.offpeak_auto)
        if not (ent and s.offpeak_auto):
            self._tariff, self._tariff_key = None, key
            return
        if (key == self._tariff_key and _time.monotonic() - self._tariff_checked < TARIFF_REFRESH_S) or self._tariff_busy:
            return
        self._tariff_busy, self._tariff_key, self._tariff_checked = True, key, _time.monotonic()
        try:
            from homeassistant.components.recorder import get_instance, history
            end = dt_util.utcnow()

            def load():
                return history.get_significant_states(self.hass, end - timedelta(days=TARIFF_HISTORY_DAYS), end, [ent],
                                                      include_start_time_state=True, significant_changes_only=False,
                                                      no_attributes=True).get(ent, [])

            samples = []
            for st in await get_instance(self.hass).async_add_executor_job(load):
                try:
                    samples.append((st.last_changed, _real(st.state)))
                except (TypeError, ValueError):
                    continue
            found = infer_offpeak(samples, end, dt_util.DEFAULT_TIME_ZONE)
            if found != self._tariff:
                self._log("tariff", (f"cheap window from price history: {found.start:%H:%M}-{found.end:%H:%M} "
                                     f"({found.offpeak:g} vs {found.peak:g})") if found else "no repeating cheap window in the price history")
            self._tariff = found
        except Exception as err:  # noqa: BLE001 - recorder missing/unavailable: keep what we had
            _LOGGER.debug("tariff history unavailable: %s", err)
        finally:
            self._tariff_busy = False

    def _export_better(self, rates: RateTable | None, now: datetime) -> bool:
        """Is spare solar worth more exported than stored? True when the export price beats buying the same energy back at
        the cheapest upcoming import price (allowing ~10% for what is lost going through the battery), less the wear that
        storing it would put on the battery."""
        exp = self._export_price_now(rates, now)
        if not exp or rates is None:
            return False
        cheapest = self._cheapest_price(rates, now)
        if cheapest is None:
            return False
        return exp + self._wear(rates) > to_main_units(cheapest, self._unit(rates)) / 0.9

    def _export_table(self) -> RateTable | None:
        """Export prices from the sensor chosen in Settings (Agile Outgoing, Flux, Nord Pool...): price slots in its attributes,
        or a plain sensor whose state is the export price right now."""
        ent = self.settings.export_rates_entity
        st = self.hass.states.get(ent) if ent else None
        if st is None:
            return None
        slots = parse_rates(dict(st.attributes), dt_util.DEFAULT_TIME_ZONE)
        if not slots:
            try:
                now = dt_util.now()
                lo = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)
                slots = [Slot(lo, lo + SLOT, _real(st.state))]
            except (TypeError, ValueError):
                return None
        return RateTable(slots)

    def _export_price_now(self, rates: RateTable | None, now: datetime) -> float | None:
        """What exporting a kWh pays right now (main units): the export sensor's slot, else the fixed export price."""
        t = getattr(self, "_exp_table", None)
        p = t.price_at(now) if t is not None else None
        if p is None:
            p = self.settings.export_price
        return None if p is None else to_main_units(p, self._unit(rates))

    def _wear(self, rates: RateTable | None) -> float:
        """Battery wear per kWh cycled (main units); 0 when not set."""
        w = getattr(self.settings, "wear_cost", None)
        return to_main_units(w, self._unit(rates)) if w else 0.0

    def _buyback_price(self, rates: RateTable | None, now: datetime) -> float | None:
        """The cheapest import price in the next 24 h (main units): what a kWh exported now costs to buy back."""
        p = self._cheapest_price(rates, now)
        return None if p is None else to_main_units(p, self._unit(rates))

    def _export_keep_pct(self, rates: RateTable | None, now: datetime) -> int:
        """How low paid export may take the batteries: the user's setting, raised to cover the house's usual use until the
        cheapest coming price (when buying it back is cheap again). Unknown use counts as nothing (the setting still holds)."""
        keep = int(getattr(self.settings, "export_keep_pct", 0) or 0)
        if rates is None:
            return keep
        ahead = rates.slots_between(now, now + timedelta(hours=24))
        if not ahead:
            return keep
        cheapest = min(ahead, key=lambda s: s.price)
        hours = max(0.0, (cheapest.start - now).total_seconds() / 3600)      # both are real instants
        cap = sum(a.config.caps.capacity_wh for a in self.adapters)
        need = self.profile.expected_wh(now, hours) if hours >= 0.5 else 0.0
        if not need or cap <= 0:
            return keep
        base = max((a.config.caps.min_soc for a in self.adapters), default=0)
        return min(100, max(keep, base + int(-(-need * 100 // cap))))

    def _event_entities(self) -> list[str]:
        return [e.strip() for e in str(getattr(self.settings, "event_entity", "") or "").split(",") if e.strip()]

    def _event_windows(self) -> list:
        """Start and end of the events the chosen entities describe (a calendar, the Octopus saving-session entities...)."""
        from .core.events import event_windows

        out = []
        for ent in self._event_entities():
            st = self.hass.states.get(ent)
            if st is not None:
                out += event_windows(dict(st.attributes), dt_util.DEFAULT_TIME_ZONE)
        return sorted(set(out))

    def _event_goal(self, now: datetime):
        """Settings > Grid events: a goal that charges the batteries up, in the cheapest half-hours, before the next event starts."""
        pct = int(getattr(self.settings, "event_charge_pct", 0) or 0)
        if not pct:
            return None
        from .core.events import charge_goal, next_event_start

        return charge_goal(next_event_start(self._event_windows(), now), pct, now)

    def _day_plan(self, rates: RateTable | None, now: datetime, actuals: dict):
        """Where the stored energy is worth most until refilling is cheapest (core/dayplan.py). Only when paid export or 'keep
        stored energy for dearer hours' is on, and prices are known."""
        from .core.dayplan import plan_day
        from .core.models import Mode

        s = self.settings
        if rates is None or not (s.engine.export_discharge or s.engine.hold_for_dearer):
            return None
        unit = self._unit(rates)
        imp = [Slot(x.start, x.end, to_main_units(x.price, unit)) for x in rates.slots_between(now, now + timedelta(hours=24))]
        keep = int(getattr(s, "export_keep_pct", 0) or 0) if s.engine.export_discharge else 0   # the export setting's floor, only for export
        budget = export_w = house_w = 0.0
        for a in self.adapters:
            act, c = actuals.get(a.config.device_id), a.config.caps
            if not (act and act.online and act.soc is not None and a.config.extra.get("control_enabled", True)):
                continue
            budget += max(0.0, act.soc - max(c.min_soc, keep)) / 100 * c.capacity_wh
            house_w += c.max_discharge_w or 0
            if c.power_control and Mode.DISCHARGE in c.supported_modes and a.config.device_id != s.engine.zero_import_device:
                export_w += c.max_discharge_w
        export_at = (lambda t: self._export_price_now(rates, t)) if s.engine.export_discharge else None
        return plan_day(now, imp, export_at, self.profile.expected_wh, budget, export_w, self._wear(rates),
                        discharge_max_w=house_w or None)

    def _grid_event_on(self) -> bool:
        """Is a demand-flexibility event running (a saving session)? Any of the chosen entities on: a binary sensor, a calendar
        while one of its events runs, or an input_boolean."""
        from .core.events import all_day

        for e in self._event_entities():
            st = self.hass.states.get(e)
            if st is not None and st.state == "on" and not all_day(dict(st.attributes)):
                return True              # (a calendar is on all day for an all-day entry: that is a note, not a session)
        now = dt_util.now()          # an entity that lists the events (not on / off): one of them is running now
        return any(s <= now < e for s, e in self._event_windows())

    def _unit(self, rates: RateTable | None) -> str:
        """pence or main units for the prices in use. With "auto" the whole table decides (any price of 5 or more means pence),
        not each price on its own: otherwise a 3.5p slot or a 4.1p export rate would be read as 3.50 / 4.10 pounds."""
        u = self.settings.rates_unit
        if u != "auto":
            return u
        vals = [x.price for x in rates.slots] if rates is not None else []
        if rates is not None:
            vals += [v for v in (rates.peak, rates.offpeak) if v is not None]
        if self.settings.export_price:
            vals.append(self.settings.export_price)
        exp = getattr(self, "_exp_table", None)
        if exp is not None:
            vals += [x.price for x in exp.slots]
        return "pence" if any(abs(v) >= 5 for v in vals) else "main"

    def _is_cheap(self, rates: RateTable | None, now: datetime, price: float | None) -> bool | None:
        """Is `now` a cheap-rate period? None when there is nothing to compare with (a flat tariff)."""
        if rates is None or price is None:
            return None
        if rates.peak is not None and rates.offpeak is not None and rates.peak != rates.offpeak:
            return price <= to_main_units((rates.peak + rates.offpeak) / 2, self._unit(rates))
        horizon = [s.price for s in rates.slots_between(now, now + timedelta(hours=24))]
        if len(horizon) < 4 or max(horizon) == min(horizon):
            return None
        return to_main_units(sum(horizon) / len(horizon), self._unit(rates)) > price * 1.1

    def _price_now(self, rates: RateTable | None, now: datetime) -> float | None:
        p = rates.price_at(now) if rates else None
        return None if p is None else to_main_units(p, self._unit(rates))

    def tariff_comparison(self, days: int = 30) -> dict:
        """The recorded half-hourly usage priced under each tariff typed in Settings (see core/tariffs.py)."""
        from .core.tariffs import compare, parse_tariffs
        return compare(self.usage.days, parse_tariffs(self.settings.compare_tariffs), days)

    def _price_later_max(self, rates: RateTable | None, now: datetime, hours: int = 6) -> float | None:
        """The dearest import price in the next few hours (main units), for 'keep it for later'."""
        if rates is None:
            return None
        ahead = [to_main_units(x.price, self._unit(rates)) for x in rates.slots_between(now, now + timedelta(hours=hours))]
        return max(ahead) if ahead else None

    async def _check_tariff_drift(self, wall: float) -> None:
        """Alert when the cheap hours in the price history no longer match the hours typed into Settings."""
        s, inf = self.settings, self._tariff
        msg = window_drift(s.offpeak_start, s.offpeak_end, inf.start, inf.end) if inf is not None and inf.days >= 5 else None
        ev = self.alerts.observe("tariffdrift", bool(msg), wall, 6 * 3600)
        if ev:
            await self._notify("tariffdrift", msg or "Your cheap-rate hours match the price history again.", ev == "raised")

    @staticmethod
    def _cheapest_price(rates: RateTable | None, now: datetime) -> float | None:
        """The cheapest import price in the next 24 hours (rates unit)."""
        if rates is None:
            return None
        ahead = [x.price for x in rates.slots_between(now, now + timedelta(hours=24))]
        return min(ahead) if ahead else rates.offpeak

    def _price_slots(self, now: datetime, hours: int) -> list[dict]:
        rates = self._rates()
        if rates is None:
            return []
        lo = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)
        found = rates.slots_between(lo, lo + timedelta(hours=hours))
        mean = sum(x.price for x in found) / len(found) if found else 0.0   # per-slot tariffs: 'cheap' = well below average
        return [{"start": x.start.isoformat(), "end": x.end.isoformat(), "price": round(to_main_units(x.price, self._unit(rates)), 4),
                 "cheap": rates.in_cheap_window(x.start.astimezone(now.tzinfo).time()) if rates.windows else (x.price < mean - 0.1 * abs(mean))}
                for x in found]

    def _export_view(self, rates: RateTable | None, now: datetime) -> dict:
        """Paid export, for the panel: what exporting pays now, what buying it back costs, and whether it is worth it."""
        from .core.engine import export_pays

        exp, buy, wear = self._export_price_now(rates, now), self._buyback_price(rates, now), self._wear(rates)
        return {"price_now": exp, "buyback": buy, "wear": wear or None, "pays": export_pays(exp, buy, wear),
                "on": self.settings.engine.export_discharge, "sensor": bool(self._exp_table),
                "keep_pct": (None if not self.settings.engine.export_discharge else int(self.settings.export_keep_pct or 0)
                             if getattr(self, "day_plan", None) else self._export_keep_pct(rates, now)),
                "planned": bool(getattr(self, "day_plan", None)),
                "export_now_w": self.day_plan.export_now_w if getattr(self, "day_plan", None) else None}

    def _cycling(self, did: str, rates: RateTable | None, now: datetime) -> dict | None:
        """What storing a kWh in this battery is worth right now: the cheapest import in the next 24 h against the dearest
        price it could avoid, at this battery's own measured round-trip efficiency (or an 85 % estimate until it has one)."""
        if rates is None:
            return None
        ahead = [to_main_units(x.price, self._unit(rates)) for x in rates.slots_between(now, now + timedelta(hours=24))]
        if len(ahead) < 2:
            return None
        eff = self.health.efficiency(did)
        measured = bool(eff)
        out = cycling_margin(min(ahead), max(ahead), (eff[-1][1] / 100) if measured else 0.85, self._wear(rates))
        out["measured"] = measured
        return out
