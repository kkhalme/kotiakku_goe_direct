"""Cheap charge-window planner and solar gating. Pure; no Home Assistant.

The plan is a function of prices, forecasts, the off-sun mask and knobs,
never of the wall clock (only via local day boundaries). A cheapest window
that already ended stays the plan, so 15-minute ticks cannot slide it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone, tzinfo

from .model import Plan, Settings, Window

GAP_S = 60
EPS_S = 30
PRICE_EPS = 1e-7
SAMPLE_S = 900
PAST_DAYS = 2

Slot = tuple[float, float, float]


def local_midnight(day: date, tz: tzinfo) -> datetime:
    return datetime.combine(day, time(0), tzinfo=tz)


def day_start(now: datetime, offset: int = 0) -> datetime:
    """Local midnight ``offset`` days from ``now``, stepped via noon so DST cannot skip a date."""
    noon = local_midnight(now.date(), now.tzinfo).replace(hour=12)
    return local_midnight((noon + timedelta(days=offset)).date(), now.tzinfo)


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
    step = {96: 900, 48: 1800, 24: 3600}.get(len(raw), 86400 // len(raw))
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
    today = local_midnight(now.date(), tz)
    tomorrow = local_midnight(now.date() + timedelta(days=1), tz)
    slots = _series(_first(attrs, ("raw_today", "today")), today, tz)
    slots += _series(_first(attrs, ("raw_tomorrow", "tomorrow")), tomorrow, tz)
    return sorted(slots)


def imported_price_cache(stored: dict | None) -> tuple[dict, dict]:
    """v1 ``price_days`` / ``epoch_seen`` in the new store's ``days`` / ``seen`` shape.

    The legacy ``seen`` map is surplus state, not the price epoch, and is ignored.
    """
    if not isinstance(stored, dict):
        return {}, {}
    days = {}
    for key, entry in (stored.get("price_days") or {}).items():
        if not isinstance(entry, dict) or entry.get("start") is None:
            continue
        slots = []
        for raw in entry.get("slots") or []:
            try:
                slot = [float(raw[0]), float(raw[1]), float(raw[2])]
            except (TypeError, ValueError, IndexError):
                continue
            if slot[1] > slot[0]:
                slots.append(slot)
        if not slots:
            continue
        kwh = entry.get("kwh")
        days[str(key)] = {"start": float(entry["start"]), "slots": slots, "kwh": None if kwh is None else float(kwh)}
    seen = {}
    for key, raw in (stored.get("epoch_seen") or {}).items():
        try:
            seen[str(key)] = float(raw)
        except (TypeError, ValueError):
            continue
    return days, seen


def remember_day(days: dict | None, now: datetime, live: list[Slot], today_kwh: float | None) -> dict:
    """Cache today's spot slots and last known solar kWh. Empty curves do not erase a day."""
    start, end = day_start(now, 0).timestamp(), day_start(now, 1).timestamp()
    oldest = day_start(now, -PAST_DAYS).timestamp()
    out = {
        key: entry
        for key, entry in (days or {}).items()
        if isinstance(entry, dict) and (entry.get("start") or 0) >= oldest - 1
    }
    today = [list(slot) for slot in live if start - 1 <= slot[0] < end - 1]
    if today:
        key = now.date().isoformat()
        previous = out.get(key) or {}
        out[key] = {
            "start": start,
            "slots": today,
            "kwh": today_kwh if today_kwh is not None else previous.get("kwh"),
        }
    return out


def _cached_days(days: dict | None) -> list[tuple[float, list[Slot], float | None]]:
    out = []
    for entry in (days or {}).values():
        if not isinstance(entry, dict) or entry.get("start") is None:
            continue
        slots = []
        for raw in entry.get("slots") or []:
            try:
                slot = (float(raw[0]), float(raw[1]), float(raw[2]))
            except (TypeError, ValueError, IndexError):
                continue
            if slot[1] > slot[0]:
                slots.append(slot)
        kwh = entry.get("kwh")
        out.append((float(entry["start"]), slots, None if kwh is None else float(kwh)))
    return out


def epoch_curve(attrs, now, days, _today_kwh, _tomorrow_kwh) -> tuple[list[Slot], int]:
    """Live prices plus cached earlier days. Offset 0 is today+tomorrow; -1 is yesterday+today."""
    live = price_slots(attrs, now)
    starts = {k: day_start(now, k).timestamp() for k in range(-PAST_DAYS, 3)}
    merged = list(live)
    for start, slots, _kwh in _cached_days(days):
        for k in range(-PAST_DAYS, 0):
            if abs(start - starts[k]) > 1:
                continue
            for slot in slots:
                if starts[k] - 1 <= slot[0] < starts[k + 1] - 1 and not any(
                    slot[0] < other[1] and slot[1] > other[0] for other in live
                ):
                    merged.append(slot)
    merged.sort()
    if any(slot[0] >= starts[1] - 1 for slot in merged):
        offset = 0
    elif any(starts[-1] - 1 <= slot[0] < starts[0] - 1 for slot in merged):
        offset = -1
    else:
        offset = 0
    return merged, offset


