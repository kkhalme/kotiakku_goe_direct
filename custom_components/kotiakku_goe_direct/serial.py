"""Guess a go-e charger serial for form prefill only.

MQTT writes go to ``go-eCharger/<serial>/<key>/set``. That path is the
device serial, not the Home Assistant entity id. Users can rename entity
ids, and more than one MQTT stack exists (firmware discovery vs HACS
goecharger-mqtt), so a guess is never applied silently.

Prefill order (first confident source wins; two high-confidence sources
that disagree → no prefill):

1. State attributes ``sse`` / ``serial`` / ``serial_number``
2. Device registry ``serial_number``
3. Device identifiers (``go-e_407436``, ``(goecharger_mqtt, 407436)``, …)
4. MQTT state/command topic ``go-eCharger/<serial>/…``
5. Entity ``unique_id`` (``go-e_407436_car_state``, ``407436-sensor-car-…``)
6. Entity id / name, last resort — skipped for Controller entities

Guesses are 6-digit (go-e). The form still asks the user to confirm.
"""

from __future__ import annotations

import re

SERIAL_RE = re.compile(r"^[A-Za-z0-9]{4,12}$")
GUESSED_SERIAL_RE = re.compile(r"^\d{6}$")

_CONTROLLER_RE = re.compile(r"controller", re.I)
_CHARGER_TOPIC_RE = re.compile(r"go-eCharger/([^/]+)", re.I)
_CONTROLLER_TOPIC_RE = re.compile(r"go-eController/", re.I)

# Firmware discovery, HACS syssi, and entity_id leftovers.
_PREFIX_RES = (
    re.compile(r"go-eCharger[_/:-]([A-Za-z0-9]{4,12})", re.I),
    re.compile(r"go-e[_-]([A-Za-z0-9]{4,12})", re.I),
    re.compile(r"go_echarger[_-]([A-Za-z0-9]{4,12})", re.I),
    re.compile(r"goecharger[_-]([A-Za-z0-9]{4,12})", re.I),
)

HIGH = 3
MEDIUM = 2
LOW = 1

ATTR_KEYS = ("sse", "serial", "serial_number")


