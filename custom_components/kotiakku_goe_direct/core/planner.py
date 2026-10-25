"""Cheap charge-window planner and solar gating. Pure; no Home Assistant.

The plan is a function of prices, forecasts, the off-sun mask and knobs,
never of the wall clock (only via local day boundaries). A cheapest window
that already ended stays the plan, so 15-minute ticks cannot slide it.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import NamedTuple

from .model import Plan, Settings, Window

GAP_S = 60
EPS_S = 30
TS_EPS_S = 1
PRICE_EPS = 1e-7
SAMPLE_S = 900
HALF_HOUR_S = 1800
DAY_S = 86400
PAST_DAYS = 2

Slot = tuple[float, float, float]


def local_midnight(day: date, tz: tzinfo) -> datetime:
    return datetime.combine(day, time(0), tzinfo=tz)


def day_start(now: datetime, offset: int = 0) -> datetime:
    """Local midnight ``offset`` days from ``now``."""
    return local_midnight(now.date() + timedelta(days=offset), now.tzinfo)


def _day_bounds(now: datetime, offset: int, days: int = 1) -> tuple[float, float]:
    return day_start(now, offset).timestamp(), day_start(now, offset + days).timestamp()


def _same_ts(a: float, b: float) -> bool:
    return abs(a - b) <= TS_EPS_S


def _on_day(rows: Iterable[Slot], start: float, end: float) -> list[Slot]:
    """Rows that start in ``[start, end)``."""
    return [row for row in rows if start - TS_EPS_S <= row[0] < end - TS_EPS_S]


def _ts(value, tz: tzinfo) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=tz)
    return value.timestamp()


def _price(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first(item: Mapping, keys):
    for key in keys:
        value = item.get(key)
        if value not in (None, "", []):
            return value
    return None


def _rows(raw) -> list[Slot]:
    """Stored ``(start, end, value)`` rows; malformed and zero-length rows are dropped."""
    rows = []
    for item in raw or []:
        try:
            row = (float(item[0]), float(item[1]), float(item[2]))
        except (TypeError, ValueError, IndexError):
            continue
        if row[1] > row[0]:
            rows.append(row)
    return rows


def weighted_avg(slots: list[Slot]) -> float | None:
    """Duration-weighted mean value of ``slots``."""
    dur = sum(s[1] - s[0] for s in slots)
    return sum(s[2] * (s[1] - s[0]) for s in slots) / dur if dur else None


def _series(raw, day_start: datetime, tz: tzinfo) -> list[Slot]:
    if not raw or not isinstance(raw, (list, tuple)):
        return []
    if isinstance(raw[0], Mapping):
        out = []
        for item in raw:
            start = _ts(_first(item, ("start", "from", "begin")), tz)
            end = _ts(_first(item, ("end", "to", "until")), tz)
            price = _price(_first(item, ("value", "price")))
            if start is not None and end is not None and price is not None and end > start:
                out.append((start, end, price))
        return out
    step = DAY_S // len(raw)
    base = day_start.timestamp()
    out = []
    for i, raw_price in enumerate(raw):
        price = _price(raw_price)
        if price is not None:
            out.append((base + i * step, base + (i + 1) * step, price))
    return out


def price_slots(attrs: Mapping | None, now: datetime) -> list[Slot]:
    if not attrs:
        return []
    tz = now.tzinfo
    slots = _series(_first(attrs, ("raw_today", "today")), day_start(now, 0), tz)
    slots += _series(_first(attrs, ("raw_tomorrow", "tomorrow")), day_start(now, 1), tz)
    return sorted(slots)


def tomorrow_prices_ok(attrs: Mapping | None) -> bool:
    """Nordpool ``tomorrow_valid``. A price tail after midnight is not a published day."""
    flag = (attrs or {}).get("tomorrow_valid")
    return flag is True or str(flag).lower() in ("on", "true")


def remember_day(days: dict | None, now: datetime, live: list[Slot], today_kwh: float | None, periods=None) -> dict:
    """Cache today's spot slots, solar kWh and forecast periods. Empty curves do not erase a day."""
    start, end = _day_bounds(now, 0)
    oldest = day_start(now, -PAST_DAYS).timestamp()
    out = {
        key: entry
        for key, entry in (days or {}).items()
        if isinstance(entry, dict) and (entry.get("start") or 0) >= oldest - TS_EPS_S
    }
    today = _on_day(live, start, end)
    if today:
        key = now.date().isoformat()
        previous = out.get(key) or {}
        kept = _on_day(_rows(periods), start, end) or previous.get("periods") or []
        entry = {
            "start": start,
            "slots": today,
            "kwh": today_kwh if today_kwh is not None else previous.get("kwh"),
        }
        if kept:
            entry["periods"] = kept
        out[key] = entry
    return out


