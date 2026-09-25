"""The single state-transition authority: gathers inputs, runs the engine, sends commands."""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, UnitOfEnergy, UnitOfPower
from homeassistant.core import CoreState, Event, HomeAssistant, State, callback
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter, PowerConverter

from .const import (
    CONF_CHARGER_SERIALS,
    CONF_CONTROLLER_ENTITY,
    CONF_HOUSE_ENTITY,
    CONF_PRICE_ENTITY,
    CONF_SOC_ENTITY,
    CONF_SOLAR_ENTITY,
    CONF_SOLAR_TODAY_ENTITY,
    CONF_SOLAR_TOMORROW_ENTITY,
    DOMAIN,
    STORE_KEY,
)
from .core import planner
from .core.engine import decide
from .core.model import (
    IDLE_COMPLETE_W,
    POLICY_FORCE_OFF,
    TAKE_MIN_W,
    Charger,
    Decision,
    HouseReading,
    Memory,
    Plan,
    Sample,
    Settings,
)
from .goe import GoeMqtt

_LOGGER = logging.getLogger(__package__)

STALE = timedelta(minutes=20)
UNUSABLE = ("", "unknown", "unavailable", "none", "nan")


@dataclass
class Snapshot:
    plan: Plan
    decision: Decision | None
    sample: Sample | None
    usable: bool

    @property
    def available_w(self) -> int | None:
        return self.sample.budget_w if self.usable and self.sample is not None else None


def _number(state: State | None) -> float | None:
    if state is None or str(state.state).strip().lower() in UNUSABLE:
        return None
    try:
        value = float(state.state)
    except ValueError:
        return None
    return None if math.isnan(value) else value


def _crossed(old: int | None, new: int | None, limit: int) -> bool:
    return ((old or 0) >= limit) != ((new or 0) >= limit)


