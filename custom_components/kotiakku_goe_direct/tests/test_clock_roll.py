"""Roll the planner clock forward the way the controller does.

``async_plan`` stores one result per rank and feeds ``prev_from_result`` on
the next tick. These tests walk 15-minute (and boundary) ticks so freeze,
active windows, tomorrow switch, surplus floor, split hold, and 1↔3
``psm`` hold can be seen over time.
"""

from __future__ import annotations

import datetime
from datetime import timezone
from zoneinfo import ZoneInfo

from harness import (
    Clock,
    assert_eq,
    assert_true,
    case_runner,
    iso,
    load_mod,
    plan_ranks as run_ranks,
    slots_from,
    window_ends,
    window_starts,
)

planner = load_mod("planner", "_clock")
surplus = load_mod("surplus", "_clock")
const = load_mod("const", "_clock")
now_in_windows = planner.now_in_windows
charger_full_power = planner.charger_full_power
SLOT = planner.SLOT_SECONDS
POLICY_CHEAPEST = const.POLICY_CHEAPEST
POLICY_SUPERCHEAP = const.POLICY_SUPERCHEAP
POLICY_LONGEST = const.POLICY_LONGEST
POLICY_EARLIEST = const.POLICY_EARLIEST
POLICY_FORCE_ON = const.POLICY_FORCE_ON
POLICY_FORCE_OFF = const.POLICY_FORCE_OFF
POLICY_UNTIL_UNPLUG = const.POLICY_UNTIL_UNPLUG

HELSINKI = ZoneInfo("Europe/Helsinki")


starts_of = window_starts
ends_of = window_ends


def plan_ranks(clock, attrs, results, min_hours=2.0, max_hours=5.0, ceiling=0.1, blocked=None):
    return run_ranks(
        planner,
        clock,
        attrs,
        results,
        min_hours=min_hours,
        max_hours=max_hours,
        ceiling=ceiling,
        source_entity="sensor.price",
        blocked=blocked,
    )


def until_unplug_tick(policy, plugged, seen, restore):
    """Same restore rules as KotiakkuGoeDirectController.async_charge for one charger."""
    if policy == POLICY_UNTIL_UNPLUG and plugged:
        seen = True
    elif policy != POLICY_UNTIL_UNPLUG and seen:
        seen = False
    restored = None
    if policy == POLICY_UNTIL_UNPLUG and seen and not plugged:
        restored = restore if restore not in (None, POLICY_UNTIL_UNPLUG) else POLICY_FORCE_OFF
        policy = restored
        seen = False
    return policy, seen, restored


def tick_times(start_ts, end_ts, windows, step=SLOT):
    times = set()
    t = start_ts
    while t <= end_ts:
        times.add(t)
        t += step
    for w in windows:
        times.add(w["start"])
        times.add(w["end"])
        times.add(w["end"] - 1)
    return sorted(t for t in times if start_ts <= t <= end_ts)


