from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)

from .const import DOMAIN, RANKS, rank_label
from .device import HubEntity


async def async_setup_entry(hass, entry, async_add_entities):
    controller = hass.data[DOMAIN][entry.entry_id]
    entities = [RankBinary(controller, rank) for rank in RANKS]
    entities.append(EnoughSolarBinary(controller))
    entities.extend(ChargerBinary(controller, serial) for serial in controller.chargers)
    entities.append(AnyChargerBinary(controller))
    async_add_entities(entities)


class _Base(HubEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:ev-station"


class RankBinary(_Base):
    def __init__(self, controller, rank):
        super().__init__(controller)
        self._rank = rank
        self.entity_id = f"binary_sensor.kotiakku_goe_direct_{rank}_window_active"
        self._attr_unique_id = f"kotiakku_goe_direct_{rank}_window_active"
        self._attr_name = f"{rank_label(rank)} window"

    @property
    def is_on(self):
        return self._controller.rank_active(self._rank)


class EnoughSolarBinary(_Base):
    _attr_device_class = None
    _attr_icon = "mdi:solar-power"

    def __init__(self, controller):
        super().__init__(controller)
        self.entity_id = "binary_sensor.kotiakku_goe_direct_solar_enough"
        self._attr_unique_id = "kotiakku_goe_direct_solar_enough"
        self._attr_name = "Enough solar"

    @property
    def is_on(self):
        return self._controller.enough_solar


class ChargerBinary(_Base):
    def __init__(self, controller, serial):
        super().__init__(controller)
        self._serial = serial
        self.entity_id = f"binary_sensor.kotiakku_goe_direct_{serial}_full_power"
        self._attr_unique_id = f"kotiakku_goe_direct_{serial}_full_power"
        self._attr_name = f"{serial} full power"

    @property
    def is_on(self):
        return self._controller.charger_full_power(self._serial)


class AnyChargerBinary(_Base):
    def __init__(self, controller):
        super().__init__(controller)
        self.entity_id = "binary_sensor.kotiakku_goe_direct_any_full_power"
        self._attr_unique_id = "kotiakku_goe_direct_any_full_power"
        self._attr_name = "Any full power"

    @property
    def is_on(self):
        return self._controller.any_charger_full_power()
