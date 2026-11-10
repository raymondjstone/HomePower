"""Config + options flow: global safety limits, grid sensor, zero-import battery."""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CHARGE_CAP_W,
    CONF_COOLDOWN_S,
    CONF_DISCHARGE_CAP_W,
    CONF_GRID_ENTITY,
    CONF_GRID_INVERT,
    CONF_IMPORT_LIMIT_W,
    CONF_ZERO_IMPORT_DEVICE,
    DEFAULT_COOLDOWN_S,
    DEFAULT_IMPORT_LIMIT_W,
    DOMAIN,
)

_W = selector.NumberSelector(
    selector.NumberSelectorConfig(min=0, max=50000, step=100, unit_of_measurement="W")
)


def _schema(d: dict, device_ids: list[str] | None = None) -> vol.Schema:
    fields: dict = {
        vol.Required(CONF_IMPORT_LIMIT_W, default=d.get(CONF_IMPORT_LIMIT_W, DEFAULT_IMPORT_LIMIT_W)): int,
        vol.Required(CONF_COOLDOWN_S, default=d.get(CONF_COOLDOWN_S, DEFAULT_COOLDOWN_S)): int,
        # 0 / empty = no cap. Applies to the SUM across all batteries and all schedules.
        vol.Optional(CONF_CHARGE_CAP_W, default=d.get(CONF_CHARGE_CAP_W, 0)): _W,
        vol.Optional(CONF_DISCHARGE_CAP_W, default=d.get(CONF_DISCHARGE_CAP_W, 0)): _W,
        vol.Optional(CONF_GRID_ENTITY, description={"suggested_value": d.get(CONF_GRID_ENTITY)}):
            selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="power")),
        vol.Optional(CONF_GRID_INVERT, default=d.get(CONF_GRID_INVERT, False)): bool,
    }
    if device_ids:
        fields[vol.Optional(CONF_ZERO_IMPORT_DEVICE, description={"suggested_value": d.get(CONF_ZERO_IMPORT_DEVICE)})] = (
            selector.SelectSelector(selector.SelectSelectorConfig(options=device_ids, mode="dropdown"))
        )
    return vol.Schema(fields)


class HomePowerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(entry):
        return HomePowerOptionsFlow()

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="HomePower", data=user_input)
        return self.async_show_form(step_id="user", data_schema=_schema({}))


class HomePowerOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        coord = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        ids = [a.config.device_id for a in coord.adapters] if coord else []
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(current, ids))
