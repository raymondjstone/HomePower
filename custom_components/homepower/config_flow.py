"""Config flow: a single instance with global safety settings."""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries

from .const import (
    CONF_COOLDOWN_S,
    CONF_IMPORT_LIMIT_W,
    DEFAULT_COOLDOWN_S,
    DEFAULT_IMPORT_LIMIT_W,
    DOMAIN,
)


class HomePowerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="HomePower", data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_IMPORT_LIMIT_W, default=DEFAULT_IMPORT_LIMIT_W): int,
                    vol.Required(CONF_COOLDOWN_S, default=DEFAULT_COOLDOWN_S): int,
                }
            ),
        )
