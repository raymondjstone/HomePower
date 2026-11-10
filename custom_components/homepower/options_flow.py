"""Native Home Assistant forms (Configure button) for batteries and schedules.

A second, panel-free way to manage HomePower, so setup never depends on the custom sidebar panel.
"""
from __future__ import annotations

import re

from homeassistant import config_entries
from homeassistant.helpers import selector as sel

from ._schema import vol
from .const import DOMAIN
from .core.profiles import PROFILE_BY_ID, PROFILES, STRATEGIES
from .core.serde import ValidationError

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MODE_OPTIONS = [
    {"value": "auto", "label": "Auto (hand back to the battery)"},
    {"value": "charge", "label": "Charge"},
    {"value": "discharge", "label": "Discharge"},
    {"value": "hold", "label": "Hold (no charge or discharge)"},
]


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "battery"


def _num(lo, hi, unit=None, step=1):
    return sel.NumberSelector(sel.NumberSelectorConfig(min=lo, max=hi, step=step, unit_of_measurement=unit, mode=sel.NumberSelectorMode.BOX))


def _dropdown(options):
    return sel.SelectSelector(sel.SelectSelectorConfig(options=options, mode=sel.SelectSelectorMode.DROPDOWN))


class HomePowerOptionsFlow(config_entries.OptionsFlow):
    def __init__(self) -> None:
        self._d: dict = {}

    def _mgr(self):
        return self.hass.data.get(DOMAIN, {}).get("manager")

    def _done(self):
        return self.async_create_entry(data=dict(self.config_entry.options))

    async def async_step_init(self, user_input=None):
        mgr = self._mgr()
        if mgr is None:
            return self.async_abort(reason="not_loaded")
        menu = ["add_battery", "add_schedule"]
        if mgr.devices:
            menu.append("remove_battery")
        if mgr.schedules:
            menu.append("remove_schedule")
        return self.async_show_menu(step_id="init", menu_options=menu)

    # ---- add battery ----
    async def async_step_add_battery(self, user_input=None):
        if user_input is not None:
            self._d = {"name": user_input["name"].strip(), "kind": user_input["kind"]}
            return await self.async_step_battery_details()
        options = [{"value": p["id"], "label": p["label"]} for p in PROFILES]
        return self.async_show_form(step_id="add_battery", data_schema=vol.Schema({
            vol.Required("name"): str,
            vol.Required("kind", default=PROFILES[0]["id"]): _dropdown(options),
        }))

    def _spec(self):
        return STRATEGIES[PROFILE_BY_ID[self._d["kind"]]["strategy"]]

    async def async_step_battery_details(self, user_input=None):
        spec = self._spec()
        if user_input is not None:
            self._d.update({k: user_input[k] for k in ("capacity_wh", "max_charge_w", "max_discharge_w", "min_soc") if k in user_input})
            self._d["invert_power"] = bool(user_input.get("invert_power", False))
            self._d["entities"] = {f["k"]: user_input[f"e_{f['k']}"] for f in spec["entities"] if user_input.get(f"e_{f['k']}")}
            if any(m["entity"] in self._d["entities"] for m in spec["mode_map"]):
                return await self.async_step_battery_modes()
            return await self._save_battery()
        fields: dict = {
            vol.Required("capacity_wh"): _num(100, 200000, "Wh", 10),
            vol.Required("max_charge_w"): _num(0, 50000, "W", 50),
            vol.Required("max_discharge_w"): _num(0, 50000, "W", 50),
            vol.Required("min_soc", default=10): _num(0, 100, "%"),
        }
        for f in spec["entities"]:
            marker = vol.Required if f["req"] else vol.Optional
            fields[marker(f"e_{f['k']}")] = sel.EntitySelector(sel.EntitySelectorConfig(domain=f["domains"]))
        if any(f["k"] == "power" for f in spec["entities"]):
            fields[vol.Optional("invert_power", default=bool(spec.get("defaults", {}).get("invert_power", False)))] = bool
        return self.async_show_form(step_id="battery_details", data_schema=vol.Schema(fields),
                                    description_placeholders={"model": PROFILE_BY_ID[self._d["kind"]]["label"]})

    async def async_step_battery_modes(self, user_input=None):
        spec = self._spec()
        if user_input is not None:
            self._d["mode_map"] = {m["k"]: user_input[f"mm_{m['k']}"] for m in spec["mode_map"] if user_input.get(f"mm_{m['k']}")}
            return await self._save_battery()
        fields: dict = {}
        for m in spec["mode_map"]:
            eid = self._d["entities"].get(m["entity"])
            if not eid:
                continue
            st = self.hass.states.get(eid)
            opts = list((st.attributes.get("options") if st else None) or [])
            guess = next((o for o in opts if m["rx"] and re.search(m["rx"], o, re.I)), None)
            marker = vol.Optional if m["optional"] else vol.Required
            key = marker(f"mm_{m['k']}", default=guess) if guess else marker(f"mm_{m['k']}")
            fields[key] = sel.SelectSelector(sel.SelectSelectorConfig(options=opts, custom_value=True, mode=sel.SelectSelectorMode.DROPDOWN)) if opts else str
        return self.async_show_form(step_id="battery_modes", data_schema=vol.Schema(fields))

    async def _save_battery(self):
        raw = {**self._d, "device_id": _slug(self._d["name"])}
        try:
            await self._mgr().save_device(raw)
        except ValidationError as err:
            return self.async_abort(reason="invalid", description_placeholders={"error": str(err)})
        return self._done()

    # ---- add schedule ----
    async def async_step_add_schedule(self, user_input=None):
        mgr = self._mgr()
        if user_input is not None:
            raw = {
                "id": user_input["name"].strip(), "device_ids": user_input.get("devices", []),
                "mode": user_input["mode"], "power_w": user_input.get("power_w"), "target_soc": user_input.get("target_soc"),
                "start": str(user_input["start"])[:5], "end": str(user_input["end"])[:5],
                "days": [DAYS.index(d) for d in user_input.get("days", DAYS)],
                "condition_entity": user_input.get("condition_entity"), "all_day": bool(user_input.get("all_day", False)),
            }
            try:
                await mgr.save_schedule(raw)
            except ValidationError as err:
                return self.async_abort(reason="invalid", description_placeholders={"error": str(err)})
            return self._done()
        devices = [{"value": d["device_id"], "label": d["name"]} for d in mgr.devices.values()]
        fields: dict = {
            vol.Required("name"): str,
            vol.Optional("devices", default=[]): sel.SelectSelector(sel.SelectSelectorConfig(options=devices, multiple=True, mode=sel.SelectSelectorMode.DROPDOWN)),
            vol.Required("mode", default="charge"): _dropdown(MODE_OPTIONS),
            vol.Required("start", default="23:30:00"): sel.TimeSelector(),
            vol.Required("end", default="07:30:00"): sel.TimeSelector(),
            vol.Optional("power_w", default=3000): _num(0, 50000, "W", 100),
            vol.Optional("target_soc"): _num(0, 100, "%"),
            vol.Optional("days", default=DAYS): sel.SelectSelector(sel.SelectSelectorConfig(options=DAYS, multiple=True, translation_key="days")),
            vol.Optional("condition_entity"): sel.EntitySelector(sel.EntitySelectorConfig(domain=["binary_sensor", "input_boolean", "switch"])),
            vol.Optional("all_day", default=False): bool,
        }
        return self.async_show_form(step_id="add_schedule", data_schema=vol.Schema(fields))

    # ---- remove ----
    async def async_step_remove_battery(self, user_input=None):
        mgr = self._mgr()
        if user_input is not None:
            await mgr.delete_device(user_input["device"])
            return self._done()
        opts = [{"value": d["device_id"], "label": d["name"]} for d in mgr.devices.values()]
        return self.async_show_form(step_id="remove_battery", data_schema=vol.Schema({vol.Required("device"): _dropdown(opts)}))

    async def async_step_remove_schedule(self, user_input=None):
        mgr = self._mgr()
        if user_input is not None:
            await mgr.delete_schedule(user_input["schedule"])
            return self._done()
        opts = [{"value": s["id"], "label": s["id"]} for s in mgr.schedules.values()]
        return self.async_show_form(step_id="remove_schedule", data_schema=vol.Schema({vol.Required("schedule"): _dropdown(opts)}))