class CachedDay(NamedTuple):
    start: float
    slots: list[Slot]
    kwh: float | None
    periods: list[Slot]


def _cached_days(days: dict | None) -> list[CachedDay]:
    out = []
    for entry in (days or {}).values():
        if not isinstance(entry, dict) or entry.get("start") is None:
            continue
        kwh = entry.get("kwh")
        out.append(
            CachedDay(
                float(entry["start"]),
                _rows(entry.get("slots")),
                None if kwh is None else float(kwh),
                _rows(entry.get("periods")),
            )
        )
    return out


def _cached_day(cached: list[CachedDay], start: float) -> CachedDay | None:
    return next((day for day in cached if _same_ts(day.start, start)), None)


def epoch_curve(live: list[Slot], now: datetime, days: dict | None, tomorrow_valid: bool) -> tuple[list[Slot], int]:
    """Live prices plus cached earlier days. Offset 0 is today+tomorrow; -1 is yesterday+today."""
    cached = _cached_days(days)
    merged = list(live)
    for offset in range(-PAST_DAYS, 0):
        start, end = _day_bounds(now, offset)
        day = _cached_day(cached, start)
        if day is None:
            continue
        merged += [
            slot
            for slot in _on_day(day.slots, start, end)
            if not any(slot[0] < other[1] and slot[1] > other[0] for other in live)
        ]
    merged.sort()
    if not tomorrow_valid and _on_day(merged, *_day_bounds(now, -1)):
        return merged, -1
    return merged, 0


def epoch_day(slots: list[Slot], now: datetime, offset: int, tomorrow_valid: bool) -> float:
    """Local midnight of the epoch's newest searchable day. It moves when prices arrive, not at midnight."""
    newest, following = _day_bounds(now, offset + 1)
    published = tomorrow_valid if offset == 0 else bool(_on_day(slots, newest, following))
    if published:
        return newest
    return day_start(now, offset).timestamp()


def note_epoch(seen: dict | None, epoch_start: float | None, now: datetime) -> tuple[dict, float | None]:
    """Remember when this epoch was first seen. The first epoch on record carries nothing."""
    seen = dict(seen or {})
    if epoch_start is None:
        return seen, None
    key = str(round(epoch_start))
    first = seen.get(key)
    if first is None:
        first = now.timestamp()
        oldest = day_start(now, -PAST_DAYS - 1).timestamp()
        seen = {k: v for k, v in seen.items() if float(k) >= oldest - TS_EPS_S}
        seen[key] = first
    earlier = any(float(k) < epoch_start - TS_EPS_S for k in seen)
    return seen, (float(first) if earlier else None)


def update_history(
    attrs: Mapping | None,
    now: datetime,
    days: dict | None,
    seen: dict | None,
    today_kwh: float | None,
    periods=None,
) -> tuple[dict, dict, float | None]:
    """Stored price days and epoch first-seen times after this cycle, plus when the current epoch was first seen."""
    if attrs is None:
        return dict(days or {}), dict(seen or {}), None
    valid = tomorrow_prices_ok(attrs)
    live = price_slots(attrs, now)
    days = remember_day(days, now, live, today_kwh, periods)
    slots, offset = epoch_curve(live, now, days, valid)
    seen, seen_ts = note_epoch(seen, epoch_day(slots, now, offset, valid), now)
    return days, seen, seen_ts


