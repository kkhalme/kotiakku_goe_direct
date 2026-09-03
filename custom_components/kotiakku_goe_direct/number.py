from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.helpers.restore_state import RestoreEntity

from .config import entry_config
from .const import (
    DEFAULT_CEILING,
    DEFAULT_MAX_HOURS,
    DEFAULT_MIN_HOURS,
    DOMAIN,
    EID_CEILING,
    EID_MAX,
    EID_MIN,
    EID_SOLAR_ENOUGH_KWH,
    EID_OFFSUN_HOUR_KWH,
    SURPLUS_NUMBER_SPECS,
)
from .device import hub_device_info

_UNITS = {
    "percent": PERCENTAGE,
    "W": UnitOfPower.WATT,
    "s": UnitOfTime.SECONDS,
    "min": UnitOfTime.MINUTES,
    "V": UnitOfElectricPotential.VOLT,
    "A": UnitOfElectricCurrent.AMPERE,
    "kWh": UnitOfEnergy.KILO_WATT_HOUR,
}


async def async_setup_entry(hass, entry, async_add_entities):
    controller = hass.data[DOMAIN][entry.entry_id]
    entities = [
        WindowHours(controller, EID_MIN, "kotiakku_goe_direct_window_min_h", "Window min", DEFAULT_MIN_HOURS),
        WindowHours(controller, EID_MAX, "kotiakku_goe_direct_window_max_h", "Window max", DEFAULT_MAX_HOURS),
        PriceCeiling(controller),
    ]
    cfg = entry_config(entry)
    entities.extend(SurplusNumber(controller, spec, cfg) for spec in SURPLUS_NUMBER_SPECS)
    async_add_entities(entities)


class _HubNumber(NumberEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    _attr_should_poll = False

    def __init__(self, controller):
        self._controller = controller
        self._attr_device_info = hub_device_info()

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None:
            return
        try:
            self._attr_native_value = float(last.state)
        except (TypeError, ValueError):
            pass

    async def async_set_native_value(self, value: float):
        self._attr_native_value = value
        self.async_write_ha_state()
        await self._on_changed()

    async def _on_changed(self):
        await self._controller.async_plan()
        await self._controller.async_charge()


class WindowHours(_HubNumber):
    _attr_native_min_value = 0.25
    _attr_native_max_value = 24
    _attr_native_step = 0.25
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_icon = "mdi:timer-outline"

    def __init__(self, controller, entity_id, unique_id, name, default):
        super().__init__(controller)
        self._default = default
        self._attr_native_value = default
        self.entity_id = entity_id
        self._attr_unique_id = unique_id
        self._attr_name = name


class PriceCeiling(_HubNumber):
    _attr_native_min_value = -1
    _attr_native_max_value = 5
    _attr_native_step = 0.001
    _attr_icon = "mdi:currency-eur"

    def __init__(self, controller):
        super().__init__(controller)
        self._attr_native_value = DEFAULT_CEILING
        self.entity_id = EID_CEILING
        self._attr_unique_id = "kotiakku_goe_direct_electricity_price_ceiling"
        self._attr_name = "Electricity price ceiling"


class SurplusNumber(_HubNumber):
    def __init__(self, controller, spec, cfg):
        super().__init__(controller)
        self._default = float(cfg.get(spec["conf"], spec["default"]))
        self._attr_native_value = self._default
        self._attr_native_min_value = spec["min"]
        self._attr_native_max_value = spec["max"]
        self._attr_native_step = spec["step"]
        self._attr_native_unit_of_measurement = _UNITS[spec["unit"]]
        self._attr_icon = spec["icon"]
        self.entity_id = spec["entity_id"]
        self._attr_unique_id = spec["unique_id"]
        self._attr_name = spec["name"]

    async def _on_changed(self):
        if self.entity_id in (EID_SOLAR_ENOUGH_KWH, EID_OFFSUN_HOUR_KWH):
            await self._controller.async_plan()
        await self._controller.async_knobs_changed()
