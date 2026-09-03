from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.helpers import selector

from .config import (
    CHARGER_SLOTS,
    apply_serial_guesses,
    chargers_from_form,
    charger_entities,
    clamp_priority,
    default_config,
    entry_config,
    form_from_chargers,
    persistable,
    validate_charger_rows,
)
from .const import (
    CONF_CHARGERS,
    CONF_CONTROLLER_ENTITY,
    CONF_CONTROLLER_IN_KW,
    CONF_HOUSE_ENTITY,
    CONF_KOTIAKKU_IN_KW,
    CONF_PRICE_ENTITY,
    CONF_PRIORITY,
    CONF_SOC_ENTITY,
    CONF_SOLAR_ENTITY,
    CONF_SOLAR_REMAINING_ENTITY,
    CONF_SOLAR_TOMORROW_ENTITY,
    DOMAIN,
    PRIORITY_MAX,
    PRIORITY_MIN,
    default_charger_priority,
)
from .hass_hints import collect_serial_hints
from .serial import guess_serial, serial_suggestion

_HOUSE_KEYS = (
    CONF_CONTROLLER_ENTITY,
    CONF_CONTROLLER_IN_KW,
    CONF_SOC_ENTITY,
    CONF_SOLAR_ENTITY,
    CONF_HOUSE_ENTITY,
    CONF_KOTIAKKU_IN_KW,
    CONF_SOLAR_REMAINING_ENTITY,
    CONF_SOLAR_TOMORROW_ENTITY,
)
_HOUSE_OPTIONAL = (CONF_SOLAR_REMAINING_ENTITY, CONF_SOLAR_TOMORROW_ENTITY)


def _entity(domain=None):
    if domain:
        return selector.EntitySelector(selector.EntitySelectorConfig(domain=domain))
    return selector.EntitySelector()


def _text():
    return selector.TextSelector()


def _bool():
    return selector.BooleanSelector()


def _priority():
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=PRIORITY_MIN,
            max=PRIORITY_MAX,
            step=1,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _guess_entries(hass, entities) -> dict[str, tuple[str, str]]:
    out = {}
    if hass is None:
        return out
    for entity in entities:
        if not entity:
            continue
        serial, source = guess_serial(**collect_serial_hints(hass, entity))
        if serial:
            out[entity] = (serial, source)
    return out


def _guess_map(hass, entities) -> dict[str, str]:
    return {entity: serial for entity, (serial, _source) in _guess_entries(hass, entities).items()}


def _suggested(values: dict) -> dict:
    return {key: value for key, value in values.items() if value not in (None, "")}


def _suggested_rows(hass, entities, previous_rows):
    guesses = _guess_entries(hass, entities)
    rows = []
    notes = []
    previous_rows = list(previous_rows or ())
    for index, entity in enumerate(entities):
        guessed, source = guesses.get(entity) or ("", "")
        serial, how = serial_suggestion(entity, previous_rows, guessed, source)
        previous = previous_rows[index] if index < len(previous_rows) else {}
        rows.append(
            {
                "entity": entity,
                "serial": serial,
                CONF_PRIORITY: clamp_priority(
                    previous.get(CONF_PRIORITY), default_charger_priority(index)
                ),
            }
        )
        if serial:
            notes.append(f"{entity} → {serial} ({how})")
        else:
            notes.append(f"{entity} → (enter the MQTT serial)")
    hint = "; ".join(notes) if notes else "Enter each charger's MQTT serial."
    return rows, hint


def _charger_entity_schema():
    fields = {vol.Required("charger_1_entity"): _entity()}
    for index in range(2, CHARGER_SLOTS + 1):
        fields[vol.Optional(f"charger_{index}_entity")] = _entity()
    return vol.Schema(fields)


def _charger_serial_schema(rows):
    fields = {}
    for index, _row in enumerate(rows, 1):
        fields[vol.Required(f"charger_{index}_serial")] = _text()
        fields[vol.Required(f"charger_{index}_priority")] = _priority()
    return vol.Schema(fields)


def _house_fields():
    return {
        vol.Required(CONF_CONTROLLER_ENTITY): _entity("sensor"),
        vol.Required(CONF_CONTROLLER_IN_KW): _bool(),
        vol.Required(CONF_SOC_ENTITY): _entity("sensor"),
        vol.Required(CONF_SOLAR_ENTITY): _entity("sensor"),
        vol.Required(CONF_HOUSE_ENTITY): _entity("sensor"),
        vol.Required(CONF_KOTIAKKU_IN_KW): _bool(),
        vol.Optional(CONF_SOLAR_REMAINING_ENTITY): _entity("sensor"),
        vol.Optional(CONF_SOLAR_TOMORROW_ENTITY): _entity("sensor"),
    }


def _house_schema():
    return vol.Schema(_house_fields())


