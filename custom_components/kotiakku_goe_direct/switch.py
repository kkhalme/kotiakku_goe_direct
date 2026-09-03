from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, until_unplug_entity_id
from .device import hub_device_info


async def async_setup_entry(hass, entry, async_add_entities):
    controller = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        UntilUnplugSwitch(controller, serial) for serial in controller.chargers
    )


class UntilUnplugSwitch(SwitchEntity, RestoreEntity):
    """Temporary 22 kW override. Turns off when that charger’s car is Idle."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:power-plug"

    def __init__(self, controller, serial):
        self._controller = controller
        self._serial = serial
        self._attr_is_on = False
        self.entity_id = until_unplug_entity_id(serial)
        self._attr_unique_id = f"kotiakku_goe_direct_until_unplug_{serial}"
        self._attr_name = f"{serial} until unplug"
        self._attr_device_info = hub_device_info()

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == "on":
            self._attr_is_on = True
        elif self._serial in self._controller.legacy_until_unplug:
            self._attr_is_on = True

    async def async_turn_on(self, **kwargs):
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        self._attr_is_on = False
        self.async_write_ha_state()
