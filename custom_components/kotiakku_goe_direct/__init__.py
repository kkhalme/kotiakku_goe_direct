from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from . import binary_sensor, number, select, sensor, switch
from .const import (
    CONF_CHARGER_SERIALS,
    LEGACY_STORE_KEY,
    OPTIONAL_ENTITIES,
    REQUIRED_ENTITIES,
)
from .entity import unique_id
from .hub import Hub

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.NUMBER, Platform.SELECT, Platform.SWITCH]
PLATFORM_MODULES = (sensor, binary_sensor, number, select, switch)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hub = Hub(hass, entry)
    entry.runtime_data = hub
    _remove_orphaned_entities(hass, entry, hub.serials)
    await hub.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    hub.ready = True
    await hub.async_refresh()
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    await entry.runtime_data.async_stop()
    return unloaded


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.version == 1:
        old = {**entry.data, **entry.options}
        data = {key: old[key] for key in REQUIRED_ENTITIES + OPTIONAL_ENTITIES if old.get(key)}
        rows = [row for row in old.get("chargers") or () if isinstance(row, dict)]
        serials = [str(row.get("serial") or "").strip() for row in rows]
        data.update(zip(CONF_CHARGER_SERIALS, [s for s in serials if s]))
        hass.config_entries.async_update_entry(entry, data=data, options={}, version=2)
        await Store(hass, 1, LEGACY_STORE_KEY).async_remove()
    return True


def _remove_orphaned_entities(hass: HomeAssistant, entry: ConfigEntry, serials: list[str]) -> None:
    expected = {unique_id(key) for module in PLATFORM_MODULES for key in module.HUB_KEYS}
    expected |= {unique_id(key, s) for module in PLATFORM_MODULES for key in module.CHARGER_KEYS for s in serials}
    registry = er.async_get(hass)
    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        if item.unique_id not in expected:
            registry.async_remove(item.entity_id)