class Hub(DataUpdateCoordinator[Snapshot]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=15),
            request_refresh_debouncer=Debouncer(hass, _LOGGER, cooldown=2, immediate=False),
        )
        self.conf = dict(entry.data)
        self.serials = [s for key in CONF_CHARGER_SERIALS if (s := str(self.conf.get(key) or "").strip())]
        self.settings = Settings()
        for slot, serial in enumerate(self.serials):
            self.settings.policy[serial] = POLICY_FORCE_OFF
            self.settings.priority[serial] = slot + 1
            self.settings.until_unplug[serial] = False
            self.settings.keep[serial] = False
            self.settings.keep_enable[serial] = True
        self.memory = Memory()
        self.goe = GoeMqtt.for_hass(hass, self.serials, self._on_goe_status)
        self.ready = False
        self._lock = asyncio.Lock()
        self._store: Store = Store(hass, 1, STORE_KEY)
        self._sample_armed = True
        self._unsubs: list = []
        self._wakeup = None
        self._warned_units: set[str] = set()
        self._last_roles: dict[str, str] = {}
        self._last_windows = None
        self._last_usable: bool | None = None
        self.price_days: dict = {}
        self.epoch_seen: dict = {}

    def entity(self, key: str) -> str:
        return str(self.conf.get(key) or "").strip()

    async def async_start(self) -> None:
        stored = await self._store.async_load() or {}
        for serial, data in (stored.get("chargers") or {}).items():
            if serial in self.serials:
                self.memory.of(serial).last_plugged = data.get("last_plugged")
                self.memory.of(serial).cut = bool(data.get("cut"))
        self.price_days = stored.get("days") or {}
        self.epoch_seen = stored.get("seen") or {}
        self._unsubs += await self.goe.async_subscribe(self.hass)
        kotiakku = [self.entity(k) for k in (CONF_SOC_ENTITY, CONF_SOLAR_ENTITY, CONF_HOUSE_ENTITY)]
        sources = [self.entity(k) for k in (CONF_PRICE_ENTITY, CONF_SOLAR_TODAY_ENTITY, CONF_SOLAR_TOMORROW_ENTITY)]
        self._unsubs.append(async_track_state_change_event(self.hass, [e for e in kotiakku if e], self._on_kotiakku))
        self._unsubs.append(async_track_state_change_event(self.hass, [e for e in sources if e], self._on_source))
        if self.hass.state is not CoreState.running:
            self._unsubs.append(self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self._on_source))

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._wakeup:
            self._wakeup()
            self._wakeup = None
        await self.async_shutdown()

    @callback
    def request(self) -> None:
        self.config_entry.async_create_task(self.hass, self.async_request_refresh())

    @callback
    def _on_kotiakku(self, event: Event) -> None:
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if _number(new) is not None and (old is None or old.state != new.state):
            self._sample_armed = True
            self.request()

    @callback
    def _on_source(self, _event: Event) -> None:
        self.request()

    @callback
    def _on_goe_status(self, serial: str, key: str, old: int | None, new: int | None) -> None:
        if key == "nrg" and not (_crossed(old, new, TAKE_MIN_W) or _crossed(old, new, IDLE_COMPLETE_W)):
            return
        if key not in ("car", "nrg", "frc", "lot"):
            return
        if key == "car":
            _LOGGER.info("%s car %s → %s", serial, old, new)
        self.request()

    @callback
    def _on_wakeup(self, _now: datetime) -> None:
        self._wakeup = None
        self.request()

    async def _async_update_data(self) -> Snapshot:
        async with self._lock:
            return await self._cycle()

    async def _cycle(self) -> Snapshot:
        now = dt_util.now()
        price = self.hass.states.get(self.entity(CONF_PRICE_ENTITY))
        attrs = None if price is None else price.attributes
        today_kwh = self._kwh(self.entity(CONF_SOLAR_TODAY_ENTITY))
        tomorrow_kwh = self._kwh(self.entity(CONF_SOLAR_TOMORROW_ENTITY))
        if attrs is not None:
            live = planner.price_slots(attrs, now)
            self.price_days = planner.remember_day(self.price_days, now, live, today_kwh)
            slots, offset = planner.epoch_curve(attrs, now, self.price_days, today_kwh, tomorrow_kwh)
            self.epoch_seen, seen_ts = planner.note_epoch(self.epoch_seen, planner.epoch_day(slots, now, offset), now)
        else:
            seen_ts = None
        plan = planner.plan(
            attrs,
            now,
            self.settings,
            today_kwh,
            tomorrow_kwh,
            self.hass.config.latitude,
            self.hass.config.longitude,
            self.price_days,
            seen_ts,
        )
        self._log_plan(plan)
        usable = self._usable(now)
        if not self.ready:
            return Snapshot(plan, None, self.memory.sample, usable)
        sample = None
        if usable and self._sample_armed:
            self._sample_armed = False
            controller = self._watts(self.entity(CONF_CONTROLLER_ENTITY))
            sample = (self._watts(self.entity(CONF_SOLAR_ENTITY)), self._watts(self.entity(CONF_HOUSE_ENTITY)), controller)
        house = HouseReading(_number(self.hass.states.get(self.entity(CONF_SOC_ENTITY))), usable, sample)
        chargers = [
            Charger(serial, slot, car=self.goe.live[serial].get("car"), nrg_w=self.goe.live[serial].get("nrg"))
            for slot, serial in enumerate(self.serials)
        ]
        decision = decide(self.settings, chargers, house, plan, now, self.memory)
        for serial, changes in decision.switches.items():
            for name, value in changes.items():
                getattr(self.settings, name)[serial] = value
        self._log_roles(decision)
        for serial, charger_decision in decision.chargers.items():
            await self.goe.send(serial, charger_decision.command, now)
        self._store.async_delay_save(self._stored, 5)
        self._schedule_wakeup(now, decision.next_wakeup, self.goe.next_retry_at())
        return Snapshot(plan, decision, self.memory.sample, usable)

    def price_history(self) -> dict:
        """Sensor view of the cached spot days. Slot lists are unrecorded."""
        now = dt_util.now()
        names = {0: "today", -1: "yesterday", -2: "day_before_yesterday"}
        view = {"yesterday_avg": None, "days": [], "epoch_seen": {}, **{f"raw_{name}": [] for name in names.values()}}
        starts = {k: planner.day_start(now, k).timestamp() for k in names}
        for key, entry in sorted((self.price_days or {}).items()):
            slots = entry.get("slots") or []
            start = entry.get("start")
            if start is None or not slots:
                continue
            dur = sum(s[1] - s[0] for s in slots)
            avg = sum(s[2] * (s[1] - s[0]) for s in slots) / dur if dur else None
            view["days"].append({"date": key, "kwh": entry.get("kwh"), "slot_count": len(slots), "avg": avg})
            for offset, day_ts in starts.items():
                if abs(start - day_ts) > 1:
                    continue
                view[f"raw_{names[offset]}"] = [
                    {"start": datetime.fromtimestamp(s, now.tzinfo).isoformat(), "end": datetime.fromtimestamp(e, now.tzinfo).isoformat(), "value": p}
                    for s, e, p in slots
                ]
                if offset == -1:
                    view["yesterday_avg"] = None if avg is None else round(avg, 6)
        return view

    def _stored(self) -> dict:
        return {
            "chargers": {
                serial: {"last_plugged": self.memory.of(serial).last_plugged, "cut": self.memory.of(serial).cut}
                for serial in self.serials
            },
            "days": self.price_days,
            "seen": self.epoch_seen,
        }

    def _schedule_wakeup(self, now: datetime, *times: datetime | None) -> None:
        if self._wakeup:
            self._wakeup()
            self._wakeup = None
        future = [t for t in times if t is not None and t > now]
        if future:
            self._wakeup = async_track_point_in_utc_time(self.hass, self._on_wakeup, min(future))

    def _usable(self, now: datetime) -> bool:
        ids = [self.entity(k) for k in (CONF_SOC_ENTITY, CONF_SOLAR_ENTITY, CONF_HOUSE_ENTITY)]
        states = [self.hass.states.get(e) for e in ids]
        numeric = all(_number(s) is not None for s in states)
        newest = max((s.last_reported for s in states if s is not None), default=None)
        usable = numeric and newest is not None and now - newest <= STALE
        if usable != self._last_usable:
            if usable:
                _LOGGER.info("Kotiakku sensors usable")
            else:
                detail = ", ".join(f"{e}={None if s is None else s.state}" for e, s in zip(ids, states))
                _LOGGER.warning("Kotiakku sensors unusable or stale (%s): surplus holds 6 A, then stops", detail)
            self._last_usable = usable
        return usable

    def _watts(self, entity_id: str) -> int | None:
        state = self.hass.states.get(entity_id)
        value = _number(state)
        if value is None:
            return None
        unit = state.attributes.get("unit_of_measurement")
        if unit in PowerConverter.VALID_UNITS:
            return int(PowerConverter.convert(value, unit, UnitOfPower.WATT))
        if entity_id not in self._warned_units:
            self._warned_units.add(entity_id)
            _LOGGER.warning("%s has no power unit (%s); assuming W", entity_id, unit)
        return int(value)

    def _kwh(self, entity_id: str) -> float | None:
        state = self.hass.states.get(entity_id) if entity_id else None
        value = _number(state)
        if value is None:
            return None
        unit = state.attributes.get("unit_of_measurement")
        if unit in EnergyConverter.VALID_UNITS:
            return EnergyConverter.convert(value, unit, UnitOfEnergy.KILO_WATT_HOUR)
        return None if unit in PowerConverter.VALID_UNITS else value

    def _log_plan(self, plan: Plan) -> None:
        windows = [(w.start, w.end) for w in plan.windows]
        if windows != self._last_windows:
            spans = ", ".join(f"{s:%a %H:%M}–{e:%H:%M}" for s, e in windows) or "none"
            _LOGGER.info("plan %s: %s (tomorrow prices %s)", plan.reason, spans, plan.tomorrow_ok)
            self._last_windows = windows

    def _log_roles(self, decision: Decision) -> None:
        for serial, d in decision.chargers.items():
            if self._last_roles.get(serial) != d.role:
                _LOGGER.info("%s role %s → %s", serial, self._last_roles.get(serial, "none"), d.role)
                self._last_roles[serial] = d.role
