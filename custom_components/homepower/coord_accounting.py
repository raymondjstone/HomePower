"""Bookkeeping after each cycle: energy counters, the savings ledger and money totals, learned charge rates, plan against actual, command results, solar strings and the solar-surplus load.

Part of HomePowerCoordinator (coordinator.py), kept in its own file so each file has one concern: the methods run on
the coordinator itself and use its state."""
from __future__ import annotations

from datetime import datetime

from .core.models import Actual, Mode
from .core.insights import Diverter
from .core.rates import RateTable, to_main_units
from .coord_base import _LOGGER, MONEY_SAVED, MONEY_EXPORT


class AccountingMixin:
    async def _divert(self, now_s: float, actuals: dict[str, Actual], grid: float | None) -> None:
        """Solar surplus: run a chosen load while the batteries are full and power is being exported."""
        s = self.settings
        if self._divert_entity and self._divert_entity != s.divert_entity:      # setting changed or cleared: release the old load
            if self.diverter.on:
                await self._switch(self._divert_entity, False)
                self.manager.divert_on = ""
            self.diverter = Diverter()
        self._divert_entity = s.divert_entity
        if not s.divert_entity:
            return
        # While paused HomePower switches nothing on, and lets go of the load if it is running.
        soc = None if self.paused else self._combined(self.shown_actuals())["soc"]   # a short read failure does not flip the load
        act = self.diverter.decide(now_s, soc, grid, s.divert_export_w, s.divert_min_soc, cheap=bool(s.divert_cheap and self._cheap_now and not self.paused))
        if act:
            await self._switch(s.divert_entity, act == "on")
            self.manager.divert_on = s.divert_entity if act == "on" else ""
            self.hass.async_create_task(self.manager.save_runtime())
            self._log("divert", f"{s.divert_entity} switched {act} ({('cheap rate' if s.divert_cheap and self._cheap_now else 'surplus solar') if act == 'on' else 'no surplus'})")

    async def _switch(self, entity_id: str, on: bool) -> None:
        try:
            await self.hass.services.async_call("homeassistant", "turn_on" if on else "turn_off", {"entity_id": entity_id})
        except Exception:  # noqa: BLE001 - a missing entity must not affect battery control
            _LOGGER.debug("could not switch %s", entity_id, exc_info=True)

    def _account(self, now: datetime, wall: float, actuals: dict[str, Actual], grid: float | None, rates: RateTable | None) -> None:
        """Energy counters and the savings ledger (an estimate: see core/energy.py)."""
        steps: dict[str, tuple[float, float]] = {}
        for did, act in actuals.items():
            steps[did] = self.meter.add(f"bat:{did}", wall, act.power_w if act.online else None)
        g_in, g_out = self.meter.add("grid", wall, grid)
        for did, act in actuals.items():
            if act.online and (act.pv_w or 0) > 50 and did not in self.manager.pv_seen:
                self.manager.pv_seen.add(did)      # this battery has panels: remembered for 'solar: auto'
        pv = [a.pv_w for a in actuals.values() if a.online and a.pv_w is not None]
        pv_in, _ = self.meter.add("pv", wall, sum(pv) if pv else None)
        price = self._price_now(rates, now)
        self._cheap_now = self._is_cheap(rates, now, price)
        self.daily.add(now.strftime("%Y-%m-%d"), pv_kwh=pv_in, export_kwh=g_out, import_kwh=g_in, cheap=self._is_cheap(rates, now, price))
        load_w = self._house_load(actuals, grid, sum(pv) if pv else 0.0)
        l_in, _ = self.meter.add("load", wall, load_w)
        self.usage.add(now, g_in, g_out, l_in, pv_in)
        self.profile.record(now, load_w)
        self.health.update(now.strftime("%G-W%V"), {did: (self.meter.get(f"bat:{did}_in"), self.meter.get(f"bat:{did}_out"))
                                                     for did in actuals})
        self._account_savings(now, actuals, grid, rates, steps, g_out)

    def _account_savings(self, now: datetime, actuals: dict[str, Actual], grid: float | None, rates: RateTable | None,
                         steps: dict[str, tuple[float, float]], g_out_kwh: float = 0.0) -> None:
        """Savings ledger (an estimate: see core/energy.py Ledger)."""
        on = {did: a for did, a in actuals.items() if a.online}
        # Batteries told to charge from the grid: all their charging is bought. Solar-only and grid + solar ones, and
        # batteries charging by themselves, are judged from the grid reading: whatever is imported beyond the forced
        # charging is shared among them; with no reading their charging is counted as solar.
        forced = {did for did, st in self._snap.devices.items() if did in on and self._grid_charging(st)}
        forced_w = sum(max(0.0, on[d].power_w or 0.0) for d in forced)
        # Charging a battery does from its own panels never passes the house meter and is free: only what it charges beyond
        # its panels can have come from the grid, so only that part takes a share of the import.
        free = {d: (max(0.0, a.power_w or 0.0), max(0.0, (a.power_w or 0.0) - max(0.0, a.pv_w or 0.0))) for d, a in on.items() if d not in forced}
        free_np = sum(np_ for _, np_ in free.values())
        np_share = min(1.0, max(0.0, grid - forced_w) / free_np) if grid is not None and free_np > 0 else 0.0
        shares = {d: (np_share * np_ / p if p > 0 else 0.0) for d, (p, np_) in free.items()}
        # Battery energy that went out to the grid replaced no import: it earns the export price (if any) instead.
        out_w = sum(max(0.0, -(a.power_w or 0.0)) for a in on.values())
        exp_share = min(1.0, max(0.0, -grid) / out_w) if grid is not None and out_w > 0 else 0.0
        unit, price = self._unit(rates), self._price_now(rates, now)
        exp_price = self._export_price_now(rates, now) or 0.0
        cheapest = self._cheapest_price(rates, now)
        first = None if cheapest is None else to_main_units(cheapest, unit)
        caps = {a.config.device_id: a.config.caps.capacity_wh for a in self.adapters}
        day = now.strftime("%Y-%m-%d")
        saved = 0.0
        for did, act in actuals.items():
            d_in, d_out = steps.get(did, (0.0, 0.0))
            stored = act.soc / 100 * caps.get(did, 0) / 1000 if act.online and act.soc is not None and did in caps else None
            saved += self.ledger.add(day, did, d_in, 1.0 if did in forced else shares.get(did, 0.0), d_out, exp_share, price, exp_price, stored, first) or 0.0
        self.ledger.forget(set(caps))
        # lifetime money counters (kept with the energy counters) for the 'total' sensors that Home Assistant keeps statistics of
        totals = self.meter.totals
        if saved == saved and abs(saved) < 1e6:
            totals[MONEY_SAVED] = totals.get(MONEY_SAVED, 0.0) + saved
        if g_out_kwh > 0 and exp_price:
            totals[MONEY_EXPORT] = totals.get(MONEY_EXPORT, 0.0) + g_out_kwh * exp_price

    def _record_plan(self, now: datetime, actuals: dict[str, Actual]) -> None:
        """Remember what each battery was told to do, and what it did, for the plan-versus-actual chart."""
        for did, act in actuals.items():
            st = self._snap.devices.get(did)
            want = st.desired if st else None
            planned = None
            if want is not None and want.mode is not Mode.AUTO:
                planned = {Mode.CHARGE: 1, Mode.DISCHARGE: -1}.get(want.mode, 0) * float(want.power_w or 0)
            self.plan_log.add(now, did, planned, act.power_w if act.online else None)

    def _watch_traces(self, wall: float, now: datetime, actuals: dict[str, Actual]) -> None:
        for did, act in actuals.items():
            if act.online:
                rec = self.traces.observe(did, wall, act.soc, act.power_w, act.mode.value if act.mode else None)
                if rec:
                    self._log(did, f"command result: {rec['mode']} {'worked' if rec['ok'] else 'had no effect (nearly full: not counted)' if rec.get('inconclusive') else 'had no effect'} "
                                   f"(peak {rec['peak_w']} W, {'reacted after ' + str(rec['reaction_s']) + ' s' if rec['ok'] else 'no reaction in 10 min'})")

    def _watch_pv(self, now: datetime, mono: float, actuals: dict[str, Actual]) -> None:
        """X1: feed each solar string's reading to the string health check (core/pv_health.py)."""
        day = now.strftime("%Y-%m-%d")
        for did, act in actuals.items():
            if act.online and act.pv_strings:
                self.pv_health.observe(did, day, act.pv_strings, mono)

    def _forget_plan(self, did: str) -> None:
        for devs in self.plan_log.days.values():
            devs.pop(did, None)

    def _learn_rates(self, adapters) -> None:
        changed = False
        for a in adapters:
            st = self._snap.devices.get(a.config.device_id)
            if st is None or st.actual is None or a.config.caps.power_control:
                continue
            changed |= self.rate_learner.observe(a.config.device_id, self._grid_charging(st), st.actual.power_w, st.actual.soc)
        if changed:
            self.manager.charge_rates = dict(self.rate_learner.rates)
