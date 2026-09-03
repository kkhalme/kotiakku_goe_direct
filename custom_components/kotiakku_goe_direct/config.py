"""Merge ConfigEntry data/options and charger rows. No Home Assistant import."""

from __future__ import annotations

from .const import (
    CONF_CHARGERS,
    CONF_CONTROLLER_ENTITY,
    CONF_CONTROLLER_IN_KW,
    CONF_ECO_PSM,
    CONF_HOUSE_ENTITY,
    CONF_KOTIAKKU_IN_KW,
    CONF_PRICE_ENTITY,
    CONF_PRIORITY,
    CONF_SOC_ENTITY,
    CONF_SOLAR_ENTITY,
    CONF_SOLAR_ENOUGH_KWH,
    CONF_SOLAR_REMAINING_ENTITY,
    CONF_SOLAR_TOMORROW_ENTITY,
    DEFAULT_CONTROLLER_IN_KW,
    DEFAULT_ECO_PSM,
    DEFAULT_KOTIAKKU_IN_KW,
    PRIORITY_MAX,
    PRIORITY_MIN,
    SURPLUS_NUMBER_SPECS,
    default_charger_priority,
)
from .serial import valid_serial

CHARGER_SLOTS = 4

# Older stored YAML / options. persistable() writes only the new keys.
_LEGACY_SOLAR_REMAINING = "solar_forecast_remaining_entity"
_LEGACY_SOLAR_TOMORROW = "solar_forecast_tomorrow_entity"
_LEGACY_SOLAR_ENOUGH = "supercheap_min_kwh"

INT_KEYS = {
    **{spec["conf"]: spec["default"] for spec in SURPLUS_NUMBER_SPECS},
    CONF_ECO_PSM: DEFAULT_ECO_PSM,
}

STRING_KEYS = (
    CONF_PRICE_ENTITY,
    CONF_CONTROLLER_ENTITY,
    CONF_SOC_ENTITY,
    CONF_SOLAR_ENTITY,
    CONF_HOUSE_ENTITY,
    CONF_SOLAR_REMAINING_ENTITY,
    CONF_SOLAR_TOMORROW_ENTITY,
)

BOOL_KEYS = {
    CONF_CONTROLLER_IN_KW: DEFAULT_CONTROLLER_IN_KW,
    CONF_KOTIAKKU_IN_KW: DEFAULT_KOTIAKKU_IN_KW,
}


def default_config() -> dict:
    cfg = {key: "" for key in STRING_KEYS}
    cfg[CONF_CHARGERS] = []
    cfg.update(BOOL_KEYS)
    cfg.update(INT_KEYS)
    return cfg