def epoch_day(slots: list[Slot], now: datetime, offset: int) -> float:
    """Local midnight of the epoch's newest searchable day. It moves when prices arrive, not at midnight."""
    newest, following = day_start(now, offset + 1).timestamp(), day_start(now, offset + 2).timestamp()
    if any(newest - 1 <= slot[0] < following - 1 for slot in slots):
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
        seen = {k: v for k, v in seen.items() if float(k) >= oldest - 1}
        seen[key] = first
    earlier = any(float(k) < epoch_start - 1 for k in seen)
    return seen, (float(first) if earlier else None)


def _span(slots: list[Slot], now: datetime, offset: int) -> list[Slot]:
    lo, hi = day_start(now, offset).timestamp(), day_start(now, offset + 2).timestamp()
    return [slot for slot in slots if lo - 1 <= slot[0] < hi - 1]


def carry_windows(windows, previous, seen_ts: float | None):
    """Previous-epoch windows that were already running when this epoch was first seen."""
    if seen_ts is None:
        return []
    out = []
    for window in previous:
        if not window[0] <= seen_ts < window[1]:
            continue
        if any(c[0] <= window[0] + 1 and c[1] >= window[1] - 1 for c in list(windows) + out):
            continue
        out.append(window)
    return out


def tomorrow_prices_ok(attrs: Mapping | None, slots: list[Slot], now: datetime) -> bool:
    flag = (attrs or {}).get("tomorrow_valid")
    if flag is True or str(flag).lower() in ("on", "true"):
        return True
    tomorrow = local_midnight(now.date() + timedelta(days=1), now.tzinfo).timestamp()
    return any(slot[0] >= tomorrow - 1 for slot in slots)


def solar_elevation_deg(ts: float, lat: float, lon: float) -> float:
    """Approximate solar elevation (no refraction)."""
    utc = datetime.fromtimestamp(ts, timezone.utc)
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
    while t < end - 1:
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


def blocked_hours(now, today_kwh, tomorrow_kwh, threshold, lat, lon, extra=()) -> list[tuple[float, float]]:
    if threshold <= 0:
        return []
    days = ((now.date(), today_kwh), (now.date() + timedelta(days=1), tomorrow_kwh), *extra)
    ranges = []
    for day, kwh in days:
        for start, end, value in hour_kwh(day, now.tzinfo, kwh, lat, lon):
            if value >= threshold:
                if ranges and start <= ranges[-1][1] + 1:
                    ranges[-1] = (ranges[-1][0], end)
                else:
                    ranges.append((start, end))
    return ranges


def usable_solar_end(now, today_kwh, threshold, lat, lon) -> float | None:
    """End of today's last hour whose expected kWh reaches the off-sun threshold."""
    limit = threshold if threshold > 0 else 1e-12
    ends = [end for _s, end, kwh in hour_kwh(now.date(), now.tzinfo, today_kwh, lat, lon) if kwh >= limit]
    return max(ends, default=None)


def _avg(slots: list[Slot], i: int, j: int) -> float:
    dur = sum(s[1] - s[0] for s in slots[i : j + 1])
    return sum(s[2] * (s[1] - s[0]) for s in slots[i : j + 1]) / dur


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


def grow(slots, i, j, seed_avg, max_s, ceiling, flex_pct, flex_eur) -> tuple[int, int]:
    """Add one native slot at a time on the cheaper side while under flex headroom."""
    extras = [abs(seed_avg) * flex_pct / 100.0] if flex_pct > 0 else []
    extras += [flex_eur] if flex_eur > 0 else []
    if not extras:
        return i, j
    allowed = seed_avg + max(extras)
    while slots[j][1] - slots[i][0] < max_s - EPS_S:
        options = []
        for side, k in ((-1, i - 1), (1, j + 1)):
            if not 0 <= k < len(slots):
                continue
            left, right = (k, j) if side < 0 else (i, k)
            joined = slots[i][0] - slots[k][1] if side < 0 else slots[k][0] - slots[j][1]
            if (
                joined <= GAP_S
                and slots[k][2] <= ceiling + PRICE_EPS
                and slots[right][1] - slots[left][0] <= max_s + EPS_S
                and _avg(slots, left, right) <= allowed + PRICE_EPS
            ):
                options.append((slots[k][2], side, k))
        if not options:
            break
        _price_k, side, k = min(options)
        i, j = (k, j) if side < 0 else (i, k)
    return i, j


def _window(slots, seed, max_s, ceiling, flex_pct, flex_eur):
    avg, i, j = seed
    i, j = grow(slots, i, j, avg, max_s, ceiling, flex_pct, flex_eur)
    return (slots[i][0], slots[j][1], _avg(slots, i, j))


