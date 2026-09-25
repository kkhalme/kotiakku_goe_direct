from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import HubEntity

SWITCHES = (
    ("until_unplug", "until_unplug", "Force On Until Unplug", "mdi:power-plug"),
    ("after_charge_complete_keep_enable", "keep_enable", "After charge complete keep enable", "mdi:ev-station"),
    ("after_charge_complete_keep", "keep", "After charge complete keep", "mdi:ev-plug-type2"),
)
HUB_KEYS = ()
CHARGER_KEYS = tuple(switch[0] for switch in SWITCHES)


async def async_setup_entry(hass, entry, async_add_entities):
    hub = entry.runtime_data
    async_add_entities(
        Toggle(hub, key, field, name, icon, serial) for serial in hub.serials for key, field, name, icon in SWITCHES
    )


class Toggle(HubEntity, RestoreEntity, SwitchEntity):
    def __init__(self, hub, key, field, name, icon, serial):
        super().__init__(hub, "switch", key, name, serial, field)
        self._attr_icon = icon

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self.put(last.state == "on")

    @property
    def is_on(self) -> bool:
        return bool(self.get())

    async def async_turn_on(self, **kwargs) -> None:
        self.change(True)

    async def async_turn_off(self, **kwargs) -> None:
        self.change(False)
