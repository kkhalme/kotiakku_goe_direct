"""Single hub device that holds every kotiakku_goe_direct entity."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN, HUB_ID


def hub_device_info() -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, HUB_ID)},
        name="Kotiakku go-e Direct",
        manufacturer="go-e",
        model="Leftover and charge windows",
    )


class HubEntity:
    """Listen for controller.notify() and write HA state."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, controller):
        self._controller = controller
        self._unsub = None
        self._attr_device_info = hub_device_info()

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self._unsub = self._controller.listen(self.async_write_ha_state)

    async def async_will_remove_from_hass(self):
        if self._unsub:
            self._unsub()
            self._unsub = None
        await super().async_will_remove_from_hass()
