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
        assert_eq(flex_headroom(0.05, 20, 0), 0.01, "percent only")
        assert_eq(flex_headroom(0.05, 0, 0.02), 0.02, "euro only")
        assert_eq(flex_headroom(0.0, 20, 0), 0.0, "percent of a zero seed is 0")
        assert_eq(flex_headroom(-0.05, 20, 0.02), 0.02, "euro wins over abs percent")
        assert_eq(planner.clamp_flex(None, None), (0.0, 0.0), "missing flex is unused")
        assert_eq(planner.clamp_flex(-5, -0.01), (0.0, 0.0), "negative flex is unused")
        mn, mx = clamp_hours(0.1, 30)
        assert_eq((mn, mx), (0.25, 24.0), "clamped to 0.25–24 h")
        mn, mx = clamp_hours(0, 0)
        assert_eq((mn, mx), (2.0, 5.0), "non-positive falls back to defaults")

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
        flat = pick_windows(
            ts_slots(base, [0.04] * 16), 2 * 3600, 5 * 3600, 0.2, base - 3600, 0, 0
        )
        assert_eq(round(dur_h(flat[0]), 2), 2.0, "flex 0 does not grow equal-price neighbors")
        fixed = pick_windows(
            ts_slots(base, prices), 2 * 3600, 2 * 3600, 0.2, base - 3600, 50, 1
        )
        assert_eq(round(dur_h(fixed[0]), 2), 2.0, "min==max: no grow even with large flex")
        quarter = pick_windows(
            ts_slots(base, [0.01] * 8), 0.25 * 3600, 0.25 * 3600, 0.2, base - 3600, 50, 1
        )
        assert_eq(round(dur_h(quarter[0]), 2), 0.25, "0.25 h / 0.25 h is one slot")

    def test_deep_valley_stays_short():
        # 0.20 flanks: first added slot would raise avg above seed+0.02 (0.03).
        prices = [0.20] * 8 + [0.01] * 8 + [0.20] * 8
        windows = pick_windows(
            ts_slots(base, prices), 2 * 3600, 5 * 3600, 0.2, base - 3600, 20, 0.02
        )
        assert_eq(round(dur_h(windows[0]), 2), 2.0, "0.20 flanks blow seed+0.02")
        assert_eq(windows[0]["start"], base + 8 * SLOT, "stays on the valley")

    def test_one_window_even_with_two_nights():
        night = [0.04] * 12
        day = [0.25] * 48
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
        assert_eq(charger_full_power("SolarPriority", result, 0), False, "outside window")
        assert_eq(charger_full_power("SolarPriority", result, 3000), True, "on at start")
        assert_eq(charger_full_power("SolarPriority", result, 4000), False, "off at exclusive end")
        assert_eq(
            charger_full_power("Force on", result, 0, enough_solar=True),
            True,
            "Force on ignores enough solar",
        )
        assert_eq(
            charger_full_power("Longest", result, 3500, enough_solar=True),
            False,
            "legacy Longest now skips on enough solar",
        )
        assert_eq(
            charger_full_power("Earliest", result, 3500),
            True,
            "legacy Earliest maps",
        )
        assert_eq(charger_full_power("SolarPriority", {}, 3500), False, "empty result")
        assert_eq(charger_full_power("SolarPriority", None, 3500), False, "missing result")
        assert_eq(charger_full_power("unknown", result, 3500), False, "unknown policy")
        ov, seen = until_unplug_step(True, False, True)
        assert_eq((ov, seen), (False, False), "clears on unplug after seen")

    def test_plan_result_is_a_window_list():
        out = plan(
            Clock(datetime.datetime.fromtimestamp(base, tz=timezone.utc)),
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
        assert_eq(out["windows"][0]["rank"], 1, "iso rank is the 1-based window index")

    def test_seed_tie_elapsed_gap_and_weighted_avg():
        tied = find_seed(ts_slots(base, [0.03] * 8 + [0.20] * 8 + [0.03] * 8), 2 * 3600, base - 3600)
        assert_eq(tied[1], base, "equal-avg seeds: earlier start")
        later = find_seed(ts_slots(base, [0.04] * 8 + [0.20] * 8 + [0.03] * 8), 2 * 3600, base - 3600)
        assert_eq(later[1], base + 16 * SLOT, "later valley wins when it is cheaper")
        now_past = base + 3 * 3600
        elapsed = find_seed(ts_slots(base, [0.01] * 8 + [0.08] * 16), 2 * 3600, now_past)
        assert_true(elapsed[1] >= now_past - SLOT, "elapsed cheap valley is not a seed")
        mid = find_seed(ts_slots(base, [0.01] * 8 + [0.08] * 8), 2 * 3600, base + SLOT + 450)
        assert_eq(mid[1], base + SLOT, "current quarter is included; the elapsed slot is not")
        gap_slots = ts_slots(base, [0.02] * 8) + ts_slots(base + 8 * SLOT + 3600, [0.01] * 8)
        gapped = find_seed(gap_slots, 2 * 3600, base - 3600)
        assert_eq(gapped[1], base + 8 * SLOT + 3600, "cannot seed across a gap")
        weighted = [
            [base, base + 3600, 0.01],
            [base + 3600, base + 7200, 0.10],
            [base + 7200, base + 14400, 0.06],
        ]
        seed = find_seed(weighted, 2 * 3600, base - 3600)
        assert_eq(seed[1], base, "duration-weighted 0.055 beats a later 0.06 flat")
        assert_eq(round(seed[0], 4), 0.055, "weighted avg of 1 h @ 0.01 and 1 h @ 0.10")

    def test_grow_sides_flex_modes_and_caps():
        right = pick_windows(
            ts_slots(base, [0.25] * 8 + [0.02] * 8 + [0.03] * 8),
            2 * 3600,
            5 * 3600,
            0.2,
            base - 3600,
            20,
            0.02,
        )
        assert_eq(right[0]["start"], base + 8 * SLOT, "left 0.25 is above ceiling")
        assert_true(right[0]["end"] > base + 16 * SLOT, "grew toward the cheaper 0.03 right")
        equal = pick_windows(
            ts_slots(base, [0.06] * 8 + [0.04] * 8 + [0.06] * 8),
            2 * 3600,
            3 * 3600,
            0.2,
            base - 3600,
            20,
            0.02,
        )
        assert_eq(equal[0]["start"], base + 4 * SLOT, "equal neighbors: extend earlier")
        assert_eq(round(dur_h(equal[0]), 2), 3.0, "stopped at max hours")
        now_mid = base + 8 * SLOT
        elapsed_left = pick_windows(
            ts_slots(base, [0.03] * 8 + [0.04] * 8 + [0.05] * 8),
            2 * 3600,
            5 * 3600,
            0.2,
            now_mid,
            20,
            0.02,
        )
        assert_eq(elapsed_left[0]["start"], now_mid, "elapsed left neighbor is not added")
        assert_true(elapsed_left[0]["end"] > now_mid + 8 * SLOT, "grows right instead")
        plateau = ts_slots(base, [0.10] * 8 + [0.11] * 8)
        pct_only = pick_windows(plateau, 2 * 3600, 5 * 3600, 0.2, base - 3600, 20, 0)
        euro_only = pick_windows(plateau, 2 * 3600, 5 * 3600, 0.2, base - 3600, 0, 0.02)
        unused = pick_windows(plateau, 2 * 3600, 5 * 3600, 0.2, base - 3600, 0, 0)
        assert_true(dur_h(pct_only[0]) > 2.0, "percent only still grows")
        assert_true(dur_h(euro_only[0]) > 2.0, "euro only still grows")
        assert_eq(round(dur_h(unused[0]), 2), 2.0, "flex 0 / 0 does not grow")
        zeros = ts_slots(base, [0.0] * 16)
        zero_pct = pick_windows(zeros, 2 * 3600, 5 * 3600, 0.2, base - 3600, 20, 0)
        zero_off = pick_windows(zeros, 2 * 3600, 5 * 3600, 0.2, base - 3600, 0, 0)
        assert_true(dur_h(zero_pct[0]) > 2.0, "zero seed + percent: avg stays 0 so neighbors are legal")
        assert_eq(round(dur_h(zero_off[0]), 2), 2.0, "zero seed + unused flex stays at min")
        capped = pick_windows(
            ts_slots(base, [0.04] * 32), 2 * 3600, 3 * 3600, 0.2, base - 3600, 50, 1
        )
        assert_eq(round(dur_h(capped[0]), 2), 3.0, "max hours is a cap, not a target")
        too_tight = pick_windows(
            ts_slots(base, [0.04] * 16), 2 * 3600, 2.1 * 3600, 0.2, base - 3600, 50, 1
        )
        assert_eq(round(dur_h(too_tight[0]), 2), 2.0, "next 15 min would exceed 2.1 h")
        one_more = pick_windows(
            ts_slots(base, [0.04] * 16), 2 * 3600, 2.25 * 3600, 0.2, base - 3600, 50, 1
        )
        assert_eq(round(dur_h(one_more[0]), 2), 2.25, "2.25 h max allows one native slot")

    def test_negative_zero_and_ceiling_edges():
        euro_neg = pick_windows(
            ts_slots(base, [-0.05] * 8 + [-0.04] * 8),
            2 * 3600,
            5 * 3600,
            0.2,
            base - 3600,
            0,
            0.02,
        )
        assert_true(dur_h(euro_neg[0]) > 2.0, "negative seed + euro flex grows")
        assert_true(euro_neg[0]["avg"] <= -0.03 + 1e-9, "allowed = seed + 0.02")
        hard_no = pick_windows(
            ts_slots(base, [-0.05] * 8 + [0.25] + [-0.04] * 8),
            2 * 3600,
            5 * 3600,
            0.2,
            base - 3600,
            0,
            0.02,
        )
        assert_eq(hard_no[0]["end"], base + 8 * SLOT, "0.25 > ceiling is a hard-no even if avg stays cheap")
        exact = pick_windows(
            ts_slots(base, [0.20] * 8), 2 * 3600, 5 * 3600, 0.2, base - 3600, 0, 0
        )
        assert_eq(len(exact), 1, "seed avg == ceiling is kept")
        spike_eq = pick_windows(
            ts_slots(base, [0.10] * 4 + [0.30] * 4), 2 * 3600, 5 * 3600, 0.2, base - 3600, 0, 0
        )
        assert_eq(len(spike_eq), 1, "spike inside seed ok when avg == ceiling")
        above = pick_windows(
            ts_slots(base, [0.21] * 8), 2 * 3600, 5 * 3600, 0.2, base - 3600, 20, 0.02
        )
        assert_eq(above, [], "seed avg above ceiling aborts; no runner-up hunt")
        at_ceil = pick_windows(
            ts_slots(base, [0.05] * 8 + [0.20] + [0.40] * 8),
            2 * 3600,
            5 * 3600,
            0.2,
            base - 3600,
            50,
            1,
        )
        assert_eq(at_ceil[0]["end"], base + 9 * SLOT, "neighbor priced at the ceiling may grow")
        assert_true(at_ceil[0]["end"] < base + 10 * SLOT + 1, "0.40 neighbor is still a hard-no")

    def test_hourly_and_half_hour_native_steps():
        hourly = ts_slots(base, [0.10] * 3 + [0.02] * 2 + [0.05] * 3, slot=3600)
        grown = pick_windows(hourly, 2 * 3600, 5 * 3600, 0.2, base - 3600, 20, 0.02)
        assert_eq(grown[0]["start"], base + 3 * 3600, "hourly seed is the 0.02 dip")
        assert_eq((grown[0]["end"] - grown[0]["start"]) % 3600, 0, "hourly grow is 1 h steps")
        assert_eq(round(dur_h(grown[0]), 2), 5.0, "grew three 1 h slots to the max")
        half = ts_slots(base, [0.04] * 4 + [0.05] * 6, slot=1800)
        half_w = pick_windows(half, 2 * 3600, 4 * 3600, 0.2, base - 3600, 50, 1)
        assert_eq((half_w[0]["end"] - half_w[0]["start"]) % 1800, 0, "30-min grow is native 30 min")
        assert_eq(round(dur_h(half_w[0]), 2), 4.0, "capped at 4 h on a 30-min curve")

    def test_blocked_splits_islands_and_dict_ranges():
        prices = [0.03] * 8 + [0.01] * 8 + [0.02] * 8
        blocked = [(base + 8 * SLOT, base + 16 * SLOT)]
        chosen = choose(
            ts_slots(base, prices),
            2.0,
            5.0,
            0.2,
            base - 3600,
            None,
            blocked=blocked,
            flex_pct=0,
            flex_euro=0,
        )
        assert_eq(len(chosen["windows"]), 1, "one island")
        assert_eq(chosen["windows"][0]["start"], base + 16 * SLOT, "cheaper later island, not the earlier 0.03")
        as_dict = choose(
            ts_slots(base, prices),
            2.0,
            5.0,
            0.2,
            base - 3600,
            None,
            blocked=[{"start": base + 8 * SLOT, "end": base + 16 * SLOT}, "bad", (1,)],
            flex_pct=0,
            flex_euro=0,
        )
        assert_eq(as_dict["windows"][0]["start"], base + 16 * SLOT, "dict ranges + junk rows still block the dip")

    def test_horizon_clip_combinations():
        today = slots_from(base, [0.001] * 16)
        tomorrow = slots_from(base + 86400, [0.08] * 16)
        attrs = {"raw_today": today, "raw_tomorrow": tomorrow}
        clock = Clock(datetime.datetime.fromtimestamp(base, tz=timezone.utc))
        tom_only = plan(
            clock, attrs, remaining_today=None, tomorrow_kwh=10.0, flex_pct=0, flex_euro=0
        )
        assert_true(
            tom_only["raw_windows"][0]["start"] >= base + 86400 - 1,
            "tomorrow-only solar drops today even when today is cheaper",
        )
        both_today = plan(
            clock,
            {
                "raw_today": slots_from(base, [0.01] * 16),
                "raw_tomorrow": slots_from(base + 86400, [0.08] * 16),
            },
            remaining_today=10.0,
            tomorrow_kwh=8.0,
            flex_pct=0,
            flex_euro=0,
        )
        assert_true(
            both_today["raw_windows"][0]["end"] <= base + 86400 + 1,
            "both forecasts: cheaper today wins",
        )
        both_tom = plan(
            clock,
            {
                "raw_today": slots_from(base, [0.08] * 16),
                "raw_tomorrow": slots_from(base + 86400, [0.01] * 16),
            },
            remaining_today=10.0,
            tomorrow_kwh=8.0,
            flex_pct=0,
            flex_euro=0,
        )
        assert_true(
            both_tom["raw_windows"][0]["start"] >= base + 86400 - 1,
            "both forecasts: cheaper tomorrow wins",
        )
        zero_today = plan(
            clock,
            attrs,
            remaining_today=0.0,
            tomorrow_kwh=None,
            flex_pct=0,
            flex_euro=0,
        )
        assert_true(
            zero_today["raw_windows"][0]["end"] <= base + 86400 + 1,
            "remaining_today=0 is present: keep today, ignore tomorrow",
        )
        no_tom_prices = plan(
            clock,
            {"raw_today": today},
            remaining_today=None,
            tomorrow_kwh=10.0,
            flex_pct=0,
            flex_euro=0,
        )
        assert_eq(no_tom_prices["count"], 0, "tomorrow kWh without tomorrow prices: nothing to search")
        assert_eq(no_tom_prices["reason"], "no_slots", "clipped empty is no_slots")

    def test_freeze_switch_and_replan_combinations():
        today = [0.04] * 16
        clock = Clock(datetime.datetime.fromtimestamp(base + 3600, tz=timezone.utc))
        first = plan(clock, {"raw_today": slots_from(base, today)}, flex_pct=0, flex_euro=0)
        prev = planner.prev_from_result(clock, first)
        expensive_tom = {
            "raw_today": slots_from(base, today),
            "raw_tomorrow": slots_from(base + 86400, [0.10] * 16),
        }
        held = plan(clock, expensive_tom, prev=prev, flex_pct=0, flex_euro=0)
        assert_eq(held["reason"], "frozen", "horizon grew but the new set is not cheaper")
        assert_eq(held["raw_windows"][0]["start"], first["raw_windows"][0]["start"], "keep the started set")
        started = plan(
            clock, {"raw_today": slots_from(base, [0.09] * 16)}, flex_pct=0, flex_euro=0
        )
        assert_true(
            started["raw_windows"][0]["start"] <= base + 3600 <= started["raw_windows"][0]["end"],
            "window already covers now",
        )
        switched = plan(
            clock,
            {
                "raw_today": slots_from(base, [0.09] * 16),
                "raw_tomorrow": slots_from(base + 86400, [0.01] * 16),
            },
            prev=planner.prev_from_result(clock, started),
            flex_pct=0,
            flex_euro=0,
        )
        assert_eq(switched["reason"], "switched", "started window can still be replaced")
        slots = ts_slots(base, [0.04] * 16)
        planned = pick_windows(slots, 2 * 3600, 5 * 3600, 0.2, base - 3600, 0, 0)
        prev_w = {
            "windows": planned,
            "min_hours": 2.0,
            "max_hours": 5.0,
            "ceiling": 0.2,
            "flex_pct": 0,
            "flex_euro": 0,
            "horizon": slots[-1][1],
            "blocked": [],
        }
        longer = choose(slots, 3.0, 5.0, 0.2, base, prev_w, flex_pct=0, flex_euro=0)
        assert_eq(longer["reason"], "planned", "min hours change replans")
        assert_true(dur_h(longer["windows"][0]) >= 3.0 - 0.01, "new min is 3 h")
        shorter_max = choose(slots, 2.0, 2.0, 0.2, base, prev_w, flex_pct=0, flex_euro=0)
        assert_eq(shorter_max["reason"], "planned", "max hours change replans")
        assert_eq(round(dur_h(shorter_max["windows"][0]), 2), 2.0, "fixed 2 h after max shrinks")

    def test_after_window_later_island_and_new_horizon():
        early = [0.02] * 8
        gap = [0.20] * 16
        late = [0.04] * 8
        slots = ts_slots(base, early + gap + late)
        first = pick_windows(slots, 2 * 3600, 5 * 3600, 0.2, base - 3600, 0, 0)
        assert_eq(first[0]["start"], base, "first night is cheaper")
        prev = {
            "windows": first,
            "min_hours": 2.0,
            "max_hours": 5.0,
            "ceiling": 0.2,
            "flex_pct": 0,
            "flex_euro": 0,
            "horizon": slots[-1][1],
            "blocked": [],
        }
        after = first[0]["end"] + 60
        next_island = choose(slots, 2.0, 5.0, 0.2, after, prev, flex_pct=0, flex_euro=0)
        assert_eq(next_island["reason"], "planned", "same horizon: later island after the first ends")
        assert_eq(next_island["windows"][0]["start"], base + 24 * SLOT, "second island")
        today_only = ts_slots(base, [0.04] * 8)
        done = pick_windows(today_only, 2 * 3600, 5 * 3600, 0.2, base - 3600, 0, 0)
        prev_done = {
            "windows": done,
            "min_hours": 2.0,
            "max_hours": 5.0,
            "ceiling": 0.2,
            "flex_pct": 0,
            "flex_euro": 0,
            "horizon": today_only[-1][1],
            "blocked": [],
        }
        grown = today_only + ts_slots(base + 86400, [0.03] * 8)
        new_h = choose(
            grown, 2.0, 5.0, 0.2, done[0]["end"] + 60, prev_done, flex_pct=0, flex_euro=0
        )
        assert_eq(new_h["reason"], "planned_new_horizon", "ended window + cheaper new day")
        assert_true(new_h["windows"][0]["start"] >= base + 86400 - 1, "new window is tomorrow")
        dear = today_only + ts_slots(base + 86400, [0.40] * 8)
        empty_h = choose(
            dear, 2.0, 5.0, 0.2, done[0]["end"] + 60, prev_done, flex_pct=0, flex_euro=0
        )
        assert_eq(empty_h["reason"], "no_window", "ended window + new horizon with no legal seed")
        assert_eq(empty_h["windows"][0]["start"], done[0]["start"], "keeps the old set")
        empty = choose([], 2.0, 5.0, 0.2, base, None, flex_pct=0, flex_euro=0)
        assert_eq(empty["reason"], "no_slots", "no price slots")

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
    case("seed_tie_elapsed_gap_and_weighted_avg", test_seed_tie_elapsed_gap_and_weighted_avg)
    case("grow_sides_flex_modes_and_caps", test_grow_sides_flex_modes_and_caps)
    case("negative_zero_and_ceiling_edges", test_negative_zero_and_ceiling_edges)
    case("hourly_and_half_hour_native_steps", test_hourly_and_half_hour_native_steps)
    case("blocked_splits_islands_and_dict_ranges", test_blocked_splits_islands_and_dict_ranges)
    case("horizon_clip_combinations", test_horizon_clip_combinations)
    case("freeze_switch_and_replan_combinations", test_freeze_switch_and_replan_combinations)
    case("after_window_later_island_and_new_horizon", test_after_window_later_island_and_new_horizon)
    run()


if __name__ == "__main__":
    main()