def choose_windows(slots, blocked, now, min_h, max_h, ceiling, flex_pct, flex_eur):
    """Up to two (start_ts, end_ts, avg) windows and a reason code."""
    if not slots:
        return [], "no_slots"
    search = [s for s in slots if not any(s[0] < e and s[1] > b for b, e in blocked)]
    min_s, max_s = min_h * 3600.0, max_h * 3600.0
    seed = find_seed(search, min_s)
    if seed is None or seed[0] > ceiling + PRICE_EPS:
        return [], "no_window"
    windows = [_window(search, seed, max_s, ceiling, flex_pct, flex_eur)]
    tz = now.tzinfo
    today_22 = local_midnight(now.date(), tz).replace(hour=22).timestamp()
    tomorrow = local_midnight(now.date() + timedelta(days=1), tz).timestamp()
    day_after = local_midnight(now.date() + timedelta(days=2), tz).timestamp()
    first = windows[0]
    if any(s[0] >= tomorrow - 1 for s in search) and not (first[0] < day_after and first[1] > today_22):
        offset = next(k for k, s in enumerate(search) if s[0] >= tomorrow - 1)
        tom = [s for s in search[offset:] if s[0] < day_after]
        follow = find_seed(tom, min_s)
        if follow is not None and follow[0] <= ceiling + PRICE_EPS:
            shifted = (follow[0], follow[1] + offset, follow[2] + offset)
            windows.append(_window(search, shifted, max_s, ceiling, flex_pct, flex_eur))
    return windows, "planned"


def plan(
    attrs: Mapping | None,
    now: datetime,
    settings: Settings,
    today_kwh: float | None,
    tomorrow_kwh: float | None,
    lat: float,
    lon: float,
    history: dict | None = None,
    epoch_seen: float | None = None,
) -> Plan:
    min_h = min(24.0, max(0.25, float(settings.window_min_h)))
    max_h = min(24.0, max(0.25, float(settings.window_max_h)))
    min_h, max_h = min(min_h, max_h), max(min_h, max_h)
    threshold = float(settings.offsun_hour_kwh)
    slots, offset = ([], 0) if attrs is None else epoch_curve(attrs, now, history, today_kwh, tomorrow_kwh)
    extra = [
        (day_start(now, k).date(), kwh)
        for start, _cached, kwh in _cached_days(history)
        for k in range(-PAST_DAYS, 0)
        if kwh is not None and abs(start - day_start(now, k).timestamp()) <= 1
    ]
    blocked = blocked_hours(now, today_kwh, tomorrow_kwh, threshold, lat, lon, extra)
    knobs = (min_h, max_h, float(settings.electricity_price_ceiling), max(float(settings.window_flex_pct), 0.0), max(float(settings.window_flex_eur), 0.0))
    carried = []
    if attrs is None:
        windows, reason = [], "no_source"
    else:
        anchor = day_start(now, offset).replace(hour=12)
        windows, reason = choose_windows(_span(slots, now, offset), blocked, anchor, *knobs)
        if epoch_seen is not None:
            previous, _reason = choose_windows(_span(slots, now, offset - 1), blocked, day_start(now, offset - 1).replace(hour=12), *knobs)
            carried = carry_windows(windows, previous, epoch_seen)
            if carried and not windows:
                reason = "planned"
            windows = list(windows) + carried
    live = price_slots(attrs, now)
    tomorrow_ok = tomorrow_prices_ok(attrs, live, now)
    epoch_ts = None if attrs is None else epoch_day(slots, now, offset)
    usable_end = usable_solar_end(now, today_kwh, threshold, lat, lon)
    use_tomorrow = tomorrow_ok and (usable_end is None or now.timestamp() >= usable_end)
    gating_kwh = tomorrow_kwh if use_tomorrow else today_kwh
    enough_kwh = float(settings.solar_enough_kwh)
    tz = now.tzinfo

    def dt(ts):
        return datetime.fromtimestamp(ts, tz)

    return Plan(
        windows=[Window(dt(s), dt(e), avg) for s, e, avg in windows],
        blocked=[(dt(s), dt(e)) for s, e in blocked],
        reason=reason,
        tomorrow_ok=tomorrow_ok,
        today_kwh=today_kwh,
        tomorrow_kwh=tomorrow_kwh,
        usable_end=None if usable_end is None else dt(usable_end),
        gating_day="tomorrow" if use_tomorrow else "today",
        gating_kwh=gating_kwh,
        enough=enough_kwh > 0 and gating_kwh is not None and gating_kwh >= enough_kwh,
        epoch_start=None if epoch_ts is None else dt(epoch_ts),
        epoch_seen=None if epoch_seen is None else dt(epoch_seen),
        carried=len(carried),
    )
