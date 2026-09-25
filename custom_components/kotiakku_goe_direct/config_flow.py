from __future__ import annotations

import re

import voluptuous as vol
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    TextSelector,
)

from homeassistant import config_entries

from .const import (
    CONF_CHARGER_SERIALS,
    DOMAIN,
    NAME,
    OPTIONAL_ENTITIES,
    REQUIRED_ENTITIES,
    SERIAL_PATTERN,
)


def _schema() -> vol.Schema:
    sensor = EntitySelector(EntitySelectorConfig(domain="sensor"))
    fields = {vol.Required(key): sensor for key in REQUIRED_ENTITIES}
    fields.update({vol.Optional(key): sensor for key in OPTIONAL_ENTITIES})
    fields[vol.Required(CONF_CHARGER_SERIALS[0])] = TextSelector()
    fields.update({vol.Optional(key): TextSelector() for key in CONF_CHARGER_SERIALS[1:]})
    return vol.Schema(fields)


def _validate(user_input: dict) -> tuple[dict, dict]:
    data = {k: v for k, v in user_input.items() if k not in CONF_CHARGER_SERIALS and v}
    serials = [str(user_input.get(key) or "").strip() for key in CONF_CHARGER_SERIALS]
    serials = [s for s in serials if s]
    if not all(re.fullmatch(SERIAL_PATTERN, s) for s in serials) or not serials:
        return data, {"base": "invalid_serial"}
    if len(set(serials)) != len(serials):
        return data, {"base": "duplicate_serial"}
    data.update(zip(CONF_CHARGER_SERIALS, serials))
    return data, {}


class KotiakkuGoeDirectConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return KotiakkuGoeDirectOptionsFlow()

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors = {}
        if user_input is not None:
            data, errors = _validate(user_input)
            if not errors:
                return self.async_create_entry(title=NAME, data=data)
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(_schema(), user_input or {}),
            errors=errors,
        )


class KotiakkuGoeDirectOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            data, errors = _validate(user_input)
            if not errors:
                self.hass.config_entries.async_update_entry(self.config_entry, data=data)
                return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(_schema(), user_input or dict(self.config_entry.data)),
            errors=errors,
        )
