"""Charge-window planner.

Same rules as the python_script version: contiguous slots, duration in
[min, max], every 15-minute price ≤ ceiling, then greedy cheapest / longest /
earliest / offsun. Off-sun is cheapest after dropping hours with enough
forecast energy. Freeze is per rank.
"""

from __future__ import annotations

import datetime

SLOT_SECONDS = 900
GAP_S = 60
TARGET_EPS_S = 30
HOUR_EPS = 0.001
PRICE_EPS = 0.0000001
MAX_WINDOWS = 16
RANKS = ("cheapest", "longest", "earliest", "offsun")


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
    """Drop price slots that overlap surplus-hour ranges. Gaps split windows."""
    blocked = _norm_blocked(blocked)
    if not blocked:
        return slots
    return [
        slot
        for slot in slots
        if not any(_overlaps(slot, start, end) for start, end in blocked)
    ]


def norm_rank(value):
    if value is None or value == "":
        return "cheapest"
    word = "".join(
        ch for ch in str(value).strip().lower() if ch not in (" ", "-", "_")
    )
    if word == "longest":
        return "longest"
    if word == "earliest":
        return "earliest"
    if word == "offsun":
        return "offsun"
    return "cheapest"


def _win_dur(w):
    return w["end"] - w["start"]


def _is_better_cand(rank, avg, start, dur, best_avg, best_start, best_dur):
    if best_start is None:
        return True
    longer = dur > best_dur + TARGET_EPS_S
    same_len = abs(dur - best_dur) <= TARGET_EPS_S
    cheaper = avg < best_avg - PRICE_EPS
    equal_avg = abs(avg - best_avg) <= PRICE_EPS
    earlier = start < best_start
    same_start = abs(start - best_start) <= TARGET_EPS_S
    if rank == "longest":
        return longer or (same_len and cheaper) or (same_len and equal_avg and earlier)
    if rank == "earliest":
        return earlier or (same_start and longer) or (same_start and same_len and cheaper)
    # cheapest and offsun: lowest average, then longer, then earlier
    return cheaper or (equal_avg and longer) or (equal_avg and same_len and earlier)


def _without_overlap(slots, start, end):
    return [slot for slot in slots if not _overlaps(slot, start, end)]


