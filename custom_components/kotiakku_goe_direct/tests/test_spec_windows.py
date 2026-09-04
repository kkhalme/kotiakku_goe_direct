"""Spec lock for SolarPriority charge windows.

One cheapest windowMinHours seed, grown under flex (looser of % of
|seed| and €) up to windowMaxHours. Off-sun hours are blocked. Ceiling
is a seed-average abort and a grow hard-no. Result is still a windows list.
"""

from __future__ import annotations

import datetime
from datetime import timezone

from harness import Clock, SLOT, assert_eq, assert_true, case_runner, load_mod, slots_from

planner = load_mod("planner", "_spec_win")
choose = planner.choose
clamp_hours = planner.clamp_hours
now_in_windows = planner.now_in_windows
plan = planner.plan
pick_windows = planner.pick_windows
find_seed = planner.find_seed
flex_headroom = planner.flex_headroom
charger_full_power = planner.charger_full_power
until_unplug_step = planner.until_unplug_step


def ts_slots(base, prices, slot=SLOT):
    slots = []
    t = base
    for p in prices:
        slots.append([t, t + slot, p])
        t += slot
    return slots


def dur_h(w):
    return (w["end"] - w["start"]) / 3600.0


def main():
    case, run = case_runner()
    now = datetime.datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)
    base = datetime.datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc).timestamp()

    def test_clamp_and_flex_defaults():
        assert_eq(planner.SLOT_SECONDS, 900, "15-minute slots")
        assert_eq(planner.MAX_WINDOWS, 16, "window_N_* still go to 16")
        mn, mx = clamp_hours(None, None)
        assert_eq((mn, mx), (2.0, 5.0), "default 2–5 h")
        mn, mx = clamp_hours(2.0, 2.0)
        assert_eq((mn, mx), (2.0, 2.0), "min==max kept")
        mn, mx = clamp_hours(5, 2)
        assert_eq((mn, mx), (2.0, 5.0), "min>max swapped")
        assert_eq(round(flex_headroom(0.05, 20, 0.02), 4), 0.02, "euro wins on a cheap seed")
        assert_eq(round(flex_headroom(0.15, 20, 0.02), 4), 0.03, "percent wins on a dearer seed")
        assert_eq(flex_headroom(-0.05, 20, 0), 0.01, "percent uses abs(seed)")
        assert_eq(flex_headroom(0.05, 0, 0), 0.0, "both unused: no headroom")

    def test_seed_is_cheapest_min_hours():
        prices = [0.08] * 8 + [0.01] * 8 + [0.07] * 8
        seed = find_seed(ts_slots(base, prices), 2 * 3600, base - 3600)
        assert_eq(seed[1], base + 8 * SLOT, "seed starts at the 0.01 dip")
        assert_eq(round((seed[2] - seed[1]) / 3600, 2), 2.0, "seed is min hours")

    def test_grow_cheaper_side_and_flex_or():
        # 2 h of 0.05, left 0.04, right 0.06; allowed = 0.05+0.02
        prices = [0.04] * 4 + [0.05] * 8 + [0.06] * 8
        windows = pick_windows(
            ts_slots(base, prices), 2 * 3600, 5 * 3600, 0.2, base - 3600, 20, 0.02
        )
        assert_eq(len(windows), 1, "one window")
        assert_eq(windows[0]["start"], base, "grew left first (0.04 cheaper than 0.06)")
        assert_true(dur_h(windows[0]) > 2.0, "grew past the seed")
        assert_true(windows[0]["avg"] <= 0.07 + 1e-9, "avg under seed+0.02")

    def test_flex_zero_and_min_equals_max_do_not_grow():
        prices = [0.01] * 8 + [0.012] * 8
        no_flex = pick_windows(
            ts_slots(base, prices), 2 * 3600, 5 * 3600, 0.2, base - 3600, 0, 0
        )
        assert_eq(round(dur_h(no_flex[0]), 2), 2.0, "both flex 0: stay at min")
        fixed = pick_windows(
            ts_slots(base, prices), 2 * 3600, 2 * 3600, 0.2, base - 3600, 50, 1
        )
        assert_eq(round(dur_h(fixed[0]), 2), 2.0, "min==max: no grow even with large flex")
        quarter = pick_windows(
            ts_slots(base, [0.01] * 8), 0.25 * 3600, 0.25 * 3600, 0.2, base - 3600, 50, 1
        )
        assert_eq(round(dur_h(quarter[0]), 2), 0.25, "0.25 h / 0.25 h is one slot")

    def test_deep_valley_stays_short():
        prices = [0.08] * 8 + [0.01] * 8 + [0.08] * 8
        windows = pick_windows(
            ts_slots(base, prices), 2 * 3600, 5 * 3600, 0.2, base - 3600, 20, 0.02
        )
        assert_eq(round(dur_h(windows[0]), 2), 2.0, "0.08 flanks exceed seed+0.02")
        assert_eq(windows[0]["start"], base + 8 * SLOT, "stays on the valley")

    def test_one_window_even_with_two_nights():
        night = [0.04] * 12
        day = [0.20] * 48
        cheaper = [0.01] * 12
        prices = night + day + cheaper
        windows = pick_windows(
            ts_slots(base, prices), 2 * 3600, 5 * 3600, 0.2, base - 3600, 20, 0.02
        )
        assert_eq(len(windows), 1, "list length 1")
        assert_eq(windows[0]["start"], base + (12 + 48) * SLOT, "cheaper second night")

    def test_blocked_hours_not_filled():
        prices = [0.05] * 32 + [0.01] * 32 + [0.05] * 32
        blocked = [(base + 8 * 3600, base + 16 * 3600)]
        chosen = choose(
            ts_slots(base, prices),
            2.0,
            5.0,
            0.2,
            base - 3600,
            None,
            blocked=blocked,
            flex_pct=20,
            flex_euro=0.02,
        )
        assert_eq(len(chosen["windows"]), 1, "one island")
        assert_eq(chosen["windows"][0]["start"], base, "night, not the blocked midday dip")
        assert_true(
            chosen["windows"][0]["end"] <= base + 8 * 3600 + 1,
            "does not grow into the blocked hour",
        )

    def test_ceiling_aborts_when_cheapest_seed_is_dear():
        none = pick_windows(
            ts_slots(base, [0.25] * 8 + [0.22] * 8 + [0.30] * 8),
            2 * 3600,
            5 * 3600,
            0.2,
            base - 3600,
            20,
            0.02,
        )
        assert_eq(none, [], "cheapest seed avg > ceiling: no window")
        ok = pick_windows(
            ts_slots(base, [0.25] * 8 + [0.15] * 8),
            2 * 3600,
            5 * 3600,
            0.2,
            base - 3600,
            0,
            0,
        )
        assert_eq(ok[0]["start"], base + 8 * SLOT, "cheapest seed under ceiling is kept")

    def test_grow_refuses_over_ceiling_slot():
        prices = [0.05] * 8 + [0.25] + [0.05] * 8
        windows = pick_windows(
            ts_slots(base, prices), 2 * 3600, 5 * 3600, 0.2, base - 3600, 50, 1
        )
        assert_eq(windows[0]["end"], base + 8 * SLOT, "grow stops before the 0.25 slot")

    def test_seed_may_contain_ceiling_spike():
        # cheapest 2 h includes one 0.25 slot; avg still under 0.2
        prices = [0.01] * 7 + [0.25] + [0.40] * 8
        windows = pick_windows(
            ts_slots(base, prices), 2 * 3600, 5 * 3600, 0.2, base - 3600, 0, 0
        )
        assert_eq(len(windows), 1, "seed avg under ceiling is kept")
        assert_true(windows[0]["avg"] < 0.2, "avg still under ceiling")

    def test_horizon_clip_and_prices_only_fallback():
        today = slots_from(base, [0.08] * 8 + [0.01] * 8)
        tomorrow = slots_from(base + 86400, [0.001] * 16)
        attrs = {"raw_today": today, "raw_tomorrow": tomorrow}
        clock = Clock(datetime.datetime.fromtimestamp(base, tz=timezone.utc))
        clipped = plan(
            clock,
            attrs,
            remaining_today=10.0,
            tomorrow_kwh=None,
            flex_pct=0,
            flex_euro=0,
        )
        assert_eq(clipped["count"], 1, "today-only search")
        assert_true(
            clipped["raw_windows"][0]["end"] <= base + 86400 + 1,
            "tomorrow prices ignored without tomorrow kWh",
        )
        fallback = plan(clock, attrs, remaining_today=None, tomorrow_kwh=None, flex_pct=0, flex_euro=0)
        assert_eq(
            fallback["raw_windows"][0]["start"],
            base + 86400,
            "no solar at all: search all prices, take cheapest tomorrow",
        )

    def test_freeze_and_switch():
        today = [0.04] * 16
        clock = Clock(datetime.datetime.fromtimestamp(base + 3600, tz=timezone.utc))
        first = plan(clock, {"raw_today": slots_from(base, today)}, flex_pct=0, flex_euro=0)
        assert_eq(first["reason"], "planned", "morning plan")
        prev = planner.prev_from_result(clock, first)
        clock.set(datetime.datetime.fromtimestamp(base + 3600 + SLOT, tz=timezone.utc))
        held = plan(
            clock, {"raw_today": slots_from(base, today)}, prev=prev, flex_pct=0, flex_euro=0
        )
        assert_eq(held["reason"], "frozen", "same horizon does not slide")
        assert_eq(held["raw_windows"][0]["start"], first["raw_windows"][0]["start"], "start held")
        both = {
            "raw_today": slots_from(base, today),
            "raw_tomorrow": slots_from(base + 86400, [0.01] * 16),
        }
        switched = plan(clock, both, prev=prev, flex_pct=0, flex_euro=0)
        assert_eq(switched["reason"], "switched", "cheaper tomorrow wins")
        assert_true(
            switched["raw_windows"][0]["start"] >= base + 86400 - 1,
            "switched onto tomorrow",
        )

    def test_full_power_solarpriority():
        result = {"raw_windows": [{"start": 3000, "end": 4000}]}
        assert_eq(charger_full_power("SolarPriority", result, 3500), True, "in window")
        assert_eq(
            charger_full_power("SolarPriority", result, 3500, enough_solar=True),
            False,
            "enough solar skips 22 kW",
        )
        assert_eq(charger_full_power("Supercheap", result, 3500), True, "legacy Supercheap maps")
        assert_eq(charger_full_power("Cheapest", result, 3500), True, "legacy Cheapest maps")
        assert_eq(charger_full_power("Force off", result, 3500), False, "force off")
        assert_eq(charger_full_power("Force on", result, 0), True, "force on")
        assert_eq(
            charger_full_power("SolarPriority", result, 0, until_unplug=True),
            True,
            "until-unplug",
        )
        ov, seen = until_unplug_step(True, False, True)
        assert_eq((ov, seen), (False, False), "clears on unplug after seen")

    def test_plan_result_is_a_window_list():
        out = plan(
            Clock(now),
            {"raw_today": slots_from(base, [0.04] * 16)},
            flex_pct=0,
            flex_euro=0,
        )
        assert_true(isinstance(out["windows"], list), "windows list")
        assert_true(isinstance(out["raw_windows"], list), "raw_windows list")
        assert_eq(out["count"], 1, "one filled")
        assert_eq(out["window_1_start"], out["windows"][0]["start"], "window_1 filled")
        assert_eq(out["window_2_start"], None, "window_2 empty")
        assert_true("rank" not in out, "no policy rank on the result")
        assert_eq(out["flex_pct"], 0.0, "flex stored")

    case("clamp_and_flex_defaults", test_clamp_and_flex_defaults)
    case("seed_is_cheapest_min_hours", test_seed_is_cheapest_min_hours)
    case("grow_cheaper_side_and_flex_or", test_grow_cheaper_side_and_flex_or)
    case("flex_zero_and_min_equals_max_do_not_grow", test_flex_zero_and_min_equals_max_do_not_grow)
    case("deep_valley_stays_short", test_deep_valley_stays_short)
    case("one_window_even_with_two_nights", test_one_window_even_with_two_nights)
    case("blocked_hours_not_filled", test_blocked_hours_not_filled)
    case("ceiling_aborts_when_cheapest_seed_is_dear", test_ceiling_aborts_when_cheapest_seed_is_dear)
    case("grow_refuses_over_ceiling_slot", test_grow_refuses_over_ceiling_slot)
    case("seed_may_contain_ceiling_spike", test_seed_may_contain_ceiling_spike)
    case("horizon_clip_and_prices_only_fallback", test_horizon_clip_and_prices_only_fallback)
    case("freeze_and_switch", test_freeze_and_switch)
    case("full_power_solarpriority", test_full_power_solarpriority)
    case("plan_result_is_a_window_list", test_plan_result_is_a_window_list)
    run()


if __name__ == "__main__":
    main()
