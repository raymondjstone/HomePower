"""What the panel, Diagnostics and shared reports show: the live view, setup check, readiness, debug file and hardware report.

Part of HomePowerCoordinator (coordinator.py), kept in its own file so each file has one concern: the methods
run on the coordinator itself and use its state."""
from __future__ import annotations

import re
import time as _time
from datetime import datetime, timedelta

from homeassistant.util import dt as dt_util

from .core.preview import find_conflicts, preview as build_preview
from .core.report import ecoflow_report
from .core.sanity import explain_idle, offline_hint
from .coord_base import _LOGGER, GRID_SLOW_AFTER_S, DeviceStatus, _PRIVATE_KEY


class ReportsMixin:
    def readiness(self) -> dict | None:
        """How long the batteries could run the house if the grid went down now: the usable energy (above each battery's
        minimum) against the usual household load for the coming hours, or the current load if that is not learned yet.
        Ignores solar, so it is the cautious figure."""
        now, wh, n = dt_util.now(), 0.0, 0
        for a in self.adapters:
            st = self._snap.devices.get(a.config.device_id)
            act = st.actual if st else None
            if act and act.online and act.soc is not None:
                wh += max(0.0, act.soc - a.config.caps.min_soc) / 100 * a.config.caps.capacity_wh
                n += 1
        if not n:
            return None
        left, t, hours, basis = wh, now, 0.0, "usual use at this time of day"
        while hours < 72:
            e = self.profile.expected_wh(t, 0.5)
            if e is None:
                hours = None
                break
            if e >= left:
                hours += 0.5 * left / max(e, 1e-9)
                left = 0.0
                break
            left -= e
            t += timedelta(minutes=30)
            hours += 0.5
        if hours is None:
            loads = [s.actual.load_w for s in self._snap.devices.values() if s.actual and s.actual.online and s.actual.load_w]
            basis = "the load right now"
            hours = wh / max(loads) if loads else None
        return {"usable_kwh": round(wh / 1000, 1), "hours": None if hours is None else round(min(hours, 72.0), 1),
                "capped": bool(hours is not None and hours >= 72), "basis": basis}

    def _cover_text(self) -> str:
        r = self.readiness()
        if not r or r["hours"] is None:
            return ""
        return f" About {'more than ' if r['capped'] else ''}{r['hours']:g} h of cover left at {r['basis']}, ignoring solar."

    def whatif(self, days: int, base: dict, alt: dict) -> dict:
        """Replay the recorded house load and solar with two different sets of battery settings (core/whatif.py)."""
        from .core.tariffs import parse_tariffs
        from .core.whatif import simulate
        ts = parse_tariffs(self.settings.compare_tariffs)
        if not ts:
            raise ValueError("List the tariff you are on, first, in Settings > Compare tariffs so there are prices to work with")
        usable = {k: v for k, v in self.usage.days.items() if "load" in v}
        keys = sorted(usable)[-days:]
        if len(keys) < 3:
            raise ValueError(f"needs at least 3 days of recorded house load and solar; HomePower has {len(keys)} so far (it records them from this version on)")
        cap = sum(a.config.caps.capacity_wh for a in self.adapters) / 1000
        kw = sum(a.config.caps.max_charge_w for a in self.adapters) / 1000
        if cap <= 0 or kw <= 0:
            raise ValueError("the batteries' capacity and charge power are not known")
        data = {k: usable[k] for k in keys}
        return {"days": len(keys), "capacity_kwh": round(cap, 1), "max_kw": round(kw, 1), "tariff": ts[0]["name"],
                "base": simulate(data, ts[0], cap, kw, **base), "alt": simulate(data, ts[0], cap, kw, **alt)}

    def setup_check(self) -> list[dict]:
        """Is everything set up? A plain checklist for the panel (ok / what is missing / where to fix it)."""
        s, items = self.settings, []

        def add(key: str, ok: bool, title: str, detail: str = "", action: str = "", target: str = "") -> None:
            items.append({"key": key, "ok": bool(ok), "title": title, "detail": detail, "action": action, "target": target})

        add("batteries", bool(self.adapters), "At least one battery added", "" if self.adapters else "Add your first battery.", "tab", "batteries")
        for a in self.adapters:
            did, name, st = a.config.device_id, a.config.name, self._snap.devices.get(a.config.device_id)
            act = st.actual if st else None
            reading = bool(act and act.online and act.soc is not None)
            add(f"reading:{did}", reading, f"{name} is reporting", "" if reading else
                ("It is offline or unreachable." if not (act and act.online) else "It is online but sends no charge level."), "tab", "diagnostics")
            modes = a.config.caps.supported_modes
            if len(modes) > 1:
                allowed = bool(a.config.extra.get("control_enabled", True))
                add(f"control:{did}", allowed, f"{name}: control allowed", "" if allowed else
                    "Monitor-only: HomePower reads it but sends no commands. Allow control on the Batteries tab when you are ready.", "tab", "batteries")
                if allowed:
                    t = self.manager.tests.get(did)
                    add(f"tested:{did}", bool(t and t.get("ok")), f"{name}: control tested",
                        "" if t and t.get("ok") else ("The last test failed." if t else "Run Test control once to check the battery follows commands."),
                        "test", did)
        for a in self.adapters:
            if not a.config.caps.power_control and a.config.extra.get("control_enabled", True):
                did, t = a.config.device_id, self.manager.tests.get(a.config.device_id)
                ok = did in self.manager.confirmed or bool(t and t.get("ok"))
                add(f"first:{did}", ok, f"{a.config.name}: first command allowed", "" if ok else
                    "Press 'Allow first command' on its card (or pass Test control). This battery's control has not been proven yet.", "tab", "overview")
        rates = self._rates()
        add("prices", rates is not None, "Electricity prices known", "" if rates is not None else
            "Pick a rates sensor or type your standard and cheap rate.", "tab", "settings")
        slots = bool(rates and rates.slots and len(rates.slots) > 2)
        exact = bool(s.offpeak_start and s.offpeak_end) or bool(s.offpeak_extra)
        add("tariff", exact or slots, "Cheap-rate hours confirmed", "" if exact or slots else
            ("They are only estimated from price history." if self._tariff else "Enter when your cheap rate starts and ends."), "tab", "settings")
        if any(self._has_solar(a.config) for a in self.adapters):
            fc = self._forecast_by_day(dt_util.now())
            ok = bool(fc)
            add("solar_forecast", ok, "Solar forecast available", "" if ok else
                "Some batteries have solar panels. Add the Forecast.Solar or Solcast integration (Settings > Devices & services > "
                "Add integration), or pick a forecast sensor in HomePower's Settings, so overnight charging leaves room for "
                "the next day's solar. Until then solar is counted as zero.", "tab", "settings")
        big = self._big_loads()
        if big:
            add("big_loads", False, "Big loads covered by a load rule",
                f"{', '.join(big)} looks like an EV charger or heat pump that no load rule covers, so a battery could be emptied into it. "
                "In Settings > Protect loads, add a rule for it (for example: busy above 1000 W, block discharge).", "tab", "settings")
        add("grid", self.last_grid is not None, "Grid meter reading available", "" if self.last_grid is not None else
            "Needed for peak shaving, the export limit, solar-only charging and savings. Pick a grid sensor in Settings.", "tab", "settings")
        if self.last_grid is not None:
            age = self.grid_age_s
            fresh = age is None or age <= GRID_SLOW_AFTER_S
            add("grid_fresh", fresh, "Grid reading is live", "" if fresh else
                f"The reading from {self.grid_source} is {age / 60:.0f} min old. Solar-only charging, peak shaving and the export "
                "limit need a live reading: pick a faster grid sensor in Settings.", "tab", "settings")
        return items

    def _big_loads(self) -> list[str]:
        """EV-charger / heat-pump power sensors no load rule covers (looked for at most every 5 minutes)."""
        now = _time.monotonic()
        cache = getattr(self, "_big_cache", None)
        if cache is not None and now - cache[0] < 300 and cache[1] == repr(self.settings.load_rules):
            return cache[2]
        from .core.loads import big_load_sensors

        try:
            ents = [(st.entity_id, st.attributes.get("device_class"), st.attributes.get("unit_of_measurement"))
                    for st in self.hass.states.async_all("sensor")]
            found = big_load_sensors(ents, self.settings.load_rules)
        except Exception:  # noqa: BLE001 - only a suggestion
            _LOGGER.debug("big-load search failed", exc_info=True)
            found = []
        self._big_cache = (now, repr(self.settings.load_rules), found)
        return found

    def pv_view(self, did: str) -> dict | None:
        """One X1's solar strings for the panel: live volts, amps and watts with each string's health."""
        st = self._snap.devices.get(did)
        act = st.actual if st else None
        if did not in self.pv_health.used and not (act and act.pv_strings):
            return None
        now = dt_util.now()
        rows = self.pv_health.status(did, now.strftime("%Y-%m-%d"), _time.monotonic())
        live = list(act.pv_strings) if act and act.online and act.pv_strings else []
        for r in rows:
            i = r["string"] - 1
            if i < len(live):
                v, a = live[i]
                r.update({"volts": round(v, 1), "amps": round(a, 2), "watts": round(v * a)})
        return {"strings": rows, "total_w": None if not (act and act.online and act.pv_total_w is not None) else round(act.pv_total_w)}

    def preview_view(self, hours: int = 24) -> dict:
        now = dt_util.now()
        ids = [a.config.device_id for a in self.adapters]
        kw = dict(condition_ok=self._condition_ok, away=self.away)
        segs = build_preview(self.schedules, ids, now, hours, allocs=self.last_result.allocs, **kw)
        return {
            "segments": {d: [{"start": s.start.isoformat(), "end": s.end.isoformat(), "mode": s.mode,
                              "power_w": s.power_w, "source": s.source} for s in lst] for d, lst in segs.items()},
            "conflicts": [{**c, "start": c["start"].isoformat(), "end": c["end"].isoformat()}
                          for c in find_conflicts(self.schedules, ids, now, hours * 2, **kw)],
            "plan": [{"device_id": p.device_id, "start": p.start.isoformat(), "end": p.end.isoformat(),
                      "power_w": p.power_w, "price": p.price} for p in self.last_result.allocs],
            "notes": self.last_result.notes,
            "prices": self._price_slots(now, hours),
            # 'holds': whether 'keep stored energy for dearer hours' is on, i.e. whether the half-hours left out are really kept
            "day_plan": ({**self.day_plan.as_dict(), "holds": bool(self.settings.engine.hold_for_dearer)}
                         if getattr(self, "day_plan", None) else None),
            "events": [{"start": s.isoformat(), "end": e.isoformat()} for s, e in self._event_windows()
                       if e > now and s < now + timedelta(hours=hours)],
        }

    _IPV4 = re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?![\w])")

    def scrub(self, obj, strict: bool = False):
        """Remove what must never be shared from any text: the keys and passwords always, and (strict, for anything the
        user may paste or attach) also each battery's address and serial number and any IPv4 address. Error messages are
        where these end up, so this works on every string, however deep."""
        secrets: set[str] = set()
        conns = [a.config.extra.get("connection", {}) for a in self.adapters]
        conns += [d.get("connection", {}) for d in self.manager.devices.values() if isinstance(d, dict)]   # also ones that failed to build
        keys = ("access_key", "secret_key", "password", "token") + (("host", "sn", "serial") if strict else ())
        for conn in conns:
            if isinstance(conn, dict):
                secrets |= {str(conn[k]) for k in keys if isinstance(conn.get(k), (str, int)) and len(str(conn[k])) >= 4}
        ordered = sorted(secrets, key=len, reverse=True)

        def clean(text: str) -> str:
            for s in ordered:
                text = text.replace(s, "<hidden>")
            return self._IPV4.sub("<ip>", text) if strict else text

        def walk(x):
            if isinstance(x, str):
                return clean(x)
            if isinstance(x, dict):
                return {k: walk(v) for k, v in x.items()}
            if isinstance(x, (list, tuple)):
                return [walk(v) for v in x]
            return x
        return walk(obj)

    def debug_bundle(self) -> dict:
        """One file for a bug report: everything below is already free of keys; serial numbers and addresses are removed too."""
        now = dt_util.now()
        diag = self.diagnostics()
        for d in diag.values():
            d["connection"] = {k: v for k, v in (d.get("connection") or {}).items() if k in ("region", "port", "unit_id")}
        from dataclasses import fields

        plain = {f.name: getattr(self.settings, f.name) for f in fields(self.settings)
                 if isinstance(getattr(self.settings, f.name), (int, float, str, bool, type(None))) and not re.search(r"notify|entity", f.name)}
        return self.scrub({"made_at": now.isoformat(), "startup": self.startup_note, "settings": plain,
                "batteries": diag, "command_traces": self.traces.dump(), "charge_rates": dict(self.rate_learner.rates),
                "plan_against_actual": {a.config.device_id: self.plan_log.view(a.config.device_id, now) for a in self.adapters},
                "events": [list(e) for e in list(self._snap.events)[-150:]], "alerts": sorted(self.alerts.active())}, strict=True)

    def hardware_report(self) -> dict:
        """What each battery really did under HomePower's control, to share so a model can be marked as proven on real hardware.
        Only model, firmware, limits and results: no names, keys, serial numbers or addresses (scrubbed again to be sure)."""
        now = dt_util.now()
        bats = []
        for n, a in enumerate(self.adapters, 1):
            did, c = a.config.device_id, a.config.caps
            st = self._snap.devices.get(did) or DeviceStatus()
            act = st.actual
            test = self.manager.tests.get(did) or {}
            results = self.traces.results.get(did, [])
            plan = self.plan_log.view(did, now)
            drv = getattr(a, "driver", None)
            conn = a.config.extra.get("connection") or {}
            bats.append({
                "battery": f"battery {n}", "kind": a.config.kind, "driver": a.config.extra.get("driver"),
                "model": conn.get("model") or a.config.extra.get("model"), "firmware": act.firmware if act else None,
                "limits": {"capacity_wh": c.capacity_wh, "max_charge_w": c.max_charge_w, "max_discharge_w": c.max_discharge_w,
                           "min_soc": c.min_soc, "modes": sorted(m.value for m in c.supported_modes), "sets_watts": c.power_control},
                "reading": {"online_now": bool(act and act.online), "reports_mode": bool(act and act.mode is not None),
                            "reports_set_power": bool(act and act.set_power_w is not None), "reports_solar": did in self.manager.pv_seen,
                            "recent_outages": len(getattr(drv, "outages", None) or [])},
                "control_allowed": bool(a.config.extra.get("control_enabled", True)),
                "control_test": {"passed": test.get("ok"), "at": test.get("at"),
                                 "steps": [{k: s.get(k) for k in ("name", "ok", "detail", "info")} for s in test.get("steps", [])]} if test else None,
                "commands_watched": len(results), "commands_worked": sum(1 for r in results if r.get("ok")),
                "charge_verdict": self.traces.verdict(did) if not c.power_control else None,
                "measured_charge_w": self.rate_learner.rates.get(did),
                "followed_plan_pct": plan.get("match_pct"),
                "not_confirmed_in_a_row": st.unconfirmed, "last_error": st.last_error,
                "proven": bool(test.get("ok")) and (plan.get("match_pct") or 0) >= 80,
            })
        try:
            from . import VERSION      # read from manifest.json when the integration loads
        except ImportError:
            VERSION = None
        return self.scrub({"kind": "homepower hardware report", "made_at": now.isoformat(timespec="minutes"), "homepower": VERSION,
                           "batteries": bats,
                           "how_to_read": "proven = Test control passed and the battery followed at least 80% of HomePower's plan over the last day"},
                          strict=True)

    def diagnostics(self) -> dict:
        out = {}
        for a in self.adapters:
            did, st = a.config.device_id, self._snap.devices.get(a.config.device_id)
            conn = dict(a.config.extra.get("connection", {}))
            for secret in ("secret_key", "access_key"):
                if secret in conn:
                    conn[secret] = "***"
            out[did] = {
                "name": a.config.name, "kind": a.config.kind, "driver": a.config.extra.get("driver"), "connection": conn,
                "control_enabled": bool(a.config.extra.get("control_enabled", True)),
                "last_read_ok": datetime.fromtimestamp(st.last_read_ok, dt_util.UTC).isoformat() if st and st.last_read_ok else None,
                "online": bool(st and st.actual and st.actual.online),
                "link": self.link(did),
                "actual_mode": st.actual.mode.value if st and st.actual and st.actual.mode else None,
                "desired": ({"mode": st.desired.mode.value, "power_w": st.desired.power_w} if st and st.desired else None),
                "reason": st.reason if st else "", "decision": st.decision.value if st and st.decision else None,
                "last_command": st.last_command if st else None, "last_error": st.last_error if st else None,
                "unconfirmed": st.unconfirmed if st else 0, "release_pending": bool(st and st.release_pending),
                "word_order": getattr(getattr(a, "driver", None), "_order", None),
                "driver_error": getattr(getattr(a, "driver", None), "last_error", None),
                "recent_outages": getattr(getattr(a, "driver", None), "outages", None) or None,
                "pv_readings_ignored": getattr(getattr(a, "driver", None), "pv_ignored", None) or None,
                "command_traces": self.traces.recent(did, 10) or None,
                "recent_setting_changes": list(getattr(getattr(a, "driver", None), "changes", None) or []) or None,
                "raw_readings": {k: v for k, v in (getattr(getattr(a, "driver", None), "last_quota", None) or {}).items()
                                 if not _PRIVATE_KEY.search(k)} or None,
            }
        return out

    def devices_view(self, now: datetime | None = None) -> dict:
        """Per-battery part of the live view (also what the sensors' attributes are built from)."""
        now = now or dt_util.now()
        out = {}
        for a in self.adapters:
            did = a.config.device_id
            st = self._snap.devices.get(did) or DeviceStatus()
            # while reads have only failed for a short time (another Modbus client may hold the battery) the last reading is shown
            act, want, link = self.shown_actual(did) or st.actual, st.desired, self.link(did)
            stale = link == "stale" and act is not None and act.online
            active = [s.id for s in self.schedules if s.applies(did, now)
                      and (not s.condition_entity or self._condition_ok(s.condition_entity))]
            ov = self.overrides.get(did)
            out[did] = {
                "online": bool(act and act.online),
                "stale": stale,                 # shown as online with the last reading, but this cycle's read failed
                "link": link,
                "stale_reason": getattr(getattr(a, "driver", None), "last_error", None) if stale else None,
                "soc": act.soc if act else None,
                "power_w": act.power_w if act else None,
                "pv_w": act.pv_w if act and act.online else None, "has_solar": self._has_solar(a.config),
                "mode": act.mode.value if act and act.mode else None,
                "protected": bool(act and act.protected), "off_grid": bool(act and act.off_grid),
                "limits": list(act.limits) if act and act.limits else None,
                "temp_c": act.temp_c if act else None, "firmware": act.firmware if act else None,
                "alarms": list(act.alarms) if act else [],
                "desired_mode": want.mode.value if want else None,
                "desired_power_w": want.power_w if want else None,
                "reason": st.reason,
                "decision": st.decision.value if st.decision else None,
                "managed": want is not None,
                "control_enabled": bool(a.config.extra.get("control_enabled", True)),
                "active_schedules": active,
                "override": ({"mode": ov[0].mode.value, "power_w": ov[0].power_w, "until": ov[1].isoformat()} if ov else None),
                "guard_held": st.guard_held,
                "last_error": st.last_error,
                "awaiting_confirm": st.awaiting_confirm,
                "charge_verdict": self.traces.verdict(did) if not a.config.caps.power_control else None,
                "traces": self.traces.recent(did) if not a.config.caps.power_control else [],
                "report": (ecoflow_report(self.traces.results.get(did, []), self.plan_log.view(did, now)["match_pct"],
                                          self.rate_learner.rates.get(did), self.traces.verdict(did))
                           if not a.config.caps.power_control else None),
                "measured_charge_w": self.rate_learner.rates.get(did) if not a.config.caps.power_control else None,
                "last_read_ok": datetime.fromtimestamp(st.last_read_ok, dt_util.UTC).isoformat() if st.last_read_ok else None,
                "offline_hint": offline_hint(a.config.kind) if not (act and act.online) else None,
                "offline_reason": (getattr(getattr(a, "driver", None), "last_error", None) if not (act and act.online) else None),
                "why": explain_idle(control_enabled=bool(a.config.extra.get("control_enabled", True)), paused=self.paused, hint=offline_hint(a.config.kind),
                                    dry_run=self.settings.dry_run, online=bool(act and act.online), off_grid=bool(act and act.off_grid),
                                    protected=bool(act and act.protected), last_error=st.last_error, guard_held=st.guard_held,
                                    unconfirmed=st.unconfirmed, awaiting_confirm=st.awaiting_confirm),
                "setting_changes": list(getattr(getattr(a, "driver", None), "changes", None) or [])[-8:],
                "pv": self.pv_view(did),          # None for a battery that does not report its solar inputs one by one
            }
        return out

    def view(self) -> dict:
        """JSON-safe live view for the panel."""
        now = dt_util.now()
        day_key = now.strftime("%Y-%m-%d")
        out = self.devices_view(now)
        actuals = self.shown_actuals()
        day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
        rates = self._rates()
        return {
            "devices": out,
            "events": [list(e) for e in list(self._snap.events)[-60:]],
            "grid_w": self.last_grid, "grid_source": self.grid_source, "grid_age_s": None if self.grid_age_s is None else round(self.grid_age_s),
            "now": now.isoformat(),
            "paused": self.paused, "backup": self.backup_active, "away": self.away,
            "alerts": sorted(self.alerts.active()), "notes": self.last_result.notes,
            "price_now": self._price_now(rates, now),
            "meter_entity": self.grid_source, "rates_entity": self._rates_entity(),
            "combined": self._combined(actuals), "setup": self.setup_check(), "solar_room": self.solar_room, "export_better": self._export_better(rates, now),
            "export": self._export_view(rates, now), "grid_event": self.grid_event,
            "divert": {"entity": self.settings.divert_entity, "on": self.diverter.on} if self.settings.divert_entity else None,
            "daily": {"self_consumption": self.daily.self_consumption(day_key), "cheap_share": self.daily.cheap_share(day_key)},
            "plan_actual": {a.config.device_id: self.plan_log.view(a.config.device_id, now) for a in self.adapters},
            "health": {a.config.device_id: {"efficiency": self.health.efficiency(a.config.device_id)[-8:],
                                             "warning": self.health.degraded(a.config.device_id),
                                             "cycling": self._cycling(a.config.device_id, rates, now)} for a in self.adapters},
            "grid_events": self.grid_events.view(),
            "trend": {a.config.device_id: {"days": self.trend.series(a.config.device_id, 60), "warning": self.trend.warning(a.config.device_id)} for a in self.adapters},
            "forecast_reserve": self.forecast_floor, "readiness": self.readiness(), "startup": self.startup_note, "weather": self.weather,
            "learning": {"known_slots": sum(1 for n in self.profile.n["wd"] if n >= self.profile.MIN_VISITS)},
            "tariff_inferred": ({"start": self._tariff.start.strftime("%H:%M"), "end": self._tariff.end.strftime("%H:%M"),
                                 "offpeak": self._tariff.offpeak, "peak": self._tariff.peak, "days": round(self._tariff.days, 1)}
                                if self._tariff else None), "currency": self.settings.currency,
            "savings": {"today": round(self.ledger.saved(day), 2) + 0.0, "month": round(self.ledger.saved(month), 2) + 0.0},
            "energy": {k: round(v, 3) for k, v in self.meter.totals.items()},
        }