def history_view(days: dict | None, now: datetime) -> dict:
    """Sensor view of the cached spot days. State is yesterday's average."""
    names = {0: "today", -1: "yesterday", -2: "day_before_yesterday"}
    view = {"yesterday_avg": None, "days": [], **{f"raw_{name}": [] for name in names.values()}}
    starts = {offset: day_start(now, offset).timestamp() for offset in names}
    for key, entry in sorted((days or {}).items()):
        slots = _rows(entry.get("slots"))
        start = entry.get("start")
        if start is None or not slots:
            continue
        avg = weighted_avg(slots)
        view["days"].append({"date": key, "kwh": entry.get("kwh"), "slot_count": len(slots), "avg": avg})
        for offset, day_ts in starts.items():
            if not _same_ts(start, day_ts):
                continue
            view[f"raw_{names[offset]}"] = [
                {
                    "start": datetime.fromtimestamp(s, now.tzinfo).isoformat(),
                    "end": datetime.fromtimestamp(e, now.tzinfo).isoformat(),
                    "value": p,
                }
                for s, e, p in slots
            ]
            if offset == -1:
                view["yesterday_avg"] = None if avg is None else round(avg, 6)
    return view


def carry_windows(windows, previous, seen_ts: float | None):
    """Previous-epoch windows that were already running when this epoch was first seen."""
    if seen_ts is None:
        return []
    out = []
    for window in previous:
        if not window[0] <= seen_ts < window[1]:
            continue
        covered = any(
            c[0] <= window[0] + TS_EPS_S and c[1] >= window[1] - TS_EPS_S for c in list(windows) + out
        )
        if not covered:
            out.append(window)
    return out


def solar_elevation_deg(ts: float, lat: float, lon: float) -> float:
    """Approximate solar elevation (no refraction)."""
    utc = datetime.fromtimestamp(ts, UTC)
    decl = 23.45 * math.sin(math.radians(360.0 / 365.0 * (utc.timetuple().tm_yday - 81)))
    hour = utc.hour + utc.minute / 60.0 + utc.second / 3600.0
    ha = 15.0 * (hour + lon / 15.0 - 12.0)
    lat_r, decl_r = math.radians(lat), math.radians(decl)
    sin_el = math.sin(lat_r) * math.sin(decl_r) + math.cos(lat_r) * math.cos(decl_r) * math.cos(
        math.radians(ha)
    )
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_el))))


