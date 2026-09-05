"""Tests for the SolarPriority charge-window planner (no Home Assistant)."""

from __future__ import annotations

import datetime
from datetime import timezone

from harness import Clock, SLOT, assert_eq, assert_true, case_runner, load_mod, slots_from

planner = load_mod("planner")
choose = planner.choose
clamp_hours = planner.clamp_hours
find_seed = planner.find_seed
flex_headroom = planner.flex_headroom
pick_windows = planner.pick_windows
plan = planner.plan
now_in_windows = planner.now_in_windows


def run_plan(now, source_attrs, data=None, **extra):
    payload = {
        "min_hours": 2,
        "max_hours": 5,
        "ceiling": 0.2,
        "flex_pct": 20,
        "flex_euro": 0.02,
    }
    if data:
        payload.update(data)
    return plan(
        Clock(now),
        source_attrs,
        min_hours=payload["min_hours"],
        max_hours=payload["max_hours"],
        ceiling=payload["ceiling"],
        flex_pct=payload.get("flex_pct", 20),
        flex_euro=payload.get("flex_euro", 0.02),
        source_entity="sensor.nordpool_kwh_fi",
        **extra,
    )


def dur_h(w):
    return (w["end"] - w["start"]) / 3600.0


def ts_slots(base, prices):
    slots = []
    t = base
    for p in prices:
        slots.append([t, t + SLOT, p])
        t = t + SLOT
    return slots


