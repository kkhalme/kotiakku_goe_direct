"""Base entity: one hub device, explicit ids, values read from and written to ``hub.settings``."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME
from .hub import Hub, Snapshot


def unique_id(key: str, serial: str | None = None) -> str:
    return f"{DOMAIN}_{key}_{serial}" if serial else f"{DOMAIN}_{key}"


class HubEntity(CoordinatorEntity[Hub]):
    _attr_has_entity_name = True

    def __init__(self, hub: Hub, platform: str, key: str, name: str, serial: str | None = None, field: str | None = None):
        super().__init__(hub)
        self.serial = serial
        self.field = field or key
        self._attr_unique_id = unique_id(key, serial)
        self.entity_id = f"{platform}.{self._attr_unique_id}"
        self._attr_name = f"{serial} {name}" if serial else name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, DOMAIN)}, name=NAME, manufacturer="go-e", model="Leftover and charge windows"
        )

    @property
    def snapshot(self) -> Snapshot | None:
        return self.coordinator.data

    def get(self):
        value = getattr(self.coordinator.settings, self.field)
        return value if self.serial is None else value.get(self.serial)

    def put(self, value) -> None:
        if self.serial is None:
            setattr(self.coordinator.settings, self.field, value)
        else:
            getattr(self.coordinator.settings, self.field)[self.serial] = value

    def change(self, value) -> None:
        self.put(value)
        self.async_write_ha_state()
        self.coordinator.request()
