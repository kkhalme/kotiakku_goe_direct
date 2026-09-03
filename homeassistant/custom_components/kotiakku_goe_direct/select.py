from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.helpers.restore_state import RestoreEntity

from .config import entry_config
from .const import (
    CONF_ECO_PSM,
    DEFAULT_ECO_PSM,
    DOMAIN,
    EID_ECO_PSM,
    POLICIES,
    POLICY_FORCE_OFF,
    PSM_OPTIONS,
    psm_option,
)
from .device import hub_device_info


async def async_setup_entry(hass, entry, async_add_entities):
    controller = hass.data[DOMAIN][entry.entry_id]
    entities = [PolicySelect(controller, serial) for serial in controller.chargers]
    entities.append(EcoPsmSelect(controller, entry))
    async_add_entities(entities)


class _HubSelect(SelectEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, controller, *, options, default, entity_id, unique_id, name, icon):
        self._controller = controller
        self._attr_options = list(options)
        self._attr_current_option = default
        self._attr_icon = icon
        self.entity_id = entity_id
        self._attr_unique_id = unique_id
        self._attr_name = name
        self._attr_device_info = hub_device_info()

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in self._attr_options:
            self._attr_current_option = last.state

    async def async_select_option(self, option: str):
        if option not in self._attr_options:
            return
        self._attr_current_option = option
        self.async_write_ha_state()
        await self._on_changed()

    async def _on_changed(self):
        return


class PolicySelect(_HubSelect):
    def __init__(self, controller, serial):
        super().__init__(
            controller,
            options=POLICIES,
            default=POLICY_FORCE_OFF,
            entity_id=f"select.kotiakku_goe_direct_policy_{serial}",
            unique_id=f"kotiakku_goe_direct_policy_{serial}",
            name=f"{serial} policy",
            icon="mdi:ev-station",
        )
        self._serial = serial

    async def _on_changed(self):
        await self._controller.async_charge()
        self._controller._schedule_surplus()


class EcoPsmSelect(_HubSelect):
    def __init__(self, controller, entry):
        cfg = entry_config(entry)
        super().__init__(
            controller,
            options=PSM_OPTIONS,
            default=psm_option(cfg.get(CONF_ECO_PSM, DEFAULT_ECO_PSM)),
            entity_id=EID_ECO_PSM,
            unique_id="kotiakku_goe_direct_eco_phase",
            name="ECO phase",
            icon="mdi:sine-wave",
        )

    async def _on_changed(self):
        await self._controller.async_knobs_changed()
