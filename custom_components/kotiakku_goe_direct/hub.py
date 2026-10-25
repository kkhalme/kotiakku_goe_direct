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
from .core.engine import decide, ev_for_window, note_draw, pair_base, reconciled_reading
from .core.model import (
    IDLE_COMPLETE_W,
    KOTIAKKU_AVG_S,
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
BURST_S = 15
UNUSABLE = ("", "unknown", "unavailable", "none", "nan")
KOTIAKKU_ENTITIES = (CONF_SOC_ENTITY, CONF_SOLAR_ENTITY, CONF_HOUSE_ENTITY)
SOURCE_ENTITIES = (CONF_PRICE_ENTITY, CONF_SOLAR_TODAY_ENTITY, CONF_SOLAR_TOMORROW_ENTITY)
FORECAST_ENTITIES = (CONF_SOLAR_TODAY_ENTITY, CONF_SOLAR_TOMORROW_ENTITY)


@dataclass
class Snapshot:
    plan: Plan
    decision: Decision | None
    sample: Sample | None
    usable: bool
    history: dict

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
            self.settings.add_charger(serial, slot)
        self.memory = Memory()
        self.goe = GoeMqtt.for_hass(hass, self.serials, self._on_goe_status)
        self.ready = False
        self._lock = asyncio.Lock()
        self._store: Store = Store(hass, 1, STORE_KEY)
        self._draw_trace: list[tuple[datetime, int]] = []
        self._burst_until: datetime | None = None
        self._burst_house = False
        self._burst_solar = False
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

    def _state(self, key: str) -> State | None:
        entity_id = self.entity(key)
        return self.hass.states.get(entity_id) if entity_id else None

    async def async_start(self) -> None:
        stored = await self._store.async_load() or {}
        for serial, data in (stored.get("chargers") or {}).items():
            if serial in self.serials:
                self.memory.of(serial).last_plugged = data.get("last_plugged")
                self.memory.of(serial).cut = bool(data.get("cut"))
        self.price_days = stored.get("days") or {}
        self.epoch_seen = stored.get("seen") or {}
        self._unsubs += await self.goe.async_subscribe(self.hass)
        kotiakku = [e for key in KOTIAKKU_ENTITIES if (e := self.entity(key))]
        sources = [e for key in SOURCE_ENTITIES if (e := self.entity(key))]
        self._unsubs.append(async_track_state_change_event(self.hass, kotiakku, self._on_kotiakku))
        self._unsubs.append(async_track_state_change_event(self.hass, sources, self._on_source))
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
        if _number(new) is None or (old is not None and old.state == new.state):
            return
        entity = event.data.get("entity_id")
        if entity == self.entity(CONF_SOC_ENTITY):
            self.request()
            return
        if entity == self.entity(CONF_HOUSE_ENTITY):
            self._burst_house = True
        elif entity == self.entity(CONF_SOLAR_ENTITY):
            self._burst_solar = True
        else:
            return
        if self._burst_until is None:
            self._burst_until = dt_util.now() + timedelta(seconds=BURST_S)
        self.request()

    @callback
    def _on_source(self, _event: Event) -> None:
        self.request()

    @callback
    def _on_goe_status(self, serial: str, key: str, old: int | None, new: int | None) -> None:
        if key == "nrg":
            self._note_draw()
            if not (_crossed(old, new, TAKE_MIN_W) or _crossed(old, new, IDLE_COMPLETE_W)):
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
        price = self._state(CONF_PRICE_ENTITY)
        attrs = None if price is None else price.attributes
        today_kwh = self._kwh(CONF_SOLAR_TODAY_ENTITY)
        tomorrow_kwh = self._kwh(CONF_SOLAR_TOMORROW_ENTITY)
        periods = self._forecast_periods(now)
        self.price_days, self.epoch_seen, seen_ts = planner.update_history(
            attrs, now, self.price_days, self.epoch_seen, today_kwh, periods
        )
        plan = planner.plan(
            attrs,
            now,
            self.settings,
            lat=self.hass.config.latitude,
            lon=self.hass.config.longitude,
            today_kwh=today_kwh,
            tomorrow_kwh=tomorrow_kwh,
            history=self.price_days,
            epoch_seen=seen_ts,
            periods=periods,
        )
        self._log_plan(plan)
        usable = self._usable(now)
        history = planner.history_view(self.price_days, now)
        if not self.ready:
            return Snapshot(plan, None, self.memory.sample, usable, history)
        sample = None
        if self._burst_until is not None and now >= self._burst_until:
            if usable:
                sample = self._publication(now)
            self._burst_until = None
            self._burst_house = False
            self._burst_solar = False
        house = HouseReading(_number(self._state(CONF_SOC_ENTITY)), usable, sample)
        chargers = [
            Charger(serial, slot, car=self.goe.live[serial].get("car"), nrg_w=self.goe.live[serial].get("nrg"))
            for slot, serial in enumerate(self.serials)
        ]
        decision = decide(self.settings, chargers, house, plan, now, self.memory)
        self.settings.apply(decision.switches)
        self._log_roles(decision)
        for serial, charger_decision in decision.chargers.items():
            await self.goe.send(serial, charger_decision.command, now)
        self._store.async_delay_save(self._stored, 5)
        self._schedule_wakeup(now, decision.next_wakeup, self.goe.next_retry_at(), self._burst_until)
        return Snapshot(plan, decision, self.memory.sample, usable, history)

    def _stored(self) -> dict:
        return {
            "chargers": {
                serial: {"last_plugged": self.memory.of(serial).last_plugged, "cut": self.memory.of(serial).cut}
                for serial in self.serials
            },
            "days": self.price_days,
            "seen": self.epoch_seen,
        }

    def _site_draw(self) -> int | None:
        known = [nrg for serial in self.serials if (nrg := self.goe.live[serial].get("nrg")) is not None]
        return sum(max(nrg, 0) for nrg in known) if known else None

    def _note_draw(self) -> None:
        """Append the site draw. One charger's update keeps the others' last known watts."""
        draw = self._site_draw()
        if draw is not None:
            self._draw_trace = note_draw(self._draw_trace, dt_util.now(), draw)

    def _report_end(self, key: str, now: datetime) -> datetime:
        state = self._state(key)
        reported = None if state is None else state.last_reported
        return now if reported is None else dt_util.as_local(reported)

    def _publication(self, now: datetime) -> tuple[int, int, int] | None:
        solar = self._watts(CONF_SOLAR_ENTITY)
        if solar is None or not (self._burst_house or self._burst_solar):
            return None
        house_w = ev = None
        steady = False
        if self._burst_house:
            house_w = self._watts(CONF_HOUSE_ENTITY)
            if house_w is not None:
                end = self._report_end(CONF_HOUSE_ENTITY, now)
                ev, steady = ev_for_window(
                    self._draw_trace,
                    end - timedelta(seconds=KOTIAKKU_AVG_S),
                    end,
                    self._watts(CONF_CONTROLLER_ENTITY),
                )
                self._log_pair(house_w, ev, steady)
        return reconciled_reading(self.memory.sample, solar, house_w, ev, steady)

    def _log_pair(self, house_w: int, ev: int | None, steady: bool) -> None:
        previous = self.memory.sample
        held = None if previous is None else previous.solar_w - previous.leftover_w
        if ev is None:
            _LOGGER.info("surplus window is not covered by the nrg trace; base stays %s W", held)
            return
        base = pair_base(house_w, ev, steady)
        if base is None:
            _LOGGER.info("surplus pair did not close: house %s W, ev avg %s W; base stays %s W", house_w, ev, held)
        elif base != held:
            _LOGGER.info("surplus base %s W (house %s W, ev avg %s W)", base, house_w, ev)

    def _schedule_wakeup(self, now: datetime, *times: datetime | None) -> None:
        if self._wakeup:
            self._wakeup()
            self._wakeup = None
        future = [t for t in times if t is not None and t > now]
        if future:
            self._wakeup = async_track_point_in_utc_time(self.hass, self._on_wakeup, min(future))

    def _usable(self, now: datetime) -> bool:
        states = [self._state(key) for key in KOTIAKKU_ENTITIES]
        numeric = all(_number(s) is not None for s in states)
        newest = max((s.last_reported for s in states if s is not None), default=None)
        usable = numeric and newest is not None and now - newest <= STALE
        if usable != self._last_usable:
            if usable:
                _LOGGER.info("Kotiakku sensors usable")
            else:
                detail = ", ".join(
                    f"{self.entity(key)}={None if s is None else s.state}" for key, s in zip(KOTIAKKU_ENTITIES, states)
                )
                _LOGGER.warning("Kotiakku sensors unusable or stale (%s): surplus holds 6 A, then stops", detail)
            self._last_usable = usable
        return usable

    def _watts(self, key: str) -> int | None:
        """Unsigned watts: sensors may report consumption or export as negative."""
        state = self._state(key)
        value = _number(state)
        if value is None:
            return None
        unit = state.attributes.get("unit_of_measurement")
        if unit in PowerConverter.VALID_UNITS:
            value = PowerConverter.convert(value, unit, UnitOfPower.WATT)
        elif state.entity_id not in self._warned_units:
            self._warned_units.add(state.entity_id)
            _LOGGER.warning("%s has no power unit (%s); assuming W", state.entity_id, unit)
        return abs(int(value))

    def _forecast_periods(self, now: datetime) -> list:
        periods = []
        for key in FORECAST_ENTITIES:
            state = self._state(key)
            raw = None if state is None else state.attributes.get("detailedForecast")
            periods += planner.forecast_periods(raw, now.tzinfo)
        return periods

    def _kwh(self, key: str) -> float | None:
        state = self._state(key)
        value = _number(state)
        if value is None:
            return None
        unit = state.attributes.get("unit_of_measurement")
        if unit in EnergyConverter.VALID_UNITS:
            return EnergyConverter.convert(value, unit, UnitOfEnergy.KILO_WATT_HOUR)
        return None if unit in PowerConverter.VALID_UNITS else value

    def _log_plan(self, plan: Plan) -> None:
        windows = [(w.start, w.end) for w in plan.windows]
        trips = [(w.start, w.end) for w in plan.trip_windows]
        if (windows, trips) != self._last_windows:
            spans = ", ".join(f"{s:%a %H:%M}–{e:%H:%M}" for s, e in windows) or "none"
            extra = ", ".join(f"{s:%a %H:%M}–{e:%H:%M}" for s, e in trips) or "none"
            _LOGGER.info("plan %s: %s, daily trip extra %s (tomorrow prices %s)", plan.reason, spans, extra, plan.tomorrow_ok)
            self._last_windows = (windows, trips)

    def _log_roles(self, decision: Decision) -> None:
        for serial, d in decision.chargers.items():
            if self._last_roles.get(serial) != d.role:
                _LOGGER.info("%s role %s → %s", serial, self._last_roles.get(serial, "none"), d.role)
                self._last_roles[serial] = d.role
