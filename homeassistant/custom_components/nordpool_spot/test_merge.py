"""Tests for the generic Nordpool spot merge (no Home Assistant)."""

from __future__ import annotations

import datetime
import json
from datetime import timezone
from pathlib import Path
import importlib.util

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("nordpool_spot_merge", ROOT / "merge.py")
merge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merge)

SLOT = 900


def assert_eq(actual, expected, msg):
    if actual != expected:
        raise AssertionError("%s: got %r expected %r" % (msg, actual, expected))


def assert_true(cond, msg):
    if not cond:
        raise AssertionError(msg)


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def dict_slots(start, prices, slot=SLOT):
    out = []
    t = start
    for price in prices:
        out.append({"start": iso(t), "end": iso(t + slot), "value": price})
        t += slot
    return out


def main():
    now = datetime.datetime(2026, 3, 15, 10, 32, tzinfo=timezone.utc)
    now_ts = now.timestamp()
    t0 = datetime.datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc).timestamp()
    s1 = [t0, t0 + SLOT, 0.10]
    s2 = [t0 + SLOT, t0 + 2 * SLOT, 0.11]
    s3 = [t0 + 2 * SLOT, t0 + 3 * SLOT, 0.12]
    s4 = [t0 + 3 * SLOT, t0 + 4 * SLOT, 0.13]
    live = [[t0, t0 + SLOT, 0.99], [t0 + SLOT, t0 + 2 * SLOT, 0.11], s3, s4]

    merged = merge.merge_slots([s1, s2], live, now_ts)
    assert_eq(merged[0][2], 0.10, "ended slot stays frozen")
    assert_eq(merged[1][2], 0.11, "previous ended slot stays")
    assert_eq(merged[2], s3, "current slot follows live")
    assert_eq(merged[3], s4, "forecast follows live")
    assert_eq(merge.current_price(merged, now_ts), 0.12, "now in current slot")
    raw = merge.as_raw(merged)
    assert_eq(len(raw), 4, "raw length")
    assert_eq(raw[0]["value"], 0.10, "raw value")
    assert_true("T" in raw[-1]["end"], "raw iso end")

    replaced = merge.merge_slots([s1, [t0 + 3 * SLOT, t0 + 4 * SLOT, 0.20]], live, now_ts)
    by_start = {slot[0]: slot[2] for slot in replaced}
    assert_eq(by_start[t0 + 3 * SLOT], 0.13, "live forecast replaces stored future")
    assert_eq(0.20 in by_start.values(), False, "stale forecast dropped")

    empty_live = merge.merge_slots([s1, s4], [], now_ts)
    assert_eq(len(empty_live), 2, "empty live keeps stored past and forecast")
    assert_eq(empty_live[1][2], 0.13, "kept last forecast")

    first = merge.merge_slots([], live, now_ts)
    assert_eq(len(first), 4, "first collection uses full live curve")
    assert_eq(first[0][2], 0.99, "unfrozen past comes from live")

    old = [t0 - (9 * 86400), t0 - (9 * 86400) + SLOT, 0.01]
    trimmed = merge.merge_slots([old, s1], live, now_ts, keep_days=8)
    assert_eq(any(slot[2] == 0.01 for slot in trimmed), False, "older than keep window trimmed")

    payload = json.loads(json.dumps({"raw": merge.as_raw([s1, s2])}))["raw"]
    roundtrip = merge.merge_slots(payload, live, now_ts)
    assert_eq(roundtrip[0][2], 0.10, "json store roundtrip keeps realized")

    week_stored = []
    t = t0 - 7 * 86400
    while t + SLOT <= t0:
        week_stored.append([t, t + SLOT, 0.08])
        t += 3600
    week = merge.merge_slots(week_stored, live, now_ts)
    assert_true(week[0][0] <= now_ts - 6 * 86400, "week chart still has past slots")
    assert_true(week[-1][1] > now_ts, "week chart still extends into forecast")
    assert_eq(week[-1], s4, "forecast tail is the last live slot")

    attrs = {
        "raw_today": dict_slots(t0, [0.10, 0.11, 0.12]),
        "raw_tomorrow": dict_slots(t0 + 86400, [0.04]),
    }
    from_attrs = merge.live_slots(attrs)
    assert_eq(len(from_attrs), 4, "concat today+tomorrow")
    assert_eq(from_attrs[-1][2], 0.04, "tomorrow last")
    assert_eq(merge.parse_slots([[1, 0, 1], ["x"], [1, 2, 3]]), [[1.0, 2.0, 3.0]], "parse drops junk")
    print("ok  merge_slots")
    print("1 passed, 0 failed")


if __name__ == "__main__":
    main()
