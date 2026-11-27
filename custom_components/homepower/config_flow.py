"""Config flow: one click to add. All settings, batteries and schedules live in the HomePower panel."""
from __future__ import annotations

from homeassistant import config_entries

from homeassistant.core import callback

from ._schema import vol
from .const import DOMAIN
from .core.serde import ValidationError
from .options_flow import HomePowerOptionsFlow
from .runtime import running_manager


class HomePowerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return HomePowerOptionsFlow()

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="HomePower", data={})
        return self.async_show_form(step_id="user")

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
            region = (cloud[0].get("connection") or {}).get("region", "eu") if cloud else "eu"
            try:
                found = {d["sn"] for d in await mgr.discover_ecoflow(region, keys["access_key"], keys["secret_key"])}
                mine = [d for d in cloud if (d.get("connection") or {}).get("sn") in found]
                if not mine:
                    errors = {"base": "no_match"}
                else:
                    for dev in mine:
                        await mgr.save_device({**dev, "connection": {**dev["connection"], **keys}})
                    return self.async_abort(reason="reauth_successful")
            except ValidationError as err:
                errors, detail = {"base": "cannot_connect"}, str(err)
        return self.async_show_form(
            step_id="reauth_confirm", errors=errors, description_placeholders={"error": detail},
            data_schema=vol.Schema({vol.Required("access_key"): str, vol.Required("secret_key"): str}))
