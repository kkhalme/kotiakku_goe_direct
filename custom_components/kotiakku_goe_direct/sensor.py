from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    RANKS,
    rank_label,
    window_sensor_entity_id,
    window_sensor_legacy_unique_id,
    window_sensor_unique_id,
)
from .device import HubEntity


def _migrate_window_sensor_ids(hass):
    """Keep history: old unique_id ended in ``_window_start``."""
    registry = er.async_get(hass)
    for rank in RANKS:
        old_uid = window_sensor_legacy_unique_id(rank)
        new_uid = window_sensor_unique_id(rank)
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, old_uid)
        if entity_id is None:
            continue
        new_entity_id = window_sensor_entity_id(rank)
        kwargs = {"new_unique_id": new_uid}
        if entity_id == f"sensor.{old_uid}" and registry.async_get(new_entity_id) is None:
            kwargs["new_entity_id"] = new_entity_id
        registry.async_update_entity(entity_id, **kwargs)


async def async_setup_entry(hass, entry, async_add_entities):
    _migrate_window_sensor_ids(hass)
    controller = hass.data[DOMAIN][entry.entry_id]
    entities = [WindowSensor(controller, rank) for rank in RANKS]
    entities.append(ForecastSolarSensor(controller))
    async_add_entities(entities)


class WindowSensor(HubEntity, SensorEntity):
    """Current or next window. State is the start; end and the plan are attributes."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:ev-station"

    def __init__(self, controller, rank):
        super().__init__(controller)
        self._rank = rank
        self.entity_id = window_sensor_entity_id(rank)
        self._attr_unique_id = window_sensor_unique_id(rank)
        self._attr_name = f"{rank_label(rank)} window"

    @property
    def native_value(self):
        start = (self._controller.window_results.get(self._rank) or {}).get("start")
        if not start:
            return None
        return dt_util.parse_datetime(str(start))

    @property
    def extra_state_attributes(self):
        result = dict(self._controller.window_results.get(self._rank) or {})
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