def as_bool(value, default=False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    return default


def as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def clamp_priority(value, default: int) -> int:
    try:
        parsed = int(round(float(value)))
    except (TypeError, ValueError):
        parsed = int(default)
    if parsed < PRIORITY_MIN:
        return PRIORITY_MIN
    if parsed > PRIORITY_MAX:
        return PRIORITY_MAX
    return parsed


def _row_priority(item, slot: int) -> int:
    default = default_charger_priority(slot)
    if not isinstance(item, dict):
        return default
    raw = item.get(CONF_PRIORITY)
    if raw is None or raw == "":
        return default
    return clamp_priority(raw, default)


def normalize_chargers(raw) -> list[dict]:
    if not raw:
        return []
    out = []
    for item in raw:
        slot = len(out)
        if isinstance(item, str):
            serial = valid_serial(item)
            if not serial:
                continue
            out.append(
                {
                    "entity": f"sensor.go_echarger_{serial}_car_state",
                    "serial": serial,
                    CONF_PRIORITY: default_charger_priority(slot),
                }
            )
            continue
        if not isinstance(item, dict):
            continue
        entity = str(item.get("entity") or "").strip()
        serial = str(item.get("serial") or "").strip()
        if not entity and not serial:
            continue
        out.append(
            {
                "entity": entity,
                "serial": serial,
                CONF_PRIORITY: _row_priority(item, slot),
            }
        )
    return out


def charger_entities(rows) -> list[str]:
    return [row["entity"] for row in rows if (row.get("entity") or "").strip()]


def form_from_chargers(rows) -> dict:
    values = {}
    for index, row in enumerate(rows[:CHARGER_SLOTS], 1):
        values[f"charger_{index}_entity"] = row.get("entity") or ""
        values[f"charger_{index}_serial"] = row.get("serial") or ""
        values[f"charger_{index}_priority"] = clamp_priority(
            row.get(CONF_PRIORITY), default_charger_priority(index - 1)
        )
    return values


def chargers_from_form(user_input) -> list[dict]:
    rows = []
    for index in range(1, CHARGER_SLOTS + 1):
        entity = str(user_input.get(f"charger_{index}_entity") or "").strip()
        serial = str(user_input.get(f"charger_{index}_serial") or "").strip()
        if not entity and not serial:
            continue
        rows.append(
            {
                "entity": entity,
                "serial": serial,
                CONF_PRIORITY: clamp_priority(
                    user_input.get(f"charger_{index}_priority"),
                    default_charger_priority(index - 1),
                ),
            }
        )
    return rows


def _with_legacy(raw: dict) -> dict:
    """Copy stored options, mapping old solar keys onto the current names."""
    src = dict(raw or {})
    if CONF_SOLAR_REMAINING_ENTITY not in src and src.get(_LEGACY_SOLAR_REMAINING):
        src[CONF_SOLAR_REMAINING_ENTITY] = src[_LEGACY_SOLAR_REMAINING]
    if CONF_SOLAR_TOMORROW_ENTITY not in src and src.get(_LEGACY_SOLAR_TOMORROW):
        src[CONF_SOLAR_TOMORROW_ENTITY] = src[_LEGACY_SOLAR_TOMORROW]
    if CONF_SOLAR_ENOUGH_KWH not in src and _LEGACY_SOLAR_ENOUGH in src:
        src[CONF_SOLAR_ENOUGH_KWH] = src[_LEGACY_SOLAR_ENOUGH]
    return src


def persistable(raw: dict) -> dict:
    src = _with_legacy(raw)
    cfg = default_config()
    cfg.update(src)
    out = {key: str(cfg.get(key) or "").strip() for key in STRING_KEYS}
    out[CONF_CHARGERS] = normalize_chargers(cfg.get(CONF_CHARGERS))
    for key, default in BOOL_KEYS.items():
        out[key] = as_bool(cfg.get(key), default)
    for key, default in INT_KEYS.items():
        out[key] = as_int(cfg.get(key), default)
    return out


def entry_config(entry) -> dict:
    merged = {}
    merged.update(dict(getattr(entry, "data", None) or {}))
    merged.update(dict(getattr(entry, "options", None) or {}))
    return persistable(merged)


def apply_serial_guesses(rows, previous_rows, guesses_by_entity) -> list[dict]:
    """Fill empty serials, and replace a stale slot serial when the entity changed."""
    out = []
    previous_rows = list(previous_rows or ())
    guesses_by_entity = guesses_by_entity or {}
    for index, row in enumerate(rows):
        entity = (row.get("entity") or "").strip()
        serial = (row.get("serial") or "").strip()
        old_entity = ""
        old_serial = ""
        if index < len(previous_rows):
            old_entity = (previous_rows[index].get("entity") or "").strip()
            old_serial = (previous_rows[index].get("serial") or "").strip()
        guessed = str(guesses_by_entity.get(entity) or "").strip() if entity else ""
        if entity and not serial:
            serial = guessed
        elif entity and entity != old_entity and serial == old_serial and guessed:
            serial = guessed
        out.append(
            {
                "entity": entity,
                "serial": serial,
                CONF_PRIORITY: clamp_priority(
                    row.get(CONF_PRIORITY), default_charger_priority(index)
                ),
            }
        )
    return out


def validate_charger_rows(rows) -> str | None:
    """Return an error key, or None if the rows can be stored.

    Charger 1 is required. Chargers 2–4 may be omitted.
    """
    if not rows:
        return "charger_required"
    seen = []
    for row in rows:
        entity = (row.get("entity") or "").strip()
        serial = valid_serial(row.get("serial"))
        if not entity:
            return "entity_required"
        if not serial:
            return "serial_required"
        if serial in seen:
            return "duplicate_serial"
        seen.append(serial)
    return None
