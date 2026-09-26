from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfPower

from .const import CONF_PRICE_ENTITY
from .entity import HubEntity

HUB_KEYS = ("window", "available_surplus", "spot_price_history")
CHARGER_KEYS = ("role",)


def _iso(value):
    return None if value is None else value.isoformat()


async def async_setup_entry(hass, entry, async_add_entities):
    hub = entry.runtime_data
    entities = [WindowSensor(hub), AvailableSurplusSensor(hub), SpotPriceHistorySensor(hub)]
    entities += [RoleSensor(hub, serial) for serial in hub.serials]
    async_add_entities(entities)


class WindowSensor(HubEntity, SensorEntity):
    """First planned window start (a finished window stays the plan)."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:ev-station"

    def __init__(self, hub):
        super().__init__(hub, "sensor", "window", "Window")

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
            "end": _iso(first and first.end),
            "avg": first and first.avg,
            "windows": [{"start": _iso(w.start), "end": _iso(w.end), "avg": w.avg} for w in plan.windows],
            "blocked": [{"start": _iso(s), "end": _iso(e)} for s, e in plan.blocked],
            "reason": plan.reason,
            "tomorrow_ok": plan.tomorrow_ok,
            "epoch_start": _iso(plan.epoch_start),
            "epoch_seen": _iso(plan.epoch_seen),
            "carried": plan.carried,
            "source_entity": self.coordinator.entity(CONF_PRICE_ENTITY),
        }


class AvailableSurplusSensor(HubEntity, SensorEntity):
    """Held Kotiakku leftover still free for surplus chargers (after keep take)."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_icon = "mdi:lightning-bolt-outline"

    def __init__(self, hub):
        super().__init__(hub, "sensor", "available_surplus", "Available surplus")

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
            "leftover_w": sample.leftover_w,
            "keep_take_w": sample.keep_take_w,
            "sampled_at": _iso(sample.at),
            "usable": self.snapshot.usable,
        }


class SpotPriceHistorySensor(HubEntity, SensorEntity):
    """Cached spot days the planner searches after midnight. State is yesterday's average."""

    _attr_icon = "mdi:database-clock"
    _attr_suggested_display_precision = 4
    _unrecorded_attributes = frozenset({"raw_day_before_yesterday", "raw_yesterday", "raw_today"})

    def __init__(self, hub):
        super().__init__(hub, "sensor", "spot_price_history", "Spot price history")

    @property
    def native_value(self):
        return self.coordinator.price_history().get("yesterday_avg")

    @property
    def extra_state_attributes(self):
        view = self.coordinator.price_history()
        view.pop("yesterday_avg", None)
        view["source_entity"] = self.coordinator.entity(CONF_PRICE_ENTITY) or None
        return view


class RoleSensor(HubEntity, SensorEntity):
    """What this charger is doing: full, keep, surplus or off."""

    _attr_icon = "mdi:ev-station"

    def __init__(self, hub, serial):
        super().__init__(hub, "sensor", "role", "role", serial)

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
        command = None
        if d:
            command = {"frc": 2, "psm": d.command.psm, "amp": d.command.amp, "lot": d.command.lot} if d.command.on else {"frc": 1}
        return {
            "command": command,
            "car": live.get("car"),
            "nrg_w": live.get("nrg"),
            "share_w": d and d.share_w,
            "low_hold_until": _iso(d and d.low_hold_until),
            "phase_hold_until": _iso(d and d.phase_hold_until),
            "keep_cut": self.coordinator.memory.of(self.serial).cut,
        }