def hour_kwh(day: date, tz: tzinfo, kwh: float | None, lat: float, lon: float) -> list[Slot]:
    """Spread a local day's kWh over its hours by sun elevation: (start, end, kwh)."""
    if kwh is None:
        return []
    start = local_midnight(day, tz).timestamp()
    end = local_midnight(day + timedelta(days=1), tz).timestamp()
    weights: dict[int, float] = {}
    t = start
    while t < end - TS_EPS_S:
        hour = int((t - start) // 3600)
        el = solar_elevation_deg(t, lat, lon)
        weights[hour] = weights.get(hour, 0.0) + (math.sin(math.radians(el)) if el > 0 else 0.0)
        t += SAMPLE_S
    total = sum(weights.values())
    energy = max(float(kwh), 0.0)
    return [
        (start + h * 3600, start + (h + 1) * 3600, energy * w / total if total > 0 else 0.0)
        for h, w in weights.items()
    ]


def forecast_periods(raw, tz: tzinfo) -> list[Slot]:
    """Solcast ``detailedForecast`` rows as (start, end, kW). A row lasts 30 minutes and is not stretched."""
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        start = _ts(item.get("period_start"), tz)
        kw = _price(item.get("pv_estimate"))
        if start is None or kw is None:
            continue
        out.append((start, start + HALF_HOUR_S, kw))
    out.sort()
    return out


def _elevation_kw(day: date, tz: tzinfo, kwh: float | None, lat: float, lon: float) -> list[Slot]:
    """Daily kWh spread by sun elevation, as average kW over each hour."""
    return [
        (start, end, energy / ((end - start) / 3600.0) if end > start else 0.0)
        for start, end, energy in hour_kwh(day, tz, kwh, lat, lon)
    ]


def solar_periods(now, today_kwh, tomorrow_kwh, lat, lon, history, live) -> list[Slot]:
    """Live today and tomorrow periods, else cached periods, else the elevation spread."""
    tz = now.tzinfo
    live = list(live or [])
    cached = _cached_days(history)
    out = []
    for offset, kwh in ((0, today_kwh), (1, tomorrow_kwh)):
        own = _on_day(live, *_day_bounds(now, offset))
        out.extend(own or _elevation_kw(day_start(now, offset).date(), tz, kwh, lat, lon))
    for offset in range(-PAST_DAYS, 0):
        start, end = _day_bounds(now, offset)
        day = _cached_day(cached, start)
        if day is None:
            continue
        own = _on_day(day.periods, start, end)
        out.extend(own or _elevation_kw(day_start(now, offset).date(), tz, day.kwh, lat, lon))
    return out


def _slot_kw(slot: Slot, periods: list[Slot]) -> float:
    """Overlap-weighted kW. Minutes with no period count as 0."""
    start, end = slot[0], slot[1]
    dur = end - start
    if dur <= 0:
        return 0.0
    power = 0.0
    for ps, pe, kw in periods:
        overlap = min(end, pe) - max(start, ps)
        if overlap > 0:
            power += kw * overlap
    return power / dur


def mask_slots(slots: list[Slot], periods: list[Slot], limit: float) -> list[Slot]:
    """Drop slots whose forecast kW is at or above ``limit``. Zero removes nothing."""
    if limit <= 0:
        return list(slots)
    return [slot for slot in slots if _slot_kw(slot, periods) < limit]


def blocked_ranges(periods: list[Slot], limit: float) -> list[tuple[float, float]]:
    """Touching over-limit periods, in time order, for the chart."""
    if limit <= 0:
        return []
    ranges = []
    for start, end, kw in sorted(periods, key=lambda period: period[0]):
        if kw < limit or end <= start:
            continue
        if ranges and start <= ranges[-1][1] + TS_EPS_S:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))
    return ranges


def usable_solar_end(periods: list[Slot], now: datetime, limit: float) -> float | None:
    """End of today's last period at or above the limit, or above 0 kW when the limit is 0."""
    floor = limit if limit > 0 else 1e-12
    ends = [pe for _ps, pe, kw in _on_day(periods, *_day_bounds(now, 0)) if kw >= floor]
    return max(ends, default=None)


@dataclass(frozen=True)
class WindowKnobs:
    """Window search knobs, already clamped to usable ranges."""

    min_h: float = 2.0
    max_h: float = 5.0
    ceiling: float = 0.2
    flex_pct: float = 20.0
    flex_eur: float = 0.02
    daily_h: float = 7.0
    daily_pct: float = 5.0
    daily_eur: float = 0.03
    daily_after_h: float = 17.0

    @classmethod
    def from_settings(cls, settings: Settings) -> WindowKnobs:
        min_h = min(24.0, max(0.25, float(settings.window_min_h)))
        max_h = min(24.0, max(0.25, float(settings.window_max_h)))
        return cls(
            min_h=min(min_h, max_h),
            max_h=max(min_h, max_h),
            ceiling=float(settings.electricity_price_ceiling),
            flex_pct=max(float(settings.window_flex_pct), 0.0),
            flex_eur=max(float(settings.window_flex_eur), 0.0),
            daily_h=min(24.0, max(0.0, float(settings.daily_trip_charge_before_h))),
            daily_pct=max(float(settings.daily_trip_price_flex_pct), 0.0),
            daily_eur=max(float(settings.daily_trip_price_flex_eur), 0.0),
            daily_after_h=min(24.0, max(0.0, float(settings.daily_trip_charge_after_h))),
        )

    @property
    def min_s(self) -> float:
        return self.min_h * 3600.0

    @property
    def max_s(self) -> float:
        return self.max_h * 3600.0


