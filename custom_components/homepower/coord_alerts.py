"""Telling the user: alerts (filed under Repairs while they last), notifications, heads-ups, the daily summary and weekly digest.

Part of HomePowerCoordinator (coordinator.py), kept in its own file so each file has one concern: the methods
run on the coordinator itself and use its state."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from datetime import datetime, time, timedelta

from homeassistant.util import dt as dt_util

from .adapters.base import DeviceAdapter
from .const import DOMAIN
from .core.models import Actual
from .core.anker_x1_map import X1_ALARMS
from .core.rates import RateTable
from .core.sanity import grid_sign_mismatch, offline_hint
from .core.reconcile import Decision
from .coord_base import (_LOGGER, OFFLINE_ALERT_AFTER_S, PROTECT_ALERT_AFTER_S, UNCONFIRMED_ALERT_AFTER, ACTION_PAUSE, ACTION_HAND_BACK, REPAIR_KINDS, repair_id)

if TYPE_CHECKING:
    from homeassistant.core import Event, HomeAssistant

    from .adapters.base import DeviceAdapter
    from .coord_base import Snapshot
    from .core.alerts import AlertTracker
    from .core.energy import Ledger
    from .core.insights import DailyStats
    from .core.insights import GridEvents
    from .core.insights import Health
    from .core.insights import HealthTrend
    from .core.insights import UsageHistory
    from .core.pv_health import PvHealth
    from .core.sanity import ChargeWatch
    from .core.settings import AppSettings
    from .manager import HomePowerManager


class AlertsMixin:
    if TYPE_CHECKING:    # used here but defined on the class this is mixed into: declared for the type checker only
        _combined: Any
        _cover_text: Any
        _event_state: dict[str, Any]
        _grid_charging: Any
        _log: Any
        _log_failure: Any
        _snap: Snapshot
        adapters: list[DeviceAdapter]
        alerts: AlertTracker
        charge_watch: ChargeWatch
        daily: DailyStats
        grid_events: GridEvents
        hass: HomeAssistant
        health: Health
        last_grid: float | None
        ledger: Ledger
        manager: HomePowerManager
        paused: Any
        pv_health: PvHealth
        release_all: Any
        settings: AppSettings
        shown_actuals: Any
        tariff_comparison: Any
        testing: set[str]
        trend: HealthTrend
        usage: UsageHistory

    def _weekly_digest(self, now: datetime) -> str:
        """One message a week: savings, grid use, outages and how the batteries are doing."""
        cur = self.settings.currency
        days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, 8)]
        parts = [f"Last 7 days: saved about {cur}{sum(self.ledger.saved(d) for d in days):.2f}."]
        imp = sum(sum(self.usage.days[d]["imp"]) for d in days if d in self.usage.days)
        exp = sum(sum(self.usage.days[d]["exp"]) for d in days if d in self.usage.days)
        if imp or exp:
            parts.append(f"Imported {imp:.0f} kWh from the grid and exported {exp:.0f} kWh.")
        cutoff = (now - timedelta(days=7)).isoformat()
        count, mins = self.grid_events.outages_since(cutoff, now)
        if count:
            parts.append(f"{count} grid outage{'s' if count != 1 else ''}, {mins:.0f} min in total.")
        for a in self.adapters:
            eff = self.health.efficiency(a.config.device_id)
            if eff:
                parts.append(f"{a.config.name}: round-trip efficiency {eff[-1][1]:g}%.")
            if (w := self.trend.warning(a.config.device_id)):
                parts.append(f"{a.config.name}: {w}.")
        try:
            flag = self.tariff_comparison(30)["monthly"]["cheaper_than_current"]
        except ValueError:
            flag = None
        if flag:
            parts.append(f"{flag['tariff']} has been cheaper than {flag['current']} for {flag['months']} months running.")
        return " ".join(parts)

    def _repair(self, key: str, message: str, raised: bool) -> bool:
        """A lasting problem goes to Settings > Repairs instead of a notification: it clears itself when the problem ends.
        Returns False when this key is not such a problem, or Repairs is not available (the caller notifies instead)."""
        kind = key.split(":", 1)[0]
        if kind not in REPAIR_KINDS:
            return False
        try:
            from homeassistant.helpers import issue_registry as ir

            iid = repair_id(key)
            if raised:
                # some have an obvious remedy, offered right there (repairs.py): test the battery or stop commanding it; use the
                # cheap hours found in the price history
                fix = {"unconfirmed": "battery", "stuck": "battery", "tariffdrift": "tariff"}.get(kind)
                data: dict[str, Any] = {"fix": fix, "message": message[:500], "title": REPAIR_KINDS[kind]}
                if fix == "battery":
                    data["device_id"] = key.split(":", 1)[1]
                ir.async_create_issue(self.hass, DOMAIN, iid, is_fixable=fix is not None, is_persistent=False, data=data if fix else None,
                                      severity=ir.IssueSeverity.ERROR if kind in ("offline", "error", "stuck", "grid") else ir.IssueSeverity.WARNING,
                                      translation_key=f"alert_fix_{fix}" if fix else "alert",
                                      translation_placeholders={"title": REPAIR_KINDS[kind], "message": message})
            else:
                ir.async_delete_issue(self.hass, DOMAIN, iid)
            return True
        except Exception:  # noqa: BLE001 - Repairs unavailable: fall back to a notification
            _LOGGER.debug("could not file a repair for %s", key, exc_info=True)
            return False

    def _clear_stale_repairs(self) -> None:
        """At start: Repairs left by the last run are dropped. Any problem that is still there is raised again within minutes."""
        try:
            from homeassistant.helpers import issue_registry as ir

            for dom, iid in list(ir.async_get(self.hass).issues):
                if dom == DOMAIN:
                    ir.async_delete_issue(self.hass, DOMAIN, iid)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("could not clear old repairs", exc_info=True)

    async def _notify(self, key: str, message: str, raised: bool) -> None:
        """Tell the user: a notification in Home Assistant and, if chosen, one on the phone. Each is tried on its own, so a
        failure of one never stops the other (and alerts never break the control loop)."""
        nid = f"homepower_alert_{key}"
        repair = self._repair(key, message, raised)
        try:
            if raised and not repair:
                await self.hass.services.async_call("persistent_notification", "create",
                                                    {"title": "HomePower", "message": message, "notification_id": nid})
            else:      # a problem shown under Repairs also clears a notification an older version left for it
                await self.hass.services.async_call("persistent_notification", "dismiss", {"notification_id": nid})
        except Exception:  # noqa: BLE001
            _LOGGER.debug("in-app alert delivery failed", exc_info=True)
        try:
            svc = self.settings.notify_service
            if svc and "." in svc:
                domain, name = svc.split(".", 1)
                note: dict[str, Any] = {"title": "HomePower", "message": message if raised else f"Resolved: {message}"}
                if raised and name.startswith("mobile_app_"):     # the phone app shows these as buttons on the notification
                    note["data"] = {"tag": nid, "actions": [{"action": ACTION_PAUSE, "title": "Pause HomePower"},
                                                            {"action": ACTION_HAND_BACK, "title": "Hand back to Auto"}]}
                await self.hass.services.async_call(domain, name, note)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("phone alert delivery failed", exc_info=True)

    async def _check_events(self, now: datetime, now_s: float, actuals: dict[str, Actual], rates: RateTable | None) -> None:
        """Optional heads-up messages: battery full, battery low, cheap rate starting soon with a low battery."""
        s = self.settings
        fig = self._combined(self.shown_actuals())    # a battery whose read failed a moment ago still counts (no false "low")
        soc = fig["soc"]

        async def edge(key: str, active: bool, clear: bool, msg: str) -> None:
            # A dated heads-up (daily summary, weekly digest, cheap rate soon) replaces the last one of its kind: with the
            # date in the notification's id, a new one would be added every day and they would pile up.
            nid = "event_" + (key.split("_")[0] if key.startswith(("summary_", "digest_", "cheap_")) else key)
            was = self._event_state.get(key, False)
            if active and not was:
                self._event_state[key] = True
                self._log("events", msg)
                await self._notify(nid, msg, True)
            elif was and clear:
                self._event_state[key] = False
                await self._notify(nid, msg, False)

        today = f"{now:%Y%m%d}"
        for k in [k for k in self._event_state if k.startswith(("cheap_", "summary_", "tariff_", "digest_")) and today not in k and not k.startswith("tariff_m")]:
            del self._event_state[k]            # yesterday's once-a-day markers (the once-a-month tariff marker is cleaned up where it is set)
        if s.weekly_digest and now.weekday() == 0:
            at = s.daily_summary_time or time(8, 0)
            due = now.time() >= at
            await edge(f"digest_{today}", due, False, self._weekly_digest(now) if due and not self._event_state.get(f"digest_{today}") else "")
        if f"tariff_{today}" not in self._event_state:
            self._event_state[f"tariff_{today}"] = True
            month = f"tariff_m{now:%Y%m}"
            try:
                flag = self.tariff_comparison(30)["monthly"]["cheaper_than_current"]
            except ValueError:
                flag = None
            if flag and not self._event_state.get(month):
                for k in [k for k in self._event_state if k.startswith("tariff_m") and k != month]:
                    del self._event_state[k]
                self._event_state[month] = True      # said once this month
                await self._notify(f"tariff_{flag['tariff']}", f"{flag['tariff']} would have been cheaper than {flag['current']} for the last {flag['months']} months running "
                                   f"(about {self.settings.currency}{flag['saved']:.2f} less in total), going by the usage HomePower recorded.", True)
        if soc is None:
            return
        # "Full" is 95%: many batteries stop at their own charge ceiling below 100. Switching a heads-up off clears it.
        await edge("full", s.notify_full and soc >= 95, not s.notify_full or soc < 88, f"The home battery is full ({soc:g}%).")
        await edge("low", bool(s.notify_low_pct) and soc <= s.notify_low_pct, not s.notify_low_pct or soc > s.notify_low_pct + 5,
                   f"The home battery is low ({soc:g}%).")
        nxt = rates.minutes_to_cheap(now.time()) if rates is not None else None
        if s.notify_cheap_soon and nxt is not None:
            await edge(f"cheap_{now:%Y%m%d}_{nxt[1]:%H%M}", 0 < nxt[0] <= 30 and soc < s.cheap_soon_below_pct, False,
                       f"Cheap rate starts at {nxt[1]:%H:%M} and the battery is at {soc:g}%.")
        if s.daily_summary_time is not None:
            due = now.time() >= s.daily_summary_time and (now.hour * 60 + now.minute) - (s.daily_summary_time.hour * 60 + s.daily_summary_time.minute) < 30
            await edge(f"summary_{now:%Y%m%d}", due, False, self._daily_summary(now, fig))

    def _daily_summary(self, now: datetime, fig: dict[str, Any]) -> str:
        """One message a day: where the battery is and how yesterday went."""
        y = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        led, cur = self.ledger.days.get(y), self.settings.currency
        parts = [f"Home battery {fig['soc']:g}% ({fig['stored_kwh']:g} kWh stored)."]
        if led:
            parts.append(f"Yesterday: charged {led['charged']:.1f} kWh, discharged {led['discharged']:.1f} kWh, "
                         f"saved about {cur}{led['value'] - led['cost']:.2f}.")
        sc, ch = self.daily.self_consumption(y), self.daily.cheap_share(y)
        if sc is not None:
            parts.append(f"{sc:g}% of your solar was used at home.")
        if ch is not None:
            parts.append(f"{ch:g}% of grid energy was bought at the cheap rate.")
        if fig.get("runtime_h") is not None:
            parts.append(f"At the current draw the battery lasts about {fig['runtime_h']:g} h.")
        return " ".join(parts)

    async def _check_alerts(self, now_s: float, adapters: list[DeviceAdapter]) -> None:
        for a in adapters:
            try:
                did, name = a.config.device_id, a.config.name
                st = self._snap.devices.get(did)
                if st is None:
                    continue
                checks = {
                    f"offline:{did}": (bool(st.actual and not st.actual.online) and st.link == "offline", f"{name} is offline: HomePower cannot read it." + (f" {h}" if (h := offline_hint(a.config.kind)) else ""), OFFLINE_ALERT_AFTER_S),
                    f"error:{did}": (st.last_error is not None, f"A command to {name} failed: {st.last_error}" if st.last_error
                                     else f"A command to {name} failed earlier and is working again.", 0),
                    f"unconfirmed:{did}": (st.unconfirmed >= UNCONFIRMED_ALERT_AFTER, f"{name} is not following HomePower's commands (not confirmed after {UNCONFIRMED_ALERT_AFTER} tries).", 0),
                    f"grid:{did}": (bool(st.actual and st.actual.off_grid), f"{name} reports the grid is down and is running the house itself. HomePower is not sending it commands until the grid is back."
                                    + (self._cover_text() if st.actual and st.actual.off_grid else ""), 0),
                    f"protect:{did}": (st.decision is Decision.SKIP_PROTECTED and not (st.actual and st.actual.off_grid), f"{name} has manufacturer protection active, so HomePower is not overriding it.", PROTECT_ALERT_AFTER_S),
                }
                act = st.actual
                watching = bool(act and act.online and did not in self.testing and not self.settings.dry_run
                                and a.config.extra.get("control_enabled", True) and not self.paused and not st.awaiting_confirm
                                and st.decision is not None and st.decision is not Decision.SKIP_PROTECTED)   # nothing was sent otherwise
                if watching and act is not None:        # (watching needs a reading)
                    tgt = st.desired.target_soc if st.desired else None
                    stuck = self.charge_watch.stuck(did, self._grid_charging(st), act.soc, act.power_w, tgt, now_s)
                else:
                    self.charge_watch.forget(did)
                    stuck = False                       # offline, being tested or not controlled: the alert (if raised) clears
                checks[f"stuck:{did}"] = (stuck, f"{name} was told to charge but is not charging (no power and no rise in charge for "
                                                 f"{int(self.charge_watch.after_s // 60)} min). Check the battery's own app for a mode or schedule overriding HomePower.", 0)
                if a.config.extra.get("driver") in ("ecoflow_stream", "ecoflow_modbus"):
                    checks[f"gridsign:{did}"] = (act is not None and act.online and grid_sign_mismatch(self.last_grid, act.grid_w),
                                                 f"{name}'s grid reading ({act.grid_w:g} W) points the opposite way to your grid meter ({self.last_grid:g} W). "
                                                 "One of them is reversed or measures a different point: check Settings > grid sensor and its 'invert' option."
                                                 if self.last_grid is not None and act and act.grid_w is not None else "", 600)
                for text in X1_ALARMS.values():     # alarms the X1 itself reports (a failed meter or inverter link, ...)
                    checks[f"alarm:{did}:{text}"] = (bool(act and text in act.alarms), f"{name} reports: {text}.", 0)
                fw, fw_key = act.firmware if act else None, f"fw:{did}"
                if fw:
                    before = self._event_state.get(fw_key)
                    if before and before != fw:
                        await self._notify(f"fwchg:{did}:{fw}", f"{name}'s software changed from {before} to {fw}. If something stops working, "
                                           "run Diagnostics > Registers: compare: register addresses can differ between versions.", True)
                    self._event_state[fw_key] = fw
                weak = self.health.degraded(did)
                checks[f"health:{did}"] = (weak is not None, f"{name}: {weak}", 0)
                if act and act.online:
                    self.grid_events.observe(did, name, act.off_grid, dt_util.now(), act.soc)
                    self.trend.update(dt_util.now().strftime("%Y-%m-%d"), did, act.temp_c, act.pack_v)
                drift = self.trend.warning(did)
                checks[f"trend:{did}"] = (drift is not None, f"{name}: {drift}", 0)
                if a.config.extra.get("house_meter") or did in self.pv_health.used:   # X1 / EcoFlow: a solar string dead or clearly weaker than usual
                    pv = self.pv_health.warnings(did, dt_util.now().strftime("%Y-%m-%d"), now_s)
                    for n in range(1, 9):
                        checks[f"pv:{did}:{n}"] = (n in pv, f"{name}: {pv.get(n, f'PV{n} is producing normally again')}.", 0)
                for key, (active, msg, after) in checks.items():
                    ev = self.alerts.observe(key, active, now_s, after)
                    if ev:
                        self._log(did, ("ALERT: " if ev == "raised" else "resolved: ") + msg)
                        await self._notify(key, msg, ev == "raised")
            except Exception:  # noqa: BLE001 - one battery whose alerts cannot be worked out must not silence the others
                self._log_failure(f"alerts {a.config.device_id}", "could not check the alerts of battery %s", a.config.device_id)

    async def _on_action(self, event: Event) -> None:
        """A button on a HomePower phone notification was tapped."""
        action = (event.data or {}).get("action")
        try:
            if action == ACTION_PAUSE:
                await self.manager.set_paused(True)
                self._log("events", "paused from a notification button")
            elif action == ACTION_HAND_BACK:
                await self.manager.set_paused(True)          # paused first: stay handed back, and no cycle forces one in between
                await self.release_all(force=True)
                self._log("events", "batteries handed back and HomePower paused from a notification button")
        except Exception:  # noqa: BLE001 - a button must never break the control loop
            _LOGGER.warning("notification action failed", exc_info=True)