def pick_best(slots, min_s, max_s, ceiling, now_ts, rank="cheapest"):
    n = len(slots)
    if n == 0:
        return None
    slot_ts = (now_ts // SLOT_SECONDS) * SLOT_SECONDS
    best_avg = None
    best_start = None
    best_end = None
    best_dur = None
    for i in range(n):
        if slots[i][0] < slot_ts - 1 or slots[i][2] > ceiling + PRICE_EPS:
            continue
        total_p = 0.0
        total_d = 0.0
        end = None
        blocked = False
        j = i
        while j < n and not blocked:
            if j > i:
                gap = slots[j][0] - slots[j - 1][1]
                if gap > GAP_S:
                    blocked = True
            if not blocked and slots[j][2] > ceiling + PRICE_EPS:
                blocked = True
            if not blocked:
                dur = slots[j][1] - slots[j][0]
                if dur <= 0:
                    blocked = True
                else:
                    total_d = total_d + dur
                    if total_d > max_s + TARGET_EPS_S:
                        blocked = True
                    else:
                        total_p = total_p + (slots[j][2] * dur)
                        end = slots[j][1]
                        if total_d >= min_s - TARGET_EPS_S and end is not None and end > now_ts:
                            avg = total_p / total_d
                            start = slots[i][0]
                            if _is_better_cand(
                                rank,
                                avg,
                                start,
                                total_d,
                                best_avg,
                                best_start,
                                best_dur,
                            ):
                                best_avg = avg
                                best_start = start
                                best_end = end
                                best_dur = total_d
            j = j + 1
    if best_start is None:
        return None
    return [best_avg, best_start, best_end]


def pick_all(slots, min_s, max_s, ceiling, now_ts, rank="cheapest"):
    rank = norm_rank(rank)
    remaining = slots
    windows = []
    for _ in range(MAX_WINDOWS):
        best = pick_best(remaining, min_s, max_s, ceiling, now_ts, rank)
        if best is None:
            break
        windows.append({"avg": best[0], "start": best[1], "end": best[2]})
        remaining = _without_overlap(remaining, best[1], best[2])
    return windows


def _set_better(new, old, rank="cheapest"):
    if new is None or len(new) == 0:
        return False
    if old is None or len(old) == 0:
        return True
    n = min(len(new), len(old))
    for i in range(n):
        nd = _win_dur(new[i])
        od = _win_dur(old[i])
        if rank == "longest":
            if nd > od + TARGET_EPS_S:
                return True
            if nd < od - TARGET_EPS_S:
                return False
            if new[i]["avg"] < old[i]["avg"] - PRICE_EPS:
                return True
            if new[i]["avg"] > old[i]["avg"] + PRICE_EPS:
                return False
        elif rank == "earliest":
            if new[i]["start"] < old[i]["start"] - 1:
                return True
            if new[i]["start"] > old[i]["start"] + 1:
                return False
            if nd > od + TARGET_EPS_S:
                return True
            if nd < od - TARGET_EPS_S:
                return False
            if new[i]["avg"] < old[i]["avg"] - PRICE_EPS:
                return True
            if new[i]["avg"] > old[i]["avg"] + PRICE_EPS:
                return False
        else:
            if new[i]["avg"] < old[i]["avg"] - PRICE_EPS:
                return True
            if new[i]["avg"] > old[i]["avg"] + PRICE_EPS:
                return False
    return len(new) > len(old)


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


def _params_match(prev, min_hours, max_hours, ceiling, rank, blocked=None):
    if prev is None:
        return False
    if abs(prev["min_hours"] - min_hours) >= HOUR_EPS:
        return False
    if abs(prev["max_hours"] - max_hours) >= HOUR_EPS:
        return False
    if abs(prev["ceiling"] - ceiling) >= PRICE_EPS:
        return False
    if norm_rank(prev.get("rank")) != rank:
        return False
    if not _blocked_match(prev.get("blocked"), blocked):
        return False
    return True


def _choice(windows, horizon, reason):
    return {"windows": windows, "horizon": horizon, "reason": reason}


def choose(
    slots, min_hours, max_hours, ceiling, now_ts, prev, rank="cheapest", blocked=None
):
    rank = norm_rank(rank)
    if not slots:
        return _choice([], None, "no_slots")
    horizon = slots[-1][1]
    if rank == "offsun":
        blocked = _norm_blocked(blocked)
        slots = drop_blocked(slots, blocked)
    else:
        blocked = []
    min_s = min_hours * 3600.0
    max_s = max_hours * 3600.0
    new_windows = pick_all(slots, min_s, max_s, ceiling, now_ts, rank)
    prev_ok = False
    prev_windows = None
    if _params_match(prev, min_hours, max_hours, ceiling, rank, blocked):
        prev_windows = prev.get("windows")
        prev_ok = prev_windows is not None and len(prev_windows) > 0
    last = last_end(prev_windows) if prev_ok else None
    if prev_ok and last is not None and last > now_ts:
        # Same-horizon replans would drop elapsed slots and look "better"
        # (a 15-minute slide). Keep the frozen set unless the price curve
        # grew (typically tomorrow after 14:00) and the new set wins.
        prev_horizon = prev.get("horizon")
        horizon_grew = prev_horizon is None or horizon > prev_horizon + GAP_S
        if horizon_grew and _set_better(new_windows, prev_windows, rank):
            return _choice(new_windows, horizon, "switched")
        return _choice(prev_windows, prev["horizon"], "frozen")
    if prev_ok and last is not None and last <= now_ts:
        prev_horizon = prev["horizon"]
        if prev_horizon is None or horizon <= prev_horizon + GAP_S:
            return _choice(prev_windows, prev["horizon"], "idle_after_window")
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
        min_hours = 2.0
    if max_hours is None or max_hours <= 0:
        max_hours = 5.0
    min_hours = min(24.0, max(0.25, min_hours))
    max_hours = min(24.0, max(0.25, max_hours))
    if min_hours > max_hours:
        min_hours, max_hours = max_hours, min_hours
    return min_hours, max_hours


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


# Keep names here so tests can load this module without the package.
_FORCE_ON = ("Force on",)
_POLICY_RANK = {
    "Cheapest": "cheapest",
    "Supercheap": "offsun",
    "Longest": "longest",
    "Earliest": "earliest",
}


def until_unplug_step(override, plugged, seen):
    """Advance the until-unplug override. Returns ``(override, seen)``.

    ``seen`` means the car was plugged while the override was on. The
    override clears when that car goes Idle after that. Turning the
    override off clears ``seen``. Policy is not changed.
    """
    if not override:
        return False, False
    if plugged:
        return True, True
    if seen:
        return False, False
    return True, False


def charger_full_power(policy, results, now_ts, *, enough_solar=False, until_unplug=False):
    """Whether this charger wants 22 kW right now. Pure; no Home Assistant."""
    if until_unplug:
        return True
    if policy in _FORCE_ON:
        return True
    rank = _POLICY_RANK.get(policy)
    if not rank:
        return False
    if policy == "Supercheap" and enough_solar:
        return False
    return now_in_windows((results.get(rank) or {}).get("raw_windows") or [], now_ts)


def plan(
    clock,
    attrs,
    *,
    min_hours=2.0,
    max_hours=5.0,
    ceiling=0.1,
    rank="cheapest",
    prev=None,
    source_entity="",
    blocked=None,
):
    min_hours, max_hours = clamp_hours(min_hours, max_hours)
    ceiling = _to_price(ceiling)
    if ceiling is None:
        ceiling = 0.1
    rank = norm_rank(rank)
    now_dt = clock.now()
    now_ts = float(clock.as_timestamp(now_dt))
    empty = {
        "start": None,
        "end": None,
        "avg": None,
        "min_hours": min_hours,
        "max_hours": max_hours,
        "ceiling": ceiling,
        "rank": rank,
        "count": 0,
        "windows": [],
        "horizon": None,
        "reason": "no_source",
        "tomorrow_ok": False,
        "source_entity": source_entity,
        "slot_count": 0,
        "blocked_ts": [],
        "blocked": [],
    }
    if attrs is None:
        return empty
    slots = collect_slots(clock, attrs, now_dt)
    chosen = choose(
        slots, min_hours, max_hours, ceiling, now_ts, prev, rank, blocked=blocked
    )
    iso_ws = iso_windows(clock, chosen["windows"])
    active = current_or_next(chosen["windows"], now_ts)
    result = {
        "windows": iso_ws,
        "count": len(iso_ws),
        "min_hours": min_hours,
        "max_hours": max_hours,
        "ceiling": ceiling,
        "rank": rank,
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
        "blocked_ts": _norm_blocked(blocked) if rank == "offsun" else [],
        "blocked": [
            {"start": _iso(clock, start), "end": _iso(clock, end)}
            for start, end in (_norm_blocked(blocked) if rank == "offsun" else [])
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
        "rank": norm_rank(result.get("rank")),
        "horizon": horizon,
        "blocked": _norm_blocked(blocked),
    }
