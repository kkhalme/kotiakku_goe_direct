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

from .config import clamp_priority, entry_config
from .const import (
    DEFAULT_CEILING,
    DEFAULT_FLEX_EUR,
    DEFAULT_FLEX_PCT,
    DEFAULT_MAX_HOURS,
    DEFAULT_MIN_HOURS,
    DOMAIN,
    EID_CEILING,
    EID_FLEX_EUR,
    EID_FLEX_PCT,
    EID_MAX,
    EID_MIN,
    EID_SOLAR_ENOUGH_KWH,
    EID_OFFSUN_HOUR_KWH,
    PRIORITY_MAX,
    PRIORITY_MIN,
    SURPLUS_NUMBER_SPECS,
    default_charger_priority,
    priority_entity_id,
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
        WindowFlexPct(controller),
        WindowFlexEuro(controller),
    ]
    cfg = entry_config(entry)
    entities.extend(SurplusNumber(controller, spec, cfg) for spec in SURPLUS_NUMBER_SPECS)
    for index, row in enumerate(cfg.get("chargers") or ()):
        serial = row.get("serial")
        if not serial:
            continue
        entities.append(
            ChargerPriorityNumber(
                controller,
                serial,
                clamp_priority(row.get("priority"), default_charger_priority(index)),
            )
        )
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
        self._controller._schedule_apply()


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


class WindowFlexPct(_HubNumber):
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:percent-outline"

    def __init__(self, controller):
        super().__init__(controller)
        self._attr_native_value = DEFAULT_FLEX_PCT
        self.entity_id = EID_FLEX_PCT
        self._attr_unique_id = "kotiakku_goe_direct_window_flex_pct"
        self._attr_name = "Window price flex"


class WindowFlexEuro(_HubNumber):
    _attr_native_min_value = 0
    _attr_native_max_value = 1
    _attr_native_step = 0.001
    _attr_icon = "mdi:currency-eur"

    def __init__(self, controller):
        super().__init__(controller)
        self._attr_native_value = DEFAULT_FLEX_EUR
        self.entity_id = EID_FLEX_EUR
        self._attr_unique_id = "kotiakku_goe_direct_window_flex_eur"
        self._attr_name = "Window price flex euro"


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
        self._controller._schedule_apply()


class ChargerPriorityNumber(_HubNumber):
    _attr_native_min_value = PRIORITY_MIN
    _attr_native_max_value = PRIORITY_MAX
    _attr_native_step = 1
    _attr_icon = "mdi:order-numeric-ascending"

    def __init__(self, controller, serial, default):
        super().__init__(controller)
        self._serial = serial
        self._attr_native_value = float(default)
        self.entity_id = priority_entity_id(serial)
        self._attr_unique_id = f"kotiakku_goe_direct_priority_{serial}"
        self._attr_name = f"{serial} priority"

    async def _on_changed(self):
        self._controller._schedule_apply()