def _options_schema():
    fields = {vol.Optional(CONF_PRICE_ENTITY): _entity("sensor")}
    fields[vol.Required("charger_1_entity")] = _entity()
    fields[vol.Required("charger_1_serial")] = _text()
    fields[vol.Required("charger_1_priority")] = _priority()
    for index in range(2, CHARGER_SLOTS + 1):
        fields[vol.Optional(f"charger_{index}_entity")] = _entity()
        fields[vol.Optional(f"charger_{index}_serial")] = _text()
        fields[vol.Optional(f"charger_{index}_priority")] = _priority()
    fields.update(_house_fields())
    return vol.Schema(fields)


def _house_values(cfg):
    out = {}
    for key in _HOUSE_KEYS:
        value = cfg.get(key)
        if key in _HOUSE_OPTIONAL:
            out[key] = value or ""
        else:
            out[key] = value
    return out


class KotiakkuGoeDirectConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self._data = default_config()
        self._charger_rows = list(self._data["chargers"])

    @staticmethod
    def async_get_options_flow(config_entry):
        return KotiakkuGoeDirectOptionsFlow()

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        if user_input is not None:
            self._data[CONF_PRICE_ENTITY] = user_input[CONF_PRICE_ENTITY]
            return await self.async_step_chargers()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema({vol.Required(CONF_PRICE_ENTITY): _entity("sensor")}),
                _suggested(
                    {CONF_PRICE_ENTITY: self._data.get(CONF_PRICE_ENTITY) or ""}
                ),
            ),
        )

    async def async_step_chargers(self, user_input=None):
        errors = {}
        suggested = _suggested(form_from_chargers(self._charger_rows))
        if user_input is not None:
            charger_1 = str(user_input.get("charger_1_entity") or "").strip()
            if not charger_1:
                errors["base"] = "charger_required"
                suggested = _suggested(user_input)
            else:
                entities = [charger_1]
                for index in range(2, CHARGER_SLOTS + 1):
                    entity = str(user_input.get(f"charger_{index}_entity") or "").strip()
                    if entity:
                        entities.append(entity)
                self._charger_rows, self._serial_hint = _suggested_rows(
                    self.hass, entities, self._charger_rows
                )
                return await self.async_step_serials()
        return self.async_show_form(
            step_id="chargers",
            data_schema=self.add_suggested_values_to_schema(
                _charger_entity_schema(), suggested
            ),
            errors=errors,
        )

    async def async_step_serials(self, user_input=None):
        errors = {}
        suggested = _suggested(form_from_chargers(self._charger_rows))
        if user_input is not None:
            form = {}
            for index, row in enumerate(self._charger_rows, 1):
                form[f"charger_{index}_entity"] = row["entity"]
                form[f"charger_{index}_serial"] = user_input.get(
                    f"charger_{index}_serial"
                )
                form[f"charger_{index}_priority"] = user_input.get(
                    f"charger_{index}_priority", row.get(CONF_PRIORITY)
                )
            rows = chargers_from_form(form)
            error = validate_charger_rows(rows)
            if error:
                errors["base"] = error
                suggested = _suggested(form_from_chargers(rows))
            else:
                self._data["chargers"] = rows
                return await self.async_step_house()
        return self.async_show_form(
            step_id="serials",
            data_schema=self.add_suggested_values_to_schema(
                _charger_serial_schema(self._charger_rows), suggested
            ),
            errors=errors,
            description_placeholders={
                "serial_hint": getattr(self, "_serial_hint", "")
            },
        )

    async def async_step_house(self, user_input=None):
        if user_input is not None:
            self._data.update(user_input)
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="Kotiakku go-e Direct", data=persistable(self._data)
            )
        return self.async_show_form(
            step_id="house",
            data_schema=self.add_suggested_values_to_schema(
                _house_schema(), _suggested(_house_values(self._data))
            ),
        )

    async def async_step_import(self, user_input):
        await self.async_set_unique_id(DOMAIN)
        data = persistable(user_input or {})
        self._abort_if_unique_id_configured(updates=data)
        return self.async_create_entry(title="Kotiakku go-e Direct", data=data)


class KotiakkuGoeDirectOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input=None):
        cfg = entry_config(self.config_entry)
        errors = {}
        suggested = dict(cfg)
        suggested.pop(CONF_CHARGERS, None)
        suggested.update(form_from_chargers(cfg["chargers"]))
        if user_input is not None:
            previous = list(cfg["chargers"])
            if not str(user_input.get("charger_1_entity") or "").strip():
                error = "charger_required"
                rows = chargers_from_form(user_input)
            else:
                rows = chargers_from_form(user_input)
                entities = charger_entities(rows)
                rows = apply_serial_guesses(
                    rows, previous, _guess_map(self.hass, entities)
                )
                error = validate_charger_rows(rows)
            if error:
                errors["base"] = error
                suggested = dict(user_input)
                suggested.update(form_from_chargers(rows))
            else:
                merged = dict(cfg)
                merged.update(user_input)
                merged["chargers"] = rows
                return self.async_create_entry(title="", data=persistable(merged))
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                _options_schema(), _suggested(suggested)
            ),
            errors=errors,
        )
