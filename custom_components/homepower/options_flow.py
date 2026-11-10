"""Native Home Assistant forms (Configure button) for batteries and schedules.

Batteries are connected to directly: Anker over Modbus TCP (found by scanning your network) and
EcoFlow through your EcoFlow developer keys (devices listed from your account). No other integration
is needed. New batteries start in monitor-only mode until you allow control.
"""
from __future__ import annotations

import re

from homeassistant import config_entries
from homeassistant.helpers import selector as sel

from ._schema import vol
from .const import DOMAIN
from .core.profiles import DRIVERS, PROFILE_BY_ID, PROFILES
from .core.serde import ValidationError, fill_from_probe
from .core.settings import DEFAULTS, SettingsError, clean

FLOW_SETTING_KEYS = ("import_limit_w", "peak_shaving", "export_limit_w", "grid_import_entity", "rates_entity", "price_peak",
                     "price_offpeak", "offpeak_start", "offpeak_end", "forecast_entity", "backup_reserve_pct", "storm_entity",
                     "away_entity", "notify_service")
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
    cfg = {"min": lo, "max": hi, "step": step, "mode": sel.NumberSelectorMode.BOX}
    if unit:  # the validator rejects unit_of_measurement=None
        cfg["unit_of_measurement"] = unit
    return sel.NumberSelector(sel.NumberSelectorConfig(**cfg))


def _dropdown(options, custom=False):
    return sel.SelectSelector(sel.SelectSelectorConfig(options=options, custom_value=custom, mode=sel.SelectSelectorMode.DROPDOWN))


def _readings(info: dict) -> str:
    if not info:
        return ""
    bits = []
    if info.get("model"):
        bits.append(str(info["model"]))
    if info.get("capacity_wh"):
        bits.append(f"{info['capacity_wh'] / 1000:.1f} kWh")
    if info.get("soc") is not None:
        bits.append(f"charge {info['soc']:.0f}%")
    if info.get("power_w") is not None:
        p = info["power_w"]
        bits.append("idle" if abs(p) < 20 else f"{'charging' if p > 0 else 'discharging'} {abs(p):.0f} W")
    if info.get("max_charge_w") or info.get("max_discharge_w"):
        bits.append(f"limits {info.get('max_charge_w') or '?'} W in / {info.get('max_discharge_w') or '?'} W out")
    return ", ".join(bits)