def main():
    case, run = case_runner()

    def test_roll_uniform_8h_freeze_and_active():
        day = datetime.datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc)
        base = day.timestamp()
        attrs = {"raw_today": slots_from(base, [0.05] * 32)}
        clock = Clock(day)
        results = plan_ranks(clock, attrs, {})
        cheap = results["cheapest"]
        assert_eq(cheap["reason"], "planned", "first plan")
        assert_eq(len(cheap["raw_windows"]), 2, "5h + 3h")
        frozen_starts = starts_of(cheap)
        assert_eq(round((frozen_starts[1] - frozen_starts[0]) / 3600, 2), 5.0, "first 5h")

        last = max(ends_of(cheap))
        for ts in tick_times(base, last + 3600, cheap["raw_windows"]):
            clock.set(datetime.datetime.fromtimestamp(ts, tz=timezone.utc))
            results = plan_ranks(clock, attrs, results)
            cheap = results["cheapest"]
            if ts < last:
                assert_eq(cheap["reason"], "frozen", "still inside last end @ %s" % iso(ts))
                assert_eq(starts_of(cheap), frozen_starts, "starts did not slide @ %s" % iso(ts))
            else:
                assert_eq(cheap["reason"], "idle_after_window", "idle after last end @ %s" % iso(ts))
                assert_eq(starts_of(cheap), frozen_starts, "keep old set after idle")
            active = now_in_windows(cheap["raw_windows"], ts)
            expect = any(w["start"] <= ts < w["end"] for w in cheap["raw_windows"])
            assert_eq(active, expect, "now_in_windows @ %s" % iso(ts))
            full = charger_full_power(POLICY_CHEAPEST, results, ts)
            assert_eq(full, active, "Cheapest full power follows windows")
            assert_eq(
                charger_full_power(POLICY_FORCE_OFF, results, ts),
                False,
                "Force off never full power",
            )
            assert_eq(
                charger_full_power(POLICY_FORCE_ON, results, ts),
                True,
                "Force on always full power",
            )

        in_first = now_in_windows(cheap["raw_windows"], base + 60)
        in_gap = now_in_windows(cheap["raw_windows"], last)
        assert_true(in_first, "first minute is inside")
        assert_true(not in_gap, "exactly last end is outside")

    def test_longest_does_not_slide_on_falling_prices():
        day = datetime.datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc)
        base = day.timestamp()
        prices = [0.09] * 8 + [0.04] * 24
        attrs = {"raw_today": slots_from(base, prices)}
        clock = Clock(day)
        results = plan_ranks(clock, attrs, {})
        first = results["longest"]
        assert_eq(round((first["raw_windows"][0]["end"] - first["raw_windows"][0]["start"]) / 3600, 2), 5.0, "longest 5h")
        start0 = first["raw_windows"][0]["start"]
        for i in range(1, 12):
            clock.advance(minutes=15)
            results = plan_ranks(clock, attrs, results)
            long = results["longest"]
            assert_eq(long["reason"], "frozen", "tick %s frozen" % i)
            assert_eq(long["raw_windows"][0]["start"], start0, "longest start did not slide")

    def test_tomorrow_switch_then_freeze():
        day = datetime.datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
        today_start = datetime.datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc).timestamp()
        tomorrow_start = today_start + 24 * 3600
        # 10:00–22:00 at 0.09 so 14:00 is still inside the frozen set.
        today = slots_from(today_start + 10 * 3600, [0.09] * 48)
        clock = Clock(day)
        attrs = {"raw_today": today, "tomorrow_valid": False}
        results = plan_ranks(clock, attrs, {})
        first_start = starts_of(results["cheapest"])
        assert_eq(results["cheapest"]["reason"], "planned", "morning plan")
        assert_true(
            max(ends_of(results["cheapest"])) > day.timestamp() + 4 * 3600,
            "window lasts past 14:00",
        )

        clock.advance(hours=4)
        still = plan_ranks(clock, attrs, results)
        assert_eq(still["cheapest"]["reason"], "frozen", "same curve frozen at 14:00")
        assert_eq(starts_of(still["cheapest"]), first_start, "morning starts held")

        attrs = {
            "raw_today": today,
            "raw_tomorrow": slots_from(tomorrow_start, [0.02] * 16),
            "tomorrow_valid": True,
        }
        results = plan_ranks(clock, attrs, still)
        assert_eq(results["cheapest"]["reason"], "switched", "cheaper tomorrow")
        assert_true(
            results["cheapest"]["raw_windows"][0]["start"] >= tomorrow_start - 1,
            "new window is tomorrow",
        )
        switched_starts = starts_of(results["cheapest"])
        for _ in range(8):
            clock.advance(minutes=15)
            results = plan_ranks(clock, attrs, results)
            assert_eq(results["cheapest"]["reason"], "frozen", "hold the switched set")
            assert_eq(starts_of(results["cheapest"]), switched_starts, "switched set does not slide")

    def test_three_ranks_independent_over_time():
        day = datetime.datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc)
        base = day.timestamp()
        prices = [0.09] * 8 + [0.01] * 8 + [0.09] * 16
        attrs = {"raw_today": slots_from(base, prices)}
        clock = Clock(day)
        results = plan_ranks(clock, attrs, {})
        cheap0 = starts_of(results["cheapest"])
        long0 = starts_of(results["longest"])
        early0 = starts_of(results["earliest"])
        assert_true(cheap0[0] > early0[0], "cheapest is the dip, earliest is the start")
        last = max(
            max(ends_of(results["cheapest"])),
            max(ends_of(results["longest"])),
            max(ends_of(results["earliest"])),
        )
        ts = base
        while ts < last:
            clock.set(datetime.datetime.fromtimestamp(ts, tz=timezone.utc))
            results = plan_ranks(clock, attrs, results)
            if ts > base:
                assert_eq(results["cheapest"]["reason"], "frozen", "cheap frozen")
                assert_eq(results["longest"]["reason"], "frozen", "longest frozen")
                assert_eq(results["earliest"]["reason"], "frozen", "earliest frozen")
            assert_eq(starts_of(results["cheapest"]), cheap0, "cheap starts")
            assert_eq(starts_of(results["longest"]), long0, "longest starts")
            assert_eq(starts_of(results["earliest"]), early0, "earliest starts")
            ts += SLOT

    def test_helsinki_midnight_keeps_frozen_iso_slots():
        local_day = datetime.datetime(2026, 3, 15, 0, 0, tzinfo=HELSINKI)
        evening = datetime.datetime(2026, 3, 15, 22, 0, tzinfo=HELSINKI)
        next_midnight = datetime.datetime(2026, 3, 16, 0, 0, tzinfo=HELSINKI)
        cheap_start = datetime.datetime(2026, 3, 16, 2, 0, tzinfo=HELSINKI)
        today_rest = slots_from(evening.timestamp(), [0.09] * 8)
        tomorrow = slots_from(cheap_start.timestamp(), [0.02] * 16)
        clock = Clock(evening, tz=HELSINKI)
        attrs = {
            "raw_today": today_rest,
            "raw_tomorrow": tomorrow,
            "tomorrow_valid": True,
        }
        results = plan_ranks(clock, attrs, {})
        cheap = results["cheapest"]
        assert_eq(cheap["reason"], "planned", "evening plan uses tomorrow")
        assert_true(
            cheap["raw_windows"][0]["start"] >= cheap_start.timestamp() - 1,
            "window is the 02:00 dip",
        )
        frozen = starts_of(cheap)

        clock.set(next_midnight)
        rolled = {
            "raw_today": tomorrow,
            "raw_tomorrow": [],
            "tomorrow_valid": False,
        }
        results = plan_ranks(clock, rolled, results)
        assert_eq(results["cheapest"]["reason"], "frozen", "midnight does not replan")
        assert_eq(starts_of(results["cheapest"]), frozen, "ISO slots survive day roll")
        assert_true(
            not now_in_windows(results["cheapest"]["raw_windows"], next_midnight.timestamp()),
            "02:00 window not active at midnight",
        )
        clock.set(cheap_start)
        results = plan_ranks(clock, rolled, results)
        assert_true(
            now_in_windows(results["cheapest"]["raw_windows"], clock.as_timestamp(clock.now())),
            "active at 02:00",
        )
        assert_eq(
            charger_full_power(POLICY_CHEAPEST, results, clock.as_timestamp(clock.now())),
            True,
            "Cheapest on at 02:00",
        )

    def test_spot_price_independent_of_kotiakku():
        day = datetime.datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc)
        base = day.timestamp()
        attrs = {"raw_today": slots_from(base, [0.04] * 16)}
        clock = Clock(day)
        results = plan_ranks(clock, attrs, {})
        w = results["cheapest"]["raw_windows"][0]
        mid = (w["start"] + w["end"]) / 2
        dead = surplus.surplus_decision(True, 0, -1, window_ok=False)
        assert_true(dead["arm_floor"], "surplus holds when Kotiakku is down")
        assert_eq(
            charger_full_power(POLICY_CHEAPEST, results, mid),
            True,
            "Cheapest still full power with Kotiakku down",
        )

    def test_supercheap_uses_offsun_windows():
        day = datetime.datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc)
        base = day.timestamp()
        prices = [0.05] * 32 + [0.01] * 32 + [0.2] * 32
        attrs = {"raw_today": slots_from(base, prices)}
        blocked = [(base + 8 * 3600, base + 16 * 3600)]
        clock = Clock(day)
        results = plan_ranks(clock, attrs, {}, blocked=blocked)
        cheap = results["cheapest"]["raw_windows"][0]
        off = results["offsun"]["raw_windows"][0]
        assert_eq(cheap["start"], base + 8 * 3600, "cheapest is the midday dip")
        assert_eq(off["start"], base, "offsun is the night")
        midday = cheap["start"] + 1800
        night = off["start"] + 1800
        assert_eq(
            charger_full_power(POLICY_CHEAPEST, results, midday),
            True,
            "Cheapest ignores surplus hours",
        )
        assert_eq(
            charger_full_power(POLICY_SUPERCHEAP, results, midday),
            False,
            "Supercheap skips surplus hours",
        )
        assert_eq(
            charger_full_power(POLICY_SUPERCHEAP, results, night),
            True,
            "Supercheap 22 kW in offsun night",
        )
        assert_eq(
            charger_full_power(POLICY_SUPERCHEAP, results, night, enough_solar=True),
            False,
            "enough solar skips Supercheap even in offsun",
        )
        assert_eq(
            charger_full_power(POLICY_FORCE_OFF, results, midday),
            False,
            "Force off stays surplus-only",
        )
        assert_eq(
            charger_full_power(POLICY_FORCE_ON, results, midday),
            True,
            "Force on ignores Kotiakku",
        )
        clock.set(datetime.datetime.fromtimestamp(night, tz=timezone.utc))
        results = plan_ranks(clock, attrs, results, blocked=blocked)
        assert_eq(results["offsun"]["reason"], "frozen", "offsun windows still freeze")
        assert_true(
            now_in_windows(results["offsun"]["raw_windows"], night),
            "offsun active at night",
        )
        clock.set(datetime.datetime.fromtimestamp(midday, tz=timezone.utc))
        results = plan_ranks(clock, attrs, results, blocked=blocked)
        assert_true(
            not now_in_windows(results["offsun"]["raw_windows"], midday),
            "offsun not active in blocked midday",
        )
        dead = surplus.surplus_decision(True, 0, -1, window_ok=False)
        assert_true(dead["arm_floor"], "surplus holds when Kotiakku is down")
        assert_eq(
            charger_full_power(POLICY_CHEAPEST, results, midday),
            True,
            "Cheapest still full power with Kotiakku down",
        )

    def test_surplus_floor_over_15_min():
        decide = surplus.surplus_decision
        start = decide(False, 2000, 92, window_ok=True)
        assert_true(start["write_on"] and not start["write_off"], "start at 2000 W / 92%")
        assert_true(not start["arm_floor"], "2000 W is above 1000 W hold")
        too_low = decide(False, 1999, 92, window_ok=True)
        assert_true(not too_low["write_on"], "do not start under 2000 W")
        soc_low_start = decide(False, 3000, 91, window_ok=True)
        assert_true(not soc_low_start["write_on"], "do not start under 92% SoC")

        above_hold = decide(True, 1500, 96, window_ok=True)
        assert_true(above_hold["write_on"] and not above_hold["arm_floor"], "1500 W still tracks leftover")
        hold_low = decide(True, 0, 96, window_ok=True)
        assert_true(hold_low["write_on"] and not hold_low["write_off"], "hold 6 A at 0 W")
        assert_true(hold_low["arm_floor"] and hold_low["use_floor_budget"], "arm low hold")
        expired = decide(True, 0, 96, window_ok=True, floor_expired=True)
        assert_true(expired["write_off"] and not expired["write_on"], "stop after hold minutes")

        band = decide(True, 3000, 91, window_ok=True)
        assert_true(band["write_on"] and not band["arm_floor"], "91% stays on, not hold")
        soc_hold = decide(True, 4000, 89, window_ok=True)
        assert_true(soc_hold["write_on"] and not soc_hold["write_off"], "SoC 89% is low hold, not cut")
        assert_true(soc_hold["arm_floor"] and soc_hold["use_floor_budget"], "SoC 89% uses 6 A")
        soc_expired = decide(True, 4000, 89, window_ok=True, floor_expired=True)
        assert_true(soc_expired["write_off"] and not soc_expired["write_on"], "SoC hold expires then stop")

        unknown = decide(True, 4000, 96, window_ok=False)
        assert_true(unknown["write_on"] and not unknown["write_off"], "unknown is low hold, not cut")
        assert_true(unknown["arm_floor"] and unknown["use_floor_budget"], "unknown uses 6 A")
        unknown_expired = decide(True, 4000, 96, window_ok=False, floor_expired=True)
        assert_true(unknown_expired["write_off"] and not unknown_expired["write_on"], "unknown hold expires then stop")
        recovered = decide(True, 2500, 96, window_ok=True)
        assert_true(recovered["write_on"] and not recovered["arm_floor"], "usable again cancels hold")
        no_start = decide(False, 3000, 96, window_ok=False)
        assert_true(not no_start["write_on"], "cannot start while unusable")
        off = decide(False, 0, 96, window_ok=True)
        assert_true(not off["write_on"] and not off["write_off"], "stay off; do not restart under 2000 W")

    def test_surplus_split_hold_over_15_min():
        alloc = surplus.surplus_allocations
        plan = surplus.surplus_allocation_plan
        a, b = "111111", "222222"
        kwargs = dict(
            lops={a: 1, b: 50},
            plugged={a: True, b: True},
            split_min_w=3000,
            split_floor_w=500,
            charger_max_w=22080,
        )
        started = alloc(
            [a, b],
            leftover_w=18000,
            take_w={a: 10000, b: 0},
            states={a: "Charging", b: "Charging"},
            **kwargs,
        )
        assert_eq(started, {a: 10000, b: 8000}, "second car starts on unused leftover")
        held = plan([a, b], leftover_w=10000, split_hold=True, **kwargs)
        assert_eq(held["allocations"], {a: 7000, b: 3000}, "grace keeps 3 kW when high can use all 10 kW")
        assert_eq(held["arm_split_hold"], True, "arm the same 15 min hold")
        dropped = alloc([a, b], leftover_w=10000, split_hold=True, split_expired=True, **kwargs)
        assert_eq(dropped, {a: 10000}, "drop the second car after 15 min")

    def test_surplus_phase_hold_over_15_min():
        phase = surplus.surplus_phase_budget
        args = (6, 32, 50, 230, 4140)
        first = phase(8000, *args)
        assert_eq((first["psm"], first["amp"], first["arm_phase"]), (2, 11, False), "first start is 3-phase")
        up = phase(8000, *args, last_psm=1)
        assert_eq((up["psm"], up["amp"], up["arm_phase"]), (1, 32, True), "1→3 waits; amp is max 1-phase, not 11 A")
        cancel = phase(2000, *args, last_psm=1)
        assert_eq(cancel["arm_phase"], False, "leftover back on 1-phase cancels the timer")
        up_done = phase(8000, *args, last_psm=1, hold_expired=True)
        assert_eq((up_done["psm"], up_done["amp"]), (2, 11), "after 15 min: 3-phase 11 A")
        down = phase(3000, *args, last_psm=2)
        assert_eq((down["psm"], down["amp"], down["arm_phase"]), (2, 6, True), "3→1 waits at min 6 A 3-phase, not 13 A")
        still = phase(2500, *args, last_psm=2)
        assert_eq((still["psm"], still["amp"], still["arm_phase"]), (2, 6, True), "still holding after leftover chatter")
        down_done = phase(3000, *args, last_psm=2, hold_expired=True)
        assert_eq((down_done["psm"], down_done["amp"]), (1, 13), "after 15 min: 1-phase 13 A")

    def test_until_unplug_restores_only_that_charger():
        policy, seen, restored = until_unplug_tick(
            POLICY_UNTIL_UNPLUG, True, False, POLICY_CHEAPEST
        )
        assert_eq(policy, POLICY_UNTIL_UNPLUG, "stay until unplug")
        assert_eq(seen, True, "arm when plugged")
        assert_eq(restored, None, "no restore while plugged")
        policy, seen, restored = until_unplug_tick(
            POLICY_UNTIL_UNPLUG, False, True, POLICY_CHEAPEST
        )
        assert_eq(restored, POLICY_CHEAPEST, "restore previous")
        assert_eq(policy, POLICY_CHEAPEST, "restored")
        assert_eq(seen, False, "disarm")
        other = until_unplug_tick(POLICY_FORCE_OFF, False, False, POLICY_LONGEST)
        assert_eq(other[0], POLICY_FORCE_OFF, "other charger untouched")
        assert_eq(other[2], None, "other charger does not restore")

    def test_boundary_exclusive_end():
        day = datetime.datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc)
        base = day.timestamp()
        attrs = {"raw_today": slots_from(base, [0.04] * 8)}
        clock = Clock(day)
        results = plan_ranks(clock, attrs, {})
        w = results["cheapest"]["raw_windows"][0]
        assert_true(now_in_windows([w], w["start"]), "on at start")
        assert_true(now_in_windows([w], w["end"] - 1), "on 1s before end")
        assert_true(not now_in_windows([w], w["end"]), "off at end")
        assert_eq(
            charger_full_power(POLICY_CHEAPEST, results, w["end"]),
            False,
            "binary off at end",
        )

    case("roll_uniform_8h_freeze_and_active", test_roll_uniform_8h_freeze_and_active)
    case("longest_does_not_slide_on_falling_prices", test_longest_does_not_slide_on_falling_prices)
    case("tomorrow_switch_then_freeze", test_tomorrow_switch_then_freeze)
    case("three_ranks_independent_over_time", test_three_ranks_independent_over_time)
    case("helsinki_midnight_keeps_frozen_iso_slots", test_helsinki_midnight_keeps_frozen_iso_slots)
    case("spot_price_independent_of_kotiakku", test_spot_price_independent_of_kotiakku)
    case("supercheap_uses_offsun_windows", test_supercheap_uses_offsun_windows)
    case("surplus_floor_over_15_min", test_surplus_floor_over_15_min)
    case("surplus_split_hold_over_15_min", test_surplus_split_hold_over_15_min)
    case("surplus_phase_hold_over_15_min", test_surplus_phase_hold_over_15_min)
    case("until_unplug_restores_only_that_charger", test_until_unplug_restores_only_that_charger)
    case("boundary_exclusive_end", test_boundary_exclusive_end)

    run()


if __name__ == "__main__":
    main()
