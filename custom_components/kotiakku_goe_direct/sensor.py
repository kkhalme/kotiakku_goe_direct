from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    EID_WINDOW,
    WINDOW_SENSOR_UNIQUE_ID,
    migrate_window_entities,
)
from .device import HubEntity


def _migrate_window_entities(hass):
    migrate_window_entities(er.async_get(hass))


async def async_setup_entry(hass, entry, async_add_entities):
    _migrate_window_entities(hass)
    controller = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([WindowSensor(controller), ForecastSolarSensor(controller)])


class WindowSensor(HubEntity, SensorEntity):
    """Current or next window. State is the start; end and the plan are attributes."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:ev-station"

    def __init__(self, controller):
        super().__init__(controller)
        self.entity_id = EID_WINDOW
        self._attr_unique_id = WINDOW_SENSOR_UNIQUE_ID
        self._attr_name = "Window"

    @property
    def native_value(self):
        start = (self._controller.window_result or {}).get("start")
        if not start:
            return None
        return dt_util.parse_datetime(str(start))

    @property
    def extra_state_attributes(self):
        result = dict(self._controller.window_result or {})
        result.pop("raw_windows", None)
        result.pop("horizon_ts", None)
        result.pop("blocked_ts", None)
        return result


class ForecastSolarSensor(HubEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:solar-power"
    _attr_suggested_display_precision = 1

    def __init__(self, controller):
        super().__init__(controller)
        self.entity_id = "sensor.kotiakku_goe_direct_solar_kwh"
        self._attr_unique_id = "kotiakku_goe_direct_solar_kwh"
        self._attr_name = "Forecast solar"

    @property
    def native_value(self):
        value = self._controller.upcoming_solar_kwh
        if value is None:
            return None
        return round(float(value), 3)

    @property
    def extra_state_attributes(self):
        return {
            "remaining_today_kwh": self._controller.remaining_today_kwh,
            "tomorrow_kwh": self._controller.tomorrow_kwh,
            "enough_kwh": self._controller.solar_enough_kwh,
            "enough_solar": self._controller.enough_solar,
            "offsun_hour_kwh": self._controller.offsun_hour_kwh,
            "surplus_hours": self._controller.surplus_hours,
        }