def _avg(slots: list[Slot], i: int, j: int) -> float:
    return weighted_avg(slots[i : j + 1])


def _allowance(avg: float, pct: float, eur: float) -> float:
    """The looser of ``pct`` of ``avg`` and ``eur`` (both non-negative)."""
    return max(abs(avg) * pct / 100.0, eur)


def find_seed(slots: list[Slot], min_s: float) -> tuple[float, int, int] | None:
    """Cheapest contiguous run of at least ``min_s``; the ceiling is not a score."""
    best = None
    for i in range(len(slots)):
        dur = 0.0
        for j in range(i, len(slots)):
            if j > i and slots[j][0] - slots[j - 1][1] > GAP_S:
                break
            dur += slots[j][1] - slots[j][0]
            if dur + EPS_S >= min_s:
                avg = _avg(slots, i, j)
                if best is None or avg < best[0] - PRICE_EPS:
                    best = (avg, i, j)
                break
    return best


def grow(slots: list[Slot], i: int, j: int, seed_avg: float, knobs: WindowKnobs) -> tuple[int, int]:
    """Add one native slot at a time on the cheaper side until a price wall."""
    if knobs.flex_pct <= 0 and knobs.flex_eur <= 0:
        return i, j
    allowed = seed_avg + _allowance(seed_avg, knobs.flex_pct, knobs.flex_eur)
    max_s = knobs.max_s
    while slots[j][1] - slots[i][0] < max_s - EPS_S:
        options = []
        for side, k in ((-1, i - 1), (1, j + 1)):
            if not 0 <= k < len(slots):
                continue
            left, right = (k, j) if side < 0 else (i, k)
            joined = slots[i][0] - slots[k][1] if side < 0 else slots[k][0] - slots[j][1]
            if (
                joined <= GAP_S
                and slots[k][2] <= knobs.ceiling + PRICE_EPS
                and slots[k][2] <= allowed + PRICE_EPS
                and slots[right][1] - slots[left][0] <= max_s + EPS_S
            ):
                options.append((slots[k][2], side, k))
        if not options:
            break
        _price_k, side, k = min(options)
        i, j = (k, j) if side < 0 else (i, k)
    return i, j


def _window(slots: list[Slot], seed, knobs: WindowKnobs) -> Slot:
    avg, i, j = seed
    i, j = grow(slots, i, j, avg, knobs)
    return (slots[i][0], slots[j][1], _avg(slots, i, j))


