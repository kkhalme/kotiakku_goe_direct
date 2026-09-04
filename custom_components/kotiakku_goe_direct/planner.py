"""Charge-window planner.

Find the cheapest contiguous windowMinHours seed on the remaining
(off-sun-blocked, forecast-clipped) spot curve, then grow one native slot
at a time toward the cheaper neighbor while the duration-weighted average
stays under flex headroom and at most windowMaxHours. At most one window
is appended; the result is still a list so more windows can be added later.
The price ceiling is a safety abort on the seed average and a hard-no on
grow neighbors; it does not score the seed. Freeze holds a planned set so
it does not slide every 15 minutes.
"""

from __future__ import annotations

import datetime

SLOT_SECONDS = 900
GAP_S = 60
TARGET_EPS_S = 30
HOUR_EPS = 0.001
PRICE_EPS = 0.0000001
MAX_WINDOWS = 16
DEFAULT_MIN_HOURS = 2.0
DEFAULT_MAX_HOURS = 5.0
DEFAULT_CEILING = 0.2
DEFAULT_FLEX_PCT = 20.0
DEFAULT_FLEX_EUR = 0.02

POLICY_SOLAR_PRIORITY = "SolarPriority"
POLICY_FORCE_ON = "Force on"
_LEGACY_POLICIES = {
    "Cheapest": POLICY_SOLAR_PRIORITY,
    "Supercheap": POLICY_SOLAR_PRIORITY,
    "Longest": POLICY_SOLAR_PRIORITY,
    "Earliest": POLICY_SOLAR_PRIORITY,
}


