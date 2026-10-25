from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfPower

from .const import CONF_PRICE_ENTITY
from .entity import HubEntity, iso, window_attrs
from .goe import wanted_state


async def async_setup_entry(hass, entry, async_add_entities):
    hub = entry.runtime_data
    entities = [cls(hub) for cls in HUB_SENSORS]
    entities += [RoleSensor(hub, serial) for serial in hub.serials]
    async_add_entities(entities)


class WindowSensor(HubEntity, SensorEntity):
    """First planned window start (a finished window stays the plan)."""

    KEY = "window"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:ev-station"

    def __init__(self, hub):
        super().__init__(hub, "sensor", self.KEY, "Window")

    @property
    def native_value(self):
        plan = self.snapshot and self.snapshot.plan
        return plan.windows[0].start if plan and plan.windows else None

    @property
    def extra_state_attributes(self):
        plan = self.snapshot and self.snapshot.plan
        if not plan:
            return {"source_entity": self.coordinator.entity(CONF_PRICE_ENTITY)}
        first = plan.windows[0] if plan.windows else None
        return {
            "end": iso(first and first.end),
            "avg": first and first.avg,
            "windows": [window_attrs(w) for w in plan.windows],
            "trip_windows": [window_attrs(w) for w in plan.trip_windows],
            "blocked": [{"start": iso(s), "end": iso(e)} for s, e in plan.blocked],
            "reason": plan.reason,
            "tomorrow_ok": plan.tomorrow_ok,
            "epoch_start": iso(plan.epoch_start),
            "epoch_seen": iso(plan.epoch_seen),
            "carried": plan.carried,
            "source_entity": self.coordinator.entity(CONF_PRICE_ENTITY),
        }


class AvailableSurplusSensor(HubEntity, SensorEntity):
    """Surplus from the latest Kotiakku average still free for surplus chargers (after keep take)."""

    KEY = "available_surplus"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_icon = "mdi:lightning-bolt-outline"

    def __init__(self, hub):
        super().__init__(hub, "sensor", self.KEY, "Available surplus")

    @property
    def native_value(self):
        return self.snapshot and self.snapshot.available_w

    @property
    def extra_state_attributes(self):
        sample = self.snapshot and self.snapshot.sample
        if not sample:
            return {"usable": False}
        return {
            "solar_w": sample.solar_w,
            "house_w": sample.house_w,
            "ev_w": sample.ev_w,
            "base_w": sample.solar_w - sample.leftover_w,
            "leftover_w": sample.leftover_w,
            "keep_take_w": sample.keep_take_w,
            "sampled_at": iso(sample.at),
            "usable": self.snapshot.usable,
        }


class SpotPriceHistorySensor(HubEntity, SensorEntity):
    """Cached spot days the planner searches after midnight. State is yesterday's average."""

    KEY = "spot_price_history"
    _attr_icon = "mdi:database-clock"
    _attr_suggested_display_precision = 4
    _unrecorded_attributes = frozenset({"raw_day_before_yesterday", "raw_yesterday", "raw_today"})

    def __init__(self, hub):
        super().__init__(hub, "sensor", self.KEY, "Spot price history")

    @property
    def native_value(self):
        return self.snapshot and self.snapshot.history["yesterday_avg"]

    @property
    def extra_state_attributes(self):
        view = {k: v for k, v in (self.snapshot.history if self.snapshot else {}).items() if k != "yesterday_avg"}
        view["source_entity"] = self.coordinator.entity(CONF_PRICE_ENTITY) or None
        return view


class RoleSensor(HubEntity, SensorEntity):
    """What this charger is doing: full, keep, surplus or off."""

    KEY = "role"
    _attr_icon = "mdi:ev-station"

    def __init__(self, hub, serial):
        super().__init__(hub, "sensor", self.KEY, "role", serial)

    def _decision(self):
        decision = self.snapshot and self.snapshot.decision
        return decision.chargers.get(self.serial) if decision else None

    @property
    def native_value(self):
        d = self._decision()
        return d.role if d else None

    @property
    def extra_state_attributes(self):
        d = self._decision()
        live = self.coordinator.goe.live.get(self.serial, {})
        windows = None
        if d:
            trips = self.snapshot.plan.trip_windows
            windows = [{**window_attrs(w), "trip": w in trips} for w in d.windows]
        return {
            "command": d and wanted_state(d.command),
            "windows": windows,
            "car": live.get("car"),
            "nrg_w": live.get("nrg"),
            "share_w": d and d.share_w,
            "low_hold_until": iso(d and d.low_hold_until),
            "phase_hold_until": iso(d and d.phase_hold_until),
            "reserve_w": d and d.reserve_w,
            "car_limited": d and d.limited,
            "start_pending": d and d.start_pending,
            "priced_out": d and d.priced_out,
            "keep_cut": self.coordinator.memory.of(self.serial).cut,
        }


HUB_SENSORS = (WindowSensor, AvailableSurplusSensor, SpotPriceHistorySensor)
HUB_KEYS = tuple(cls.KEY for cls in HUB_SENSORS)
CHARGER_KEYS = (RoleSensor.KEY,)