def _daily_deadline(day: datetime, hours: float) -> float:
    """Local wall-clock ``hours`` on ``day``. 24 is the following midnight."""
    minutes = round(min(24.0, max(0.0, hours)) * 60)
    if minutes >= 24 * 60:
        return day_start(day, 1).timestamp()
    return day_start(day, 0).replace(hour=minutes // 60, minute=minutes % 60).timestamp()


def _trip_day(now: datetime, tomorrow_valid: bool) -> datetime:
    """Local midnight of the daily-trip deadline day: the epoch's second day when published, else its first."""
    return day_start(now, 1 if tomorrow_valid else 0)


def _trip_range(now: datetime, knobs: WindowKnobs, tomorrow_valid: bool) -> tuple[float, float]:
    """(start, deadline) of the epoch anchored at ``now``.

    The start is the last charge-after hour before the deadline; equal hours span a day.
    """
    day = _trip_day(now, tomorrow_valid)
    start_day = day if knobs.daily_after_h < knobs.daily_h else day_start(day, -1)
    return _daily_deadline(start_day, knobs.daily_after_h), _daily_deadline(day, knobs.daily_h)


def _covered_s(windows, lo: float, hi: float) -> float:
    spans = sorted((max(s, lo), min(e, hi)) for s, e, *_ in windows if min(e, hi) > max(s, lo))
    total, reach = 0.0, lo
    for start, end in spans:
        if end > reach:
            total += end - max(start, reach)
            reach = end
    return total


def trip_window(slots, now, windows, knobs: WindowKnobs, tomorrow_valid: bool, seen: float | None = None):
    """Extra (start_ts, end_ts, avg) window in the daily-trip range, or ``None``.

    Needed only when ``windows`` give less than ``min_h`` between the charge-after
    hour (or ``seen``, when later) and the charge-before deadline. It ignores the
    price ceiling for its seed and never grows out of the range.
    """
    start, deadline = _trip_range(now, knobs, tomorrow_valid)
    lo = start if seen is None else max(start, seen)
    if deadline - lo < knobs.min_s - EPS_S or _covered_s(windows, lo, deadline) + EPS_S >= knobs.min_s:
        return None
    head = [s for s in slots if s[0] >= lo - TS_EPS_S and s[1] <= deadline + EPS_S]
    seed = find_seed(head, knobs.min_s)
    if seed is None:
        return None
    return _window(head, seed, knobs)


def _prefer_daily_trip(slots: list[Slot], seed, knobs: WindowKnobs, deadline: float):
    """Cheapest min window ending by ``deadline``, when it is within the looser price allowance."""
    if knobs.daily_pct <= 0 and knobs.daily_eur <= 0:
        return seed
    early = find_seed([s for s in slots if s[1] <= deadline + EPS_S], knobs.min_s)
    if early is None or early[0] > knobs.ceiling + PRICE_EPS:
        return seed
    if early[0] <= seed[0] + _allowance(seed[0], knobs.daily_pct, knobs.daily_eur) + PRICE_EPS:
        return early
    return seed


def choose_windows(slots: list[Slot], now: datetime, knobs: WindowKnobs, tomorrow_valid: bool):
    """Up to two (start_ts, end_ts, avg) windows and a reason code."""
    if not slots:
        return [], "no_slots"
    seed = find_seed(slots, knobs.min_s)
    if seed is None or seed[0] > knobs.ceiling + PRICE_EPS:
        return [], "no_window"
    today_22 = day_start(now, 0).replace(hour=22).timestamp()
    tomorrow, day_after = _day_bounds(now, 1)
    deadline = _daily_deadline(_trip_day(now, tomorrow_valid), knobs.daily_h)
    seed = _prefer_daily_trip(slots, seed, knobs, deadline)
    windows = [_window(slots, seed, knobs)]
    first = windows[0]
    first_tomorrow = next((k for k, s in enumerate(slots) if s[0] >= tomorrow - TS_EPS_S), None)
    if tomorrow_valid and first_tomorrow is not None and not (first[0] < day_after and first[1] > today_22):
        follow = find_seed([s for s in slots[first_tomorrow:] if s[0] < day_after], knobs.min_s)
        if follow is not None and follow[0] <= knobs.ceiling + PRICE_EPS:
            shifted = (follow[0], follow[1] + first_tomorrow, follow[2] + first_tomorrow)
            windows.append(_window(slots, shifted, knobs))
    return windows, "planned"


def priced_out_spans(slots: list[Slot], ceiling: float) -> list[Slot]:
    """(start, end, avg) runs of touching slots priced above ``ceiling``.

    A missing slot splits a run, so an unknown price never stops surplus.
    """
    runs: list[list[Slot]] = []
    for slot in sorted(slots):
        if slot[2] <= ceiling + PRICE_EPS:
            continue
        if runs and slot[0] - runs[-1][-1][1] <= GAP_S:
            runs[-1].append(slot)
        else:
            runs.append([slot])
    return [(run[0][0], run[-1][1], weighted_avg(run)) for run in runs]


def plan(
    attrs: Mapping | None,
    now: datetime,
    settings: Settings,
    *,
    lat: float,
    lon: float,
    today_kwh: float | None = None,
    tomorrow_kwh: float | None = None,
    history: dict | None = None,
    epoch_seen: float | None = None,
    periods=None,
) -> Plan:
    knobs = WindowKnobs.from_settings(settings)
    limit = float(settings.offsun_kw)
    published = tomorrow_prices_ok(attrs)
    slots, offset = epoch_curve(price_slots(attrs, now), now, history, published) if attrs is not None else ([], 0)
    forecast = solar_periods(now, today_kwh, tomorrow_kwh, lat, lon, history, periods)

    def next_published(anchor_offset: int) -> bool:
        """Wall-clock tomorrow follows ``tomorrow_valid``. An earlier day follows its slots."""
        if anchor_offset == 0:
            return published
        return bool(_on_day(slots, *_day_bounds(now, anchor_offset + 1)))

    def epoch_at(anchor_offset: int):
        """(searchable slots, noon anchor, next-day published) for the epoch starting ``anchor_offset`` days away."""
        searchable = mask_slots(_on_day(slots, *_day_bounds(now, anchor_offset, days=2)), forecast, limit)
        return searchable, day_start(now, anchor_offset).replace(hour=12), next_published(anchor_offset)

    carried, trips = [], []
    if attrs is None:
        windows, reason = [], "no_source"
    else:
        search, anchor, valid = epoch_at(offset)
        windows, reason = choose_windows(search, anchor, knobs, valid)
        previous_trips = []
        if epoch_seen is not None:
            earlier, earlier_anchor, earlier_valid = epoch_at(offset - 1)
            previous, _reason = choose_windows(earlier, earlier_anchor, knobs, earlier_valid)
            earlier_trip = trip_window(earlier, earlier_anchor, previous, knobs, earlier_valid)
            previous_trips = [] if earlier_trip is None else [earlier_trip]
            carried = carry_windows(windows, previous, epoch_seen)
            if carried and not windows:
                reason = "planned"
            windows = list(windows) + carried
        trip = trip_window(search, anchor, windows, knobs, valid, epoch_seen)
        trips = [] if trip is None else [trip]
        trips += carry_windows(list(windows) + trips, previous_trips, epoch_seen)
    priced_out = priced_out_spans(slots, float(settings.surplus_charging_price_ceiling))
    epoch_ts = None if attrs is None else epoch_day(slots, now, offset, published)
    usable_end = usable_solar_end(forecast, now, limit)
    use_tomorrow = published and (usable_end is None or now.timestamp() >= usable_end)
    gating_kwh = tomorrow_kwh if use_tomorrow else today_kwh
    enough_kwh = float(settings.solar_enough_kwh)

    def dt(ts):
        return datetime.fromtimestamp(ts, now.tzinfo)

    return Plan(
        windows=[Window(dt(s), dt(e), avg) for s, e, avg in windows],
        blocked=[(dt(s), dt(e)) for s, e in blocked_ranges(forecast, limit)],
        reason=reason,
        tomorrow_ok=published,
        today_kwh=today_kwh,
        tomorrow_kwh=tomorrow_kwh,
        usable_end=None if usable_end is None else dt(usable_end),
        gating_day="tomorrow" if use_tomorrow else "today",
        gating_kwh=gating_kwh,
        enough=enough_kwh > 0 and gating_kwh is not None and gating_kwh >= enough_kwh,
        epoch_start=None if epoch_ts is None else dt(epoch_ts),
        epoch_seen=None if epoch_seen is None else dt(epoch_seen),
        carried=len(carried),
        surplus_priced_out=[Window(dt(s), dt(e), avg) for s, e, avg in priced_out],
        trip_windows=[Window(dt(s), dt(e), avg) for s, e, avg in trips],
    )
