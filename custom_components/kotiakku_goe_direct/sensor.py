from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    EID_SOLAR_GATING_DAY,
    EID_SOLAR_GATING_KWH,
    EID_SOLAR_KWH,
    EID_SOLAR_TODAY_KWH,
    EID_SOLAR_TOMORROW_KWH,
    EID_WINDOW,
    SOLAR_GATING_DAY_UNIQUE_ID,
    SOLAR_GATING_KWH_UNIQUE_ID,
    SOLAR_KWH_UNIQUE_ID,
    SOLAR_TODAY_UNIQUE_ID,
    SOLAR_TOMORROW_UNIQUE_ID,
    WINDOW_SENSOR_UNIQUE_ID,
    migrate_window_entities,
)
from .device import HubEntity


def _migrate_window_entities(hass):
    migrate_window_entities(er.async_get(hass))


def _kwh(value):
    if value is None:
        return None
    return round(float(value), 3)


async def async_setup_entry(hass, entry, async_add_entities):
    _migrate_window_entities(hass)
    controller = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            WindowSensor(controller),
            ForecastSolarSensor(controller),
            SolarTodaySensor(controller),
            SolarTomorrowSensor(controller),
            SolarGatingKwhSensor(controller),
            SolarGatingDaySensor(controller),
        ]
    )


class WindowSensor(HubEntity, SensorEntity):
    """Planned window (past is fine). State is the start; end and the plan are attributes."""

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


class _ForecastKwhSensor(HubEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:solar-power"
    _attr_suggested_display_precision = 1


class ForecastSolarSensor(_ForecastKwhSensor):
    """Headline forecast: max of today's full-day kWh and tomorrow."""

    def __init__(self, controller):
        super().__init__(controller)
        self.entity_id = EID_SOLAR_KWH
        self._attr_unique_id = SOLAR_KWH_UNIQUE_ID
        self._attr_name = "Forecast solar"

    @property
    def native_value(self):
        return _kwh(self._controller.upcoming_solar_kwh)

    @property
    def extra_state_attributes(self):
        return {
            "source_today": self._controller.solar_today_entity or None,
            "source_tomorrow": self._controller.solar_tomorrow_entity or None,
            "enough_kwh": self._controller.solar_enough_kwh,
            "enough_solar": self._controller.enough_solar,
            "offsun_hour_kwh": self._controller.offsun_hour_kwh,
            "surplus_hours": self._controller.surplus_hours,
        }


class SolarTodaySensor(_ForecastKwhSensor):
    """Today's full-day production estimate from Configure → Solar today."""

    def __init__(self, controller):
        super().__init__(controller)
        self.entity_id = EID_SOLAR_TODAY_KWH
        self._attr_unique_id = SOLAR_TODAY_UNIQUE_ID
        self._attr_name = "Solar today"

    @property
    def native_value(self):
        return _kwh(self._controller.today_kwh)

    @property
    def extra_state_attributes(self):
        return {"source": self._controller.solar_today_entity or None}


class SolarTomorrowSensor(_ForecastKwhSensor):
    """Tomorrow's production estimate from Configure → Solar tomorrow."""

    def __init__(self, controller):
        super().__init__(controller)
        self.entity_id = EID_SOLAR_TOMORROW_KWH
        self._attr_unique_id = SOLAR_TOMORROW_UNIQUE_ID
        self._attr_name = "Solar tomorrow"

    @property
    def native_value(self):
        return _kwh(self._controller.tomorrow_kwh)

    @property
    def extra_state_attributes(self):
        return {"source": self._controller.solar_tomorrow_entity or None}


class SolarGatingKwhSensor(_ForecastKwhSensor):
    """kWh that currently gates the 22 kW skip (today until sunset, then tomorrow)."""

    def __init__(self, controller):
        super().__init__(controller)
        self.entity_id = EID_SOLAR_GATING_KWH
        self._attr_unique_id = SOLAR_GATING_KWH_UNIQUE_ID
        self._attr_name = "Solar gating"

    @property
    def native_value(self):
        return _kwh(self._controller.gating_solar_kwh)

    @property
    def extra_state_attributes(self):
        return {
            "gating_day": self._controller.gating_solar_day,
            "sunset": self._controller.sunset_iso,
        }


class SolarGatingDaySensor(HubEntity, SensorEntity):
    """``today`` until sunset; ``tomorrow`` after sunset or polar night."""

    _attr_icon = "mdi:weather-sunset-down"

    def __init__(self, controller):
        super().__init__(controller)
        self.entity_id = EID_SOLAR_GATING_DAY
        self._attr_unique_id = SOLAR_GATING_DAY_UNIQUE_ID
        self._attr_name = "Solar gating day"

    @property
    def native_value(self):
        return self._controller.gating_solar_day

    @property
    def extra_state_attributes(self):
        return {"sunset": self._controller.sunset_iso}
