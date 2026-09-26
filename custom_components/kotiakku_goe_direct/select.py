from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.helpers.restore_state import RestoreEntity

from .core.model import PHASE_OPTIONS, POLICIES
from .entity import SettingEntity

HUB_CHOICES = (
    ("after_charge_complete_keep_phase", "After charge complete keep phase", PHASE_OPTIONS, "mdi:numeric-3-circle-outline"),
    ("surplus_preferred_start_phase", "Surplus preferred start phase", PHASE_OPTIONS, "mdi:numeric-1-circle-outline"),
)
HUB_KEYS = tuple(choice[0] for choice in HUB_CHOICES)
CHARGER_KEYS = ("policy",)


async def async_setup_entry(hass, entry, async_add_entities):
    hub = entry.runtime_data
    entities = [Choice(hub, *choice) for choice in HUB_CHOICES]
    entities += [Choice(hub, "policy", "policy", POLICIES, "mdi:ev-station", serial) for serial in hub.serials]
    async_add_entities(entities)


class Choice(SettingEntity, RestoreEntity, SelectEntity):
    def __init__(self, hub, key, name, options, icon, serial=None):
        super().__init__(hub, "select", key, name, serial)
        self._attr_options = list(options)
        self._attr_icon = icon

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in self.options:
            self.put(last.state)

    @property
    def current_option(self) -> str:
        return self.get()

    async def async_select_option(self, option: str) -> None:
        self.change(option)