def main():
    case, run = case_runner()
    now = datetime.datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)
    base = datetime.datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc).timestamp()

    def test_seed_is_cheapest_min_hours():
        prices = [0.08] * 8 + [0.01] * 8 + [0.07] * 8
        seed = find_seed(ts_slots(base, prices), 2 * 3600, base - 3600)
        assert_eq(seed[1], base + 8 * SLOT, "seed starts at the 0.01 dip")
        assert_eq(round((seed[2] - seed[1]) / 3600, 2), 2.0, "seed is min hours")

    def test_grow_cheaper_side_and_flex_or():
        prices = [0.04] * 4 + [0.05] * 8 + [0.06] * 8
        windows = pick_windows(
            ts_slots(base, prices), 2 * 3600, 5 * 3600, 0.2, base - 3600, 20, 0.02
        )
        assert_eq(len(windows), 1, "one window")
        assert_eq(windows[0]["start"], base, "grew left first (0.04 cheaper than 0.06)")
        assert_true(dur_h(windows[0]) > 2.0, "grew past the seed")
        assert_true(windows[0]["avg"] <= 0.07 + 1e-9, "avg under seed+0.02")
        assert_eq(round(flex_headroom(0.05, 20, 0.02), 4), 0.02, "euro wins on a cheap seed")
        assert_eq(round(flex_headroom(0.15, 20, 0.02), 4), 0.03, "percent wins on a dearer seed")

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
        mn, mx = clamp_hours(2.0, 2.0)
        assert_eq((mn, mx), (2.0, 2.0), "clamp keeps equal min/max")

    def test_one_window_even_with_two_nights():
        prices = [0.04] * 12 + [0.25] * 48 + [0.01] * 12
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

    def test_ceiling_aborts_cheapest_seed_no_runner_up():
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
        spike = pick_windows(
            ts_slots(base, [0.01] * 7 + [0.25] + [0.40] * 8),
            2 * 3600,
            5 * 3600,
            0.2,
            base - 3600,
            0,
            0,
        )
        assert_eq(len(spike), 1, "spike inside seed is allowed when avg ≤ ceiling")
        assert_true(spike[0]["avg"] < 0.2, "avg still under ceiling")
        grown = pick_windows(
            ts_slots(base, [0.05] * 8 + [0.25] + [0.05] * 8),
            2 * 3600,
            5 * 3600,
            0.2,
            base - 3600,
            50,
            1,
        )
        assert_eq(grown[0]["end"], base + 8 * SLOT, "grow will not add a slot > ceiling")

    def test_negative_seed_percent_uses_abs():
        assert_eq(flex_headroom(-0.05, 20, 0), 0.01, "percent uses abs(seed)")
        prices = [-0.05] * 8 + [-0.035] * 8
        windows = pick_windows(
            ts_slots(base, prices), 2 * 3600, 5 * 3600, 0.2, base - 3600, 20, 0
        )
        assert_true(windows[0]["avg"] <= -0.04 + 1e-9, "grow stops at seed+abs*20%")
        assert_true(dur_h(windows[0]) > 2.0, "negative seed can still grow")

    def test_horizon_clip_and_prices_only_fallback():
        today = slots_from(base, [0.08] * 8 + [0.01] * 8)
        tomorrow = slots_from(base + 86400, [0.001] * 16)
        attrs = {"raw_today": today, "raw_tomorrow": tomorrow}
        clock = Clock(datetime.datetime.fromtimestamp(base, tz=timezone.utc))
        clipped = plan(
            clock,
            attrs,
            today_kwh=10.0,
            tomorrow_kwh=None,
            flex_pct=0,
            flex_euro=0,
        )
        assert_eq(clipped["count"], 1, "today-only search")
        assert_true(
            clipped["raw_windows"][0]["end"] <= base + 86400 + 1,
            "tomorrow prices ignored without tomorrow kWh",
        )
        fallback = plan(
            clock, attrs, today_kwh=None, tomorrow_kwh=None, flex_pct=0, flex_euro=0
        )
        assert_eq(
            fallback["raw_windows"][0]["start"],
            base + 86400,
            "no solar at all: search all prices, take cheapest tomorrow",
        )

    def test_min_gt_max_swapped():
        mn, mx = clamp_hours(5, 2)
        assert_eq(mn, 2.0, "swapped min")
        assert_eq(mx, 5.0, "swapped max")
        now_dt = datetime.datetime.fromtimestamp(base - 3600, tz=timezone.utc)
        out = run_plan(
            now_dt,
            {"raw_today": slots_from(base, [0.04] * 16)},
            data={"min_hours": 5, "max_hours": 2, "flex_pct": 0, "flex_euro": 0},
        )
        assert_eq(out["min_hours"], 2.0, "swapped min")
        assert_eq(out["max_hours"], 5.0, "swapped max")
        assert_eq(out["count"], 1, "one window")
        assert_eq(out["reason"], "planned", "planned")

    def test_join_current_slot():
        windows = pick_windows(
            ts_slots(base, [0.04] * 16), 2 * 3600, 5 * 3600, 0.2, base + 600, 0, 0
        )
        assert_eq(len(windows), 1, "can start this quarter")
        assert_eq(windows[0]["start"], base, "includes current slot")

    def test_short_region_skipped():
        windows = pick_windows(
            ts_slots(base, [0.05] * 7), 2 * 3600, 5 * 3600, 0.2, base - 3600, 0, 0
        )
        assert_eq(len(windows), 0, "1.75h curve has no min-length seed")

    def test_ticks_do_not_slide():
        prices = [0.08] * 20 + [0.03] * 16
        slots = ts_slots(base, prices)
        first = pick_windows(slots, 2 * 3600, 5 * 3600, 0.2, base, 0, 0)
        chosen = choose(slots, 2.0, 5.0, 0.2, base + 900, flex_pct=0, flex_euro=0)
        assert_eq(chosen["reason"], "planned", "same curve still planned")
        assert_eq(chosen["windows"][0]["start"], first[0]["start"], "start did not slide")

    def test_switch_when_tomorrow_cheaper():
        today = ts_slots(base, [0.09] * 16)
        tomorrow_start = base + 24 * 3600
        tomorrow = ts_slots(tomorrow_start, [0.02] * 16)
        now_ts = base + 3600
        prev_windows = pick_windows(today, 2 * 3600, 5 * 3600, 0.2, now_ts, 0, 0)
        chosen = choose(today + tomorrow, 2.0, 5.0, 0.2, now_ts, flex_pct=0, flex_euro=0)
        assert_eq(chosen["reason"], "planned", "cheaper tomorrow is a new environment")
        assert_true(chosen["windows"][0]["avg"] < prev_windows[0]["avg"], "new first is cheaper")

    def test_finished_window_stays_the_plan():
        slots = ts_slots(base, [0.04] * 8)
        planned = pick_windows(slots, 2 * 3600, 5 * 3600, 0.2, base - 3600, 0, 0)
        after = planned[0]["end"] + 60
        chosen = choose(slots, 2.0, 5.0, 0.2, after, flex_pct=0, flex_euro=0)
        assert_eq(chosen["reason"], "planned", "finished cheapest window stays the plan")
        assert_eq(chosen["windows"][0]["start"], planned[0]["start"], "same start")
        assert_eq(now_in_windows(chosen["windows"], after), False, "not usable for 22 kW")

    def test_replan_when_ceiling_or_flex_changes():
        slots = ts_slots(base, [0.08] * 16)
        chosen = choose(slots, 2.0, 5.0, 0.05, base, flex_pct=0, flex_euro=0)
        assert_eq(chosen["reason"], "no_window", "stricter ceiling replans empty")
        flexed = choose(slots, 2.0, 5.0, 0.2, base, flex_pct=50, flex_euro=1)
        assert_eq(flexed["reason"], "planned", "flex change replans")
        assert_true(dur_h(flexed["windows"][0]) > 2.0, "flex grows")

    def test_end_to_end_script():
        prices = [0.04] * 8 + [0.2] + [0.06] * 16
        now_dt = datetime.datetime.fromtimestamp(base - 1800, tz=timezone.utc)
        out = run_plan(
            now_dt,
            {"raw_today": slots_from(base, prices), "tomorrow_valid": False},
            data={"flex_pct": 0, "flex_euro": 0},
        )
        assert_eq(out["reason"], "planned", "script planned")
        assert_eq(out["count"], 1, "one window")
        assert_eq(out["min_hours"], 2.0, "min")
        assert_eq(out["max_hours"], 5.0, "max")
        assert_eq(out["ceiling"], 0.2, "ceiling")
        assert_true(out["window_1_start"] is not None, "window 1")
        assert_true(out["window_2_start"] is None, "no second")
        assert_true("rank" not in out, "no policy rank on the result")
        assert_true(out["avg"] is not None, "active/next avg")

    def test_no_source():
        out = plan(Clock(now), None)
        assert_eq(out["reason"], "no_source", "missing entity")

    def test_script_ticks_do_not_slide():
        prices = [0.05] * 32
        now0 = datetime.datetime.fromtimestamp(base, tz=timezone.utc)
        first = run_plan(
            now0, {"raw_today": slots_from(base, prices)}, data={"flex_pct": 0, "flex_euro": 0}
        )
        assert_eq(first["reason"], "planned", "first plan")
        now1 = datetime.datetime.fromtimestamp(base + SLOT, tz=timezone.utc)
        second = run_plan(
            now1,
            {"raw_today": slots_from(base, prices)},
            data={"flex_pct": 0, "flex_euro": 0},
        )
        assert_eq(second["reason"], "planned", "same curve still planned")
        assert_eq(second["window_1_start"], first["window_1_start"], "no slide")
        assert_eq(second["window_2_start"], None, "still one window")

    def test_string_params_from_templates():
        now_dt = datetime.datetime.fromtimestamp(base - 3600, tz=timezone.utc)
        out = run_plan(
            now_dt,
            {"raw_today": slots_from(base, [0.04] * 16)},
            data={"min_hours": "2", "max_hours": "5", "ceiling": "0.2", "flex_pct": "0", "flex_euro": "0"},
        )
        assert_eq(out["count"], 1, "string params parse")
        assert_eq(out["min_hours"], 2.0, "min from string")
        assert_eq(out["ceiling"], 0.2, "ceiling from string")

    def test_restore_policy_maps_legacy_names():
        assert_eq(planner.restore_policy("Cheapest"), "SolarPriority", "Cheapest")
        assert_eq(planner.restore_policy("Supercheap"), "SolarPriority", "Supercheap")
        assert_eq(planner.restore_policy("Longest"), "SolarPriority", "Longest")
        assert_eq(planner.restore_policy("Earliest"), "SolarPriority", "Earliest")
        assert_eq(planner.restore_policy("SolarPriority"), "SolarPriority", "already new")
        assert_eq(planner.restore_policy("Force on"), "Force on", "force on kept")

    def test_until_unplug_overrides_policy():
        step = planner.until_unplug_step
        full = planner.charger_full_power
        result = {"raw_windows": [{"start": 1000, "end": 2000}]}
        assert_eq(full("Force off", result, 1500), False, "force off")
        assert_eq(
            planner.charger_surplus("Force off", result, 1500),
            False,
            "Force off never leftover",
        )
        assert_eq(
            planner.charger_surplus("SolarPriority", result, 0),
            True,
            "SolarPriority leftover outside a window",
        )
        assert_eq(
            planner.charger_surplus("SolarAndGrid", result, 0),
            True,
            "SolarAndGrid leftover outside a window",
        )
        assert_eq(
            full("SolarAndGrid", result, 1500, enough_solar=True),
            True,
            "SolarAndGrid 22 kW ignores enough solar",
        )
        assert_eq(
            planner.charger_surplus("SolarAndGrid", result, 1500, enough_solar=True),
            False,
            "SolarAndGrid in-window is not leftover",
        )
        assert_eq(
            planner.charger_surplus("Force off", result, 1500, until_unplug=True),
            False,
            "until unplug is full-power, not leftover",
        )
        assert_eq(
            full("Force off", result, 1500, until_unplug=True),
            True,
            "until unplug overrides Force off",
        )
        assert_eq(
            full("SolarPriority", result, 1500, until_unplug=True),
            True,
            "until unplug overrides SolarPriority",
        )
        assert_eq(
            full("SolarPriority", result, 1500, enough_solar=True, until_unplug=True),
            True,
            "until unplug overrides enough solar",
        )
        on, seen = step(True, False, False)
        assert_eq((on, seen), (True, False), "unplugged start waits for a plug")
        on, seen = step(True, True, False)
        assert_eq((on, seen), (True, True), "plug arms seen")
        on, seen = step(True, True, True)
        assert_eq((on, seen), (True, True), "still plugged (charging or complete) keeps override")
        on, seen = step(True, False, True)
        assert_eq((on, seen), (False, False), "unplug after seen clears override")
        on, seen = step(False, True, True)
        assert_eq((on, seen), (False, False), "manual off clears seen")
        other_on, other_seen = step(False, False, False)
        assert_eq((other_on, other_seen), (False, False), "other charger untouched")

    def test_collect_slots_hourly_and_half_hour():
        clock = Clock(datetime.datetime.fromtimestamp(base, tz=timezone.utc))
        hourly = [0.10] * 10 + [0.02] * 4 + [0.10] * 10
        out = plan(clock, {"raw_today": hourly}, flex_pct=0, flex_euro=0)
        assert_eq(out["slot_count"], 24, "24 hourly values")
        assert_eq(round(dur_h(out["raw_windows"][0]), 2), 2.0, "hourly seed is 2 h")
        assert_eq(out["raw_windows"][0]["start"], base + 10 * 3600, "hours 10–14")
        grown = plan(clock, {"raw_today": hourly}, flex_pct=20, flex_euro=0.02)
        step = grown["raw_windows"][0]["end"] - grown["raw_windows"][0]["start"]
        assert_eq(step % 3600, 0, "hourly grow stays on 1 h boundaries")
        half = [0.08] * 16 + [0.03] * 8 + [0.08] * 24
        half_out = plan(clock, {"raw_today": half}, flex_pct=0, flex_euro=0)
        assert_eq(half_out["slot_count"], 48, "48 half-hour values")
        assert_eq(half_out["raw_windows"][0]["start"], base + 16 * 1800, "cheapest 2 h on 30-min curve")
        empty = plan(clock, {})
        assert_eq(empty["reason"], "no_slots", "empty attrs")
        none_ceil = plan(clock, {"raw_today": hourly}, ceiling=None, flex_pct=0, flex_euro=0)
        assert_eq(none_ceil["ceiling"], 0.2, "None ceiling uses default 0.2")

    def test_current_or_next_and_flex_attrs():
        clock = Clock(datetime.datetime.fromtimestamp(base, tz=timezone.utc))
        first = plan(clock, {"raw_today": slots_from(base, [0.04] * 16)}, flex_pct=20, flex_euro=0.02)
        assert_eq(first["flex_pct"], 20.0, "flex percent stored")
        assert_eq(first["flex_euro"], 0.02, "flex euro stored")
        nxt = planner.current_or_next(first["raw_windows"], base - 60)
        assert_eq(nxt["start"], first["raw_windows"][0]["start"], "before start: next window")
        cur = planner.current_or_next(first["raw_windows"], first["raw_windows"][0]["start"] + 60)
        assert_eq(cur["start"], first["raw_windows"][0]["start"], "inside: current window")
        past = planner.current_or_next(first["raw_windows"], first["raw_windows"][0]["end"] + 60)
        assert_eq(past, None, "after end: no later window")
        assert_eq(planner.now_in_windows([], base), False, "empty windows")
        assert_eq(planner.clip_slots_to_forecast(clock, [], None, None, clock.now()), [], "empty clip")
        flagged = plan(
            clock,
            {"raw_today": slots_from(base, [0.04] * 16), "tomorrow_valid": True},
            flex_pct=0,
            flex_euro=0,
        )
        assert_eq(flagged["tomorrow_ok"], True, "tomorrow_valid flag")
        no_flag = plan(clock, {"raw_today": slots_from(base, [0.04] * 16)}, flex_pct=0, flex_euro=0)
        assert_eq(no_flag["tomorrow_ok"], False, "today-only prices are not tomorrow_ok")

    def test_horizon_tomorrow_only_and_zero_today():
        today = slots_from(base, [0.001] * 16)
        tomorrow = slots_from(base + 86400, [0.08] * 16)
        clock = Clock(datetime.datetime.fromtimestamp(base, tz=timezone.utc))
        tom = plan(
            clock,
            {"raw_today": today, "raw_tomorrow": tomorrow},
            today_kwh=None,
            tomorrow_kwh=12.0,
            flex_pct=0,
            flex_euro=0,
        )
        assert_true(tom["raw_windows"][0]["start"] >= base + 86400 - 1, "tomorrow-only solar")
        zero = plan(
            clock,
            {"raw_today": today, "raw_tomorrow": tomorrow},
            today_kwh=0.0,
            tomorrow_kwh=None,
            flex_pct=0,
            flex_euro=0,
        )
        assert_true(zero["raw_windows"][0]["end"] <= base + 86400 + 1, "today kWh=0 is present")
        both = plan(
            clock,
            {
                "raw_today": slots_from(base, [0.08] * 16),
                "raw_tomorrow": slots_from(base + 86400, [0.01] * 16),
            },
            today_kwh=5.0,
            tomorrow_kwh=5.0,
            flex_pct=0,
            flex_euro=0,
        )
        assert_true(
            both["raw_windows"][0]["start"] >= base + 86400 - 1,
            "both forecasts present: cheaper tomorrow wins",
        )
        tie = plan(
            clock,
            {"raw_today": slots_from(base, [0.08] * 16), "raw_tomorrow": tomorrow},
            today_kwh=5.0,
            tomorrow_kwh=5.0,
            flex_pct=0,
            flex_euro=0,
        )
        assert_true(
            tie["raw_windows"][0]["end"] <= base + 86400 + 1,
            "equal prices: earlier day wins",
        )

    case("seed_is_cheapest_min_hours", test_seed_is_cheapest_min_hours)
    case("grow_cheaper_side_and_flex_or", test_grow_cheaper_side_and_flex_or)
    case("flex_zero_and_min_equals_max_do_not_grow", test_flex_zero_and_min_equals_max_do_not_grow)
    case("one_window_even_with_two_nights", test_one_window_even_with_two_nights)
    case("blocked_hours_not_filled", test_blocked_hours_not_filled)
    case("ceiling_aborts_cheapest_seed_no_runner_up", test_ceiling_aborts_cheapest_seed_no_runner_up)
    case("negative_seed_percent_uses_abs", test_negative_seed_percent_uses_abs)
    case("horizon_clip_and_prices_only_fallback", test_horizon_clip_and_prices_only_fallback)
    case("min_gt_max_swapped", test_min_gt_max_swapped)
    case("join_current_slot", test_join_current_slot)
    case("short_region_skipped", test_short_region_skipped)
    case("ticks_do_not_slide", test_ticks_do_not_slide)
    case("switch_when_tomorrow_cheaper", test_switch_when_tomorrow_cheaper)
    case("finished_window_stays_the_plan", test_finished_window_stays_the_plan)
    case("replan_when_ceiling_or_flex_changes", test_replan_when_ceiling_or_flex_changes)
    case("end_to_end_script", test_end_to_end_script)
    case("no_source", test_no_source)
    case("script_ticks_do_not_slide", test_script_ticks_do_not_slide)
    case("string_params_from_templates", test_string_params_from_templates)
    case("restore_policy_maps_legacy_names", test_restore_policy_maps_legacy_names)
    case("until_unplug_overrides_policy", test_until_unplug_overrides_policy)
    case("collect_slots_hourly_and_half_hour", test_collect_slots_hourly_and_half_hour)
    case("current_or_next_and_flex_attrs", test_current_or_next_and_flex_attrs)
    case("horizon_tomorrow_only_and_zero_today", test_horizon_tomorrow_only_and_zero_today)

    surplus = load_mod("surplus")
    leftover_w = surplus.leftover_w
    budget = surplus.budget
    car_plugged = surplus.car_plugged

    def test_solar_forecast_and_full_power():
        energy_kwh = surplus.energy_kwh
        upcoming = surplus.upcoming_solar_kwh
        enough = surplus.enough_solar
        full = planner.charger_full_power
        assert_eq(energy_kwh("unknown"), None, "unknown forecast")
        assert_eq(energy_kwh("50", "kWh"), 50.0, "kWh")
        assert_eq(energy_kwh("50000", "Wh"), 50.0, "Wh to kWh")
        assert_eq(energy_kwh("5000", "W"), None, "power is not energy")
        assert_eq(upcoming(None, None), None, "no sensors")
        assert_eq(upcoming(8.0, 6.0), 8.0, "max today vs tomorrow")
        assert_eq(upcoming(None, 50.0), 50.0, "tomorrow only")
        assert_eq(upcoming(0.0, 50.0), 50.0, "today 0, use tomorrow")
        assert_eq(enough(None, 40), False, "unknown is not enough")
        assert_eq(enough(8.0, 40), False, "winter 8 kWh is not enough")
        assert_eq(enough(40.0, 40), True, "at the 40 kWh threshold")
        assert_eq(enough(50.0, 40), True, "50 kWh is enough")
        assert_eq(enough(50.0, 0), False, "threshold 0 is never enough")
        result = {"raw_windows": [{"start": 3000, "end": 4000}]}
        assert_eq(full("SolarPriority", result, 3500), True, "in window")
        assert_eq(
            full("SolarPriority", result, 3500, enough_solar=True),
            False,
            "enough solar skips 22 kW",
        )
        assert_eq(full("Supercheap", result, 3500), True, "legacy Supercheap maps")
        assert_eq(
            full("Cheapest", result, 3500, enough_solar=True),
            False,
            "legacy Cheapest now skips on enough solar",
        )
        assert_eq(full("Force off", result, 3500), False, "force off")
        assert_eq(full("Force on", result, 0, enough_solar=True), True, "Force on ignores enough solar")
        assert_eq(
            full("SolarAndGrid", result, 3500, enough_solar=True),
            True,
            "SolarAndGrid ignores enough solar",
        )
        assert_eq(
            planner.charger_surplus("SolarAndGrid", result, 0),
            True,
            "SolarAndGrid leftover outside a window",
        )
        assert_eq(full("Longest", result, 3500, enough_solar=True), False, "legacy Longest skips")
        assert_eq(full("SolarPriority", {}, 3500), False, "empty result")
        assert_eq(full("SolarPriority", None, 0), False, "missing result")

    def test_blocked_hours_pick_night_not_midday():
        night = [0.05] * 32
        midday = [0.01] * 32
        rest = [0.2] * 32
        attrs = {"raw_today": slots_from(base, night + midday + rest)}
        midday_block = [(base + 8 * 3600, base + 16 * 3600)]
        clock0 = Clock(datetime.datetime.fromtimestamp(base, tz=timezone.utc))
        off = plan(
            clock0,
            attrs,
            blocked=midday_block,
            flex_pct=0,
            flex_euro=0,
        )
        assert_eq(off["raw_windows"][0]["start"], base, "blocked midday: night island")
        assert_eq(len(off["blocked"]), 1, "stores surplus hours")
        full = planner.charger_full_power
        assert_eq(full("SolarPriority", off, base + 10 * 3600), False, "midday blocked")
        assert_eq(full("SolarPriority", off, base + 3600), True, "night window")
        none = plan(clock0, attrs, flex_pct=0, flex_euro=0)
        assert_eq(
            none["raw_windows"][0]["start"],
            base + 8 * 3600,
            "without blocked hours the midday dip wins",
        )
        held = plan(clock0, attrs, blocked=midday_block, flex_pct=0, flex_euro=0)
        assert_eq(held["reason"], "planned", "blocked window is still planned")
        assert_eq(held["raw_windows"][0]["start"], off["raw_windows"][0]["start"], "same night start")
        replanned = plan(clock0, attrs, blocked=[], flex_pct=0, flex_euro=0)
        assert_eq(replanned["reason"], "planned", "blocked change replans")
        assert_eq(
            replanned["raw_windows"][0]["start"],
            base + 8 * 3600,
            "clearing the block takes the midday dip",
        )

    def test_surplus_hour_ranges():
        from zoneinfo import ZoneInfo

        hel = ZoneInfo("Europe/Helsinki")
        clock = Clock(datetime.datetime(2026, 3, 15, 12, 0, tzinfo=hel), tz=hel)
        today_start = clock.start_of_local_day(clock.now())
        hours = surplus.expected_hour_kwh(
            clock,
            today_start,
            today_start + datetime.timedelta(days=1),
            50.0,
            60.17,
            24.94,
        )
        by_hour = {
            datetime.datetime.fromtimestamp(start, tz=hel).hour: kwh
            for start, _end, kwh in hours
        }
        assert_true(by_hour.get(8, 0) > 0, "full-day spread includes morning")
        assert_true(by_hour.get(12, 0) >= 1.0, "noon 50 kWh today is ≥ 1 kWh")
        assert_true(by_hour.get(23, 1) < 1.0, "night hour is under 1 kWh")
        ranges = surplus.surplus_hour_ranges(clock, 50.0, None, 1.0, 60.17, 24.94)
        assert_true(len(ranges) >= 1, "50 kWh today excludes productive hours")
        noon = datetime.datetime(2026, 3, 15, 12, 0, tzinfo=hel).timestamp()
        morning = datetime.datetime(2026, 3, 15, 8, 0, tzinfo=hel).timestamp()
        assert_true(
            any(start <= noon < end for start, end in ranges),
            "noon is excluded from Off-sun",
        )
        assert_true(
            any(start <= morning < end for start, end in ranges),
            "morning is still excluded when planning at noon",
        )
        night = datetime.datetime(2026, 3, 15, 23, 0, tzinfo=hel).timestamp()
        assert_true(
            not any(start <= night < end for start, end in ranges),
            "23:00 is off-sun",
        )
        dawn_only = surplus.surplus_hour_ranges(clock, 4.0, None, 1.0, 60.17, 24.94)
        assert_true(
            not any(start <= night < end for start, end in dawn_only),
            "4 kWh today does not exclude night",
        )
        assert_eq(
            surplus.surplus_hour_ranges(clock, 50.0, 50.0, 0, 60.17, 24.94),
            [],
            "hour threshold 0 excludes nothing",
        )
        assert_eq(
            surplus.surplus_hour_ranges(clock, None, None, 1.0, 60.17, 24.94),
            [],
            "unknown forecast excludes nothing",
        )

    def test_leftover_and_budget():
        assert_eq(leftover_w(3000, 1000, 500), 2500, "leftover")
        assert_eq(leftover_w(800, 3500, 3000), 300, "house includes 3 kW EV: add back")
        assert_eq(
            leftover_w(800, 500, 3000),
            300,
            "house misses the 3 kW car: do not invent surplus",
        )
        assert_eq(
            leftover_w(3800, 3500, 12000),
            300,
            "Controller still has an unplugged car: do not add 12 kW",
        )
        assert_eq(surplus.effective_ev_w(12000, 3000), 3000, "nrg beats lagged Controller")
        assert_eq(surplus.effective_ev_w(0, 3000, controller_usable=False), 3000, "unknown Controller uses nrg")
        assert_eq(surplus.effective_ev_w(3000, None), 3000, "no nrg keeps Controller")
        assert_eq(surplus.effective_ev_w(3000, 0), 3000, "zero nrg is missing, keep Controller")
        lot, psm, amp = budget(2500, 6, 32, 50, 230, 4140)
        assert_eq(lot, 10, "2500 W 1-phase lot")
        assert_eq(psm, 1, "1-phase")
        assert_eq(amp, 10, "amp")
        lot3, psm3, amp3 = budget(16000, 6, 32, 50, 230, 4140)
        assert_eq(lot3, 23, "16 kW 3-phase lot")
        assert_eq(psm3, 2, "3-phase")
        assert_eq(amp3, 23, "amp cap lot")
        lot5, psm5, amp5 = budget(5000, 6, 32, 50, 230, 4140)
        assert_eq((lot5, psm5, amp5), (7, 2, 7), "5 kW is 3-phase 7 A, not stuck at 6")
        lot8, psm8, amp8 = budget(8000, 6, 32, 50, 230, 4140)
        assert_eq((lot8, psm8, amp8), (11, 2, 11), "8 kW is 3-phase 11 A")
        lot12, psm12, amp12 = budget(12000, 6, 32, 50, 230, 4140)
        assert_eq((lot12, psm12, amp12), (17, 2, 17), "12 kW is 3-phase 17 A")
        lot_min3, psm_min3, amp_min3 = budget(4140, 6, 32, 50, 230, 4140)
        assert_eq((lot_min3, psm_min3, amp_min3), (6, 2, 6), "4140 W is the 3-phase 6 A floor")
        lot1, psm1, amp1 = budget(2000, 6, 32, 50, 230, 4140)
        assert_eq((lot1, psm1, amp1), (8, 1, 8), "2 kW is 1-phase 8 A")
        hold1 = budget(8000, 6, 32, 50, 230, 4140, force_psm=1)
        assert_eq((hold1[1], hold1[2]), (1, 32), "force 1-phase 8 kW is 32 A")
        hold3 = budget(3000, 6, 32, 50, 230, 4140, force_psm=2)
        assert_eq((hold3[1], hold3[2]), (2, 6), "force 3-phase below 4140 W stays 6 A")
        want = surplus.surplus_want_w
        assert_eq(
            want(12000, 4140, last_amp=6, last_psm=2),
            12000,
            "at 6 A 3-phase cap while leftover allows 17 A: want leftover",
        )
        assert_eq(
            want(8000, 4140, last_amp=6, last_psm=2),
            8000,
            "at 6 A 3-phase cap leftover 8 kW: raise to leftover",
        )
        assert_eq(
            want(12000, 10000, last_amp=16, last_psm=2),
            10000,
            "10 kW take below 16 A cap: car-limited, unused leftover",
        )
        assert_eq(
            want(12000, 10800, last_amp=16, last_psm=2),
            12000,
            "take at 16 A cap leftover allows 17 A: want leftover",
        )
        assert_eq(
            want(4140, 4140, last_amp=6, last_psm=2),
            4140,
            "leftover only supports 6 A 3-phase: stay at take",
        )
        assert_eq(want(12000, 0), 0, "not accepting")
        assert_eq(want(12000, None), 12000, "unknown take wants leftover")
        assert_eq(
            want(8000, 3900, last_amp=17, last_psm=1),
            8000,
            "1-phase at cap leftover allows 3-phase: want leftover",
        )
        assert_true(car_plugged("Charging"), "charging")
        assert_true(car_plugged("Complete"), "full battery is still plugged")
        assert_true(car_plugged("4"), "car state 4 is still plugged")
        assert_true(not car_plugged("Idle"), "idle is unplugged")
        assert_true(not car_plugged("1"), "unplugged")
        usable = surplus.sensor_usable
        assert_true(usable("96.2"), "soc number")
        assert_true(usable("0"), "zero is usable")
        assert_true(not usable("unknown"), "unknown")
        assert_true(not usable("unavailable"), "unavailable")
        assert_true(not usable(""), "empty")
        assert_true(not usable(None), "missing")
        assert_true(not usable("n/a"), "garbage")

    def test_group_surplus_setpoint_keeps_app_priorities():
        setp = surplus.group_surplus_setpoint
        leftover = setp(10, 1, 10, n_full=0, group_lot=50)
        assert_eq(leftover, (10, 1, 10), "pure surplus uses leftover lot")
        assert_eq(
            setp(10, 1, 10, n_full=1, group_lot=50),
            (50, 1, 10),
            "mixed: keep group lot 50, leftover amp",
        )
        assert_eq(
            setp(25, 2, 25, n_full=1, group_lot=50),
            (50, 2, 25),
            "mixed: do not cap leftover amp to reserve 32 A",
        )
        assert_eq(
            setp(6, 1, 6, n_full=1, group_lot=50),
            (50, 1, 6),
            "mixed: 6 A leftover amp beside full-power",
        )
        assert_eq(
            setp(10, 1, 10, n_full=1, group_lot=32),
            (32, 1, 10),
            "mixed: keep group_lot, leftover amp; app lop splits the group",
        )

    case("solar_forecast_and_full_power", test_solar_forecast_and_full_power)
    case("blocked_hours_pick_night_not_midday", test_blocked_hours_pick_night_not_midday)
    case("surplus_hour_ranges", test_surplus_hour_ranges)
    case("leftover_and_budget", test_leftover_and_budget)
    case("group_surplus_setpoint_keeps_app_priorities", test_group_surplus_setpoint_keeps_app_priorities)

    def test_surplus_allocations_steal_second_charger_floor():
        alloc = surplus.surplus_allocations
        plan = surplus.surplus_allocation_plan
        a, b = "111111", "222222"
        lops = {a: 1, b: 50}
        plugged = {a: True, b: True}
        kwargs = dict(
            lops=lops,
            plugged=plugged,
            split_min_w=3000,
            split_floor_w=500,
            charger_max_w=32 * 230 * 3,
        )
        assert_eq(
            alloc([a, b], leftover_w=12000, **kwargs),
            {a: 12000},
            "12 kW both wanting surplus: high keeps all until it leaves leftover",
        )
        assert_eq(
            alloc([a, b], leftover_w=8000, **kwargs),
            {a: 8000},
            "8 kW both charging unknown: high wants all, no second car yet",
        )
        assert_eq(
            alloc(
                [a, b],
                leftover_w=12000,
                lops=lops,
                plugged=plugged,
                split_min_w=3000,
                charger_max_w=22080,
                take_w={a: 10000, b: 0},
                states={a: "Charging", b: "Charging"},
            ),
            {a: 9000, b: 3000},
            "remainder 2 kW is below 3 kW and above 500 W: steal to 9+3",
        )
        assert_eq(
            alloc(
                [a, b],
                leftover_w=6000,
                lops=lops,
                plugged=plugged,
                split_min_w=3000,
                charger_max_w=22080,
                take_w={a: 5499, b: 0},
                states={a: "Charging", b: "Charging"},
            ),
            {a: 3000, b: 3000},
            "6 kW leftover is the 3+3 minimum split",
        )
        assert_eq(
            alloc(
                [a, b],
                leftover_w=4500,
                lops=lops,
                plugged=plugged,
                split_min_w=3000,
                charger_max_w=22080,
                take_w={a: 4400, b: 0},
                states={a: "Charging", b: "Charging"},
                split_hold=True,
            ),
            {a: 4400},
            "4.5 kW leftover would be 1.5+3: no steal even during grace",
        )
        assert_eq(
            alloc(
                [a, b],
                leftover_w=8000,
                lops=lops,
                plugged=plugged,
                split_min_w=3000,
                charger_max_w=22080,
                take_w={a: 7499, b: 0},
                states={a: "Charging", b: "Charging"},
            ),
            {a: 5000, b: 3000},
            "remainder 501 W: steal to 5+3",
        )
        assert_eq(
            alloc(
                [a, b],
                leftover_w=8000,
                lops=lops,
                plugged=plugged,
                split_min_w=3000,
                charger_max_w=22080,
                take_w={a: 7500, b: 0},
                states={a: "Charging", b: "Charging"},
            ),
            {a: 7500},
            "500 W remainder is the dead zone; do not start the second car",
        )
        assert_eq(
            alloc(
                [a, b],
                leftover_w=18000,
                lops=lops,
                plugged=plugged,
                split_min_w=3000,
                charger_max_w=22080,
                take_w={a: 10000, b: 0},
                states={a: "Charging", b: "Charging"},
            ),
            {a: 10000, b: 8000},
            "remainder 8 kW is at least 3 kW: high keeps what it wants",
        )
        assert_eq(
            alloc([a, b], leftover_w=4000, **kwargs),
            {a: 4000},
            "4 kW high wants all: do not start a second car",
        )
        assert_eq(
            alloc([a, b], leftover_w=12000, split_hold=True, **kwargs),
            {a: 9000, b: 3000},
            "grace: second car already on, high now takes all 12 kW, keep 9+3",
        )
        grace = plan([a, b], leftover_w=12000, split_hold=True, **kwargs)
        assert_eq(grace["arm_split_hold"], True, "grace arms 15 min hold")
        assert_eq(grace["remainder_w"], 0, "high fully utilizes leftover")
        assert_eq(
            alloc([a, b], leftover_w=12000, split_hold=True, split_expired=True, **kwargs),
            {a: 12000},
            "after 15 min grace, drop the second car",
        )
        expired = plan([a, b], leftover_w=12000, split_hold=True, split_expired=True, **kwargs)
        assert_eq(expired["arm_split_hold"], False, "expired grace does not re-arm")
        assert_eq(
            alloc(
                [a, b],
                leftover_w=8000,
                lops=lops,
                plugged=plugged,
                split_min_w=3000,
                charger_max_w=22080,
                take_w={a: 7500, b: 0},
                states={a: "Charging", b: "Charging"},
                split_hold=True,
            ),
            {a: 5000, b: 3000},
            "grace: 500 W dead zone keeps 3 kW on the second car",
        )
        assert_eq(
            alloc(
                [a, b],
                leftover_w=8000,
                lops={a: 50, b: 50},
                plugged=plugged,
                split_min_w=3000,
                charger_max_w=22080,
            ),
            {a: 8000, b: 8000},
            "equal HA priority: both get leftover, go-e shares",
        )
        assert_eq(
            alloc(
                [a, b],
                leftover_w=8000,
                lops={a: None, b: 50},
                plugged=plugged,
                split_min_w=3000,
                charger_max_w=22080,
            ),
            {a: 8000, b: 8000},
            "unknown priority: do not guess, same leftover on both",
        )
        assert_eq(
            alloc(
                [a, b],
                leftover_w=8000,
                lops=lops,
                plugged={a: False, b: True},
                split_min_w=3000,
                charger_max_w=22080,
            ),
            {b: 8000},
            "higher priority unplugged: next plugged gets leftover",
        )
        assert_eq(
            alloc(
                [a, b],
                leftover_w=300,
                lops=lops,
                plugged={a: False, b: True},
                split_min_w=3000,
                charger_max_w=22080,
                take_w={a: 0, b: 3000},
                states={a: "Idle", b: "Charging"},
                split_hold=True,
            ),
            {b: 300},
            "unplugged first + stale 3 kW steal: only leftover, not 3 kW",
        )
        tiny = leftover_w(800, 500, 3000)
        assert_eq(tiny, 300, "house-without-EV leftover is 300 W")
        assert_eq(
            alloc(
                [a, b],
                leftover_w=tiny,
                lops=lops,
                plugged={a: False, b: True},
                split_min_w=3000,
                charger_max_w=22080,
                split_hold=True,
            ),
            {b: 300},
            "300 W leftover on the only plugged car, no 3 kW floor",
        )
        _lot, psm_tiny, amp_tiny = surplus.budget(tiny, 6, 32, 50, 230, 4140)
        assert_eq((psm_tiny, amp_tiny), (1, 6), "300 W budgets 6 A, not 13 A / 3 kW")
        held = surplus.surplus_decision(
            True, 1500, 96, window_ok=True, hold_active=True, hold_exit_w=2000
        )
        assert_true(held["use_floor_budget"], "1500 W stays in 6 A hold until 2000 W")
        recovered = surplus.surplus_decision(
            True, 2100, 96, window_ok=True, hold_active=True, hold_exit_w=2000
        )
        assert_true(not recovered["arm_floor"], "2100 W leaves the hold")
        assert_eq(
            alloc(
                [a, b],
                leftover_w=8000,
                lops={a: 50, b: 50},
                plugged={a: False, b: True},
                split_min_w=3000,
                charger_max_w=22080,
            ),
            {b: 8000},
            "equal priority: unplugged charger is not offered leftover",
        )
        assert_eq(
            alloc(
                [a, b],
                leftover_w=8000,
                lops=lops,
                plugged=plugged,
                split_min_w=3000,
                charger_max_w=22080,
                take_w={a: 0, b: 0},
                states={a: "Complete", b: "Charging"},
            ),
            {b: 8000},
            "higher finished: leftover goes to lower priority",
        )
        assert_eq(
            alloc(
                [a, b],
                leftover_w=8000,
                lops=lops,
                plugged=plugged,
                split_min_w=3000,
                charger_max_w=22080,
                take_w={a: 0, b: 0},
                states={a: "WaitCar", b: "Charging"},
            ),
            {a: 8000, b: 8000},
            "higher not accepting: still offer leftover, no steal",
        )
        assert_eq(
            surplus.surplus_targets(
                [a, b],
                leftover_w=12000,
                lops=lops,
                plugged=plugged,
                split_min_w=3000,
                charger_max_w=22080,
                take_w={a: 10000, b: 0},
                states={a: "Charging", b: "Charging"},
            ),
            [a, b],
            "targets follow allocation order",
        )
        assert_eq(
            alloc([a], leftover_w=8000, **kwargs),
            {a: 8000},
            "single charger gets leftover",
        )
        lot, psm, amp = surplus.budget(12000, 6, 32, 50, 230, 4140)
        assert_eq((lot, psm, amp), (17, 2, 17), "group lot from 12 kW leftover")
        high_lot, high_psm, high_amp = surplus.budget(9000, 6, 32, 50, 230, 4140)
        assert_eq((high_psm, high_amp), (2, 13), "high 9 kW is 3-phase 13 A")
        low_lot, low_psm, low_amp = surplus.budget(3000, 6, 32, 50, 230, 4140)
        assert_eq((low_psm, low_amp), (1, 13), "low 3 kW is 1-phase 13 A")
        assert_eq(
            surplus.group_lot_for_allocations(
                17, {a: 9000, b: 3000}, min_amp=6, max_amp=32, group_lot=50, volts=230, phase3_min_w=4140
            ),
            26,
            "9+3 kW: raise group lot to 13 A + 13 A so both amp caps fit",
        )
        assert_eq(
            surplus.group_lot_for_allocations(
                17, {a: 12000, b: 12000}, min_amp=6, max_amp=32, group_lot=50, volts=230, phase3_min_w=4140
            ),
            17,
            "same leftover on both: keep leftover lot",
        )
        assert_eq(surplus.parse_lop("1"), 1, "lop 1")
        assert_eq(surplus.parse_lop("50"), 50, "lop 50")
        assert_eq(surplus.parse_lop("unknown"), None, "unknown lop")
        assert_eq(surplus.parse_lop("0"), None, "0 is out of app range")
        assert_eq(surplus.nrg_total_w([230, 230, 230, 0, 10, 10, 10, 0, 2300, 2300, 2300, 6900]), 6900, "nrg[11] watts")
        assert_eq(surplus.nrg_total_w("[230,230,230,0,10,10,10,0,2300,2300,2300,6900]"), 6900, "nrg JSON list")
        assert_eq(surplus.nrg_total_w("1500"), 1500, "nrg numeric sensor")
        assert_eq(surplus.nrg_total_w([1, 2, 3]), None, "short nrg list")
        lot_u, psm_u, amp_u = surplus.budget(4139, 6, 32, 50, 230, 4140)
        assert_eq((psm_u, amp_u), (1, 17), "4139 W is still 1-phase")
        assert_eq(surplus.surplus_want_w(12000, 50), 50, "take under 100 W is not accepting")
        assert_eq(surplus.parse_lop("99"), 99, "lop 99")
        assert_eq(surplus.parse_lop("100"), None, "lop 100")
        assert_eq(surplus.car_finished("Complete"), True, "complete finished")
        assert_eq(surplus.car_charging("Charging"), True, "charging")
        assert_eq(surplus.charger_take_w("Complete", 8000, 8000, 22080), 0, "finished takes 0")
        assert_eq(surplus.charger_take_w("WaitCar", None, 8000, 22080), 0, "waitcar takes 0")
        assert_eq(surplus.charger_take_w("Charging", None, 8000, 22080), 8000, "unknown charging assumes full")
        assert_eq(surplus.charger_take_w("Charging", 5000, 18000, 22080), 5000, "partial take")
        assert_eq(surplus.min_charge_w(8000, 6, 230, 4140), 4140, "8 kW is 3-phase 6 A")
        assert_eq(surplus.min_charge_w(2000, 6, 230, 4140), 1380, "2 kW is 1-phase 6 A")

    def test_phase_hold_both_directions():
        hold = surplus.phase_hold_psm
        phase = surplus.surplus_phase_budget
        args = (6, 32, 50, 230, 4140)
        assert_eq(hold(2, None)["arm"], False, "first start has no last psm")
        assert_eq(hold(2, None)["psm"], 2, "first start uses wanted")
        assert_eq(hold(2, 2), {"psm": 2, "arm": False}, "same 3-phase: no arm")
        assert_eq(hold(2, 1), {"psm": 1, "arm": True}, "1→3 waits")
        assert_eq(
            hold(2, 1, hold_expired=True),
            {"psm": 2, "arm": False},
            "1→3 after hold: switch",
        )
        assert_eq(hold(1, 2), {"psm": 2, "arm": True}, "3→1 waits")
        assert_eq(
            hold(1, 2, hold_expired=True),
            {"psm": 1, "arm": False},
            "3→1 after hold: switch",
        )
        up = phase(8000, *args, last_psm=1)
        assert_eq(up["psm"], 1, "8 kW still 1-phase during 1→3 hold")
        assert_eq(up["amp"], 32, "8 kW 1-phase hold is max 32 A, not 11 A 3-phase")
        assert_eq(up["wanted_psm"], 2, "leftover wants 3-phase")
        assert_eq(up["arm_phase"], True, "arm 1→3")
        up12 = phase(12000, *args, last_psm=1)
        assert_eq(up12["amp"], 32, "12 kW 1-phase hold is still max 32 A, not 17 A")
        up_done = phase(8000, *args, last_psm=1, hold_expired=True)
        assert_eq((up_done["psm"], up_done["amp"]), (2, 11), "after 1→3: 11 A 3-phase")
        assert_eq(up_done["arm_phase"], False, "expired does not re-arm")
        down = phase(3000, *args, last_psm=2)
        assert_eq((down["psm"], down["amp"]), (2, 6), "3 kW stays 6 A 3-phase during hold")
        assert_eq(down["wanted_psm"], 1, "leftover wants 1-phase")
        assert_eq(down["arm_phase"], True, "arm 3→1")
        down_from_high = phase(3000, *args, last_psm=2)
        assert_eq(down_from_high["amp"], 6, "3→1 does not keep the previous 3-phase take")
        down_done = phase(3000, *args, last_psm=2, hold_expired=True)
        assert_eq((down_done["psm"], down_done["amp"]), (1, 13), "after 3→1: 13 A 1-phase")
        stay = phase(8000, *args, last_psm=2)
        assert_eq((stay["psm"], stay["amp"], stay["arm_phase"]), (2, 11, False), "already 3-phase")
        amp_lo = phase(5000, *args, last_psm=1)
        amp_hi = phase(7000, *args, last_psm=1)
        assert_eq(amp_lo["psm"], 1, "held 1-phase while leftover is 5 kW")
        assert_eq(amp_lo["amp"], 21, "amp tracks leftover on held 1-phase, not 7 A 3-phase")
        assert_eq(amp_hi["amp"], 30, "7 kW 1-phase is 30 A still held")
        assert_eq(surplus.group_lot_for_amps(7, [21], 50), 21, "raise lot so 1-phase 21 A is not clipped")
        floor = phase(0, *args, last_psm=2)
        assert_eq((floor["psm"], floor["amp"]), (2, 6), "6 A floor stays 3-phase during hold")
        assert_eq(surplus.group_lot_for_amps(11, [32], 50), 32, "raise lot so 1-phase 32 A fits")
        assert_eq(
            surplus.group_lot_for_amps(26, [13, 13], 50),
            26,
            "same 13 A after split already raised: keep lot",
        )
        assert_eq(
            surplus.group_lot_for_amps(17, [32, 13], 50),
            45,
            "1-phase hold 32 A + 3 kW 13 A: raise to the sum",
        )
        assert_eq(
            surplus.group_lot_for_amps(17, [17, 17], 50),
            17,
            "equal leftover 17 A must not sum to 34 A",
        )

    case("surplus_allocations_steal_second_charger_floor", test_surplus_allocations_steal_second_charger_floor)
    case("phase_hold_both_directions", test_phase_hold_both_directions)

    run()


if __name__ == "__main__":
    main()