class HomePowerOptionsFlow(config_entries.OptionsFlow):
    def __init__(self) -> None:
        self._d: dict = {}
        self._info: dict = {}
        self._found: list[dict] | None = None
        self._eco_devices: list[dict] = []

    def _mgr(self):
        return self.hass.data.get(DOMAIN, {}).get("manager")

    def _done(self):
        return self.async_create_entry(data=dict(self.config_entry.options))

    def _driver(self) -> str:
        return PROFILE_BY_ID[self._d["kind"]]["driver"]

    async def async_step_init(self, user_input=None):
        mgr = self._mgr()
        if mgr is None:
            return self.async_abort(reason="not_loaded")
        menu = ["add_battery", "add_schedule", "add_goal", "settings"]
        if mgr.devices:
            menu += ["control_battery", "remove_battery"]
        if mgr.schedules:
            menu.append("remove_schedule")
        if mgr.goals:
            menu.append("remove_goal")
        return self.async_show_menu(step_id="init", menu_options=menu)

    # ---------------------------------------------------------------- add battery
    async def async_step_add_battery(self, user_input=None):
        if user_input is not None:
            self._d = {"name": user_input["name"].strip(), "kind": user_input["kind"]}
            driver = self._driver()
            if driver == "anker_modbus":
                return await self.async_step_anker_find()
            if driver == "ecoflow_stream":
                return await self.async_step_ecoflow_account()
            if driver == "modbus_generic":
                return await self.async_step_modbus_map()
            return await self.async_step_entity_details()
        options = [{"value": p["id"], "label": p["label"]} for p in PROFILES]
        return self.async_show_form(step_id="add_battery", data_schema=vol.Schema({
            vol.Required("name"): str,
            vol.Required("kind", default=PROFILES[0]["id"]): _dropdown(options),
        }))

    # ---- Anker: scan the network, then read the battery to prove it works
    async def async_step_anker_find(self, user_input=None):
        mgr = self._mgr()
        errors, detail = {}, ""
        if user_input is not None:
            conn = {"host": str(user_input["host"]).strip(), "port": int(user_input.get("port", 502))}
            try:
                self._info = await mgr.probe({"kind": self._d["kind"], "connection": conn})
                self._d["connection"] = conn
                return await self.async_step_battery_details()
            except ValidationError as err:
                errors, detail = {"base": "cannot_connect"}, str(err)
        if self._found is None:
            self._found = await mgr.discover_anker()
        options = [{"value": f["host"], "label": f"{f['host']} - {f.get('model') or 'Anker battery'} ({f['soc']}%)"} for f in self._found]
        host = _dropdown(options, custom=True) if options else str
        host_key = vol.Required("host", default=self._found[0]["host"]) if self._found else vol.Required("host")
        return self.async_show_form(
            step_id="anker_find", errors=errors,
            data_schema=vol.Schema({host_key: host,
                                    vol.Optional("port", default=502): _num(1, 65535)}),
            description_placeholders={"found": f"{len(self._found)} found on your network" if self._found else "none found automatically - enter its IP address",
                                      "error": detail})

    # ---- Generic Modbus device described by a register map
    async def async_step_modbus_map(self, user_input=None):
        errors, detail = {}, ""
        prof = PROFILE_BY_ID[self._d["kind"]]
        if user_input is not None:
            conn = {"host": user_input["host"].strip(), "port": int(user_input.get("port", 502)),
                    "unit_id": int(user_input.get("unit_id", prof.get("default_unit", 1)))}
            if user_input.get("map"):
                conn["map"] = user_input["map"]
            try:
                self._info = await self._mgr().probe({"kind": self._d["kind"], "connection": conn})
                self._d["connection"] = conn
                return await self.async_step_battery_details()
            except ValidationError as err:
                errors, detail = {"base": "cannot_connect"}, str(err)
        fields: dict = {vol.Required("host"): str, vol.Optional("port", default=502): _num(1, 65535),
                        vol.Optional("unit_id", default=int(prof.get("default_unit", 1))): _num(0, 255)}
        fields[vol.Optional("map") if prof.get("preset_map") else vol.Required("map")] = sel.TextSelector(sel.TextSelectorConfig(multiline=True))
        return self.async_show_form(step_id="modbus_map", errors=errors, data_schema=vol.Schema(fields),
                                    description_placeholders={"error": detail, "preset": "built in" if prof.get("preset_map") else "required"})

    # ---- EcoFlow: account keys -> list devices -> pick one
    async def async_step_ecoflow_account(self, user_input=None):
        errors, detail = {}, ""
        if user_input is not None:
            try:
                self._d["connection"] = {"region": user_input["region"], "access_key": user_input["access_key"].strip(),
                                         "secret_key": user_input["secret_key"].strip()}
                self._eco_devices = await self._mgr().discover_ecoflow(**self._d["connection"])
                if not self._eco_devices:
                    raise ValidationError("no devices on that EcoFlow account")
                return await self.async_step_ecoflow_device()
            except ValidationError as err:
                errors, detail = {"base": "cannot_connect"}, str(err)
        region = DRIVERS["ecoflow_stream"]["connection"][0]["options"]
        return self.async_show_form(
            step_id="ecoflow_account", errors=errors,
            data_schema=vol.Schema({vol.Required("region", default="eu"): _dropdown(region),
                                    vol.Required("access_key"): str, vol.Required("secret_key"): str}),
            description_placeholders={"error": detail})

    async def async_step_ecoflow_device(self, user_input=None):
        errors, detail = {}, ""
        if user_input is not None:
            self._d["connection"]["sn"] = user_input["sn"]
            try:
                self._info = await self._mgr().probe({"kind": self._d["kind"], "connection": self._d["connection"]})
                return await self.async_step_battery_details()
            except ValidationError as err:
                errors, detail = {"base": "cannot_connect"}, str(err)
        opts = [{"value": d["sn"], "label": f"{d['name']} ({d['sn']}){'' if d['online'] else ' - offline'}"} for d in self._eco_devices]
        return self.async_show_form(step_id="ecoflow_device", errors=errors,
                                    data_schema=vol.Schema({vol.Required("sn", default=opts[0]["value"]): _dropdown(opts)}),
                                    description_placeholders={"error": detail})

    # ---- direct batteries: everything the battery can report is read from it; ask only for the rest
    async def async_step_battery_details(self, user_input=None):
        sized = self._driver() == "anker_modbus" or (self._driver() == "modbus_generic" and self._info.get("control"))
        wanted = ("capacity_wh", "max_charge_w", "max_discharge_w") if sized else ("capacity_wh",)
        missing = [k for k in wanted if not self._info.get(k)]
        if user_input is not None:
            self._d.update({k: user_input[k] for k in missing if k in user_input})
            self._d["control_enabled"] = bool(user_input.get("control_enabled", False))
            self._d = fill_from_probe(self._d, self._info)
            if not sized:
                self._d.setdefault("max_charge_w", 0)
                self._d.setdefault("max_discharge_w", 0)
            self._d.setdefault("min_soc", 10)  # safe floor when the battery reports none
            return await self._save_battery()
        selectors = {"capacity_wh": _num(100, 200000, "Wh", 10), "max_charge_w": _num(0, 50000, "W", 50), "max_discharge_w": _num(0, 50000, "W", 50)}
        fields: dict = {vol.Required(k): selectors[k] for k in missing}
        fields[vol.Optional("control_enabled", default=False)] = bool
        return self.async_show_form(step_id="battery_details", data_schema=vol.Schema(fields),
                                    description_placeholders={"readings": _readings(self._info) or "connected"})

    # ---- advanced: any battery via Home Assistant entities
    async def async_step_entity_details(self, user_input=None):
        spec = DRIVERS["entities"]
        if user_input is not None:
            self._d.update({k: user_input[k] for k in ("capacity_wh", "max_charge_w", "max_discharge_w", "min_soc") if k in user_input})
            self._d["invert_power"] = bool(user_input.get("invert_power", False))
            self._d["entities"] = {f["k"]: user_input[f"e_{f['k']}"] for f in spec["entities"] if user_input.get(f"e_{f['k']}")}
            return await self.async_step_battery_modes()
        fields: dict = {
            vol.Required("capacity_wh"): _num(100, 200000, "Wh", 10),
            vol.Required("max_charge_w"): _num(0, 50000, "W", 50),
            vol.Required("max_discharge_w"): _num(0, 50000, "W", 50),
            vol.Required("min_soc", default=10): _num(0, 100, "%"),
        }
        for f in spec["entities"]:
            marker = vol.Required if f["req"] else vol.Optional
            fields[marker(f"e_{f['k']}")] = sel.EntitySelector(sel.EntitySelectorConfig(domain=f["domains"]))
        fields[vol.Optional("invert_power", default=False)] = bool
        return self.async_show_form(step_id="entity_details", data_schema=vol.Schema(fields))

    async def async_step_battery_modes(self, user_input=None):
        spec = DRIVERS["entities"]
        if user_input is not None:
            self._d["mode_map"] = {m["k"]: user_input[f"mm_{m['k']}"] for m in spec["mode_map"] if user_input.get(f"mm_{m['k']}")}
            self._d["control_enabled"] = True
            return await self._save_battery()
        fields: dict = {}
        for m in spec["mode_map"]:
            st = self.hass.states.get(self._d["entities"].get(m["entity"], ""))
            opts = list((st.attributes.get("options") if st else None) or [])
            guess = next((o for o in opts if m["rx"] and re.search(m["rx"], o, re.I)), None)
            marker = vol.Optional if m["optional"] else vol.Required
            key = marker(f"mm_{m['k']}", default=guess) if guess else marker(f"mm_{m['k']}")
            fields[key] = _dropdown(opts, custom=True) if opts else str
        return self.async_show_form(step_id="battery_modes", data_schema=vol.Schema(fields))

    async def _save_battery(self):
        mgr = self._mgr()
        base = _slug(self._d["name"])
        did, n = base, 2
        while did in mgr.devices:   # two batteries with the same name must not overwrite each other
            did, n = f"{base}_{n}", n + 1
        raw = {**self._d, "device_id": did}
        try:
            await mgr.save_device(raw)
        except ValidationError as err:
            return self.async_abort(reason="invalid", description_placeholders={"error": str(err)})
        return self._done()

    async def async_step_control_battery(self, user_input=None):
        mgr = self._mgr()
        if user_input is not None:
            dev = mgr.devices[user_input["device"]]
            await mgr.save_device({**dev, "control_enabled": bool(user_input["enabled"])})
            return self._done()
        opts = [{"value": d["device_id"], "label": f"{d['name']} - {'control ON' if d.get('control_enabled', True) else 'monitor only'}"} for d in mgr.devices.values()]
        return self.async_show_form(step_id="control_battery", data_schema=vol.Schema({
            vol.Required("device", default=opts[0]["value"]): _dropdown(opts),
            vol.Required("enabled", default=True): bool,
        }))

    # ---------------------------------------------------------------- schedules
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
            if raw["id"] in mgr.schedules:
                return self.async_abort(reason="invalid", description_placeholders={"error": f"a schedule called '{raw['id']}' already exists"})
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

    # ---------------------------------------------------------------- goals
    async def async_step_add_goal(self, user_input=None):
        mgr = self._mgr()
        if user_input is not None:
            raw = {"id": user_input["name"].strip(), "device_ids": user_input.get("devices", []),
                   "target_soc": user_input["target_soc"], "by": str(user_input["by"])[:5],
                   "days": [DAYS.index(d) for d in user_input.get("days", DAYS)],
                   "use_forecast": bool(user_input.get("use_forecast", False))}
            if raw["id"] in mgr.goals:
                return self.async_abort(reason="invalid", description_placeholders={"error": f"a goal called '{raw['id']}' already exists"})
            try:
                await mgr.save_goal(raw)
            except ValidationError as err:
                return self.async_abort(reason="invalid", description_placeholders={"error": str(err)})
            return self._done()
        devices = [{"value": d["device_id"], "label": d["name"]} for d in mgr.devices.values()]
        return self.async_show_form(step_id="add_goal", data_schema=vol.Schema({
            vol.Required("name"): str,
            vol.Optional("devices", default=[]): sel.SelectSelector(sel.SelectSelectorConfig(options=devices, multiple=True, mode=sel.SelectSelectorMode.DROPDOWN)),
            vol.Required("target_soc", default=90): _num(1, 100, "%"),
            vol.Required("by", default="07:00:00"): sel.TimeSelector(),
            vol.Optional("days", default=DAYS): sel.SelectSelector(sel.SelectSelectorConfig(options=DAYS, multiple=True, translation_key="days")),
            vol.Optional("use_forecast", default=False): bool,
        }))

    async def async_step_remove_goal(self, user_input=None):
        mgr = self._mgr()
        if user_input is not None:
            await mgr.delete_goal(user_input["goal"])
            return self._done()
        opts = [{"value": g["id"], "label": g["id"]} for g in mgr.goals.values()]
        return self.async_show_form(step_id="remove_goal", data_schema=vol.Schema({vol.Required("goal"): _dropdown(opts)}))

    # ---------------------------------------------------------------- settings (the common ones; the panel has all of them)
    async def async_step_settings(self, user_input=None):
        cur = {**{k: v for k, v in DEFAULTS.items()}, **self.config_entry.options}
        if user_input is not None:
            try:
                # Home Assistant leaves a cleared field out of the submission: that means "clear it", not "keep it".
                full = {k: user_input.get(k, False if isinstance(DEFAULTS[k], bool) else "") for k in FLOW_SETTING_KEYS}
                new = clean({k: ("" if v is None else v) for k, v in full.items()})
                for k in ("offpeak_start", "offpeak_end"):
                    if new.get(k):
                        new[k] = new[k][:5]
            except SettingsError as err:
                return self.async_abort(reason="invalid", description_placeholders={"error": str(err)})
            return self.async_create_entry(data={**self.config_entry.options, **new})

        def ent(domains):
            return sel.EntitySelector(sel.EntitySelectorConfig(domain=domains))

        def opt(key, selector, default=None):
            d = cur.get(key)
            return vol.Optional(key, description={"suggested_value": d if d not in (None, "") else default})

        return self.async_show_form(step_id="settings", data_schema=vol.Schema({
            opt("import_limit_w", _num(0, 100000, "W", 100)): _num(0, 100000, "W", 100),
            opt("peak_shaving", bool): bool,
            opt("export_limit_w", _num(0, 100000, "W", 100)): _num(0, 100000, "W", 100),
            opt("grid_import_entity", ent(["sensor"])): ent(["sensor"]),
            opt("rates_entity", ent(["sensor", "event"])): ent(["sensor", "event"]),
            opt("price_peak", _num(0, 1000, None, 0.01)): _num(0, 1000, None, 0.01),
            opt("price_offpeak", _num(0, 1000, None, 0.01)): _num(0, 1000, None, 0.01),
            opt("offpeak_start", sel.TimeSelector()): sel.TimeSelector(),
            opt("offpeak_end", sel.TimeSelector()): sel.TimeSelector(),
            opt("forecast_entity", ent(["sensor"])): ent(["sensor"]),
            opt("backup_reserve_pct", _num(0, 100, "%")): _num(0, 100, "%"),
            opt("storm_entity", ent(["binary_sensor", "input_boolean", "switch"])): ent(["binary_sensor", "input_boolean", "switch"]),
            opt("away_entity", ent(["binary_sensor", "input_boolean", "person", "device_tracker"])): ent(["binary_sensor", "input_boolean", "person", "device_tracker"]),
            opt("notify_service", str): str,
        }))

    # ---------------------------------------------------------------- remove
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
