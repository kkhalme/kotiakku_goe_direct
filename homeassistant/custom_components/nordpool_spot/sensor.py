"""sensor.nordpool_spot — current price, attribute `raw` for charts."""

from datetime import timedelta
import logging

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import CONF_ENTITY_ID
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import DEFAULT_ENTITY_ID, DOMAIN
from .merge import (
    KEEP_DAYS,
    as_raw,
    current_price,
    live_slots,
    merge_slots,
    parse_slots,
)

_LOGGER = logging.getLogger(__name__)
STORAGE_KEY = "nordpool_spot"


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    info = discovery_info or config or {}
    source = info.get(CONF_ENTITY_ID) or hass.data.get(DOMAIN) or DEFAULT_ENTITY_ID
    async_add_entities([NordpoolSpotSensor(hass, source)])


class NordpoolSpotSensor(SensorEntity):
    _attr_name = "Nordpool spot"
    _attr_icon = "mdi:chart-bar"
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 3
    _attr_unique_id = "nordpool_spot"

    def __init__(self, hass, source):
        self.hass = hass
        self.entity_id = "sensor.nordpool_spot"
        self._source = source
        self._store = Store(hass, 1, STORAGE_KEY)
        self._realized = []
        self._price = None
        self._unit = None
        self._raw = []
        self._forecast_end = None
        self._unsubs = []

    @property
    def native_value(self):
        return self._price

    @property
    def native_unit_of_measurement(self):
        return self._unit

    @property
    def extra_state_attributes(self):
        return {
            "raw": self._raw,
            "source": self._source,
            "forecast_end": self._forecast_end,
        }

    async def async_added_to_hass(self):
        stored = await self._store.async_load()
        if stored:
            self._realized = parse_slots(stored.get("raw"))
        self._unsubs.append(
            async_track_state_change_event(self.hass, [self._source], self._on_change)
        )
        self._unsubs.append(
            async_track_time_interval(self.hass, self._on_change, timedelta(minutes=15))
        )
        self._unsubs.append(
            async_track_time_change(self.hass, self._on_change, hour=0, minute=0, second=30)
        )
        await self._refresh()

    async def async_will_remove_from_hass(self):
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    async def _on_change(self, *_args):
        await self._refresh()

    async def _refresh(self):
        source = self.hass.states.get(self._source)
        attrs = {} if source is None else dict(source.attributes)
        now_ts = float(dt_util.as_timestamp(dt_util.now()))
        merged = merge_slots(self._realized, live_slots(attrs), now_ts, keep_days=KEEP_DAYS)
        realized = [slot for slot in merged if slot[1] <= now_ts]
        changed = realized != self._realized
        self._realized = realized
        self._price = current_price(merged, now_ts)
        self._unit = None if source is None else source.attributes.get("unit_of_measurement")
        self._raw = as_raw(merged)
        self._forecast_end = self._raw[-1]["end"] if self._raw else None
        if changed:
            await self._store.async_save({"raw": as_raw(realized)})
        self.async_write_ha_state()
        _LOGGER.debug(
            "nordpool_spot: slots=%s realized=%s forecast_end=%s",
            len(merged),
            len(realized),
            self._forecast_end,
        )
