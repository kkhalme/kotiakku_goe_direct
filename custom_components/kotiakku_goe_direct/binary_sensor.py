from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.util import dt as dt_util

from .entity import HubEntity

HUB_KEYS = ("window_active", "solar_enough")
CHARGER_KEYS = ()


async def async_setup_entry(hass, entry, async_add_entities):
    hub = entry.runtime_data
    async_add_entities([WindowActive(hub), SolarEnough(hub)])


class WindowActive(HubEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:ev-station"

    def __init__(self, hub):
        super().__init__(hub, "binary_sensor", "window_active", "Window active")

    @property
    def is_on(self):
        plan = self.snapshot and self.snapshot.plan
        return plan.in_window(dt_util.now()) if plan else None


class SolarEnough(HubEntity, BinarySensorEntity):
    """SolarPriority skips 22 kW while the gating day's forecast reaches Enough solar."""

    _attr_icon = "mdi:solar-power"

    def __init__(self, hub):
        super().__init__(hub, "binary_sensor", "solar_enough", "Enough solar")

    @property
    def is_on(self):
        plan = self.snapshot and self.snapshot.plan
        return plan.enough if plan else None

    @property
    def extra_state_attributes(self):
        plan = self.snapshot and self.snapshot.plan
        if not plan:
            return {}
        return {
            "gating_day": plan.gating_day,
            "gating_kwh": plan.gating_kwh,
            "today_kwh": plan.today_kwh,
            "tomorrow_kwh": plan.tomorrow_kwh,
            "usable_end": None if plan.usable_end is None else plan.usable_end.isoformat(),
            "tomorrow_ok": plan.tomorrow_ok,
        }
