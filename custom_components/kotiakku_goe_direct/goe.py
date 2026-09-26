"""go-e charger MQTT gateway: live status cache and de-duplicated commands.

This is the only module that talks MQTT. It never writes to ``go-eController/…``
and never writes ``ama``, ``loe``, ``loty`` or ``lop``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

_LOGGER = logging.getLogger(__name__)

STATUS_KEYS = ("car", "nrg", "frc", "amp", "lot", "psm")
RETRY = timedelta(seconds=30)
RESEND = timedelta(minutes=15)


def parse_int(payload) -> int | None:
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", "replace")
    if payload is None or isinstance(payload, bool):
        return None
    try:
        return int(float(str(payload).strip()))
    except ValueError:
        return None


def parse_nrg(payload) -> int | None:
    """Total charger watts: index 11 of the go-e ``nrg`` array."""
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", "replace")
    values = payload
    if isinstance(payload, str):
        text = payload.strip()
        try:
            values = json.loads(text) if text.startswith(("[", "{")) else text.split(",")
        except ValueError:
            return None
        if isinstance(values, dict):
            values = values.get("nrg")
    if not isinstance(values, (list, tuple)) or len(values) <= 11:
        return None
    try:
        return abs(round(float(values[11])))
    except (TypeError, ValueError):
        return None


def command_pairs(cmd) -> tuple[tuple[str, str], ...]:
    # frc=0 (Neutral) is never used: in Basic/default mode Neutral keeps charging.
    if cmd.on:
        return (("fup", "false"), ("psm", str(cmd.psm)), ("lot", str(cmd.lot)), ("amp", str(cmd.amp)), ("frc", "2"))
    return (("frc", "1"), ("fup", "false"))


def _wanted(cmd) -> dict[str, int]:
    return {"frc": 2, "psm": cmd.psm, "lot": cmd.lot, "amp": cmd.amp} if cmd.on else {"frc": 1}


class GoeMqtt:
    def __init__(
        self,
        serials: list[str],
        publish: Callable[[str, str], Awaitable[None]],
        on_status: Callable[[str, str, int | None, int | None], None],
    ) -> None:
        self.live: dict[str, dict[str, int]] = {serial: {} for serial in serials}
        self._publish = publish
        self._on_status = on_status
        self._sent: dict[str, tuple[tuple, datetime]] = {}
        self._retry_at: dict[str, datetime] = {}

    @classmethod
    def for_hass(cls, hass, serials, on_status) -> GoeMqtt:
        from homeassistant.components import mqtt

        async def publish(topic: str, payload: str) -> None:
            await mqtt.async_publish(hass, topic, payload, qos=0, retain=False)

        return cls(serials, publish, on_status)

    async def async_subscribe(self, hass) -> list[Callable[[], None]]:
        from homeassistant.components import mqtt
        from homeassistant.core import callback

        @callback
        def message(msg) -> None:
            self.handle(msg.topic, msg.payload)

        return [
            await mqtt.async_subscribe(hass, f"go-eCharger/{serial}/{key}", message)
            for serial in self.live
            for key in STATUS_KEYS
        ]

    def handle(self, topic: str, payload) -> None:
        parts = str(topic).split("/")
        if len(parts) != 3 or parts[0] != "go-eCharger" or parts[2] not in STATUS_KEYS:
            return
        serial, key = parts[1], parts[2]
        if serial not in self.live:
            return
        value = parse_nrg(payload) if key == "nrg" else parse_int(payload)
        if value is None:
            return
        old = self.live[serial].get(key)
        self.live[serial][key] = value
        if old != value:
            self._on_status(serial, key, old, value)

    def status(self, serial: str, cmd) -> str:
        """``match``, ``contradiction`` (a known live key differs) or ``unconfirmed``."""
        wanted, live = _wanted(cmd), self.live.get(serial, {})
        known = {k: live.get(k) for k in wanted}
        if any(v is not None and v != wanted[k] for k, v in known.items()):
            return "contradiction"
        return "match" if None not in known.values() else "unconfirmed"

    async def send(self, serial: str, cmd, now: datetime) -> bool:
        """Publish ``cmd`` unless live already matches or the last send is too recent.

        go-e may not echo keys that did not change, so an unconfirmed command
        is only re-sent every 15 minutes; a contradicted one after 30 s.
        """
        status = self.status(serial, cmd)
        key = (cmd.on, cmd.psm, cmd.lot, cmd.amp)
        last = self._sent.get(serial)
        if status == "match":
            self._sent[serial] = (key, now)
            self._retry_at.pop(serial, None)
            return False
        wait = RETRY if status == "contradiction" else RESEND
        if last is not None and last[0] == key and now - last[1] < wait:
            self._retry_at[serial] = last[1] + wait
            return False
        text = " ".join(f"{k}={v}" for k, v in command_pairs(cmd))
        _LOGGER.info("%s mqtt %s (%s)", serial, text, status)
        try:
            for name, payload in command_pairs(cmd):
                await self._publish(f"go-eCharger/{serial}/{name}/set", payload)
        except Exception as err:  # noqa: BLE001 - MQTT errors must not stop the other chargers
            _LOGGER.warning("%s mqtt publish failed: %s", serial, err)
        self._sent[serial] = (key, now)
        self._retry_at[serial] = now + wait
        return True

    def next_retry_at(self) -> datetime | None:
        return min(self._retry_at.values(), default=None)
