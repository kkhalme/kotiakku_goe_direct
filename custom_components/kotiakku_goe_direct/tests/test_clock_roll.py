"""Roll the planner clock forward the way the controller does.

``async_plan`` stores one SolarPriority result and plans again from the
current prices and solar forecast. These tests walk 15-minute (and
boundary) ticks so a same-curve window, active windows, tomorrow switch,
surplus floor, split hold, and 1↔3 ``psm`` hold can be seen over time.
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
    plan_once as run_once,
    slots_from,
    window_ends,
    window_starts,
)

planner = load_mod("planner", "_clock")
surplus = load_mod("surplus", "_clock")
const = load_mod("const", "_clock")
now_in_windows = planner.now_in_windows
charger_full_power = planner.charger_full_power
charger_surplus = planner.charger_surplus
until_unplug_step = planner.until_unplug_step
SLOT = planner.SLOT_SECONDS
POLICY_SOLAR_PRIORITY = const.POLICY_SOLAR_PRIORITY
POLICY_FORCE_ON = const.POLICY_FORCE_ON
POLICY_FORCE_OFF = const.POLICY_FORCE_OFF

HELSINKI = ZoneInfo("Europe/Helsinki")


starts_of = window_starts
ends_of = window_ends


def plan_once(clock, attrs, result, min_hours=2.0, max_hours=5.0, ceiling=0.2, blocked=None, **extra):
    return run_once(
        planner,
        clock,
        attrs,
        result,
        min_hours=min_hours,
        max_hours=max_hours,
        ceiling=ceiling,
        flex_pct=extra.get("flex_pct", 20),
        flex_euro=extra.get("flex_euro", 0.02),
        source_entity="sensor.price",
        blocked=blocked,
        today_kwh=extra.get("today_kwh"),
        tomorrow_kwh=extra.get("tomorrow_kwh"),
    )


def until_unplug_tick(override, plugged, seen):
    """Same override rules as KotiakkuGoeDirectController.async_charge for one charger."""
    return until_unplug_step(override, plugged, seen)


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

    def test_roll_uniform_plan_and_active():
        day = datetime.datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc)
        base = day.timestamp()
        attrs = {"raw_today": slots_from(base, [0.05] * 8)}
        clock = Clock(day)
        result = plan_once(clock, attrs, None, flex_pct=0, flex_euro=0)
        assert_eq(result["reason"], "planned", "first plan")
        assert_eq(len(result["raw_windows"]), 1, "one window")
        assert_eq(round((result["raw_windows"][0]["end"] - result["raw_windows"][0]["start"]) / 3600, 2), 2.0, "min hours, no flex")
        planned_starts = starts_of(result)
        last = max(ends_of(result))
        for ts in tick_times(base, last + 3600, result["raw_windows"]):
            clock.set(datetime.datetime.fromtimestamp(ts, tz=timezone.utc))
            result = plan_once(clock, attrs, result, flex_pct=0, flex_euro=0)
            assert_eq(result["reason"], "planned", "same curve still planned @ %s" % iso(ts))
            assert_eq(starts_of(result), planned_starts, "starts did not slide @ %s" % iso(ts))
            active = now_in_windows(result["raw_windows"], ts)
            expect = any(w["start"] <= ts < w["end"] for w in result["raw_windows"])
            assert_eq(active, expect, "now_in_windows @ %s" % iso(ts))
            full = charger_full_power(POLICY_SOLAR_PRIORITY, result, ts)
            assert_eq(full, active, "SolarPriority full power follows the window")
            assert_eq(
                charger_full_power(POLICY_FORCE_OFF, result, ts),
                False,
                "Force off never full power",
            )
            assert_eq(
                charger_surplus(POLICY_FORCE_OFF, result, ts),
                False,
                "Force off never leftover",
            )
            assert_eq(
                charger_surplus(POLICY_SOLAR_PRIORITY, result, ts),
                not full,
                "SolarPriority leftover outside the window",
            )
            assert_eq(
                charger_full_power(POLICY_FORCE_ON, result, ts),
                True,
                "Force on always full power",
            )
            assert_eq(
                charger_surplus(POLICY_FORCE_ON, result, ts),
                False,
                "Force on is not leftover",
            )

        in_first = now_in_windows(result["raw_windows"], base + 60)
        in_gap = now_in_windows(result["raw_windows"], last)
        assert_true(in_first, "first minute is inside")
        assert_true(not in_gap, "exactly last end is outside")

    def test_window_does_not_slide_on_falling_prices():
        day = datetime.datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc)
        base = day.timestamp()
        prices = [0.09] * 8 + [0.04] * 24
        attrs = {"raw_today": slots_from(base, prices)}
        clock = Clock(day)
        result = plan_once(clock, attrs, None, flex_pct=0, flex_euro=0)
        assert_eq(round((result["raw_windows"][0]["end"] - result["raw_windows"][0]["start"]) / 3600, 2), 2.0, "seed 2h")
        start0 = result["raw_windows"][0]["start"]
        assert_eq(start0, base + 8 * SLOT, "seed is the 0.04 dip")
        for i in range(1, 12):
            clock.advance(minutes=15)
            result = plan_once(clock, attrs, result, flex_pct=0, flex_euro=0)
            assert_eq(result["reason"], "planned", "tick %s still planned" % i)
            assert_eq(result["raw_windows"][0]["start"], start0, "start did not slide")

    def test_tomorrow_switch_then_holds():
        day = datetime.datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
        today_start = datetime.datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc).timestamp()
        tomorrow_start = today_start + 24 * 3600
        today = slots_from(today_start + 10 * 3600, [0.09] * 48)
        clock = Clock(day)
        attrs = {"raw_today": today, "tomorrow_valid": False}
        result = plan_once(clock, attrs, None)
        first_start = starts_of(result)
        assert_eq(result["reason"], "planned", "morning plan")
        assert_true(
            max(ends_of(result)) > day.timestamp() + 4 * 3600,
            "flex grows the 0.09 plateau past 14:00",
        )

        clock.advance(hours=4)
        still = plan_once(clock, attrs, result)
        assert_eq(still["reason"], "planned", "same curve still planned at 14:00")
        assert_eq(starts_of(still), first_start, "morning starts held")

        attrs = {
            "raw_today": today,
            "raw_tomorrow": slots_from(tomorrow_start, [0.02] * 16),
            "tomorrow_valid": True,
        }
        result = plan_once(clock, attrs, still)
        assert_eq(result["reason"], "planned", "cheaper tomorrow is a new environment")
        assert_true(
            result["raw_windows"][0]["start"] >= tomorrow_start - 1,
            "new window is tomorrow",
        )
        switched_starts = starts_of(result)
        for _ in range(8):
            clock.advance(minutes=15)
            result = plan_once(clock, attrs, result)
            assert_eq(result["reason"], "planned", "hold the switched set")
            assert_eq(starts_of(result), switched_starts, "switched set does not slide")

    def test_helsinki_midnight_rolled_today_keeps_dip():
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
        result = plan_once(clock, attrs, None, flex_pct=0, flex_euro=0)
        assert_eq(result["reason"], "planned", "evening plan uses tomorrow")
        assert_true(
            result["raw_windows"][0]["start"] >= cheap_start.timestamp() - 1,
            "window is the 02:00 dip",
        )
        planned = starts_of(result)

        clock.set(next_midnight)
        rolled = {
            "raw_today": tomorrow,
            "raw_tomorrow": [],
            "tomorrow_valid": False,
        }
        result = plan_once(clock, rolled, result, flex_pct=0, flex_euro=0)
        assert_eq(result["reason"], "planned", "midnight replans from rolled today")
        assert_eq(starts_of(result), planned, "02:00 dip is still on today's curve")
        assert_true(
            not now_in_windows(result["raw_windows"], next_midnight.timestamp()),
            "02:00 window not active at midnight",
        )
        clock.set(cheap_start)
        result = plan_once(clock, rolled, result, flex_pct=0, flex_euro=0)
        assert_true(
            now_in_windows(result["raw_windows"], clock.as_timestamp(clock.now())),
            "active at 02:00",
        )
        assert_eq(
            charger_full_power(POLICY_SOLAR_PRIORITY, result, clock.as_timestamp(clock.now())),
            True,
            "SolarPriority on at 02:00",
        )

    def test_spot_price_independent_of_kotiakku():
        day = datetime.datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc)
        base = day.timestamp()
        attrs = {"raw_today": slots_from(base, [0.04] * 16)}
        clock = Clock(day)
        result = plan_once(clock, attrs, None, flex_pct=0, flex_euro=0)
        w = result["raw_windows"][0]
        mid = (w["start"] + w["end"]) / 2
        dead = surplus.surplus_decision(True, 0, -1, window_ok=False)
        assert_true(dead["arm_floor"], "surplus holds when Kotiakku is down")
        assert_eq(
            charger_full_power(POLICY_SOLAR_PRIORITY, result, mid),
            True,
            "SolarPriority still full power with Kotiakku down",
        )

    def test_blocked_hours_and_enough_solar():
        day = datetime.datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc)
        base = day.timestamp()
        prices = [0.05] * 32 + [0.01] * 32 + [0.2] * 32
        attrs = {"raw_today": slots_from(base, prices)}
        blocked = [(base + 8 * 3600, base + 16 * 3600)]
        clock = Clock(day)
        result = plan_once(clock, attrs, None, blocked=blocked, flex_pct=0, flex_euro=0)
        assert_eq(result["raw_windows"][0]["start"], base, "night, not the blocked midday dip")
        midday = base + 10 * 3600
        night = base + 1800
        assert_eq(
            charger_full_power(POLICY_SOLAR_PRIORITY, result, midday),
            False,
            "blocked midday is not in the window",
        )
        assert_eq(
            charger_full_power(POLICY_SOLAR_PRIORITY, result, night),
            True,
            "SolarPriority 22 kW in the night window",
        )
        assert_eq(
            charger_full_power(POLICY_SOLAR_PRIORITY, result, night, enough_solar=True),
            False,
            "enough solar skips 22 kW even in the window",
        )
        assert_eq(
            charger_full_power(POLICY_FORCE_OFF, result, midday),
            False,
            "Force off never full power",
        )
        assert_eq(
            charger_surplus(POLICY_FORCE_OFF, result, midday),
            False,
            "Force off never leftover",
        )
        assert_eq(
            charger_full_power(POLICY_FORCE_ON, result, midday),
            True,
            "Force on ignores Kotiakku",
        )
        clock.set(datetime.datetime.fromtimestamp(night, tz=timezone.utc))
        result = plan_once(clock, attrs, result, blocked=blocked, flex_pct=0, flex_euro=0)
        assert_eq(result["reason"], "planned", "window still planned")
        assert_true(now_in_windows(result["raw_windows"], night), "active at night")
        clock.set(datetime.datetime.fromtimestamp(midday, tz=timezone.utc))
        result = plan_once(clock, attrs, result, blocked=blocked, flex_pct=0, flex_euro=0)
        assert_true(
            not now_in_windows(result["raw_windows"], midday),
            "not active in blocked midday",
        )
        dead = surplus.surplus_decision(True, 0, -1, window_ok=False)
        assert_true(dead["arm_floor"], "surplus holds when Kotiakku is down")

    def test_horizon_clip_over_time():
        day = datetime.datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
        today_start = datetime.datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc).timestamp()
        tomorrow_start = today_start + 24 * 3600
        today = slots_from(today_start + 10 * 3600, [0.09] * 48)
        tomorrow = slots_from(tomorrow_start, [0.01] * 16)
        clock = Clock(day)
        attrs = {"raw_today": today, "raw_tomorrow": tomorrow, "tomorrow_valid": True}
        clipped = plan_once(
            clock, attrs, None, today_kwh=10.0, tomorrow_kwh=None, flex_pct=0, flex_euro=0
        )
        assert_true(
            clipped["raw_windows"][0]["end"] <= tomorrow_start + 1,
            "tomorrow prices ignored until tomorrow solar kWh exists",
        )
        both = plan_once(
            clock, attrs, clipped, today_kwh=10.0, tomorrow_kwh=8.0, flex_pct=0, flex_euro=0
        )
        assert_eq(both["reason"], "planned", "tomorrow solar appearing is a new environment")
        assert_true(
            both["raw_windows"][0]["start"] >= tomorrow_start - 1,
            "moved onto tomorrow",
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
        chatter = decide(True, 1500, 96, window_ok=True, hold_active=True, hold_exit_w=2000)
        assert_true(chatter["arm_floor"] and chatter["use_floor_budget"], "1500 W chatter does not cancel hold")
        leave = decide(True, 2100, 96, window_ok=True, hold_active=True, hold_exit_w=2000)
        assert_true(leave["write_on"] and not leave["arm_floor"], "start leftover cancels hold")
        no_start = decide(False, 3000, 96, window_ok=False)
        assert_true(not no_start["write_on"], "cannot start while unusable")
        off = decide(False, 0, 96, window_ok=True)
        assert_true(not off["write_on"] and not off["write_off"], "stay off; do not restart under 2000 W")
        at_hold = decide(True, 1000, 96, window_ok=True)
        assert_true(at_hold["write_on"] and not at_hold["arm_floor"], "exactly 1000 W still tracks")
        under = decide(True, 999, 96, window_ok=True)
        assert_true(under["use_floor_budget"], "999 W starts the 6 A hold")
        leave_exact = decide(True, 2000, 96, window_ok=True, hold_active=True, hold_exit_w=2000)
        assert_true(leave_exact["write_on"] and not leave_exact["arm_floor"], "exactly 2000 W cancels hold")
        default_exit = decide(True, 1500, 96, window_ok=True, hold_active=True)
        assert_true(not default_exit["arm_floor"], "without hold_exit_w, 1500 W already clears hold_min_w")

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
        too_small = alloc(
            [a, b],
            leftover_w=4500,
            take_w={a: 4400, b: 0},
            states={a: "Charging", b: "Charging"},
            split_hold=True,
            **kwargs,
        )
        assert_eq(too_small, {a: 4400}, "grace does not steal below 3 kW per car")
        min_split = alloc(
            [a, b],
            leftover_w=6000,
            take_w={a: 5900, b: 0},
            states={a: "Charging", b: "Charging"},
            split_hold=True,
            **kwargs,
        )
        assert_eq(min_split, {a: 3000, b: 3000}, "grace at 6 kW leftover is 3+3")
        tiny = alloc([a, b], leftover_w=300, **kwargs)
        assert_eq(tiny, {}, "300 W two unequal cars: first cannot meet 6 A")

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

    def test_until_unplug_clears_only_that_charger():
        on, seen = until_unplug_tick(True, True, False)
        assert_eq(on, True, "stay on while plugged")
        assert_eq(seen, True, "arm when plugged")
        on, seen = until_unplug_tick(True, False, True)
        assert_eq(on, False, "clear after unplug")
        assert_eq(seen, False, "disarm")
        other_on, other_seen = until_unplug_tick(False, False, False)
        assert_eq(other_on, False, "other charger untouched")
        assert_eq(other_seen, False, "other charger seen stays off")
        assert_eq(
            charger_full_power(POLICY_SOLAR_PRIORITY, {"raw_windows": []}, 0),
            False,
            "policy is unchanged when the override clears",
        )
        on, seen = until_unplug_tick(True, False, False)
        assert_eq((on, seen), (True, False), "unplugged start waits for a plug")

    def test_min_equals_max_stays_fixed_over_time():
        day = datetime.datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc)
        base = day.timestamp()
        attrs = {"raw_today": slots_from(base, [0.04] * 16)}
        clock = Clock(day)
        result = plan_once(clock, attrs, None, min_hours=2.0, max_hours=2.0, flex_pct=50, flex_euro=1)
        assert_eq(round((result["raw_windows"][0]["end"] - result["raw_windows"][0]["start"]) / 3600, 2), 2.0, "fixed 2h")
        start0 = result["raw_windows"][0]["start"]
        for _ in range(4):
            clock.advance(minutes=15)
            result = plan_once(clock, attrs, result, min_hours=2.0, max_hours=2.0, flex_pct=50, flex_euro=1)
            assert_eq(result["reason"], "planned", "fixed-length window stays planned")
            assert_eq(result["raw_windows"][0]["start"], start0, "does not grow on later ticks")

    def test_boundary_exclusive_end():
        day = datetime.datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc)
        base = day.timestamp()
        attrs = {"raw_today": slots_from(base, [0.04] * 8)}
        clock = Clock(day)
        result = plan_once(clock, attrs, None, flex_pct=0, flex_euro=0)
        w = result["raw_windows"][0]
        assert_true(now_in_windows([w], w["start"]), "on at start")
        assert_true(now_in_windows([w], w["end"] - 1), "on 1s before end")
        assert_true(not now_in_windows([w], w["end"]), "off at end")
        assert_eq(
            charger_full_power(POLICY_SOLAR_PRIORITY, result, w["end"]),
            False,
            "binary off at end",
        )

    def test_horizon_grew_but_not_cheaper_keeps_today():
        day = datetime.datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
        today_start = datetime.datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc).timestamp()
        tomorrow_start = today_start + 24 * 3600
        today = slots_from(today_start + 10 * 3600, [0.03] * 48)
        clock = Clock(day)
        attrs = {"raw_today": today, "tomorrow_valid": False}
        result = plan_once(clock, attrs, None, flex_pct=0, flex_euro=0)
        start0 = result["raw_windows"][0]["start"]
        clock.advance(minutes=15)
        result = plan_once(clock, attrs, result, flex_pct=0, flex_euro=0)
        assert_eq(result["reason"], "planned", "held before tomorrow arrives")
        attrs = {
            "raw_today": today,
            "raw_tomorrow": slots_from(tomorrow_start, [0.12] * 16),
            "tomorrow_valid": True,
        }
        result = plan_once(clock, attrs, result, flex_pct=0, flex_euro=0)
        assert_eq(result["reason"], "planned", "dearer tomorrow does not win")
        assert_eq(result["raw_windows"][0]["start"], start0, "started set kept")
        for _ in range(4):
            clock.advance(minutes=15)
            result = plan_once(clock, attrs, result, flex_pct=0, flex_euro=0)
            assert_eq(result["reason"], "planned", "still today's cheaper valley")
            assert_eq(result["raw_windows"][0]["start"], start0, "start does not slide")

    def test_started_window_switches_then_holds():
        day = datetime.datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
        today_start = datetime.datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc).timestamp()
        tomorrow_start = today_start + 24 * 3600
        today = slots_from(today_start + 10 * 3600, [0.09] * 48)
        clock = Clock(day)
        result = plan_once(clock, {"raw_today": today}, None, flex_pct=0, flex_euro=0)
        now_ts = day.timestamp()
        assert_true(
            result["raw_windows"][0]["start"] <= now_ts < result["raw_windows"][0]["end"],
            "window has already started",
        )
        clock.advance(minutes=15)
        result = plan_once(clock, {"raw_today": today}, result, flex_pct=0, flex_euro=0)
        assert_eq(result["reason"], "planned", "in-progress window stays planned")
        attrs = {
            "raw_today": today,
            "raw_tomorrow": slots_from(tomorrow_start, [0.02] * 16),
            "tomorrow_valid": True,
        }
        result = plan_once(clock, attrs, result, flex_pct=0, flex_euro=0)
        assert_eq(result["reason"], "planned", "in-progress window is still replaceable")
        assert_true(result["raw_windows"][0]["start"] >= tomorrow_start - 1, "moved to tomorrow")
        switched = result["raw_windows"][0]["start"]
        for _ in range(4):
            clock.advance(minutes=15)
            result = plan_once(clock, attrs, result, flex_pct=0, flex_euro=0)
            assert_eq(result["reason"], "planned", "switched set stays planned")
            assert_eq(result["raw_windows"][0]["start"], switched, "does not slide after switch")

    def test_later_island_not_planned_after_first_ends():
        day = datetime.datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc)
        base = day.timestamp()
        prices = [0.02] * 8 + [0.20] * 16 + [0.04] * 8
        attrs = {"raw_today": slots_from(base, prices)}
        clock = Clock(day)
        result = plan_once(clock, attrs, None, flex_pct=0, flex_euro=0)
        assert_eq(result["raw_windows"][0]["start"], base, "first island")
        last = result["raw_windows"][0]["end"]
        clock.set(datetime.datetime.fromtimestamp(last + 60, tz=timezone.utc))
        result = plan_once(clock, attrs, result, flex_pct=0, flex_euro=0)
        assert_eq(result["reason"], "planned", "clock does not pick a later island")
        assert_eq(result["raw_windows"][0]["start"], base, "finished cheapest window stays the plan")
        assert_true(not now_in_windows(result["raw_windows"], last + 60), "not usable for 22 kW")
        start0 = result["raw_windows"][0]["start"]
        clock.advance(minutes=15)
        result = plan_once(clock, attrs, result, flex_pct=0, flex_euro=0)
        assert_eq(result["reason"], "planned", "still the finished window")
        assert_eq(result["raw_windows"][0]["start"], start0, "start held")

    def test_min_hours_change_replans_during_roll():
        day = datetime.datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc)
        base = day.timestamp()
        attrs = {"raw_today": slots_from(base, [0.04] * 24)}
        clock = Clock(day)
        result = plan_once(clock, attrs, None, min_hours=2.0, max_hours=5.0, flex_pct=0, flex_euro=0)
        assert_eq(round((result["raw_windows"][0]["end"] - result["raw_windows"][0]["start"]) / 3600, 2), 2.0, "first 2 h")
        clock.advance(minutes=15)
        result = plan_once(clock, attrs, result, min_hours=3.0, max_hours=5.0, flex_pct=0, flex_euro=0)
        assert_eq(result["reason"], "planned", "min hours change replans")
        assert_true(
            (result["raw_windows"][0]["end"] - result["raw_windows"][0]["start"]) / 3600 >= 3.0 - 0.01,
            "replanned to 3 h",
        )

    def test_hourly_curve_holds_on_hour_steps():
        day = datetime.datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc)
        base = day.timestamp()
        hourly = [0.10] * 10 + [0.02] * 4 + [0.10] * 10
        attrs = {"raw_today": hourly}
        clock = Clock(day)
        result = plan_once(clock, attrs, None, flex_pct=0, flex_euro=0)
        assert_eq(result["raw_windows"][0]["start"], base + 10 * 3600, "hourly dip")
        start0 = result["raw_windows"][0]["start"]
        for _ in range(4):
            clock.advance(minutes=15)
            result = plan_once(clock, attrs, result, flex_pct=0, flex_euro=0)
            assert_eq(result["reason"], "planned", "hourly plan stays put every 15 min")
            assert_eq(result["raw_windows"][0]["start"], start0, "hourly start does not slide")

    case("roll_uniform_plan_and_active", test_roll_uniform_plan_and_active)
    case("window_does_not_slide_on_falling_prices", test_window_does_not_slide_on_falling_prices)
    case("tomorrow_switch_then_holds", test_tomorrow_switch_then_holds)
    case("helsinki_midnight_rolled_today_keeps_dip", test_helsinki_midnight_rolled_today_keeps_dip)
    case("spot_price_independent_of_kotiakku", test_spot_price_independent_of_kotiakku)
    case("blocked_hours_and_enough_solar", test_blocked_hours_and_enough_solar)
    case("horizon_clip_over_time", test_horizon_clip_over_time)
    case("surplus_floor_over_15_min", test_surplus_floor_over_15_min)
    case("surplus_split_hold_over_15_min", test_surplus_split_hold_over_15_min)
    case("surplus_phase_hold_over_15_min", test_surplus_phase_hold_over_15_min)
    case("until_unplug_clears_only_that_charger", test_until_unplug_clears_only_that_charger)
    case("min_equals_max_stays_fixed_over_time", test_min_equals_max_stays_fixed_over_time)
    case("boundary_exclusive_end", test_boundary_exclusive_end)
    case("horizon_grew_but_not_cheaper_keeps_today", test_horizon_grew_but_not_cheaper_keeps_today)
    case("started_window_switches_then_holds", test_started_window_switches_then_holds)
    case("later_island_not_planned_after_first_ends", test_later_island_not_planned_after_first_ends)
    case("min_hours_change_replans_during_roll", test_min_hours_change_replans_during_roll)
    case("hourly_curve_holds_on_hour_steps", test_hourly_curve_holds_on_hour_steps)

    run()


if __name__ == "__main__":
    main()
