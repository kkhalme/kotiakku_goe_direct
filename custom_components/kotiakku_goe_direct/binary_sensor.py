from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.util import dt as dt_util

from .entity import HubEntity, iso, window_attrs


async def async_setup_entry(hass, entry, async_add_entities):
    hub = entry.runtime_data
    async_add_entities([cls(hub) for cls in HUB_BINARY_SENSORS])


class WindowActive(HubEntity, BinarySensorEntity):
    KEY = "window_active"
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:ev-station"

    def __init__(self, hub):
        super().__init__(hub, "binary_sensor", self.KEY, "Window active")

    @property
    def is_on(self):
        plan = self.snapshot and self.snapshot.plan
        return plan.in_window(dt_util.now()) if plan else None


class SolarEnough(HubEntity, BinarySensorEntity):
    """SolarPriority skips 22 kW while the gating day's forecast reaches Enough solar."""

    KEY = "solar_enough"
    _attr_icon = "mdi:solar-power"

    def __init__(self, hub):
        super().__init__(hub, "binary_sensor", self.KEY, "Enough solar")

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
            "usable_end": iso(plan.usable_end),
            "tomorrow_ok": plan.tomorrow_ok,
        }


class SurplusPricedOut(HubEntity, BinarySensorEntity):
    """On while the spot slot is above the surplus charging price ceiling: surplus stays off."""

    KEY = "surplus_priced_out"
    _attr_icon = "mdi:cash-lock"

    def __init__(self, hub):
        super().__init__(hub, "binary_sensor", self.KEY, "Surplus priced out")

    @property
    def is_on(self):
        plan = self.snapshot and self.snapshot.plan
        return plan.priced_out_at(dt_util.now()) is not None if plan else None

    @property
    def extra_state_attributes(self):
        plan = self.snapshot and self.snapshot.plan
        attrs = {"ceiling": self.coordinator.settings.surplus_charging_price_ceiling}
        if not plan:
            return attrs
        now = dt_util.now()
        current = plan.priced_out_at(now)
        return {
            **attrs,
            "until": iso(current and current.end),
            "avg": current and current.avg,
            "spans": [window_attrs(w) for w in plan.surplus_priced_out if w.end > now],
        }


HUB_BINARY_SENSORS = (WindowActive, SolarEnough, SurplusPricedOut)
HUB_KEYS = tuple(cls.KEY for cls in HUB_BINARY_SENSORS)
CHARGER_KEYS = ()
