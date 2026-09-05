"""Amp-style go-e control: charge windows, surplus lot/amp/psm/frc, per-charger policy."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_utc_time,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .config import clamp_priority, entry_config
from .const import (
    CONF_CONTROLLER_ENTITY,
    CONF_CONTROLLER_IN_KW,
    CONF_HOUSE_ENTITY,
    CONF_KOTIAKKU_IN_KW,
    CONF_PRICE_ENTITY,
    CONF_SOC_ENTITY,
    CONF_SOLAR_ENTITY,
    CONF_SOLAR_REMAINING_ENTITY,
    CONF_SOLAR_TOMORROW_ENTITY,
    DEFAULT_CEILING,
    DEFAULT_FLEX_EUR,
    DEFAULT_FLEX_PCT,
    DEFAULT_ECO_LOT,
    DEFAULT_HOLD_MIN,
    DEFAULT_HOLD_MIN_W,
    DEFAULT_MAX_AMP,
    DEFAULT_MAX_HOURS,
    DEFAULT_MIN_AMP,
    DEFAULT_MIN_HOURS,
    DEFAULT_PHASE3_MIN_W,
    DEFAULT_SETTLE_S,
    DEFAULT_SOC_HYST,
    DEFAULT_SOC_ON,
    DEFAULT_SPLIT_FLOOR_W,
    DEFAULT_SPLIT_MIN_W,
    DEFAULT_START_MIN_W,
    DEFAULT_SOLAR_ENOUGH_KWH,
    DEFAULT_OFFSUN_HOUR_KWH,
    DEFAULT_VOLTS,
    EID_CEILING,
    EID_FLEX_EUR,
    EID_FLEX_PCT,
    EID_ECO_LOT,
    EID_ECO_PSM,
    EID_HOLD_MIN,
    EID_HOLD_MIN_W,
    EID_MAX,
    EID_MAX_AMP,
    EID_MIN,
    EID_MIN_AMP,
    EID_PHASE3_MIN_W,
    EID_PRICE,
    EID_SETTLE_S,
    EID_SOC_HYST,
    EID_SOC_ON,
    EID_SPLIT_FLOOR_W,
    EID_SPLIT_MIN_W,
    EID_START_MIN_W,
    EID_SOLAR_ENOUGH_KWH,
    EID_OFFSUN_HOUR_KWH,
    EID_VOLTS,
    POLICY_FORCE_OFF,
    POLICIES,
    restore_policy,
    STORAGE_KEY,
    STORAGE_VERSION,
    SURPLUS_EIDS,
    WINDOW_EIDS,
    default_charger_priority,
    priority_entity_id,
    psm_int,
    until_unplug_entity_id,
)
from .hass_hints import collect_serial_hints, device_entities
from .planner import (
    charger_full_power as policy_full_power,
    now_in_windows,
    plan,
    prev_from_result,
    until_unplug_step,
)
from .serial import resolve_car_entity_id, resolve_power_entity_id
from .surplus import (
    UNUSABLE_STATES,
    DEFAULT_LAT,
    DEFAULT_LON,
    budget,
    car_plugged,
    charger_take_w,
    effective_ev_w,
    energy_kwh,
    enough_solar as solar_enough,
    group_lot_for_allocations,
    group_lot_for_amps,
    group_surplus_setpoint,
    leftover_w,
    nrg_total_w,
    parse_lop,
    sensor_usable,
    surplus_allocation_plan,
    surplus_decision,
    surplus_hour_ranges,
    surplus_phase_budget,
    surplus_want_w,
    upcoming_solar_kwh as forecast_upcoming_kwh,
    watts,
)

_LOGGER = logging.getLogger(__name__)


def _int_prop(eid, default):
    return property(lambda self, eid=eid, default=default: self._int_entity(eid, default))


class HassClock:
    def now(self):
        return dt_util.now()

    def as_timestamp(self, value):
        return dt_util.as_timestamp(value)

    def utc_from_timestamp(self, ts):
        return dt_util.utc_from_timestamp(float(ts))

    def start_of_local_day(self, dt):
        return dt_util.start_of_local_day(dt)

    def parse_datetime(self, value):
        return dt_util.parse_datetime(value)


class KotiakkuGoeDirectController:
    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry
        data = entry_config(entry)
        self.charger_rows = list(data["chargers"])
        self.chargers = [row["serial"] for row in self.charger_rows if row.get("serial")]
        self._car_entities = {}
        self._power_entities = {}
        self._priority_defaults = {}
        for index, row in enumerate(self.charger_rows):
            serial = row.get("serial")
            if not serial:
                continue
            entity = row.get("entity") or ""
            unique_id = None
            siblings = []
            if entity:
                unique_id = collect_serial_hints(self.hass, entity).get("unique_id")
                siblings = device_entities(self.hass, entity)
            kw = {"unique_id": unique_id, "siblings": siblings}
            self._car_entities[serial] = resolve_car_entity_id(entity, serial, **kw)
            self._power_entities[serial] = resolve_power_entity_id(entity, serial, **kw)
            self._priority_defaults[serial] = clamp_priority(
                row.get("priority"), default_charger_priority(index)
            )
        self._nrg_w = {}
        self.soc_entity = data[CONF_SOC_ENTITY]
        self.solar_entity = data[CONF_SOLAR_ENTITY]
        self.house_entity = data[CONF_HOUSE_ENTITY]
        self.controller_entity = data[CONF_CONTROLLER_ENTITY]
        self.solar_remaining_entity = data.get(CONF_SOLAR_REMAINING_ENTITY, "") or ""
        self.solar_tomorrow_entity = data.get(CONF_SOLAR_TOMORROW_ENTITY, "") or ""
        self.kotiakku_in_kw = bool(data[CONF_KOTIAKKU_IN_KW])
        self.controller_in_kw = bool(data[CONF_CONTROLLER_IN_KW])
        self._config_price = data.get(CONF_PRICE_ENTITY, "") or ""
        self.clock = HassClock()
        self.window_result = None
        self.session = False
        self.split_session = False
        self.restore = {s: POLICY_FORCE_OFF for s in self.chargers}
        self.seen = {s: False for s in self.chargers}
        self.legacy_until_unplug = set()
        self._charging = False
        self._last_policy = {s: POLICY_FORCE_OFF for s in self.chargers}
        self._charge_session = {s: False for s in self.chargers}
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._listeners = []
        self._unsubs = []
        self._surplus_unsub = None
        self._floor_unsub = None
        self._split_unsub = None
        self._phase_unsub = {}
        self._phase_expired = set()
        self._boundary_unsub = None
        self._price_unsub = None
        self._tracked_price = None
        self._logged_kotiakku_unusable = False
        self._surplus_amp = {}
        self._surplus_psm = {}
        self._kotiakku_ids = {
            eid
            for eid in (
                self.soc_entity,
                self.solar_entity,
                self.house_entity,
                self.controller_entity,
            )
            if eid
        }
        self._forecast_ids = {
            eid
            for eid in (self.solar_remaining_entity, self.solar_tomorrow_entity)
            if eid
        }
        self._car_ids = {self.car_entity(s) for s in self.chargers}
        self._priority_ids = {self.priority_entity(s) for s in self.chargers}
        self._power_ids = {self.power_entity(s) for s in self.chargers}

    def listen(self, callback):
        self._listeners.append(callback)

        def _remove():
            if callback in self._listeners:
                self._listeners.remove(callback)

        return _remove

    def notify(self):
        for callback in list(self._listeners):
            callback()

    def policy_entity(self, serial):
        return f"select.kotiakku_goe_direct_policy_{serial}"

    def until_unplug_entity(self, serial):
        return until_unplug_entity_id(serial)

    def until_unplug(self, serial):
        return str(self._state(self.until_unplug_entity(serial)) or "").lower() == "on"

    def car_entity(self, serial):
        return self._car_entities.get(serial) or f"sensor.go_echarger_{serial}_car_state"

    def priority_entity(self, serial):
        return priority_entity_id(serial)

    def charger_priority(self, serial):
        parsed = parse_lop(self._state(self.priority_entity(serial)))
        if parsed is not None:
            return parsed
        return self._priority_defaults.get(serial)

    def power_entity(self, serial):
        return self._power_entities.get(serial) or f"sensor.go_echarger_{serial}_nrg"

    def charger_power_w(self, serial):
        mqtt_w = self._nrg_w.get(serial)
        if mqtt_w is not None:
            return mqtt_w
        entity = self.power_entity(serial)
        st = self.hass.states.get(entity) if entity else None
        if st is None:
            return None
        unit = str((st.attributes or {}).get("unit_of_measurement") or "").lower()
        parsed = nrg_total_w(st.state)
        if parsed is None:
            return None
        if unit == "kw" or unit == "kwatt":
            return parsed * 1000
        return parsed

    def _charger_nrg_sum(self):
        total = 0
        seen = False
        for serial in self.chargers:
            power_w = self.charger_power_w(serial)
            if power_w is None:
                continue
            seen = True
            total += max(int(power_w), 0)
        return total if seen else None

    def price_entity_id(self):
        return self._text_entity(EID_PRICE, str(self._config_price).strip())

    def _ha_state(self, entity_id):
        if not entity_id:
            return None
        return self.hass.states.get(entity_id)

    def _state(self, entity_id):
        st = self._ha_state(entity_id)
        return None if st is None else st.state

    def _blank_state(self, state):
        return state is None or str(state).strip().lower() in UNUSABLE_STATES

    def _text_entity(self, entity_id, default=""):
        state = self._state(entity_id)
        if self._blank_state(state):
            return default
        return str(state).strip()

    def _float_entity(self, entity_id, default):
        state = self._state(entity_id)
        if not sensor_usable(state):
            return default
        return float(state)

    def _int_entity(self, entity_id, default):
        return int(round(self._float_entity(entity_id, default)))

    soc_on = _int_prop(EID_SOC_ON, DEFAULT_SOC_ON)
    soc_hyst = _int_prop(EID_SOC_HYST, DEFAULT_SOC_HYST)
    start_min_w = _int_prop(EID_START_MIN_W, DEFAULT_START_MIN_W)
    split_min_w = _int_prop(EID_SPLIT_MIN_W, DEFAULT_SPLIT_MIN_W)
    split_floor_w = _int_prop(EID_SPLIT_FLOOR_W, DEFAULT_SPLIT_FLOOR_W)
    hold_min_w = _int_prop(EID_HOLD_MIN_W, DEFAULT_HOLD_MIN_W)
    settle_s = _int_prop(EID_SETTLE_S, DEFAULT_SETTLE_S)
    hold_min = _int_prop(EID_HOLD_MIN, DEFAULT_HOLD_MIN)
    volts = _int_prop(EID_VOLTS, DEFAULT_VOLTS)
    min_amp = _int_prop(EID_MIN_AMP, DEFAULT_MIN_AMP)
    max_amp = _int_prop(EID_MAX_AMP, DEFAULT_MAX_AMP)
    phase3_min_w = _int_prop(EID_PHASE3_MIN_W, DEFAULT_PHASE3_MIN_W)
    eco_lot = _int_prop(EID_ECO_LOT, DEFAULT_ECO_LOT)

    @property
    def eco_psm(self):
        return psm_int(self._state(EID_ECO_PSM))

    async def async_knobs_changed(self):
        self._schedule_surplus()
        await self.async_charge()

    def policy(self, serial):
        state = restore_policy(self._state(self.policy_entity(serial)))
        if state not in POLICIES:
            return POLICY_FORCE_OFF
        return state

    def _now_ts(self):
        return float(self.clock.as_timestamp(self.clock.now()))

    def charger_full_power(self, serial):
        return policy_full_power(
            self.policy(serial),
            self.window_result,
            self._now_ts(),
            enough_solar=self.enough_solar,
            until_unplug=self.until_unplug(serial),
        )

    def any_charger_full_power(self):
        return any(self.charger_full_power(s) for s in self.chargers)

    def window_active(self):
        result = self.window_result or {}
        return now_in_windows(result.get("raw_windows") or [], self._now_ts())

    def _forecast_kwh(self, entity_id):
        st = self._ha_state(entity_id)
        if st is None:
            return None
        unit = None if st.attributes is None else st.attributes.get("unit_of_measurement")
        return energy_kwh(st.state, unit)

    @property
    def remaining_today_kwh(self):
        return self._forecast_kwh(self.solar_remaining_entity)

    @property
    def tomorrow_kwh(self):
        return self._forecast_kwh(self.solar_tomorrow_entity)

    @property
    def upcoming_solar_kwh(self):
        return forecast_upcoming_kwh(self.remaining_today_kwh, self.tomorrow_kwh)

    @property
    def solar_enough_kwh(self):
        return self._float_entity(EID_SOLAR_ENOUGH_KWH, DEFAULT_SOLAR_ENOUGH_KWH)

    @property
    def offsun_hour_kwh(self):
        return self._float_entity(EID_OFFSUN_HOUR_KWH, DEFAULT_OFFSUN_HOUR_KWH)

    @property
    def enough_solar(self):
        return solar_enough(self.upcoming_solar_kwh, self.solar_enough_kwh)

    def _site_lat_lon(self):
        try:
            lat = float(self.hass.config.latitude)
            lon = float(self.hass.config.longitude)
        except (TypeError, ValueError, AttributeError):
            return DEFAULT_LAT, DEFAULT_LON
        return lat, lon

    @property
    def surplus_hours(self):
        return list((self.window_result or {}).get("blocked") or [])

    async def async_setup(self):
        stored = await self._store.async_load()
        if stored:
            self.session = bool(stored.get("session"))
            self.split_session = bool(stored.get("split_session"))
            self.restore.update(stored.get("restore") or {})
            self.seen.update(stored.get("seen") or {})
            self._charge_session.update(stored.get("charge_session") or {})
        track = [
            self.soc_entity,
            self.solar_entity,
            self.house_entity,
            self.controller_entity,
            self.solar_remaining_entity,
            self.solar_tomorrow_entity,
        ]
        track.extend(WINDOW_EIDS)
        track.extend(SURPLUS_EIDS)
        track.extend(self.policy_entity(s) for s in self.chargers)
        track.extend(self.until_unplug_entity(s) for s in self.chargers)
        track.extend(self.car_entity(s) for s in self.chargers)
        track.extend(self.priority_entity(s) for s in self.chargers)
        track.extend(self.power_entity(s) for s in self.chargers)
        track = list(dict.fromkeys(entity for entity in track if entity))
        self._unsubs.append(
            async_track_state_change_event(self.hass, track, self._on_state)
        )
        self._unsubs.append(
            async_track_time_interval(self.hass, self._on_interval, timedelta(minutes=15))
        )
        self._unsubs.append(
            async_track_time_change(
                self.hass, self._on_interval, hour=0, minute=0, second=30
            )
        )
        self._retarget_price()
        await self._subscribe_nrg_mqtt()
        await self._migrate_legacy_until_unplug()
        await self.async_plan()
        self._schedule_surplus()
        await self.async_charge()

    def _cancel(self, attr):
        unsub = getattr(self, attr)
        if unsub:
            unsub()
            setattr(self, attr, None)

    async def async_unload(self):
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for attr in (
            "_surplus_unsub",
            "_floor_unsub",
            "_split_unsub",
            "_boundary_unsub",
            "_price_unsub",
        ):
            self._cancel(attr)
        for serial in list(self._phase_unsub):
            self._arm_phase(serial, False)

    async def _save(self):
        await self._store.async_save(
            {
                "session": self.session,
                "split_session": self.split_session,
                "restore": self.restore,
                "seen": self.seen,
                "charge_session": self._charge_session,
            }
        )

    def _retarget_price(self):
        entity = self.price_entity_id()
        if entity == self._tracked_price:
            return
        self._cancel("_price_unsub")
        self._tracked_price = entity or None
        if entity:
            self._price_unsub = async_track_state_change_event(
                self.hass, [entity], self._on_price
            )

    async def _on_state(self, event):
        entity = event.data.get("entity_id")
        if entity in self._kotiakku_ids:
            self._schedule_surplus()
            return
        if entity in self._forecast_ids:
            await self.async_plan()
            await self.async_charge()
            self._schedule_surplus()
            return
        if entity in WINDOW_EIDS:
            self._retarget_price()
            await self.async_plan()
            await self.async_charge()
            return
        if entity in SURPLUS_EIDS:
            if entity in (EID_SOLAR_ENOUGH_KWH, EID_OFFSUN_HOUR_KWH):
                await self.async_plan()
            await self.async_knobs_changed()
            return
        if entity and entity.startswith("select.kotiakku_goe_direct_policy_"):
            serial = entity.rsplit("_", 1)[-1]
            await self._on_policy(serial, event)
            return
        if entity and entity.startswith("switch.kotiakku_goe_direct_until_unplug_"):
            await self.async_charge()
            self._schedule_surplus()
            return
        if entity in self._car_ids:
            await self.async_charge()
            self._schedule_surplus()
            return
        if entity in self._priority_ids or entity in self._power_ids:
            self._schedule_surplus()

    async def _on_price(self, _event):
        await self.async_plan()
        await self.async_charge()

    async def _on_interval(self, _now=None):
        await self.async_plan()
        await self.async_charge()

    async def _on_policy(self, serial, event):
        await self.async_charge()
        self._schedule_surplus()

    def _schedule_surplus(self):
        self._cancel("_surplus_unsub")
        self._surplus_unsub = async_call_later(
            self.hass, self.settle_s, self._surplus_later
        )

    async def _surplus_later(self, _now=None):
        self._surplus_unsub = None
        await self.async_surplus(floor_expired=False)

    def _arm_hold(self, attr, fire, need):
        if not need:
            self._cancel(attr)
            return
        if getattr(self, attr):
            return
        setattr(self, attr, async_call_later(self.hass, self.hold_min * 60, fire))

    def _arm_floor(self, need):
        self._arm_hold("_floor_unsub", self._floor_fire, need)

    async def _floor_fire(self, _now=None):
        self._floor_unsub = None
        await self.async_surplus(floor_expired=True)

    def _arm_split(self, need):
        self._arm_hold("_split_unsub", self._split_fire, need)

    async def _split_fire(self, _now=None):
        self._split_unsub = None
        await self.async_surplus(split_expired=True)

    def _arm_phase(self, serial, need):
        if not serial:
            return
        if not need:
            unsub = self._phase_unsub.pop(serial, None)
            if unsub:
                unsub()
            self._phase_expired.discard(serial)
            return
        if serial in self._phase_unsub or serial in self._phase_expired:
            return
        _LOGGER.info(
            "kotiakku_goe_direct: holding psm on %s for %s min (CCS/Tesla phase switch pauses charging)",
            serial,
            self.hold_min,
        )

        async def _fire(_now=None, serial=serial):
            self._phase_unsub.pop(serial, None)
            self._phase_expired.add(serial)
            await self.async_surplus()

        self._phase_unsub[serial] = async_call_later(
            self.hass, self.hold_min * 60, _fire
        )

    def _schedule_boundaries(self):
        self._cancel("_boundary_unsub")
        now_ts = self._now_ts()
        times = []
        for w in (self.window_result or {}).get("raw_windows") or []:
            if w["start"] > now_ts:
                times.append(w["start"])
            if w["end"] > now_ts:
                times.append(w["end"])
        if not times:
            return
        when = self.clock.utc_from_timestamp(min(times))
        self._boundary_unsub = async_track_point_in_utc_time(
            self.hass, self._on_boundary, when
        )

    async def _on_boundary(self, _now=None):
        self._boundary_unsub = None
        self.notify()
        await self.async_charge()
        self._schedule_surplus()
        self._schedule_boundaries()

    async def async_plan(self):
        price_entity = self.price_entity_id()
        source = self.hass.states.get(price_entity) if price_entity else None
        attrs = None if source is None else dict(source.attributes)
        min_hours = self._float_entity(EID_MIN, DEFAULT_MIN_HOURS)
        max_hours = self._float_entity(EID_MAX, DEFAULT_MAX_HOURS)
        ceiling = self._float_entity(EID_CEILING, DEFAULT_CEILING)
        flex_pct = self._float_entity(EID_FLEX_PCT, DEFAULT_FLEX_PCT)
        flex_euro = self._float_entity(EID_FLEX_EUR, DEFAULT_FLEX_EUR)
        lat, lon = self._site_lat_lon()
        blocked = surplus_hour_ranges(
            self.clock,
            self.remaining_today_kwh,
            self.tomorrow_kwh,
            self.offsun_hour_kwh,
            lat,
            lon,
        )
        prev = prev_from_result(self.clock, self.window_result)
        self.window_result = plan(
            self.clock,
            attrs,
            min_hours=min_hours,
            max_hours=max_hours,
            ceiling=ceiling,
            flex_pct=flex_pct,
            flex_euro=flex_euro,
            prev=prev,
            source_entity=price_entity,
            blocked=blocked,
            remaining_today=self.remaining_today_kwh,
            tomorrow_kwh=self.tomorrow_kwh,
        )
        _LOGGER.info(
            "kotiakku_goe_direct plan reason=%s count=%s",
            self.window_result["reason"],
            self.window_result["count"],
        )
        self._schedule_boundaries()
        self.notify()

    def _kotiakku_problems(self):
        parts = []
        for label, entity_id in (
            ("SoC", self.soc_entity),
            ("solar", self.solar_entity),
            ("house", self.house_entity),
        ):
            st = self._ha_state(entity_id)
            state = None if st is None else st.state
            if sensor_usable(state):
                continue
            eid = entity_id or "(unset)"
            shown = "missing" if st is None else state
            parts.append("%s %s=%s" % (label, eid, shown))
        return parts

    def _log_kotiakku_unusable(self, problems, *, stopping):
        detail = ", ".join(problems) if problems else "SoC/solar/house"
        if stopping:
            _LOGGER.warning(
                "kotiakku_goe_direct: Kotiakku sensors still unusable after %s min (%s); stopping surplus",
                self.hold_min,
                detail,
            )
            self._logged_kotiakku_unusable = False
            return
        if self._logged_kotiakku_unusable:
            return
        _LOGGER.warning(
            "kotiakku_goe_direct: Kotiakku sensors unusable (%s); holding 6 A for %s min",
            detail,
            self.hold_min,
        )
        self._logged_kotiakku_unusable = True

    async def _subscribe_nrg_mqtt(self):
        try:
            from homeassistant.components.mqtt import async_subscribe
        except Exception:
            return
        for serial in self.chargers:
            topic = f"go-eCharger/{serial}/nrg"
            try:
                unsub = await async_subscribe(self.hass, topic, self._on_nrg_mqtt)
            except Exception as err:
                _LOGGER.debug("kotiakku_goe_direct: mqtt subscribe %s failed: %s", topic, err)
                continue
            self._unsubs.append(unsub)

    def _mqtt_serial_payload(self, msg, key):
        topic = str(getattr(msg, "topic", "") or "")
        parts = topic.split("/")
        if len(parts) < 3 or parts[-1] != key:
            return None, None
        serial = parts[1]
        if serial not in self.chargers:
            return None, None
        payload = getattr(msg, "payload", None)
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8", "replace")
        return serial, payload

    def _on_nrg_mqtt(self, msg):
        serial, payload = self._mqtt_serial_payload(msg, "nrg")
        if not serial:
            return
        value = nrg_total_w(payload)
        old = self._nrg_w.get(serial)
        self._nrg_w[serial] = value
        if old != value:
            self._schedule_surplus()

    def _snapshot(self):
        problems = self._kotiakku_problems()
        controller_state = self._state(self.controller_entity)
        controller_usable = sensor_usable(controller_state)
        controller_w = watts(controller_state, self.controller_in_kw) if controller_usable else 0
        solar_w = watts(self._state(self.solar_entity), self.kotiakku_in_kw)
        house_w = watts(self._state(self.house_entity), self.kotiakku_in_kw)
        ev_w = effective_ev_w(
            controller_w,
            self._charger_nrg_sum(),
            controller_usable=controller_usable,
        )
        return {
            "window_ok": not problems,
            "soc": self._float_entity(self.soc_entity, -1.0),
            "solar_w": solar_w,
            "house_w": house_w,
            "available_w": leftover_w(solar_w, house_w, ev_w),
            "problems": problems,
        }

    async def _stop_surplus(self):
        for serial in self.chargers:
            if self.charger_full_power(serial):
                continue
            await self._publish_off(serial)
        self.session = False
        self.split_session = False
        self._arm_split(False)
        await self._save()

    async def async_surplus(self, floor_expired=False, split_expired=False):
        snap = self._snapshot()
        dec = surplus_decision(
            self.session,
            snap["available_w"],
            snap["soc"],
            window_ok=snap["window_ok"],
            soc_on=self.soc_on,
            soc_hyst=self.soc_hyst,
            start_min_w=self.start_min_w,
            hold_min_w=self.hold_min_w,
            floor_expired=floor_expired,
            hold_active=self._floor_unsub is not None,
            hold_exit_w=self.start_min_w,
        )
        unusable = not snap["window_ok"]
        if unusable and (self.session or dec["write_on"] or dec["write_off"]):
            self._log_kotiakku_unusable(
                snap["problems"], stopping=bool(dec["write_off"] and floor_expired)
            )
        elif not unusable and self._logged_kotiakku_unusable:
            _LOGGER.info("kotiakku_goe_direct: Kotiakku sensors usable again")
            self._logged_kotiakku_unusable = False
        self._arm_floor(dec["arm_floor"])
        if not dec["write_on"] and not dec["write_off"]:
            if self._surplus_amp or self.session:
                await self._stop_surplus()
            return
        if dec["write_on"]:
            target_w = 0 if dec["use_floor_budget"] else snap["available_w"]
            lot, psm, amp = budget(
                target_w,
                self.min_amp,
                self.max_amp,
                self.eco_lot,
                self.volts,
                self.phase3_min_w,
            )
            n_full = sum(1 for serial in self.chargers if self.charger_full_power(serial))
            lot, psm, amp = group_surplus_setpoint(
                lot,
                psm,
                amp,
                n_full=n_full,
                eco_lot=self.eco_lot,
            )
            self.session = True
            await self._save()
            surplus = [serial for serial in self.chargers if not self.charger_full_power(serial)]
            lops = {serial: self.charger_priority(serial) for serial in surplus}
            plugged = {}
            states = {}
            take_w = {}
            charger_max_w = self.max_amp * self.volts * 3
            for serial in surplus:
                state = self._state(self.car_entity(serial))
                states[serial] = state
                plugged[serial] = car_plugged(state)
                take_w[serial] = surplus_want_w(
                    snap["available_w"],
                    charger_take_w(
                        state,
                        self.charger_power_w(serial),
                        snap["available_w"],
                        charger_max_w,
                    ),
                    last_amp=self._surplus_amp.get(serial),
                    last_psm=self._surplus_psm.get(serial),
                    volts=self.volts,
                    min_amp=self.min_amp,
                    max_amp=self.max_amp,
                    eco_lot=self.eco_lot,
                    phase3_min_w=self.phase3_min_w,
                )
            alloc_w = snap["available_w"]
            if dec["use_floor_budget"]:
                alloc_w = max(alloc_w, self.min_amp * self.volts)
            allocations = surplus_allocation_plan(
                surplus,
                lops=lops,
                plugged=plugged,
                leftover_w=alloc_w,
                split_min_w=self.split_min_w,
                charger_max_w=charger_max_w,
                take_w=take_w,
                states=states,
                min_amp=self.min_amp,
                volts=self.volts,
                phase3_min_w=self.phase3_min_w,
                split_floor_w=self.split_floor_w,
                split_hold=self.split_session,
                split_expired=split_expired,
            )
            self.split_session = len(allocations["allocations"]) >= 2
            self._arm_split(allocations["arm_split_hold"])
            await self._save()
            if not dec["use_floor_budget"]:
                lot = group_lot_for_allocations(
                    lot,
                    allocations["allocations"],
                    min_amp=self.min_amp,
                    max_amp=self.max_amp,
                    eco_lot=self.eco_lot,
                    volts=self.volts,
                    phase3_min_w=self.phase3_min_w,
                )
            targets = {}
            for serial in surplus:
                watts_i = allocations["allocations"].get(serial)
                if watts_i is None:
                    await self._publish_off(serial)
                    continue
                source_w = target_w if dec["use_floor_budget"] else min(
                    int(watts_i), max(int(snap["available_w"]), 0)
                )
                # psm may be held 15 min; amp is leftover on that held phase.
                pub = surplus_phase_budget(
                    source_w,
                    self.min_amp,
                    self.max_amp,
                    self.eco_lot,
                    self.volts,
                    self.phase3_min_w,
                    last_psm=self._surplus_psm.get(serial),
                    hold_expired=serial in self._phase_expired,
                )
                targets[serial] = pub
                self._arm_phase(serial, pub["arm_phase"])
            if n_full <= 0:
                lot = group_lot_for_amps(
                    lot, [pub["amp"] for pub in targets.values()], self.eco_lot
                )
            for serial, pub in targets.items():
                await self._publish_on(serial, pub["psm"], lot, pub["amp"])
            return
        await self._stop_surplus()

    async def async_charge(self):
        if self._charging:
            return
        self._charging = True
        try:
            await self._async_charge()
        finally:
            self._charging = False

    async def _async_charge(self):
        changed = False
        for serial in self.chargers:
            car_state = self._state(self.car_entity(serial))
            plugged = car_plugged(car_state)
            override = self.until_unplug(serial)
            new_on, new_seen = until_unplug_step(
                override, plugged, self.seen.get(serial)
            )
            if bool(self.seen.get(serial)) != new_seen:
                changed = True
            self.seen[serial] = new_seen
            if new_on != override:
                await self._turn_until_unplug(serial, new_on)
                changed = True
            want_on = policy_full_power(
                self.policy(serial),
                self.window_result,
                self._now_ts(),
                enough_solar=self.enough_solar,
                until_unplug=new_on,
            )
            want_off = self._charge_session.get(serial) and not want_on
            if want_on:
                if not self._charge_session.get(serial):
                    changed = True
                self._charge_session[serial] = True
                self._arm_phase(serial, False)
                await self._publish_on(serial, 2, self.eco_lot, self.max_amp)
            elif want_off:
                await self._publish_off(serial)
                self._charge_session[serial] = False
                changed = True
            self._last_policy[serial] = self.policy(serial)
        if changed:
            await self._save()
            self.notify()

    async def _migrate_legacy_until_unplug(self):
        for serial in list(self.legacy_until_unplug):
            restore_to = self.restore.get(serial, POLICY_FORCE_OFF)
            if restore_to not in POLICIES:
                restore_to = POLICY_FORCE_OFF
            if self.policy(serial) != restore_to:
                await self._select_policy(serial, restore_to)
            if not self.until_unplug(serial):
                await self._turn_until_unplug(serial, True)

    async def _turn_until_unplug(self, serial, on):
        entity = self.until_unplug_entity(serial)
        if self.hass.states.get(entity) is None:
            return
        await self.hass.services.async_call(
            "switch",
            "turn_on" if on else "turn_off",
            {"entity_id": entity},
            blocking=True,
        )

    async def _select_policy(self, serial, option):
        entity = self.hass.states.get(self.policy_entity(serial))
        if entity is None:
            return
        await self.hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": self.policy_entity(serial), "option": option},
            blocking=True,
        )

    async def _publish_on(self, serial, psm, lot, amp):
        await self._mqtt_many(
            serial,
            (("fup", "false"), ("psm", psm), ("lot", lot), ("amp", amp), ("frc", "2")),
        )
        if serial:
            self._surplus_psm[serial] = int(psm)
            self._surplus_amp[serial] = int(amp)

    async def _publish_off(self, serial):
        await self._mqtt_many(
            serial,
            (
                ("frc", "0"),
                ("fup", "false"),
                ("psm", self.eco_psm),
                ("lot", self.eco_lot),
                ("amp", self.max_amp),
            ),
        )
        if serial:
            self._arm_phase(serial, False)
            self._surplus_psm.pop(serial, None)
            self._surplus_amp.pop(serial, None)

    async def _mqtt_many(self, serial, pairs):
        for key, payload in pairs:
            await self._mqtt(serial, key, payload)

    async def _mqtt(self, serial, key, payload):
        if not serial:
            return
        try:
            from homeassistant.components.mqtt import async_publish
        except Exception:
            _LOGGER.warning("kotiakku_goe_direct: mqtt not available")
            return
        topic = f"go-eCharger/{serial}/{key}/set"
        try:
            await async_publish(self.hass, topic, str(payload), 0, False)
        except Exception as err:
            _LOGGER.warning("kotiakku_goe_direct: mqtt %s failed: %s", topic, err)