def valid_serial(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not SERIAL_RE.fullmatch(text):
        return None
    return text


def _six(text) -> str | None:
    text = str(text).strip()
    if GUESSED_SERIAL_RE.fullmatch(text):
        return text
    return None


def extract_guessed_serials(text, *, allow_controller=False) -> list[str]:
    """Six-digit serials that look like go-e, from one string."""
    if text is None:
        return []
    raw = str(text).strip()
    if not raw:
        return []
    if _CONTROLLER_TOPIC_RE.search(raw):
        return []
    if not allow_controller and _CONTROLLER_RE.search(raw):
        return []
    found: list[str] = []

    def add(candidate):
        serial = _six(candidate)
        if serial and serial not in found:
            found.append(serial)

    add(raw)
    for rx in _PREFIX_RES:
        for match in rx.finditer(raw):
            add(match.group(1))
    topic = _CHARGER_TOPIC_RE.search(raw)
    if topic:
        add(topic.group(1))
    dashed = re.match(r"^(\d{6})[-_]", raw)
    if dashed:
        add(dashed.group(1))
    for match in re.finditer(r"(?<!\d)(\d{6})(?!\d)", raw):
        add(match.group(1))
    return found


def _tokens(value: str) -> list[str]:
    return [part for part in re.split(r"[^a-z0-9]+", str(value).lower()) if part]


def _id_texts(entity_id=None, unique_id=None):
    for value in (entity_id, unique_id):
        if value:
            yield str(value).lower()


def looks_like_car_state(entity_id=None, unique_id=None) -> bool:
    """True if this entity is the go-e car / car_state key, not 'charger'."""
    for text in _id_texts(entity_id, unique_id):
        if "car_state" in text:
            return True
        tokens = _tokens(text)
        if tokens and tokens[-1] == "car":
            return True
    return False


def _resolve_key_entity(selected_entity, serial, unique_id, siblings, looks, template, missing):
    if selected_entity and looks(selected_entity, unique_id):
        return selected_entity
    for sibling in siblings or []:
        entity_id = sibling.get("entity_id") if isinstance(sibling, dict) else None
        sib_uid = sibling.get("unique_id") if isinstance(sibling, dict) else None
        if entity_id and looks(entity_id, sib_uid):
            return entity_id
    if serial:
        return template.format(serial=serial)
    return missing


def resolve_car_entity_id(selected_entity, serial, unique_id=None, siblings=None):
    """Prefer the picked car_state entity, else a sibling, else synthesized."""
    return _resolve_key_entity(
        selected_entity,
        serial,
        unique_id,
        siblings,
        looks_like_car_state,
        "sensor.go_echarger_{serial}_car_state",
        selected_entity,
    )


def looks_like_lop(entity_id=None, unique_id=None) -> bool:
    """True if this entity is go-e load-balancing priority (lop), not lot/loty."""
    for text in _id_texts(entity_id, unique_id):
        if "load_priority" in text or "loadpriority" in text:
            return True
        tokens = _tokens(text)
        if "lop" in tokens:
            return True
    return False


def resolve_lop_entity_id(selected_entity, serial, unique_id=None, siblings=None):
    """Read-only lop entity on the charger device, else synthesized."""
    return _resolve_key_entity(
        selected_entity,
        serial,
        unique_id,
        siblings,
        looks_like_lop,
        "number.go_echarger_{serial}_lop",
        None,
    )


def looks_like_charger_power(entity_id=None, unique_id=None) -> bool:
    """True if this entity is this charger's charging power, not house/solar."""
    for text in _id_texts(entity_id, unique_id):
        if "house" in text or "solar" in text or "kotiakku" in text:
            continue
        if "charging_power" in text or "chargingpower" in text or "p_all" in text:
            return True
        tokens = _tokens(text)
        if "nrg" in tokens and "11" in tokens:
            return True
        if tokens and tokens[-1] in ("power", "pall"):
            return True
    return False


def resolve_power_entity_id(selected_entity, serial, unique_id=None, siblings=None):
    """Read-only per-charger power for leftover acceptance."""
    return _resolve_key_entity(
        selected_entity,
        serial,
        unique_id,
        siblings,
        looks_like_charger_power,
        "sensor.go_echarger_{serial}_nrg",
        None,
    )


def _from_identifiers(identifiers) -> list[str]:
    found: list[str] = []
    for ident in identifiers or ():
        parts = ident if isinstance(ident, (list, tuple)) else (ident,)
        for part in parts:
            for serial in extract_guessed_serials(part, allow_controller=False):
                if serial not in found:
                    found.append(serial)
    return found


def guess_serial(
    *,
    entity_id=None,
    unique_id=None,
    identifiers=None,
    serial_number=None,
    attributes=None,
    mqtt_topics=None,
    name=None,
) -> tuple[str | None, str]:
    """Return ``(serial, source)`` for prefill, or ``(None, "")``."""
    votes: list[tuple[str, int, str]] = []

    def add_all(candidates, rank, source):
        for serial in candidates:
            if _six(serial):
                votes.append((serial, rank, source))

    attrs = attributes or {}
    for key in ATTR_KEYS:
        if key in attrs and attrs[key] not in (None, ""):
            add_all(
                extract_guessed_serials(attrs[key], allow_controller=True),
                HIGH,
                f"attr:{key}",
            )
    add_all(extract_guessed_serials(serial_number, allow_controller=True), HIGH, "device_serial")
    add_all(_from_identifiers(identifiers), HIGH, "identifier")
    for topic in mqtt_topics or ():
        if _CONTROLLER_TOPIC_RE.search(str(topic)):
            continue
        add_all(extract_guessed_serials(topic, allow_controller=True), HIGH, "mqtt_topic")
    add_all(extract_guessed_serials(unique_id, allow_controller=False), MEDIUM, "unique_id")
    add_all(extract_guessed_serials(entity_id, allow_controller=False), LOW, "entity_id")
    add_all(extract_guessed_serials(name, allow_controller=False), LOW, "name")

    if not votes:
        return None, ""

    by_serial: dict[str, tuple[int, str]] = {}
    for serial, rank, source in votes:
        prev = by_serial.get(serial)
        if prev is None or rank > prev[0]:
            by_serial[serial] = (rank, source)

    for rank in (HIGH, MEDIUM, LOW):
        group = {s: info for s, info in by_serial.items() if info[0] == rank}
        if not group:
            continue
        if len(group) > 1:
            return None, ""
        serial = next(iter(group))
        return serial, group[serial][1]
    return None, ""


def serial_suggestion(entity, previous_rows, guessed, guess_source=""):
    """Keep a saved serial when the entity is unchanged; otherwise use a guess."""
    entity = (entity or "").strip()
    guessed = (guessed or "").strip()
    for row in previous_rows or ():
        if (row.get("entity") or "").strip() == entity and (row.get("serial") or "").strip():
            return str(row["serial"]).strip(), "saved"
    if guessed:
        return guessed, guess_source or "guessed"
    return "", ""
