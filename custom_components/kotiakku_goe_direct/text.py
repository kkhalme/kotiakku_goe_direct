from __future__ import annotations

from homeassistant.components.text import TextEntity
from homeassistant.helpers.restore_state import RestoreEntity

from .config import entry_config
from .const import CONF_PRICE_ENTITY, DOMAIN, EID_PRICE
from .device import hub_device_info


async def async_setup_entry(hass, entry, async_add_entities):
    controller = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([PriceEntityText(controller, entry)])


class PriceEntityText(TextEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:identifier"
    _attr_native_max = 128
    _attr_should_poll = False

    def __init__(self, controller, entry):
        self._controller = controller
        self._attr_native_value = entry_config(entry).get(CONF_PRICE_ENTITY, "") or ""
        self.entity_id = EID_PRICE
        self._attr_unique_id = "kotiakku_goe_direct_electricity_price_sensor"
        self._attr_name = "Electricity price sensor"
        self._attr_device_info = hub_device_info()

    async def async_added_to_hass(self):
        last = await self.async_get_last_state()
        if last is not None and last.state not in ("unknown", "unavailable", None):
            self._attr_native_value = last.state

    async def async_set_value(self, value: str):
        self._attr_native_value = value.strip()
        self.async_write_ha_state()
        self._controller._retarget_price()
        await self._controller.async_plan()
        self._controller._schedule_apply()
