"""Config flow: one click to add. All settings, batteries and schedules live in the HomePower panel."""
from __future__ import annotations

from homeassistant import config_entries

from homeassistant.core import callback

from ._schema import vol
from .const import DOMAIN
from .core.found import ECOFLOW_LOCAL_KIND, anker_kinds, discovered_name, match_known, new_device_id, norm_serial
from .core.serde import ValidationError, fill_from_probe
from .options_flow import HomePowerOptionsFlow, _dropdown, _num, _readings
from .runtime import running_manager


class HomePowerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return HomePowerOptionsFlow()

    def __init__(self) -> None:
        self._found: dict = {}          # the battery a discovery offers: vendor, host, port, serial, model (, x1)
        self._raw: dict = {}            # the battery being added
        self._info: dict = {}           # what it reported when probed

    def _home(self):
        """HomePower's own entry, if it is set up (an ignored discovery is not one)."""
        return next(iter(self._async_current_entries(include_ignore=False)), None)

    async def async_step_user(self, user_input=None):
        # One HomePower per Home Assistant. (Not the manifest's single_config_entry: that would also stop every discovered
        # battery being offered once HomePower is set up, since each one arrives as a config flow of its own.)
        if self._home() is not None:
            return self.async_abort(reason="single_instance_allowed")
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="HomePower", data={})
        return self.async_show_form(step_id="user")

    # ---- a battery found on the network: EcoFlow announces itself (zeroconf); Anker is found by HomePower's own scan
    async def async_step_zeroconf(self, discovery_info):
        props = discovery_info.properties or {}
        return await self._discovered({
            "vendor": "ecoflow", "host": str(discovery_info.host), "port": int(discovery_info.port or 502),
            "serial": norm_serial(props.get("serial")), "model": (str(props.get("model") or "").strip() or None)})

    async def async_step_integration_discovery(self, discovery_info):
        return await self._discovered(dict(discovery_info))

    async def _discovered(self, found: dict):
        vendor, host, port, serial = found["vendor"], found["host"], int(found.get("port") or 502), norm_serial(found.get("serial"))
        await self.async_set_unique_id(f"{vendor}_{serial}" if serial else f"{vendor}_{host}:{port}")
        self._abort_if_unique_id_configured()         # the user chose Ignore for this battery before
        home = self._home()
        if home is not None and not home.options.get("auto_discover", True):
            return self.async_abort(reason="discovery_off")
        mgr = running_manager(self.hass)
        if mgr is not None:
            did, update = match_known(mgr.devices, vendor, host, port, serial)
            if did is not None:
                if update is not None:        # the same battery at a new address (or now with its serial): put it right
                    try:
                        await mgr.save_device(update)
                    except ValidationError:
                        pass
                return self.async_abort(reason="battery_known")
        self._found = {**found, "port": port, "serial": serial}
        self.context["title_placeholders"] = {"name": discovered_name(vendor, found.get("model"), serial)}
        return await self.async_step_discovery_confirm()

    def _kind_field(self):
        if self._found["vendor"] != "anker":
            return {}
        kinds, start = anker_kinds(bool(self._found.get("x1")))
        key = vol.Required("kind", default=start) if start else vol.Required("kind")
        return {key: _dropdown(kinds)}

    async def async_step_discovery_confirm(self, user_input=None):
        found, errors, detail = self._found, {}, ""
        home, mgr = self._home(), running_manager(self.hass)
        if home is not None and mgr is None:
            return self.async_abort(reason="not_loaded")
        if user_input is not None:
            conn = {"host": found["host"], "port": found["port"], **({"serial": found["serial"]} if found.get("serial") else {})}
            self._raw = {"name": str(user_input["name"]).strip() or discovered_name(found["vendor"], found.get("model"), found.get("serial")),
                         "kind": user_input.get("kind") or ECOFLOW_LOCAL_KIND, "connection": conn,
                         "control_enabled": bool(user_input.get("control_enabled", False))}
            if mgr is None:
                # HomePower is not set up yet: set it up, with this battery added as soon as it runs
                await self.async_set_unique_id(DOMAIN, raise_on_progress=False)
                return self.async_create_entry(title="HomePower", data={"discovered": [self._raw]})
            try:
                self._info = await mgr.probe({"kind": self._raw["kind"], "connection": conn})
            except ValidationError as err:
                errors, detail = {"base": "cannot_connect_battery"}, str(err)
            else:
                return await self.async_step_discovery_details()
        fields = {vol.Required("name", default=discovered_name(found["vendor"], found.get("model"), found.get("serial"))): str,
                  **self._kind_field(), vol.Optional("control_enabled", default=False): bool}
        return self.async_show_form(
            step_id="discovery_confirm", errors=errors, data_schema=vol.Schema(fields),
            description_placeholders={"name": discovered_name(found["vendor"], found.get("model"), found.get("serial")),
                                      "host": f"{found['host']}:{found['port']}", "error": detail})

    async def async_step_discovery_details(self, user_input=None):
        """Only what the battery did not report itself (usually nothing)."""
        missing = [k for k in ("capacity_wh", "max_charge_w", "max_discharge_w") if not self._info.get(k)]
        if user_input is None and missing:
            selectors = {"capacity_wh": _num(100, 200000, "Wh", 10), "max_charge_w": _num(0, 50000, "W", 50),
                         "max_discharge_w": _num(0, 50000, "W", 50)}
            return self.async_show_form(step_id="discovery_details", data_schema=vol.Schema({vol.Required(k): selectors[k] for k in missing}),
                                        description_placeholders={"readings": _readings(self._info) or "connected"})
        raw = fill_from_probe({**self._raw, **{k: (user_input or {})[k] for k in missing if k in (user_input or {})}}, self._info)
        raw.setdefault("min_soc", 10)
        mgr = running_manager(self.hass)
        if mgr is None:
            return self.async_abort(reason="not_loaded")
        try:
            await mgr.save_device({**raw, "device_id": new_device_id(raw["name"], mgr.devices)}, new=True)
        except ValidationError as err:
            return self.async_abort(reason="invalid", description_placeholders={"error": str(err)})
        return self.async_abort(reason="battery_added", description_placeholders={"name": raw["name"]})

    # ---- EcoFlow refused the account keys: take new ones, for every EcoFlow battery on that account
    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        mgr = running_manager(self.hass)
        if mgr is None:
            return self.async_abort(reason="not_loaded")
        cloud = [d for d in mgr.devices.values() if d.get("driver") == "ecoflow_stream"]
        errors, detail = {}, ""
        if user_input is not None:
            keys = {k: str(user_input[k]).strip() for k in ("access_key", "secret_key")}
            # each region the batteries are in: the account whose keys these are may not be the first battery's
            mine, answered = [], False
            for region in dict.fromkeys((d.get("connection") or {}).get("region", "eu") for d in cloud) or ["eu"]:
                try:
                    found = {d["sn"] for d in await mgr.discover_ecoflow(region, keys["access_key"], keys["secret_key"])}
                except ValidationError as err:
                    detail = str(err)
                    continue
                answered = True
                mine += [d for d in cloud if (c := d.get("connection") or {}).get("sn") in found and c.get("region", "eu") == region]
            try:
                for dev in mine:
                    await mgr.save_device({**dev, "connection": {**dev["connection"], **keys}})
                if mine:
                    return self.async_abort(reason="reauth_successful")
                errors = {"base": "no_match" if answered else "cannot_connect"}
            except ValidationError as err:
                errors, detail = {"base": "cannot_connect"}, str(err)
        return self.async_show_form(
            step_id="reauth_confirm", errors=errors, description_placeholders={"error": detail},
            data_schema=vol.Schema({vol.Required("access_key"): str, vol.Required("secret_key"): str}))
