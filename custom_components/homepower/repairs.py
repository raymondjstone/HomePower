"""Fixes offered from Settings > Repairs for HomePower's alerts that have an obvious remedy."""
from __future__ import annotations

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant

from .runtime import running_manager

from .const import DOMAIN


class BatteryFix(RepairsFlow):
    """A battery not following commands, or told to charge and not charging: test it, or stop sending it commands."""

    def __init__(self, device_id: str, message: str, title: str = "") -> None:
        self._did, self._msg, self._title = device_id, message, title or "battery"

    def _mgr(self):
        return running_manager(self.hass)

    async def async_step_init(self, user_input=None) -> data_entry_flow.FlowResult:
        return self.async_show_menu(step_id="init", menu_options=["test", "monitor"],
                                    description_placeholders={"message": self._msg, "title": self._title})

    async def async_step_test(self, user_input=None) -> data_entry_flow.FlowResult:
        mgr = self._mgr()
        if mgr is None or mgr.coordinator is None:
            return self.async_abort(reason="not_ready")
        res = await mgr.coordinator.test_control(self._did)
        steps = "; ".join(f"{s['name']}: {'ok' if s['ok'] else 'failed'}" + (f" ({s['detail']})" if s.get("detail") and not s["ok"] else "")
                          for s in res.get("steps", []))
        if res.get("ok"):
            mgr.coordinator.repair_done(self._did, tested_ok=True)     # Home Assistant drops the issue: raise it again if it carries on
            return self.async_create_entry(data={})
        return self.async_abort(reason="test_failed", description_placeholders={"steps": steps or "no steps ran"})

    async def async_step_monitor(self, user_input=None) -> data_entry_flow.FlowResult:
        mgr = self._mgr()
        dev = mgr.devices.get(self._did) if mgr else None
        if dev is None:
            return self.async_abort(reason="not_ready")
        await mgr.save_device({**dev, "control_enabled": False})       # hands it back to its own control and sends nothing more
        if mgr.coordinator is not None:
            mgr.coordinator.repair_done(self._did)
        return self.async_create_entry(data={})


class TariffFix(RepairsFlow):
    """The cheap hours typed into Settings no longer match the price history: use the history's hours instead."""

    def __init__(self, message: str) -> None:
        self._msg = message

    async def async_step_init(self, user_input=None) -> data_entry_flow.FlowResult:
        if user_input is not None:
            entries = self.hass.config_entries.async_entries(DOMAIN)
            if not entries:
                return self.async_abort(reason="not_ready")
            e = entries[0]
            start = end = ""
            if {**e.data, **e.options}.get("offpeak_extra"):
                # With more cheap periods listed, an empty main period would be filled by the first of those, not from the
                # history: so the history's hours are typed in instead, and the other periods stay.
                mgr = running_manager(self.hass)
                found = mgr.coordinator.history_window() if mgr is not None and mgr.coordinator is not None else None
                if found is None:
                    return self.async_abort(reason="not_ready")
                start, end = (t.strftime("%H:%M") for t in found)
            self.hass.config_entries.async_update_entry(e, options={**e.options, "offpeak_start": start, "offpeak_end": end, "offpeak_auto": True})
            return self.async_create_entry(data={})
        return self.async_show_form(step_id="init", description_placeholders={"message": self._msg})


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict | None) -> RepairsFlow:
    data = data or {}
    if data.get("fix") == "battery" and data.get("device_id"):
        return BatteryFix(str(data["device_id"]), str(data.get("message", "")), str(data.get("title", "")))
    return TariffFix(str(data.get("message", "")))
