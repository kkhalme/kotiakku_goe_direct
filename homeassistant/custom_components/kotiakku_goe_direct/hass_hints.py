"""Read HA registries / MQTT debug info to prefill charger serials."""

from __future__ import annotations

import json


def collect_serial_hints(hass, entity_id: str) -> dict:
    hints = {
        "entity_id": entity_id or None,
        "unique_id": None,
        "identifiers": [],
        "serial_number": None,
        "attributes": {},
        "mqtt_topics": [],
        "name": None,
    }
    if not entity_id or hass is None:
        return hints
    state = hass.states.get(entity_id)
    if state is not None:
        hints["attributes"] = dict(state.attributes)
        if state.name:
            hints["name"] = state.name
    try:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er
    except Exception:
        hints["mqtt_topics"] = _mqtt_topics(hass, entity_id)
        return hints
    registry = er.async_get(hass)
    ent = registry.async_get(entity_id)
    if ent is None:
        hints["mqtt_topics"] = _mqtt_topics(hass, entity_id)
        return hints
    hints["unique_id"] = ent.unique_id
    if ent.original_name:
        hints["name"] = ent.original_name
    if ent.device_id:
        device = dr.async_get(hass).async_get(ent.device_id)
        if device is not None:
            hints["identifiers"] = list(device.identifiers)
            hints["serial_number"] = device.serial_number
            if not hints["name"] and device.name:
                hints["name"] = device.name
    hints["mqtt_topics"] = _mqtt_topics(hass, entity_id)
    return hints


def device_entities(hass, entity_id: str) -> list[dict]:
    if not entity_id or hass is None:
        return []
    try:
        from homeassistant.helpers import entity_registry as er
    except Exception:
        return []
    registry = er.async_get(hass)
    ent = registry.async_get(entity_id)
    if ent is None or not ent.device_id:
        return []
    return [
        {"entity_id": other.entity_id, "unique_id": other.unique_id}
        for other in er.async_entries_for_device(
            registry, ent.device_id, include_disabled_entities=True
        )
    ]


def _mqtt_topics(hass, entity_id: str) -> list[str]:
    topics: list[str] = []
    try:
        from homeassistant.components.mqtt.debug_info import info_for_entity
    except Exception:
        return topics
    try:
        info = info_for_entity(hass, entity_id)
    except Exception:
        return topics
    if not info:
        return topics
    for sub in info.get("subscriptions") or ():
        topic = sub.get("topic") if isinstance(sub, dict) else None
        if topic:
            topics.append(str(topic))
    discovery = info.get("discovery_data") or {}
    payload = discovery.get("payload") if isinstance(discovery, dict) else None
    topics.extend(_topics_from_payload(payload))
    return topics


def _topics_from_payload(payload) -> list[str]:
    data = payload
    if isinstance(payload, (bytes, str)):
        try:
            data = json.loads(payload)
        except Exception:
            return []
    if not isinstance(data, dict):
        return []
    out = []
    for key in ("state_topic", "command_topic", "json_attributes_topic", "topic"):
        value = data.get(key)
        if value:
            out.append(str(value))
    return out
