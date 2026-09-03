from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .config import persistable, BOOL_KEYS, INT_KEYS, STRING_KEYS
from .const import (
    CONF_CHARGERS,
    CONF_PRICE_ENTITY,
    CONF_PRIORITY,
    DOMAIN,
    PRIORITY_MAX,
    PRIORITY_MIN,
)
from .controller import KotiakkuGoeDirectController
from .serial import SERIAL_RE

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.TEXT,
]

_CHARGER_SCHEMA = vol.Schema(
    {
        vol.Required("entity"): cv.entity_id,
        vol.Required("serial"): vol.All(cv.string, vol.Match(SERIAL_RE)),
        vol.Optional(CONF_PRIORITY): vol.All(
            vol.Coerce(int), vol.Range(min=PRIORITY_MIN, max=PRIORITY_MAX)
        ),
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                **{
                    vol.Optional(key): (
                        cv.string if key == CONF_PRICE_ENTITY else cv.entity_id
                    )
                    for key in STRING_KEYS
                },
                vol.Optional(CONF_CHARGERS): [_CHARGER_SCHEMA],
                **{
                    vol.Optional(key, default=default): vol.Coerce(int)
                    for key, default in INT_KEYS.items()
                },
                **{
                    vol.Optional(key, default=default): cv.boolean
                    for key, default in BOOL_KEYS.items()
                },
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    if DOMAIN not in config:
        return True
    conf = persistable(config.get(DOMAIN) or {})
    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_IMPORT}, data=conf
        )
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    controller = KotiakkuGoeDirectController(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = controller
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await controller.async_setup()
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    controller = hass.data[DOMAIN].pop(entry.entry_id, None)
    if controller is not None:
        await controller.async_unload()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
