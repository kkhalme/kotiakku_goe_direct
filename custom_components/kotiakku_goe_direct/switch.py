from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN,
    after_charge_complete_keep_enable_entity_id,
    after_charge_complete_keep_entity_id,
    until_unplug_entity_id,
)
from .device import hub_device_info


async def async_setup_entry(hass, entry, async_add_entities):
    controller = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for serial in controller.chargers:
        entities.append(UntilUnplugSwitch(controller, serial))
        entities.append(AfterChargeCompleteKeepEnableSwitch(controller, serial))
        entities.append(AfterChargeCompleteKeepSwitch(controller, serial))
    async_add_entities(entities)


class UntilUnplugSwitch(SwitchEntity, RestoreEntity):
    """Temporary 22 kW override. Turns off when that charger’s car unplugs."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:power-plug"

    def __init__(self, controller, serial):
        self._controller = controller
        self._serial = serial
        self._attr_is_on = False
        self.entity_id = until_unplug_entity_id(serial)
        self._attr_unique_id = f"kotiakku_goe_direct_until_unplug_{serial}"
        self._attr_name = f"{serial} Force On Until Unplug"
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


class AfterChargeCompleteKeepEnableSwitch(SwitchEntity, RestoreEntity):
    """Allow HA to auto-on after-charge-complete keep for this charger."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:ev-station"

    def __init__(self, controller, serial):
        self._controller = controller
        self._serial = serial
        self._attr_is_on = True
        self.entity_id = after_charge_complete_keep_enable_entity_id(serial)
        self._attr_unique_id = (
            f"kotiakku_goe_direct_after_charge_complete_keep_enable_{serial}"
        )
        self._attr_name = f"{serial} After charge complete keep enable"
        self._attr_device_info = hub_device_info()

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self._attr_is_on = last.state == "on"

    async def async_turn_on(self, **kwargs):
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        self._attr_is_on = False
        self.async_write_ha_state()


class AfterChargeCompleteKeepSwitch(SwitchEntity, RestoreEntity):
    """Keep charging allowed at keep amp/phase until that car unplugs."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:ev-plug-type2"

    def __init__(self, controller, serial):
        self._controller = controller
        self._serial = serial
        self._attr_is_on = False
        self.entity_id = after_charge_complete_keep_entity_id(serial)
        self._attr_unique_id = f"kotiakku_goe_direct_after_charge_complete_keep_{serial}"
        self._attr_name = f"{serial} After charge complete keep"
        self._attr_device_info = hub_device_info()

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == "on":
            self._attr_is_on = True

    async def async_turn_on(self, **kwargs):
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        self._attr_is_on = False
        self.async_write_ha_state()
