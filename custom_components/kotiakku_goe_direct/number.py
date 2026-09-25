from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import HubEntity


@dataclass(frozen=True)
class Knob:
    key: str
    name: str
    low: float
    high: float
    step: float
    unit: str | None
    icon: str


KNOBS = (
    Knob("window_min_h", "Window min", 0.25, 24, 0.25, UnitOfTime.HOURS, "mdi:timer-outline"),
    Knob("window_max_h", "Window max", 0.25, 24, 0.25, UnitOfTime.HOURS, "mdi:timer-outline"),
    Knob("electricity_price_ceiling", "Electricity price ceiling", -1, 5, 0.001, None, "mdi:currency-eur"),
    Knob("window_flex_pct", "Window price flex", 0, 100, 1, PERCENTAGE, "mdi:percent-outline"),
    Knob("window_flex_eur", "Window price flex euro", 0, 1, 0.001, None, "mdi:currency-eur"),
    Knob("soc_on_pct", "Surplus SoC on", 0, 100, 1, PERCENTAGE, "mdi:battery-charging-80"),
    Knob("soc_hyst_pct", "Surplus SoC hysteresis", 0, 20, 1, PERCENTAGE, "mdi:battery-minus"),
    Knob("surplus_start_w", "Surplus start leftover", 0, 50000, 50, UnitOfPower.WATT, "mdi:lightning-bolt"),
    Knob("hold_minutes", "Hold", 1, 120, 1, UnitOfTime.MINUTES, "mdi:timer-outline"),
    Knob("max_a", "Per-charger amp cap", 6, 32, 1, UnitOfElectricCurrent.AMPERE, "mdi:current-ac"),
    Knob("max_1phase_amp", "Surplus max 1-phase amp", 6, 32, 1, UnitOfElectricCurrent.AMPERE, "mdi:current-ac"),
    Knob("group_lot_a", "Group lot (fuse cap)", 6, 64, 1, UnitOfElectricCurrent.AMPERE, "mdi:tune"),
    Knob("solar_enough_kwh", "Enough solar", 0, 500, 1, UnitOfEnergy.KILO_WATT_HOUR, "mdi:solar-power"),
    Knob("offsun_hour_kwh", "Off-sun hour", 0, 20, 0.1, UnitOfEnergy.KILO_WATT_HOUR, "mdi:weather-sunny-off"),
    Knob("after_charge_complete_keep_a", "After charge complete keep amp", 6, 32, 1, UnitOfElectricCurrent.AMPERE, "mdi:current-ac"),
)
PRIORITY = Knob("priority", "priority", 1, 99, 1, None, "mdi:order-numeric-ascending")
HUB_KEYS = tuple(knob.key for knob in KNOBS)
CHARGER_KEYS = (PRIORITY.key,)


async def async_setup_entry(hass, entry, async_add_entities):
    hub = entry.runtime_data
    entities = [KnobNumber(hub, knob) for knob in KNOBS]
    entities += [KnobNumber(hub, PRIORITY, serial) for serial in hub.serials]
    async_add_entities(entities)


class KnobNumber(HubEntity, RestoreEntity, NumberEntity):
    _attr_mode = NumberMode.BOX

    def __init__(self, hub, knob: Knob, serial: str | None = None):
        super().__init__(hub, "number", knob.key, knob.name, serial)
        self._attr_native_min_value = knob.low
        self._attr_native_max_value = knob.high
        self._attr_native_step = knob.step
        self._attr_native_unit_of_measurement = knob.unit
        self._attr_icon = knob.icon

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        try:
            value = float(last.state) if last is not None else None
        except ValueError:
            value = None
        if value is not None and self.native_min_value <= value <= self.native_max_value:
            self.put(value)

    @property
    def native_value(self) -> float:
        return self.get()

    async def async_set_native_value(self, value: float) -> None:
        self.change(value)