def _to_ts(clock, value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(clock.as_timestamp(value))
    except Exception:
        pass
    try:
        parsed = clock.parse_datetime(str(value))
    except Exception:
        parsed = None
    if parsed is None:
        return None
    try:
        return float(clock.as_timestamp(parsed))
    except Exception:
        return None


def _to_price(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _iso(clock, ts):
    if ts is None:
        return None
    try:
        return clock.utc_from_timestamp(float(ts)).isoformat()
    except Exception:
        return None


def _first_present(attrs, keys):
    for key in keys:
        val = _dict_get(attrs, key)
        if val is not None and val != "" and val != []:
            return val
    return None


def _dict_get(item, key):
    try:
        return item.get(key)
    except Exception:
        try:
            return item[key]
        except Exception:
            return None


def _parse_dict_slot(clock, item):
    start = _to_ts(clock, _first_present(item, ("start", "from", "begin")))
    end = _to_ts(clock, _first_present(item, ("end", "to", "until")))
    price = _to_price(_first_present(item, ("value", "price", "price_ct")))
    if start is None or end is None or price is None or end <= start:
        return None
    return [start, end, price]


def _slots_from_values(values, day_start_ts):
    slots = []
    if values is None:
        return slots
    try:
        n = len(values)
    except Exception:
        return slots
    if n <= 0 or day_start_ts is None:
        return slots
    if n == 96:
        step = 900
    elif n == 24:
        step = 3600
    elif n == 48:
        step = 1800
    else:
        step = int(86400 / n)
        if step <= 0:
            return slots
    for i, raw in enumerate(values):
        price = _to_price(raw)
        if price is not None:
            start = day_start_ts + (i * step)
            slots.append([start, start + step, price])
    return slots


def _parse_series(clock, series, day_start_ts):
    slots = []
    if series is None:
        return slots
    try:
        n = len(series)
    except Exception:
        return slots
    if n == 0:
        return slots
    first = series[0]
    as_dict = _parse_dict_slot(clock, first) if first is not None else None
    if as_dict is not None or (first is not None and not isinstance(first, (int, float))):
        for item in series:
            parsed = _parse_dict_slot(clock, item)
            if parsed is not None:
                slots.append(parsed)
        return slots
    return _slots_from_values(series, day_start_ts)


def collect_slots(clock, attrs, now_dt):
    today_raw = _first_present(attrs, ["raw_today", "raw_today_prices", "today"])
    tomorrow_raw = _first_present(
        attrs, ["raw_tomorrow", "raw_tomorrow_prices", "tomorrow"]
    )
    today_start = None
    tomorrow_start = None
    try:
        today_start = clock.as_timestamp(clock.start_of_local_day(now_dt))
        tomorrow_start = clock.as_timestamp(
            clock.start_of_local_day(now_dt + datetime.timedelta(days=1))
        )
    except Exception:
        pass
    slots = _parse_series(clock, today_raw, today_start)
    slots.extend(_parse_series(clock, tomorrow_raw, tomorrow_start))
    slots.sort()
    return slots


def tomorrow_ok(clock, attrs, slots):
    flag = _dict_get(attrs, "tomorrow_valid")
    if flag is True or flag in ("on", "true", "True"):
        return True
    try:
        tomorrow_start = clock.as_timestamp(
            clock.start_of_local_day(clock.now() + datetime.timedelta(days=1))
        )
    except Exception:
        tomorrow_start = None
    if tomorrow_start is None:
        return False
    return any(slot[0] >= tomorrow_start - 1 for slot in slots)


def _overlaps(slot, start, end):
    return slot[0] < end and slot[1] > start


def _norm_blocked(blocked):
    if not blocked:
        return []
    out = []
    for item in blocked:
        start = end = None
        if isinstance(item, dict):
            start = item.get("start")
            end = item.get("end")
        else:
            try:
                start, end = item[0], item[1]
            except (TypeError, ValueError, IndexError):
                continue
        try:
            start = float(start)
            end = float(end)
        except (TypeError, ValueError):
            continue
        if end > start:
            out.append((start, end))
    out.sort()
    return out


def _blocked_match(left, right):
    left = _norm_blocked(left)
    right = _norm_blocked(right)
    if len(left) != len(right):
        return False
    for (s1, e1), (s2, e2) in zip(left, right):
        if abs(s1 - s2) > 1 or abs(e1 - e2) > 1:
            return False
    return True


def drop_blocked(slots, blocked):
    """Drop price slots that overlap surplus-hour ranges. Gaps split islands."""
    blocked = _norm_blocked(blocked)
    if not blocked:
        return slots
    return [
        slot
        for slot in slots
        if not any(_overlaps(slot, start, end) for start, end in blocked)
    ]


def clip_slots_to_forecast(clock, slots, remaining_today, tomorrow_kwh, now_dt):
    """Keep price slots on days that have solar kWh.

    If both forecast values are missing, return all price slots (prices-only
    fallback). A day stays only when that day's kWh is present.
    """
    if remaining_today is None and tomorrow_kwh is None:
        return slots
    try:
        today_start = float(clock.as_timestamp(clock.start_of_local_day(now_dt)))
        tomorrow_start = float(
            clock.as_timestamp(
                clock.start_of_local_day(now_dt + datetime.timedelta(days=1))
            )
        )
    except Exception:
        return slots
    day_after = tomorrow_start + 86400.0
    out = []
    for slot in slots:
        start = slot[0]
        if remaining_today is not None and start < tomorrow_start - 1:
            if start >= today_start - 1:
                out.append(slot)
        elif tomorrow_kwh is not None and tomorrow_start - 1 <= start < day_after:
            out.append(slot)
    return out


def _win_dur(w):
    return w["end"] - w["start"]


def _copy_windows(windows):
    return [{"avg": w["avg"], "start": w["start"], "end": w["end"]} for w in windows]


def _avg_span(slots, start, end):
    total_p = 0.0
    total_d = 0.0
    for slot in slots:
        if slot[0] >= start - 1 and slot[1] <= end + 1:
            dur = slot[1] - slot[0]
            if dur > 0:
                total_d = total_d + dur
                total_p = total_p + (slot[2] * dur)
    if total_d <= 0:
        return 0.0
    return total_p / total_d


def _current_slot_ts(now_ts):
    return (now_ts // SLOT_SECONDS) * SLOT_SECONDS


def flex_headroom(seed_avg, flex_pct, flex_euro):
    extras = []
    if flex_pct is not None and flex_pct > 0:
        extras.append(abs(float(seed_avg)) * float(flex_pct) / 100.0)
    if flex_euro is not None and flex_euro > 0:
        extras.append(float(flex_euro))
    if not extras:
        return 0.0
    return max(extras)


def find_seed(slots, min_s, now_ts):
    """Cheapest contiguous windowMinHours run. Ignores the price ceiling."""
    n = len(slots)
    if n == 0:
        return None
    slot_ts = _current_slot_ts(now_ts)
    best = None
    for i in range(n):
        if slots[i][0] < slot_ts - 1:
            continue
        total_p = 0.0
        total_d = 0.0
        for j in range(i, n):
            if j > i and slots[j][0] - slots[j - 1][1] > GAP_S:
                break
            dur = slots[j][1] - slots[j][0]
            if dur <= 0:
                break
            total_d = total_d + dur
            total_p = total_p + (slots[j][2] * dur)
            if total_d + TARGET_EPS_S < min_s:
                continue
            if slots[j][1] <= now_ts:
                break
            avg = total_p / total_d
            start = slots[i][0]
            if best is None:
                best = (avg, start, slots[j][1], i, j)
            else:
                cheaper = avg < best[0] - PRICE_EPS
                same = abs(avg - best[0]) <= PRICE_EPS
                if cheaper or (same and start < best[1]):
                    best = (avg, start, slots[j][1], i, j)
            break
    return best


def grow_window(slots, left_i, right_j, seed_avg, max_s, now_ts, ceiling, flex_pct, flex_euro):
    """Extend the seed by one native slot per step toward the cheaper neighbor."""
    pct_on = flex_pct is not None and flex_pct > 0
    euro_on = flex_euro is not None and flex_euro > 0
    if not pct_on and not euro_on:
        start = slots[left_i][0]
        end = slots[right_j][1]
        return {"avg": _avg_span(slots, start, end), "start": start, "end": end}
    allowed = seed_avg + flex_headroom(seed_avg, flex_pct, flex_euro)
    slot_ts = _current_slot_ts(now_ts)
    n = len(slots)
    while True:
        start = slots[left_i][0]
        end = slots[right_j][1]
        if end - start >= max_s - TARGET_EPS_S:
            break
        candidates = []
        if left_i > 0:
            nb = slots[left_i - 1]
            if (
                slots[left_i][0] - nb[1] <= GAP_S
                and nb[0] >= slot_ts - 1
                and nb[2] <= ceiling + PRICE_EPS
            ):
                new_end = end
                new_start = nb[0]
                if new_end - new_start <= max_s + TARGET_EPS_S:
                    new_avg = _avg_span(slots, new_start, new_end)
                    if new_avg <= allowed + PRICE_EPS:
                        candidates.append((nb[2], -1, left_i - 1))
        if right_j + 1 < n:
            nb = slots[right_j + 1]
            if (
                nb[0] - slots[right_j][1] <= GAP_S
                and nb[2] <= ceiling + PRICE_EPS
            ):
                new_start = start
                new_end = nb[1]
                if new_end - new_start <= max_s + TARGET_EPS_S:
                    new_avg = _avg_span(slots, new_start, new_end)
                    if new_avg <= allowed + PRICE_EPS:
                        candidates.append((nb[2], 1, right_j + 1))
        if not candidates:
            break
        candidates.sort(key=lambda item: (item[0], item[1]))
        _price, side, idx = candidates[0]
        if side < 0:
            left_i = idx
        else:
            right_j = idx
    start = slots[left_i][0]
    end = slots[right_j][1]
    return {"avg": _avg_span(slots, start, end), "start": start, "end": end}


def pick_windows(slots, min_s, max_s, ceiling, now_ts, flex_pct, flex_euro):
    """Return a list of at most one grown window."""
    seed = find_seed(slots, min_s, now_ts)
    if seed is None:
        return []
    avg, _start, _end, left_i, right_j = seed
    if avg > ceiling + PRICE_EPS:
        return []
    return [grow_window(slots, left_i, right_j, avg, max_s, now_ts, ceiling, flex_pct, flex_euro)]


def current_or_next(windows, now_ts):
    current = None
    nxt = None
    for w in windows:
        if w["end"] > now_ts:
            if w["start"] <= now_ts:
                current = w
            elif nxt is None or w["start"] < nxt["start"]:
                nxt = w
    return current if current is not None else nxt


def last_end(windows):
    end = None
    for w in windows:
        if end is None or w["end"] > end:
            end = w["end"]
    return end


def _params_match(prev, min_hours, max_hours, ceiling, flex_pct, flex_euro, blocked=None):
    if prev is None:
        return False
    if abs(prev["min_hours"] - min_hours) >= HOUR_EPS:
        return False
    if abs(prev["max_hours"] - max_hours) >= HOUR_EPS:
        return False
    if abs(prev["ceiling"] - ceiling) >= PRICE_EPS:
        return False
    if abs(float(prev.get("flex_pct", 0)) - flex_pct) >= HOUR_EPS:
        return False
    if abs(float(prev.get("flex_euro", 0)) - flex_euro) >= PRICE_EPS:
        return False
    if not _blocked_match(prev.get("blocked"), blocked):
        return False
    return True


def _choice(windows, horizon, reason):
    return {"windows": windows, "horizon": horizon, "reason": reason}


def _set_better(new, old):
    if new is None or len(new) == 0:
        return False
    if old is None or len(old) == 0:
        return True
    n = min(len(new), len(old))
    for i in range(n):
        if new[i]["avg"] < old[i]["avg"] - PRICE_EPS:
            return True
        if new[i]["avg"] > old[i]["avg"] + PRICE_EPS:
            return False
    return len(new) > len(old)


def choose(
    slots,
    min_hours,
    max_hours,
    ceiling,
    now_ts,
    prev,
    blocked=None,
    flex_pct=DEFAULT_FLEX_PCT,
    flex_euro=DEFAULT_FLEX_EUR,
):
    if not slots:
        return _choice([], None, "no_slots")
    horizon = slots[-1][1]
    blocked = _norm_blocked(blocked)
    search = drop_blocked(slots, blocked)
    min_s = min_hours * 3600.0
    max_s = max_hours * 3600.0
    new_windows = pick_windows(
        search, min_s, max_s, ceiling, now_ts, flex_pct, flex_euro
    )
    prev_ok = False
    prev_windows = None
    if _params_match(prev, min_hours, max_hours, ceiling, flex_pct, flex_euro, blocked):
        prev_windows = prev.get("windows")
        prev_ok = prev_windows is not None and len(prev_windows) > 0
    last = last_end(prev_windows) if prev_ok else None
    if prev_ok and last is not None and last > now_ts:
        prev_horizon = prev.get("horizon")
        horizon_grew = prev_horizon is None or horizon > prev_horizon + GAP_S
        if horizon_grew and _set_better(new_windows, prev_windows):
            return _choice(new_windows, horizon, "switched")
        return _choice(prev_windows, prev["horizon"], "frozen")
    if prev_ok and last is not None and last <= now_ts:
        prev_horizon = prev["horizon"]
        if prev_horizon is None or horizon <= prev_horizon + GAP_S:
            if not new_windows:
                return _choice(prev_windows, prev["horizon"], "idle_after_window")
            return _choice(new_windows, horizon, "planned")
        if not new_windows:
            return _choice(prev_windows, prev["horizon"], "no_window")
        return _choice(new_windows, horizon, "planned_new_horizon")
    if not new_windows:
        return _choice([], horizon, "no_window")
    return _choice(new_windows, horizon, "planned")


def clamp_hours(min_hours, max_hours):
    min_hours = _to_price(min_hours)
    max_hours = _to_price(max_hours)
    if min_hours is None or min_hours <= 0:
        min_hours = DEFAULT_MIN_HOURS
    if max_hours is None or max_hours <= 0:
        max_hours = DEFAULT_MAX_HOURS
    min_hours = min(24.0, max(0.25, min_hours))
    max_hours = min(24.0, max(0.25, max_hours))
    if min_hours > max_hours:
        min_hours, max_hours = max_hours, min_hours
    return min_hours, max_hours


def clamp_flex(flex_pct, flex_euro):
    flex_pct = _to_price(flex_pct)
    flex_euro = _to_price(flex_euro)
    if flex_pct is None or flex_pct < 0:
        flex_pct = 0.0
    if flex_euro is None or flex_euro < 0:
        flex_euro = 0.0
    return flex_pct, flex_euro


def iso_windows(clock, windows):
    out = []
    for i, w in enumerate(windows):
        out.append(
            {
                "rank": i + 1,
                "start": _iso(clock, w["start"]),
                "end": _iso(clock, w["end"]),
                "avg": w["avg"],
            }
        )
    return out


def now_in_windows(windows, now_ts):
    for w in windows:
        if w["start"] <= now_ts < w["end"]:
            return True
    return False


def restore_policy(policy):
    if policy in _LEGACY_POLICIES:
        return _LEGACY_POLICIES[policy]
    return policy


def until_unplug_step(override, plugged, seen):
    """Advance the until-unplug override. Returns ``(override, seen)``.

    ``seen`` means the car was plugged while the override was on. The
    override clears when that car **unplugs** after that, not when the
    battery is full (Complete is still plugged). Turning the override
    off clears ``seen``. Policy is not changed.
    """
    if not override:
        return False, False
    if plugged:
        return True, True
    if seen:
        return False, False
    return True, False


def charger_full_power(policy, result, now_ts, *, enough_solar=False, until_unplug=False):
    """Whether this charger wants 22 kW right now. Pure; no Home Assistant."""
    if until_unplug:
        return True
    policy = restore_policy(policy)
    if policy == POLICY_FORCE_ON:
        return True
    if policy != POLICY_SOLAR_PRIORITY:
        return False
    if enough_solar:
        return False
    windows = []
    if isinstance(result, dict):
        windows = result.get("raw_windows") or []
    return now_in_windows(windows, now_ts)


def _empty_result(
    min_hours,
    max_hours,
    ceiling,
    flex_pct,
    flex_euro,
    source_entity,
    reason="no_source",
):
    return {
        "start": None,
        "end": None,
        "avg": None,
        "min_hours": min_hours,
        "max_hours": max_hours,
        "ceiling": ceiling,
        "flex_pct": flex_pct,
        "flex_euro": flex_euro,
        "count": 0,
        "windows": [],
        "horizon": None,
        "reason": reason,
        "tomorrow_ok": False,
        "source_entity": source_entity,
        "slot_count": 0,
        "blocked_ts": [],
        "blocked": [],
        "raw_windows": [],
        "horizon_ts": None,
    }


def plan(
    clock,
    attrs,
    *,
    min_hours=DEFAULT_MIN_HOURS,
    max_hours=DEFAULT_MAX_HOURS,
    ceiling=DEFAULT_CEILING,
    flex_pct=DEFAULT_FLEX_PCT,
    flex_euro=DEFAULT_FLEX_EUR,
    prev=None,
    source_entity="",
    blocked=None,
    remaining_today=None,
    tomorrow_kwh=None,
):
    min_hours, max_hours = clamp_hours(min_hours, max_hours)
    flex_pct, flex_euro = clamp_flex(flex_pct, flex_euro)
    ceiling = _to_price(ceiling)
    if ceiling is None:
        ceiling = DEFAULT_CEILING
    now_dt = clock.now()
    now_ts = float(clock.as_timestamp(now_dt))
    empty = _empty_result(
        min_hours, max_hours, ceiling, flex_pct, flex_euro, source_entity
    )
    if attrs is None:
        return empty
    slots = collect_slots(clock, attrs, now_dt)
    slots = clip_slots_to_forecast(clock, slots, remaining_today, tomorrow_kwh, now_dt)
    chosen = choose(
        slots,
        min_hours,
        max_hours,
        ceiling,
        now_ts,
        prev,
        blocked=blocked,
        flex_pct=flex_pct,
        flex_euro=flex_euro,
    )
    iso_ws = iso_windows(clock, chosen["windows"])
    active = current_or_next(chosen["windows"], now_ts)
    blocked_ts = _norm_blocked(blocked)
    result = {
        "windows": iso_ws,
        "count": len(iso_ws),
        "min_hours": min_hours,
        "max_hours": max_hours,
        "ceiling": ceiling,
        "flex_pct": flex_pct,
        "flex_euro": flex_euro,
        "horizon": _iso(clock, chosen["horizon"]),
        "reason": chosen["reason"],
        "tomorrow_ok": tomorrow_ok(clock, attrs, slots),
        "source_entity": source_entity,
        "slot_count": len(slots),
        "start": None,
        "end": None,
        "avg": None,
        "raw_windows": chosen["windows"],
        "horizon_ts": chosen["horizon"],
        "blocked_ts": blocked_ts,
        "blocked": [
            {"start": _iso(clock, start), "end": _iso(clock, end)}
            for start, end in blocked_ts
        ],
    }
    if active is not None:
        result["start"] = _iso(clock, active["start"])
        result["end"] = _iso(clock, active["end"])
        result["avg"] = active["avg"]
    for i in range(1, MAX_WINDOWS + 1):
        result["window_%s_start" % i] = None
        result["window_%s_end" % i] = None
        result["window_%s_avg" % i] = None
    for i, w in enumerate(iso_ws):
        n = i + 1
        if n > MAX_WINDOWS:
            break
        result["window_%s_start" % n] = w["start"]
        result["window_%s_end" % n] = w["end"]
        result["window_%s_avg" % n] = w["avg"]
    return result


def prev_from_result(clock, result):
    if not result:
        return None
    min_hours = _to_price(result.get("min_hours"))
    max_hours = _to_price(result.get("max_hours"))
    ceiling = _to_price(result.get("ceiling"))
    if min_hours is None or max_hours is None or ceiling is None:
        return None
    flex_pct, flex_euro = clamp_flex(result.get("flex_pct"), result.get("flex_euro"))
    raw = result.get("raw_windows")
    windows = []
    if isinstance(raw, list) and raw and isinstance(raw[0], dict) and "start" in raw[0]:
        for item in raw:
            start = item.get("start")
            end = item.get("end")
            avg = item.get("avg")
            if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                windows.append({"start": float(start), "end": float(end), "avg": avg})
    if not windows:
        for item in result.get("windows") or []:
            start = _to_ts(clock, _dict_get(item, "start"))
            end = _to_ts(clock, _dict_get(item, "end"))
            avg = _to_price(_dict_get(item, "avg"))
            if start is not None and end is not None and avg is not None:
                windows.append({"start": start, "end": end, "avg": avg})
    if not windows:
        return None
    horizon = result.get("horizon_ts")
    if horizon is None:
        horizon = _to_ts(clock, result.get("horizon"))
    blocked = result.get("blocked_ts")
    if blocked is None:
        blocked = result.get("blocked")
    return {
        "windows": windows,
        "min_hours": min_hours,
        "max_hours": max_hours,
        "ceiling": ceiling,
        "flex_pct": flex_pct,
        "flex_euro": flex_euro,
        "horizon": horizon,
        "blocked": _norm_blocked(blocked),
    }
