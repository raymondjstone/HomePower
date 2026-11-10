"""Config flow: one click to add. All settings, batteries and schedules live in the HomePower panel."""
from __future__ import annotations

from homeassistant import config_entries

from homeassistant.core import callback

from .const import DOMAIN
from .options_flow import HomePowerOptionsFlow


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
