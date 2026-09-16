"""Merge realized Nordpool slots with the live today+tomorrow forecast."""

from __future__ import annotations

import datetime

KEEP_DAYS = 8


def _ts(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime.datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _iso(ts):
    return datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc).isoformat()


def parse_slots(items):
    out = []
    for item in items or []:
        start = end = price = None
        if isinstance(item, dict):
            start = _ts(item.get("start"))
            end = _ts(item.get("end"))
            try:
                price = float(item.get("value"))
            except (TypeError, ValueError):
                price = None
        else:
            try:
                start = _ts(item[0])
                end = _ts(item[1])
                price = float(item[2])
            except (TypeError, ValueError, IndexError):
                pass
        if start is None or end is None or price is None or end <= start:
            continue
        out.append([start, end, price])
    out.sort()
    return out


def live_slots(attrs):
    attrs = attrs or {}
    return parse_slots(list(attrs.get("raw_today") or []) + list(attrs.get("raw_tomorrow") or []))


def as_raw(slots):
    return [{"start": _iso(start), "end": _iso(end), "value": price} for start, end, price in slots]


def current_price(slots, now_ts):
    last = None
    for start, end, price in slots or []:
        if start <= now_ts < end:
            return price
        if start <= now_ts:
            last = price
    return last


def merge_slots(stored, live, now_ts, keep_days=KEEP_DAYS):
    """Ended slots stay frozen; current+future follow live Nordpool."""
    try:
        keep_days = float(keep_days)
    except (TypeError, ValueError):
        keep_days = float(KEEP_DAYS)
    keep_after = now_ts - keep_days * 86400.0
    stored_by_start = {slot[0]: slot for slot in parse_slots(stored) if slot[1] > keep_after}
    live = parse_slots(live)
    merged = {}
    for slot in stored_by_start.values():
        if slot[1] <= now_ts:
            merged[slot[0]] = slot
    if live:
        for slot in live:
            if slot[1] <= keep_after:
                continue
            if slot[1] <= now_ts:
                if slot[0] not in merged:
                    merged[slot[0]] = slot
            else:
                merged[slot[0]] = slot
    else:
        for slot in stored_by_start.values():
            if slot[1] > now_ts:
                merged[slot[0]] = slot
    return [merged[key] for key in sorted(merged)]
