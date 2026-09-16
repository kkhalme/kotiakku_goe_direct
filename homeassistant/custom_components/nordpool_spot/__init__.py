"""Generic helper: realized Nordpool past + live forecast. Only input is the Nordpool sensor."""

from homeassistant.const import CONF_ENTITY_ID, Platform
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.discovery import async_load_platform
import voluptuous as vol

DOMAIN = "nordpool_spot"
DEFAULT_ENTITY_ID = "sensor.nordpool"

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {vol.Optional(CONF_ENTITY_ID, default=DEFAULT_ENTITY_ID): cv.entity_id}
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass, config):
    conf = config.get(DOMAIN) or {}
    source = conf.get(CONF_ENTITY_ID, DEFAULT_ENTITY_ID)
    hass.data[DOMAIN] = source
    await async_load_platform(
        hass, Platform.SENSOR, DOMAIN, {CONF_ENTITY_ID: source}, config
    )
    return True
