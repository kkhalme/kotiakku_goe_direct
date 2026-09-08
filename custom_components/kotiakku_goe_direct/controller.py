"""Amp-style go-e control: charge windows, surplus lot/amp/psm/frc, per-charger policy."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState
from homeassistant.helpers.entity_component import async_update_entity
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_utc_time,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .config import clamp_priority, entry_config, source_refresh_ids
from .const import (
    CONF_CONTROLLER_ENTITY,
    CONF_CONTROLLER_IN_KW,
    CONF_HOUSE_ENTITY,
    CONF_KOTIAKKU_IN_KW,
    CONF_PRICE_ENTITY,
    CONF_SOC_ENTITY,
    CONF_SOLAR_ENTITY,
    CONF_SOLAR_TODAY_ENTITY,
    CONF_SOLAR_TOMORROW_ENTITY,
    DEFAULT_CEILING,
    DEFAULT_FLEX_EUR,
    DEFAULT_FLEX_PCT,
    DEFAULT_GROUP_LOT,
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
    EID_GROUP_LOT,
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
    EID_KEEP_AMP,
    EID_KEEP_PHASE,
    POLICY_FORCE_ON,
    POLICY_FORCE_OFF,
    POLICIES,
    DEFAULT_KEEP_AMP,
    DEFAULT_KEEP_PHASE,
    charger_off_mqtt,
    charger_on_mqtt,
    keep_phase_psm,
    restore_policy,
    STORAGE_KEY,
    STORAGE_VERSION,
    SURPLUS_EIDS,
    WINDOW_EIDS,
    default_charger_priority,
    after_charge_complete_keep_enable_entity_id,
    after_charge_complete_keep_entity_id,
    priority_entity_id,
    until_unplug_entity_id,
)
from .hass_hints import collect_serial_hints, device_entities
from .planner import (
    MQTT_APPLY_S,
    charger_full_power as policy_full_power,
    charger_mqtt_command,
    charger_mqtt_live_complete,
    charger_mqtt_needs_update,
    charger_mqtt_role,
    charger_mqtt_status_value,
    charger_surplus as policy_surplus,
    mqtt_apply_window_action,
    now_in_windows,
    plan,
    tomorrow_prices_ok as planner_tomorrow_prices_ok,
    keep_until_unplug_step,
    restore_keep_phase,
    until_unplug_step,
    KEEP_IDLE,
    KEEP_CUT,
    ROLE_FULL,
    ROLE_KEEP,
    ROLE_SURPLUS,
)
from .serial import resolve_car_entity_id, resolve_power_entity_id
from .surplus import (
    UNUSABLE_STATES,
    DEFAULT_LAT,
    DEFAULT_LON,
    OFFER_WAIT_S,
    TAKE_MIN_W,
    budget,
    car_finished,
    car_plugged,
    charger_take_w,
    effective_ev_w,
    energy_kwh,
    enough_solar_now as solar_enough_now,
    gating_solar_day,
    gating_solar_kwh as forecast_gating_kwh,
    last_sun_end_ts as forecast_last_sun_end,
    last_usable_solar_end_ts as forecast_last_usable_end,
    leftover_w,
    leftover_for_surplus,
    group_lot_for_allocations,
    group_lot_for_amps,
    group_surplus_setpoint,
    nrg_total_w,
    parse_lop,
    sensor_usable,
    surplus_allocation_plan,
    surplus_decision,
    surplus_higher_keep_on,
    surplus_hour_ranges,
    surplus_phase_budget,
    surplus_want_w,
    upcoming_solar_kwh as forecast_upcoming_kwh,
    watts,
)

_LOGGER = logging.getLogger(__name__)


def _mqtt_cmd_text(cmd):
    if cmd is None:
        return "noop"
    if not cmd or cmd[0] == "off":
        return "off frc=1"
    if cmd[0] == "on" and len(cmd) >= 4:
        return "on psm=%s lot=%s amp=%s" % (cmd[1], cmd[2], cmd[3])
    return str(cmd)


def _roles_text(roles):
    if not roles:
        return "-"
    return " ".join("%s=%s" % item for item in roles.items())


def _event_states(event):
    old = event.data.get("old_state")
    new = event.data.get("new_state")
    old_s = None if old is None else old.state
    new_s = None if new is None else new.state
    return old_s, new_s


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
        self.solar_today_entity = data.get(CONF_SOLAR_TODAY_ENTITY, "") or ""
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
        self._keep_min = {s: False for s in self.chargers}
        self._keep_min_seen = {s: False for s in self.chargers}
        self._keep_min_phase = {s: KEEP_IDLE for s in self.chargers}
        self._charging = False
        self._apply_again = False
        self._pending_floor = False
        self._pending_split = False
        self._pending_force = False
        self._last_policy = {s: POLICY_FORCE_OFF for s in self.chargers}
        self._charge_session = {s: False for s in self.chargers}
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._listeners = []
        self._unsubs = []
        self._apply_unsub = None
        self._floor_unsub = None
        self._split_unsub = None
        self._phase_unsub = {}
        self._phase_expired = set()
        self._offer_unsub = {}
        self._offer_expired = set()
        self._boundary_unsub = None
        self._price_unsub = None
        self._tracked_price = None
        self._logged_kotiakku_unusable = False
        self._last_roles = {}
        self._last_window_active = None
        self._last_enough_solar = None
        self._last_gating_day = None
        self._last_surplus_w = None
        self._refreshing = False
        self._surplus_amp = {}
        self._surplus_psm = {}
        self._last_mqtt = {}
        self._charger_mqtt = {}
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
            for eid in (self.solar_today_entity, self.solar_tomorrow_entity)
            if eid
        }
        self._car_ids = {self.car_entity(s) for s in self.chargers}
        self._priority_ids = {self.priority_entity(s) for s in self.chargers}
        self._power_ids = {self.power_entity(s) for s in self.chargers}
        self._until_unplug_ids = {self.until_unplug_entity(s) for s in self.chargers}
        self._keep_enable_ids = {self.keep_min_enable_entity(s) for s in self.chargers}
        self._keep_ids = {self.keep_min_entity(s) for s in self.chargers}

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

    def keep_min_entity(self, serial):
        return after_charge_complete_keep_entity_id(serial)

    def keep_min_enable_entity(self, serial):
        return after_charge_complete_keep_enable_entity_id(serial)

    def keep_min(self, serial):
        return str(self._state(self.keep_min_entity(serial)) or "").lower() == "on"

    def keep_min_enable(self, serial):
        """True unless the per-charger enable switch is explicitly off."""
        state = str(self._state(self.keep_min_enable_entity(serial)) or "on").lower()
        return state != "off"

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

    def _charger_log(self, serial):
        nrg = self.charger_power_w(serial)
        nrg_s = "unknown" if nrg is None else "%sW" % nrg
        return "policy=%s car=%s nrg=%s" % (
            self.policy(serial),
            self._state(self.car_entity(serial)),
            nrg_s,
        )

    def _log_gate_changes(self):
        active = self.window_active()
        if self._last_window_active is not None and active != self._last_window_active:
            _LOGGER.info(
                "kotiakku_goe_direct: cheap window %s",
                "started" if active else "ended",
            )
        self._last_window_active = active
        enough = self.enough_solar
        if self._last_enough_solar is not None and enough != self._last_enough_solar:
            _LOGGER.info(
                "kotiakku_goe_direct: enough-solar %s (gating %s %s kWh)",
                "on" if enough else "off",
                self.gating_solar_day,
                self.gating_solar_kwh,
            )
        self._last_enough_solar = enough
        day = self.gating_solar_day
        if self._last_gating_day is not None and day != self._last_gating_day:
            _LOGGER.info(
                "kotiakku_goe_direct: solar gate now %s (%s kWh)",
                day,
                self.gating_solar_kwh,
            )
        self._last_gating_day = day

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
    group_lot = _int_prop(EID_GROUP_LOT, DEFAULT_GROUP_LOT)

    @property
    def keep_amp(self):
        value = self._int_entity(EID_KEEP_AMP, DEFAULT_KEEP_AMP)
        if value < 6:
            return 6
        if value > 32:
            return 32
        return value

    @property
    def keep_psm(self):
        return keep_phase_psm(self._text_entity(EID_KEEP_PHASE, DEFAULT_KEEP_PHASE))

    async def async_knobs_changed(self):
        self._schedule_apply()

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

    def surplus_allowed(self, serial):
        return policy_surplus(
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
    def today_kwh(self):
        """Today's full-day solar production estimate."""
        return self._forecast_kwh(self.solar_today_entity)

    @property
    def tomorrow_kwh(self):
        return self._forecast_kwh(self.solar_tomorrow_entity)

    @property
    def upcoming_solar_kwh(self):
        return forecast_upcoming_kwh(self.today_kwh, self.tomorrow_kwh)

    @property
    def solar_enough_kwh(self):
        return self._float_entity(EID_SOLAR_ENOUGH_KWH, DEFAULT_SOLAR_ENOUGH_KWH)

    @property
    def offsun_hour_kwh(self):
        return self._float_entity(EID_OFFSUN_HOUR_KWH, DEFAULT_OFFSUN_HOUR_KWH)

    @property
    def tomorrow_prices_ok(self):
        """True when the next day's unclipped spot curve is present."""
        price_entity = self.price_entity_id()
        source = self.hass.states.get(price_entity) if price_entity else None
        attrs = None if source is None else dict(source.attributes)
        return planner_tomorrow_prices_ok(self.clock, attrs)

    @property
    def enough_solar(self):
        lat, lon = self._site_lat_lon()
        return solar_enough_now(
            self.clock,
            self.today_kwh,
            self.tomorrow_kwh,
            self.solar_enough_kwh,
            lat,
            lon,
            self.offsun_hour_kwh,
            self.tomorrow_prices_ok,
        )

    @property
    def gating_solar_kwh(self):
        lat, lon = self._site_lat_lon()
        return forecast_gating_kwh(
            self.clock,
            self.today_kwh,
            self.tomorrow_kwh,
            lat,
            lon,
            self.offsun_hour_kwh,
            self.tomorrow_prices_ok,
        )

    @property
    def gating_solar_day(self):
        lat, lon = self._site_lat_lon()
        return gating_solar_day(
            self.clock,
            self.today_kwh,
            self.offsun_hour_kwh,
            lat,
            lon,
            self.tomorrow_prices_ok,
        )

    def _today_start_end(self):
        now = self.clock.now()
        today_start = self.clock.start_of_local_day(now)
        return today_start, today_start + timedelta(days=1)

    def _iso_from_ts(self, ts):
        if ts is None:
            return None
        try:
            return self.clock.utc_from_timestamp(ts).isoformat()
        except Exception:
            return None

    @property
    def sunset_iso(self):
        """Exclusive end of today's last sun, ISO UTC, or None on polar night."""
        lat, lon = self._site_lat_lon()
        try:
            today_start, today_end = self._today_start_end()
            ts = forecast_last_sun_end(self.clock, today_start, today_end, lat, lon)
        except Exception:
            return None
        return self._iso_from_ts(ts)

    @property
    def usable_solar_end_iso(self):
        """Exclusive end of today's last usable solar hour, ISO UTC, or None."""
        lat, lon = self._site_lat_lon()
        try:
            today_start, today_end = self._today_start_end()
            ts = forecast_last_usable_end(
                self.clock,
                today_start,
                today_end,
                self.today_kwh,
                self.offsun_hour_kwh,
                lat,
                lon,
            )
        except Exception:
            return None
        return self._iso_from_ts(ts)

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

    def _source_refresh_ids(self) -> list[str]:
        extras = [self.price_entity_id()]
        extras.extend(self.car_entity(serial) for serial in self.chargers)
        extras.extend(self.power_entity(serial) for serial in self.chargers)
        return source_refresh_ids(
            (
                self.soc_entity,
                self.solar_entity,
                self.house_entity,
                self.controller_entity,
                self.solar_today_entity,
                self.solar_tomorrow_entity,
            ),
            extras,
        )

    async def _refresh_one_entity(self, entity_id: str) -> None:
        if self.hass.states.get(entity_id) is None:
            return
        try:
            await async_update_entity(self.hass, entity_id)
        except Exception as exc:
            _LOGGER.debug(
                "kotiakku_goe_direct: update %s at start failed: %s",
                entity_id,
                exc,
            )

    async def _refresh_source_entities(self) -> None:
        """Ask wired sensors to fetch so the first plan is not leftover restore."""
        entity_ids = self._source_refresh_ids()
        if not entity_ids:
            return
        self._refreshing = True
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *(self._refresh_one_entity(eid) for eid in entity_ids),
                    return_exceptions=True,
                ),
                timeout=20,
            )
        except TimeoutError:
            _LOGGER.debug("kotiakku_goe_direct: source refresh timed out at start")
        finally:
            self._refreshing = False

    async def _on_hass_started(self, _event=None):
        _LOGGER.debug("kotiakku_goe_direct: Home Assistant started, refresh and replan")
        await self._refresh_source_entities()
        await self.async_plan()
        self._schedule_apply()

    async def async_setup(self):
        stored = await self._store.async_load()
        if stored:
            self.session = bool(stored.get("session"))
            self.split_session = bool(stored.get("split_session"))
            self.restore.update(stored.get("restore") or {})
            self.seen.update(stored.get("seen") or {})
            self._charge_session.update(stored.get("charge_session") or {})
            self._keep_min.update(
                {k: bool(v) for k, v in (stored.get("keep_min") or {}).items()}
            )
            self._keep_min_seen.update(
                {k: bool(v) for k, v in (stored.get("keep_min_seen") or {}).items()}
            )
            offered = stored.get("keep_min_offered") or {}
            interrupted = stored.get("keep_min_interrupted") or {}
            phases = stored.get("keep_min_phase") or {}
            for serial in self.chargers:
                self._keep_min_phase[serial] = restore_keep_phase(
                    phases.get(serial),
                    offered=offered.get(serial),
                    interrupted=interrupted.get(serial),
                )
        await self._refresh_source_entities()
        track = [
            self.soc_entity,
            self.solar_entity,
            self.house_entity,
            self.controller_entity,
            self.solar_today_entity,
            self.solar_tomorrow_entity,
        ]
        track.extend(WINDOW_EIDS)
        track.extend(SURPLUS_EIDS)
        track.extend(self.policy_entity(s) for s in self.chargers)
        track.extend(self.until_unplug_entity(s) for s in self.chargers)
        track.extend(self.keep_min_enable_entity(s) for s in self.chargers)
        track.extend(self.keep_min_entity(s) for s in self.chargers)
        track.append(EID_KEEP_PHASE)
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
        await self._subscribe_charger_mqtt()
        await self._migrate_legacy_until_unplug()
        await self._migrate_keep_min_switch()
        keep_on = ",".join(s for s, on in self._keep_min.items() if on) or "none"
        _LOGGER.info(
            "kotiakku_goe_direct: loaded chargers=%s leftover_session=%s keep=%s",
            ",".join(self.chargers) or "none",
            self.session,
            keep_on,
        )
        await self.async_plan()
        self._last_window_active = self.window_active()
        self._last_enough_solar = self.enough_solar
        self._last_gating_day = self.gating_solar_day
        _LOGGER.info(
            "kotiakku_goe_direct: cheap window %s enough-solar=%s gating=%s %s kWh",
            "active" if self._last_window_active else "inactive",
            self._last_enough_solar,
            self._last_gating_day,
            self.gating_solar_kwh,
        )
        self._schedule_apply()
        if self.hass.state is not CoreState.running:
            self._unsubs.append(
                self.hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STARTED, self._on_hass_started
                )
            )

    def _cancel(self, attr):
        unsub = getattr(self, attr)
        if unsub:
            unsub()
            setattr(self, attr, None)

    async def async_unload(self):
        _LOGGER.debug("kotiakku_goe_direct: unloading")
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for attr in (
            "_apply_unsub",
            "_floor_unsub",
            "_split_unsub",
            "_boundary_unsub",
            "_price_unsub",
        ):
            self._cancel(attr)
        for serial in list(self._phase_unsub):
            self._arm_phase(serial, False)
        for serial in list(self._offer_unsub):
            self._arm_offer_wait(serial, False)

    async def _save(self):
        await self._store.async_save(
            {
                "session": self.session,
                "split_session": self.split_session,
                "restore": self.restore,
                "seen": self.seen,
                "charge_session": self._charge_session,
                "keep_min": {s: self.keep_min(s) for s in self.chargers},
                "keep_min_seen": self._keep_min_seen,
                "keep_min_phase": self._keep_min_phase,
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
        if self._refreshing:
            return
        entity = event.data.get("entity_id")
        if entity in self._kotiakku_ids:
            self._schedule_apply()
            return
        if entity in self._forecast_ids:
            old_s, new_s = _event_states(event)
            _LOGGER.debug(
                "kotiakku_goe_direct: forecast %s %s → %s",
                entity,
                old_s,
                new_s,
            )
            await self.async_plan()
            self._schedule_apply()
            return
        if entity in WINDOW_EIDS:
            self._retarget_price()
            await self.async_plan()
            self._schedule_apply()
            return
        if entity in SURPLUS_EIDS:
            if entity in (EID_SOLAR_ENOUGH_KWH, EID_OFFSUN_HOUR_KWH):
                await self.async_plan()
            self._schedule_apply()
            return
        if entity and entity.startswith("select.kotiakku_goe_direct_policy_"):
            serial = entity.rsplit("_", 1)[-1]
            await self._on_policy(serial, event)
            return
        if entity in self._until_unplug_ids:
            old_s, new_s = _event_states(event)
            if old_s != new_s:
                _LOGGER.info(
                    "kotiakku_goe_direct: %s until-unplug %s → %s",
                    entity.rsplit("_", 1)[-1],
                    old_s,
                    new_s,
                )
            self._schedule_apply()
            return
        if entity in self._keep_enable_ids or entity in self._keep_ids:
            old_s, new_s = _event_states(event)
            if old_s != new_s:
                _LOGGER.info(
                    "kotiakku_goe_direct: %s %s → %s",
                    entity,
                    old_s,
                    new_s,
                )
            self._schedule_apply()
            return
        if entity == EID_KEEP_PHASE:
            old_s, new_s = _event_states(event)
            if old_s != new_s:
                _LOGGER.info(
                    "kotiakku_goe_direct: keep phase %s → %s",
                    old_s,
                    new_s,
                )
            self._schedule_apply()
            return
        if entity in self._car_ids:
            old_s, new_s = _event_states(event)
            if old_s != new_s:
                serial = None
                for cand in self.chargers:
                    if self.car_entity(cand) == entity:
                        serial = cand
                        break
                _LOGGER.info(
                    "kotiakku_goe_direct: %s car %s → %s",
                    serial or entity,
                    old_s,
                    new_s,
                )
            self._schedule_apply()
            return
        if entity in self._priority_ids or entity in self._power_ids:
            self._schedule_apply()

    async def _on_price(self, _event):
        if self._refreshing:
            return
        await self.async_plan()
        self._schedule_apply()

    async def _on_interval(self, _now=None):
        _LOGGER.debug("kotiakku_goe_direct: safety interval apply")
        await self.async_plan()
        self._schedule_apply(force=True)

    async def _on_policy(self, serial, event):
        old_s, new_s = _event_states(event)
        if old_s != new_s:
            _LOGGER.info(
                "kotiakku_goe_direct: %s policy %s → %s",
                serial,
                old_s,
                new_s,
            )
        self._schedule_apply()

    def _schedule_apply(self, floor_expired=False, split_expired=False, force=False):
        if floor_expired:
            self._pending_floor = True
        if split_expired:
            self._pending_split = True
        if force:
            self._pending_force = True
        action = mqtt_apply_window_action(
            self._apply_unsub is not None, self._charging
        )
        if action == "defer":
            self._apply_again = True
            return
        if action == "join":
            return
        self._apply_unsub = async_call_later(
            self.hass, MQTT_APPLY_S, self._apply_later
        )

    async def _apply_later(self, _now=None):
        self._apply_unsub = None
        await self._async_apply()

    def _arm_hold(self, attr, fire, need, *, name):
        active = getattr(self, attr)
        if not need:
            if active:
                _LOGGER.info("kotiakku_goe_direct: %s hold cancelled", name)
            self._cancel(attr)
            return
        if active:
            return
        _LOGGER.info(
            "kotiakku_goe_direct: %s hold for %s min",
            name,
            self.hold_min,
        )
        setattr(self, attr, async_call_later(self.hass, self.hold_min * 60, fire))

    def _arm_floor(self, need):
        self._arm_hold("_floor_unsub", self._floor_fire, need, name="leftover 6 A")

    async def _floor_fire(self, _now=None):
        self._floor_unsub = None
        _LOGGER.info("kotiakku_goe_direct: leftover 6 A hold expired")
        self._schedule_apply(floor_expired=True)

    def _arm_split(self, need):
        self._arm_hold(
            "_split_unsub", self._split_fire, need, name="second-car leftover steal"
        )

    async def _split_fire(self, _now=None):
        self._split_unsub = None
        _LOGGER.info("kotiakku_goe_direct: second-car leftover steal hold expired")
        self._schedule_apply(split_expired=True)

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
            _LOGGER.info(
                "kotiakku_goe_direct: psm hold expired on %s, applying wanted phase",
                serial,
            )
            self._schedule_apply()

        self._phase_unsub[serial] = async_call_later(
            self.hass, self.hold_min * 60, _fire
        )

    def _arm_offer_wait(self, serial, need, taking=False):
        if not serial:
            return
        if taking:
            unsub = self._offer_unsub.pop(serial, None)
            if unsub:
                unsub()
                _LOGGER.debug(
                    "kotiakku_goe_direct: %s leftover take ≥%s W, offer wait done",
                    serial,
                    int(TAKE_MIN_W),
                )
            self._offer_expired.discard(serial)
            return
        if not need:
            unsub = self._offer_unsub.pop(serial, None)
            if unsub:
                unsub()
            return
        if serial in self._offer_unsub or serial in self._offer_expired:
            return
        _LOGGER.info(
            "kotiakku_goe_direct: waiting %ss for %s to take leftover",
            OFFER_WAIT_S,
            serial,
        )

        async def _fire(_now=None, serial=serial):
            self._offer_unsub.pop(serial, None)
            self._offer_expired.add(serial)
            _LOGGER.info(
                "kotiakku_goe_direct: leftover offer wait expired on %s",
                serial,
            )
            self._schedule_apply()

        self._offer_unsub[serial] = async_call_later(
            self.hass, OFFER_WAIT_S, _fire
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
        self._schedule_apply()
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
            self.today_kwh,
            self.tomorrow_kwh,
            self.offsun_hour_kwh,
            lat,
            lon,
        )
        self.window_result = plan(
            self.clock,
            attrs,
            min_hours=min_hours,
            max_hours=max_hours,
            ceiling=ceiling,
            flex_pct=flex_pct,
            flex_euro=flex_euro,
            source_entity=price_entity,
            blocked=blocked,
            today_kwh=self.today_kwh,
            tomorrow_kwh=self.tomorrow_kwh,
        )
        windows = self.window_result.get("raw_windows") or []
        first = windows[0] if windows else {}
        _LOGGER.info(
            "kotiakku_goe_direct: plan reason=%s count=%s start=%s end=%s tomorrow_ok=%s",
            self.window_result.get("reason"),
            self.window_result.get("count"),
            first.get("start"),
            first.get("end"),
            self.window_result.get("tomorrow_ok"),
        )
        _LOGGER.debug(
            "kotiakku_goe_direct: plan source=%s slots=%s blocked=%s enough_solar=%s gating=%s %s kWh",
            self.window_result.get("source_entity"),
            self.window_result.get("slot_count"),
            len(self.window_result.get("blocked") or []),
            self.enough_solar,
            self.gating_solar_day,
            self.gating_solar_kwh,
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

    async def _subscribe_charger_mqtt(self):
        try:
            from homeassistant.components.mqtt import async_subscribe
        except Exception:
            return
        keys = (
            ("nrg", self._on_nrg_mqtt),
            ("frc", self._on_status_mqtt),
            ("amp", self._on_status_mqtt),
            ("lot", self._on_status_mqtt),
            ("psm", self._on_status_mqtt),
        )
        for serial in self.chargers:
            for key, handler in keys:
                topic = f"go-eCharger/{serial}/{key}"
                try:
                    unsub = await async_subscribe(self.hass, topic, handler)
                except Exception as err:
                    _LOGGER.debug(
                        "kotiakku_goe_direct: mqtt subscribe %s failed: %s",
                        topic,
                        err,
                    )
                    continue
                self._unsubs.append(unsub)

    def _mqtt_serial_payload(self, msg, key=None):
        topic = str(getattr(msg, "topic", "") or "")
        parts = topic.split("/")
        if len(parts) < 3:
            return None, None
        serial = parts[1]
        topic_key = parts[-1]
        if serial not in self.chargers:
            return None, None
        if key is not None and topic_key != key:
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
        old_take = old is not None and old >= TAKE_MIN_W
        new_take = value is not None and value >= TAKE_MIN_W
        if old_take != new_take:
            _LOGGER.debug(
                "kotiakku_goe_direct: %s nrg %s → %s W",
                serial,
                old,
                value,
            )
        if old != value:
            self._schedule_apply()

    def _on_status_mqtt(self, msg):
        topic = str(getattr(msg, "topic", "") or "")
        key = topic.split("/")[-1]
        if key not in ("frc", "amp", "lot", "psm"):
            return
        serial, payload = self._mqtt_serial_payload(msg, key)
        if not serial:
            return
        value = charger_mqtt_status_value(payload)
        if value is None:
            return
        bucket = self._charger_mqtt.setdefault(serial, {})
        old = bucket.get(key)
        bucket[key] = value
        if key == "frc" and old != value:
            _LOGGER.debug(
                "kotiakku_goe_direct: %s live frc %s → %s",
                serial,
                old,
                value,
            )
            self._schedule_apply()

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

    def _clear_surplus_session(self):
        self.session = False
        self.split_session = False
        self._arm_split(False)
        for serial in list(self._offer_unsub):
            self._arm_offer_wait(serial, False)
        self._offer_expired.clear()

    async def async_surplus(self, floor_expired=False, split_expired=False):
        self._schedule_apply(floor_expired=floor_expired, split_expired=split_expired)

    async def async_charge(self):
        self._schedule_apply()

    async def _async_apply(self):
        if self._charging:
            self._apply_again = True
            return
        self._charging = True
        try:
            floor = self._pending_floor
            split = self._pending_split
            force = self._pending_force
            self._pending_floor = False
            self._pending_split = False
            self._pending_force = False
            await self._apply_chargers(floor, split, force=force)
        finally:
            self._charging = False
            if (
                self._apply_again
                or self._pending_floor
                or self._pending_split
                or self._pending_force
            ):
                self._apply_again = False
                self._schedule_apply()

    async def _sync_until_unplug(self):
        changed = False
        until_on = {}
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
                if not new_on:
                    _LOGGER.info(
                        "kotiakku_goe_direct: %s until-unplug off (unplug, car=%s)",
                        serial,
                        car_state,
                    )
                await self._turn_until_unplug(serial, new_on)
                changed = True
            until_on[serial] = new_on
        return changed, until_on

    async def _sync_keep_min(self, commanded=None):
        """Arm after-charge-complete keep until unplug for a finished pack."""
        changed = False
        keep_on = {}
        for serial in self.chargers:
            car_state = self._state(self.car_entity(serial))
            override = self.keep_min(serial)
            was_on = bool(self._keep_min.get(serial))
            old_phase = self._keep_min_phase.get(serial, KEEP_IDLE)
            plugged = car_plugged(car_state)
            finished = car_finished(car_state)
            enable = (
                self.keep_min_enable(serial)
                and self.policy(serial) != POLICY_FORCE_OFF
            )
            new_on, new_seen, new_phase = keep_until_unplug_step(
                override,
                self._keep_min_seen.get(serial),
                old_phase,
                plugged=plugged,
                finished=finished,
                commanded_on=(
                    None if commanded is None else bool(commanded.get(serial))
                ),
                was_on=was_on,
                enable=enable,
            )
            if (
                was_on != new_on
                or bool(self._keep_min_seen.get(serial)) != new_seen
                or old_phase != new_phase
            ):
                changed = True
            if new_on and not was_on:
                _LOGGER.info(
                    "kotiakku_goe_direct: %s after_charge_complete_keep on (%s-phase %s A until unplug, car=%s)",
                    serial,
                    3 if self.keep_psm == 2 else 1,
                    self.keep_amp,
                    car_state,
                )
            elif was_on and not new_on:
                why = "unplug" if not plugged else "switch off"
                _LOGGER.info(
                    "kotiakku_goe_direct: %s after_charge_complete_keep off (%s, car=%s)",
                    serial,
                    why,
                    car_state,
                )
            if old_phase != new_phase:
                if new_phase == KEEP_CUT:
                    _LOGGER.info(
                        "kotiakku_goe_direct: %s keep cut (HA stopped charge before Complete, car=%s)",
                        serial,
                        car_state,
                    )
                else:
                    _LOGGER.debug(
                        "kotiakku_goe_direct: %s keep phase %s → %s (on=%s car=%s)",
                        serial,
                        old_phase,
                        new_phase,
                        new_on,
                        car_state,
                    )
            self._keep_min[serial] = new_on
            self._keep_min_seen[serial] = new_seen
            self._keep_min_phase[serial] = new_phase
            if new_on != override:
                await self._turn_keep_min(serial, new_on)
                changed = True
            keep_on[serial] = new_on
        return changed, keep_on

    def _charger_roles(self, now_ts, until_on, keep_on):
        roles = {}
        for serial in self.chargers:
            roles[serial] = charger_mqtt_role(
                self.policy(serial),
                self.window_result,
                now_ts,
                enough_solar=self.enough_solar,
                until_unplug=until_on.get(serial),
                keep_min=keep_on.get(serial),
            )
            self._last_policy[serial] = self.policy(serial)
        return roles

    def _leftover_pubs(self, surplus, dec, snap, split_expired, n_full):
        """Per-serial leftover psm/lot/amp. Empty if leftover is not writing."""
        target_w = 0 if dec["use_floor_budget"] else snap["available_w"]
        lot, psm, amp = budget(
            target_w,
            self.min_amp,
            self.max_amp,
            self.group_lot,
            self.volts,
            self.phase3_min_w,
        )
        lot, psm, amp = group_surplus_setpoint(
            lot,
            psm,
            amp,
            n_full=n_full,
            group_lot=self.group_lot,
        )
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
                group_lot=self.group_lot,
                phase3_min_w=self.phase3_min_w,
            )
        alloc_w = snap["available_w"]
        if dec["use_floor_budget"]:
            alloc_w = max(alloc_w, self.min_amp * self.volts)
        offer_pending = {
            serial
            for serial in surplus
            if take_w.get(serial, 0) < TAKE_MIN_W and serial not in self._offer_expired
        }
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
            offer_pending=offer_pending,
        )
        taking = allocations.get("taking") or []
        was_split = self.split_session
        self.split_session = len(taking) >= 2
        if self.split_session and not was_split:
            _LOGGER.info(
                "kotiakku_goe_direct: leftover split taking=%s remainder=%sW",
                ",".join(taking),
                allocations.get("remainder_w"),
            )
        self._arm_split(allocations["arm_split_hold"])
        allocated = allocations["allocations"]
        for serial in surplus:
            taking_now = take_w.get(serial, 0) >= TAKE_MIN_W
            self._arm_offer_wait(
                serial,
                serial in allocated and not taking_now,
                taking=taking_now,
            )
        lot_alloc = allocations.get("lot_allocations")
        if lot_alloc is None:
            lot_alloc = allocations["allocations"]
        overdraw = bool(allocations.get("overdraw"))
        if not dec["use_floor_budget"]:
            lot = group_lot_for_allocations(
                lot,
                lot_alloc,
                min_amp=self.min_amp,
                max_amp=self.max_amp,
                group_lot=self.group_lot,
                volts=self.volts,
                phase3_min_w=self.phase3_min_w,
                overdraw=overdraw,
            )
        targets = {}
        for serial in surplus:
            watts_i = allocations["allocations"].get(serial)
            if watts_i is None:
                if surplus_higher_keep_on(serial, allocated, lops, states):
                    watts_i = alloc_w
                    _LOGGER.debug(
                        "kotiakku_goe_direct: %s leftover stays armed (lower-priority car has leftover)",
                        serial,
                    )
                else:
                    continue
            source_w = target_w if dec["use_floor_budget"] else min(
                int(watts_i), max(int(snap["available_w"]), 0)
            )
            pub = surplus_phase_budget(
                source_w,
                self.min_amp,
                self.max_amp,
                self.group_lot,
                self.volts,
                self.phase3_min_w,
                last_psm=self._surplus_psm.get(serial),
                hold_expired=serial in self._phase_expired,
            )
            targets[serial] = pub
            self._arm_phase(serial, pub["arm_phase"])
        if n_full <= 0:
            lot_serials = set(lot_alloc)
            lot = group_lot_for_amps(
                lot,
                [
                    pub["amp"]
                    for serial, pub in targets.items()
                    if serial in lot_serials
                ],
                self.group_lot,
                overdraw=overdraw,
            )
        for pub in targets.values():
            pub["lot"] = lot
        _LOGGER.debug(
            "kotiakku_goe_direct: leftover alloc %sW floor=%s taking=%s remainder=%sW overdraw=%s pending=%s pubs=%s",
            snap["available_w"],
            dec["use_floor_budget"],
            ",".join(taking) or "none",
            allocations.get("remainder_w"),
            overdraw,
            ",".join(sorted(offer_pending)) or "none",
            ",".join(
                "%s=%sA/%s" % (s, pub["amp"], "3p" if pub["psm"] == 2 else "1p")
                for s, pub in targets.items()
            )
            or "none",
        )
        return targets

    async def _apply_chargers(self, floor_expired=False, split_expired=False, force=False):
        self._log_gate_changes()
        changed, until_on = await self._sync_until_unplug()
        keep_changed, keep_on = await self._sync_keep_min()
        changed = changed or keep_changed
        now_ts = self._now_ts()
        roles = self._charger_roles(now_ts, until_on, keep_on)
        snap = self._snapshot()
        raw_w = snap["available_w"]
        keep_serials = [s for s in self.chargers if roles[s] == ROLE_KEEP]
        keep_powers = [self.charger_power_w(s) for s in keep_serials]
        snap["available_w"] = leftover_for_surplus(raw_w, *keep_powers)
        if keep_serials:
            parts = ",".join(
                "%s=%sW" % (s, "unknown" if p is None else p)
                for s, p in zip(keep_serials, keep_powers)
            )
            _LOGGER.debug(
                "kotiakku_goe_direct: keep nrg %s leftover %s W → %s W",
                parts,
                raw_w,
                snap["available_w"],
            )
            if snap["available_w"] < 0 and (
                self._last_surplus_w is None or self._last_surplus_w >= 0
            ):
                _LOGGER.info(
                    "kotiakku_goe_direct: keep using leftover pool (%s); leftover %s W → %s W (deficit)",
                    parts,
                    raw_w,
                    snap["available_w"],
                )
        self._last_surplus_w = snap["available_w"]
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
        _LOGGER.debug(
            "kotiakku_goe_direct: apply leftover=%sW soc=%s window_ok=%s session=%s "
            "write_on=%s write_off=%s floor=%s roles=%s floor_exp=%s split_exp=%s force=%s solar=%sW house=%sW",
            snap["available_w"],
            snap["soc"],
            snap["window_ok"],
            self.session,
            dec["write_on"],
            dec["write_off"],
            dec["arm_floor"],
            _roles_text(roles),
            floor_expired,
            split_expired,
            force,
            snap["solar_w"],
            snap["house_w"],
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
        surplus = [serial for serial in self.chargers if roles[serial] == ROLE_SURPLUS]
        was_session = self.session
        had_leftover_setpoint = set(self._surplus_amp)
        surplus_on = False
        pubs = {}
        n_held = sum(
            1
            for serial in self.chargers
            if roles[serial] in (ROLE_FULL, ROLE_KEEP)
        )
        if dec["write_on"] and surplus:
            surplus_on = True
            self.session = True
            pubs = self._leftover_pubs(
                surplus,
                dec,
                snap,
                split_expired,
                n_full=n_held,
            )
            if not was_session:
                _LOGGER.info(
                    "kotiakku_goe_direct: leftover surplus on leftover=%sW soc=%s chargers=%s",
                    snap["available_w"],
                    snap["soc"],
                    ",".join(surplus),
                )
            if not pubs:
                _LOGGER.debug(
                    "kotiakku_goe_direct: leftover on but no setpoint (chargers=%s)",
                    ",".join(surplus),
                )
        elif dec["write_on"] or dec["write_off"] or was_session or had_leftover_setpoint:
            if was_session or had_leftover_setpoint:
                if dec["write_off"] and floor_expired:
                    why = "6 A hold expired"
                elif not surplus:
                    why = "no surplus chargers"
                elif not dec["write_on"]:
                    why = "leftover/SoC stop"
                else:
                    why = "leftover off"
                _LOGGER.info(
                    "kotiakku_goe_direct: leftover surplus off (%s, leftover=%sW soc=%s)",
                    why,
                    snap["available_w"],
                    snap["soc"],
                )
            elif dec["write_on"] and not surplus:
                _LOGGER.debug(
                    "kotiakku_goe_direct: leftover %sW but no surplus chargers roles=%s",
                    snap["available_w"],
                    _roles_text(roles),
                )
            self._clear_surplus_session()
        commanded = {
            serial: roles[serial] == ROLE_FULL or serial in pubs
            for serial in self.chargers
        }
        keep_changed, keep_on = await self._sync_keep_min(commanded)
        changed = changed or keep_changed
        roles = self._charger_roles(now_ts, until_on, keep_on)
        for serial, role in roles.items():
            old = self._last_roles.get(serial)
            if old == role:
                continue
            why = ""
            if role == ROLE_FULL:
                if until_on.get(serial):
                    why = " (until-unplug)"
                elif self.policy(serial) == POLICY_FORCE_ON:
                    why = " (Force on)"
                else:
                    why = " (cheap window)"
            elif role == ROLE_KEEP:
                why = " (after-charge keep)"
            _LOGGER.info(
                "kotiakku_goe_direct: %s role %s → %s%s (%s)",
                serial,
                old or "none",
                role,
                why,
                self._charger_log(serial),
            )
        self._last_roles = dict(roles)
        for serial in self.chargers:
            role = roles[serial]
            had_full = bool(self._charge_session.get(serial))
            leftover_was_writing = serial in had_leftover_setpoint or (
                was_session and role == ROLE_SURPLUS
            )
            cmd = charger_mqtt_command(
                role,
                surplus_on=surplus_on,
                surplus_pub=pubs.get(serial),
                had_full=had_full,
                leftover_session=leftover_was_writing,
                group_lot=self.group_lot,
                max_amp=self.max_amp,
                min_amp=self.min_amp,
                keep_psm=self.keep_psm,
                keep_amp=self.keep_amp,
                live_frc=(self._charger_mqtt.get(serial) or {}).get("frc"),
            )
            if role == ROLE_FULL:
                if not had_full:
                    changed = True
                self._charge_session[serial] = True
                self._arm_phase(serial, False)
            else:
                if had_full:
                    changed = True
                self._charge_session[serial] = False
                if role == ROLE_KEEP:
                    self._arm_phase(serial, False)
            if cmd is None:
                continue
            published = await self._publish_cmd(
                serial, cmd, force=force, leftover=role == ROLE_SURPLUS
            )
            if published:
                changed = True
        if changed:
            await self._save()
            self.notify()

    async def _migrate_legacy_until_unplug(self):
        for serial in list(self.legacy_until_unplug):
            restore_to = self.restore.get(serial, POLICY_FORCE_OFF)
            if restore_to not in POLICIES:
                restore_to = POLICY_FORCE_OFF
            _LOGGER.info(
                "kotiakku_goe_direct: %s migrate Force on until unplug → switch, policy %s",
                serial,
                restore_to,
            )
            if self.policy(serial) != restore_to:
                await self._select_policy(serial, restore_to)
            if not self.until_unplug(serial):
                await self._turn_until_unplug(serial, True)

    async def _migrate_keep_min_switch(self):
        for serial in self.chargers:
            if self._keep_min.get(serial) and not self.keep_min(serial):
                _LOGGER.info(
                    "kotiakku_goe_direct: %s restore after_charge_complete_keep switch on",
                    serial,
                )
                await self._turn_keep_min(serial, True)

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

    async def _turn_keep_min(self, serial, on):
        entity = self.keep_min_entity(serial)
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

    def _remember_leftover(self, serial, cmd):
        """Store last leftover surplus phase/amp. Not used for 22 kW or keep."""
        if cmd[0] == "off":
            self._arm_phase(serial, False)
            self._surplus_psm.pop(serial, None)
            self._surplus_amp.pop(serial, None)
            return
        self._surplus_psm[serial] = int(cmd[1])
        self._surplus_amp[serial] = int(cmd[3])

    async def _publish_cmd(self, serial, cmd, force=False, leftover=False):
        if not serial or cmd is None:
            return False
        live = self._charger_mqtt.get(serial)
        text = _mqtt_cmd_text(cmd)
        if not charger_mqtt_needs_update(cmd, live):
            if leftover or cmd[0] == "off":
                self._remember_leftover(serial, cmd)
            return False
        if (
            not force
            and not charger_mqtt_live_complete(cmd, live)
            and self._last_mqtt.get(serial) == cmd
        ):
            _LOGGER.debug(
                "kotiakku_goe_direct: %s mqtt skip waiting-live %s",
                serial,
                text,
            )
            if leftover or cmd[0] == "off":
                self._remember_leftover(serial, cmd)
            return False
        starting = leftover and cmd[0] == "on" and serial not in self._surplus_amp
        if leftover and cmd[0] == "on" and not starting:
            _LOGGER.debug("kotiakku_goe_direct: %s leftover mqtt %s", serial, text)
        else:
            _LOGGER.info("kotiakku_goe_direct: %s mqtt %s", serial, text)
        if cmd[0] == "off":
            await self._publish_off(serial)
        else:
            await self._publish_on(serial, cmd[1], cmd[2], cmd[3])
        self._last_mqtt[serial] = cmd
        if leftover or cmd[0] == "off":
            self._remember_leftover(serial, cmd)
        return True

    async def _publish_on(self, serial, psm, lot, amp):
        await self._mqtt_many(serial, charger_on_mqtt(psm, lot, amp))

    async def _publish_off(self, serial):
        await self._mqtt_many(serial, charger_off_mqtt())
        if serial:
            self._last_mqtt[serial] = ("off",)

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
