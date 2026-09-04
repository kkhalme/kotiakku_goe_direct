"""Tests for the charge-window planner (no Home Assistant)."""

from __future__ import annotations

import datetime
from datetime import timezone

from harness import Clock, SLOT, assert_eq, assert_true, case_runner, load_mod, slots_from

planner = load_mod("planner")
choose = planner.choose
clamp_hours = planner.clamp_hours
norm_rank = planner.norm_rank
pick_all = planner.pick_all
plan = planner.plan
now_in_windows = planner.now_in_windows


def run_plan(now, source_attrs, data=None, prev=None):
    payload = {"min_hours": 2, "max_hours": 5, "ceiling": 0.1, "rank": "cheapest"}
    if data:
        payload.update(data)
    return plan(
        Clock(now),
        source_attrs,
        min_hours=payload["min_hours"],
        max_hours=payload["max_hours"],
        ceiling=payload["ceiling"],
        rank=payload.get("rank", "cheapest"),
        prev=prev,
        source_entity="sensor.nordpool_kwh_fi",
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

    def test_split_on_ceiling():
        prices = [0.04] * 12 + [0.25] + [0.05] * 12
        windows = pick_all(ts_slots(base, prices), 2 * 3600, 5 * 3600, 0.1, base - 3600)
        assert_eq(len(windows), 2, "two windows around spike")
        assert_eq(round(dur_h(windows[0]), 2), 3.0, "first duration")
        assert_eq(round(dur_h(windows[1]), 2), 3.0, "second duration")
        assert_true(windows[0]["avg"] < windows[1]["avg"], "cheapest first")
        assert_eq(windows[0]["start"], base, "cheap 0.04 block first")
        spike_start = base + 12 * SLOT
        for w in windows:
            assert_true(not (w["start"] <= spike_start < w["end"]), "spike excluded")

    def test_exact_ceiling_allowed():
        windows = pick_all(ts_slots(base, [0.1] * 8), 2 * 3600, 5 * 3600, 0.1, base - 3600)
        assert_eq(len(windows), 1, "exactly 0.1 is allowed")
        assert_eq(round(dur_h(windows[0]), 2), 2.0, "2h at cap")

    def test_uniform_6h_picks_max():
        windows = pick_all(ts_slots(base, [0.05] * 24), 2 * 3600, 5 * 3600, 0.1, base - 3600)
        assert_eq(len(windows), 1, "leftover 1h < min")
        assert_eq(round(dur_h(windows[0]), 2), 5.0, "max length at same avg")
        assert_eq(windows[0]["start"], base, "earliest 5h")

    def test_uniform_8h_then_leftover():
        windows = pick_all(ts_slots(base, [0.05] * 32), 2 * 3600, 5 * 3600, 0.1, base - 3600)
        assert_eq(len(windows), 2, "5h + 3h leftover")
        assert_eq(round(dur_h(windows[0]), 2), 5.0, "first is max")
        assert_eq(round(dur_h(windows[1]), 2), 3.0, "leftover 3h")
        assert_eq(windows[0]["end"], windows[1]["start"], "contiguous leftover")

    def test_dip_taken_first_flanks_kept():
        prices = [0.09] * 8 + [0.01] * 8 + [0.09] * 8
        windows = pick_all(ts_slots(base, prices), 2 * 3600, 5 * 3600, 0.1, base - 3600)
        assert_eq(len(windows), 3, "dip plus two flanks")
        assert_true(abs(windows[0]["avg"] - 0.01) < 1e-9, "dip cheapest")
        assert_eq(windows[0]["start"], base + 8 * SLOT, "dip start")
        assert_eq(windows[1]["start"], base, "left flank second")
        assert_eq(windows[2]["start"], base + 16 * SLOT, "right flank third")

    def test_short_region_skipped():
        prices = [0.20] * 4 + [0.05] * 7 + [0.20] * 4
        windows = pick_all(ts_slots(base, prices), 2 * 3600, 5 * 3600, 0.1, base - 3600)
        assert_eq(len(windows), 0, "1.75h < min 2h")

    def test_min_gt_max_swapped():
        mn, mx = clamp_hours(5, 2)
        assert_eq(mn, 2.0, "swapped min")
        assert_eq(mx, 5.0, "swapped max")
        now_dt = datetime.datetime.fromtimestamp(base - 3600, tz=timezone.utc)
        out = run_plan(now_dt, {"raw_today": slots_from(base, [0.04] * 16)}, data={"min_hours": 5, "max_hours": 2})
        assert_eq(out["min_hours"], 2.0, "swapped min")
        assert_eq(out["max_hours"], 5.0, "swapped max")
        assert_eq(out["count"], 1, "one window")
        assert_eq(out["reason"], "planned", "planned")

    def test_all_above_ceiling():
        windows = pick_all(ts_slots(base, [0.2] * 16), 2 * 3600, 5 * 3600, 0.1, base - 3600)
        assert_eq(len(windows), 0, "nothing under cap")

    def test_join_current_slot():
        windows = pick_all(ts_slots(base, [0.04] * 16), 2 * 3600, 5 * 3600, 0.1, base + 600)
        assert_eq(len(windows), 1, "can start this quarter")
        assert_eq(windows[0]["start"], base, "includes current slot")

    def test_freeze_does_not_slide():
        prices = [0.08] * 20 + [0.03] * 16
        slots = ts_slots(base, prices)
        first = pick_all(slots, 2 * 3600, 5 * 3600, 0.1, base)
        prev = {
            "windows": first,
            "min_hours": 2.0,
            "max_hours": 5.0,
            "ceiling": 0.1,
            "horizon": slots[-1][1],
        }
        chosen = choose(slots, 2.0, 5.0, 0.1, base + 900, prev)
        assert_eq(chosen["reason"], "frozen", "frozen after 15 min")
        assert_eq(chosen["windows"][0]["start"], first[0]["start"], "start did not slide")

    def test_switch_when_tomorrow_cheaper():
        today = ts_slots(base, [0.09] * 16)
        tomorrow_start = base + 24 * 3600
        tomorrow = ts_slots(tomorrow_start, [0.02] * 16)
        now_ts = base + 3600
        prev_windows = pick_all(today, 2 * 3600, 5 * 3600, 0.1, now_ts)
        prev = {
            "windows": prev_windows,
            "min_hours": 2.0,
            "max_hours": 5.0,
            "ceiling": 0.1,
            "horizon": today[-1][1],
        }
        chosen = choose(today + tomorrow, 2.0, 5.0, 0.1, now_ts, prev)
        assert_eq(chosen["reason"], "switched", "cheaper tomorrow set")
        assert_true(chosen["windows"][0]["avg"] < prev_windows[0]["avg"], "new first is cheaper")

    def test_min_drops_short_bottoms():
        # Isolated 15-minute dip is under the cap but shorter than min.
        prices = [0.2] * 4 + [0.01] + [0.2] * 4
        windows = pick_all(
            ts_slots(base, prices), 1 * 3600, 5 * 3600, 0.1, base - 3600
        )
        assert_eq(len(windows), 0, "15 min bottom is not a session when min is 1 h")
        assert_true(
            not now_in_windows(windows, base + 4 * SLOT),
            "a simple ceiling would have taken 0.01",
        )

    def test_one_hour_min_joins_short_pauses():
        prices = [0.02, 0.05, 0.05, 0.03, 0.03, 0.15, 0.04, 0.04, 0.04, 0.04]
        slots = ts_slots(base, prices)
        for rank in ("cheapest", "offsun"):
            windows = pick_all(slots, 1 * 3600, 5 * 3600, 0.1, base - 3600, rank)
            for w in windows:
                assert_true(dur_h(w) + 0.01 >= 1.0, "%s session ≥ 1 h" % rank)
            assert_true(now_in_windows(windows, base), "%s includes the 0.02 bottom" % rank)
            assert_true(
                now_in_windows(windows, base + SLOT),
                "%s does not pause 15–30 min in the first bottom" % rank,
            )
            assert_true(
                not now_in_windows(windows, base + 5 * SLOT),
                "%s keeps the over-ceiling spike" % rank,
            )
            assert_true(
                now_in_windows(windows, base + 6 * SLOT),
                "%s second bottom after the spike" % rank,
            )

    def test_adjacent_bands_stay_split():
        prices = [0.09] * 8 + [0.01] * 8 + [0.09] * 8
        windows = pick_all(ts_slots(base, prices), 2 * 3600, 5 * 3600, 0.1, base - 3600)
        assert_eq(len(windows), 3, "touching bands are not merged")
        assert_eq(windows[0]["start"], base + 8 * SLOT, "dip still first")

    def test_over_ceiling_pause_not_joined():
        prices = [0.02] * 4 + [0.2] * 4 + [0.03] * 4
        windows = pick_all(
            ts_slots(base, prices), 1 * 3600, 5 * 3600, 0.1, base - 3600
        )
        ordered = sorted(windows, key=lambda w: w["start"])
        assert_eq(len(ordered), 2, "spike keeps two bottoms")
        assert_true(
            not now_in_windows(windows, base + 4 * SLOT),
            "over-ceiling hour stays off",
        )

    def test_join_respects_max_hours():
        prices = [0.04] * 12 + [0.06] + [0.04] * 12
        windows = pick_all(
            ts_slots(base, prices), 1 * 3600, 5 * 3600, 0.1, base - 3600
        )
        for w in windows:
            assert_true(dur_h(w) <= 5.0 + 0.01, "each window still ≤ max")
        assert_true(len(windows) >= 2, "6.25 h span does not become one window")

    def test_frozen_short_pause_is_joined():
        prices = [0.02] * 4 + [0.05] + [0.03] * 4 + [0.15] + [0.04] * 4
        slots = ts_slots(base, prices)
        cheesy = [
            {"avg": 0.02, "start": base, "end": base + 4 * SLOT},
            {"avg": 0.03, "start": base + 5 * SLOT, "end": base + 9 * SLOT},
        ]
        prev = {
            "windows": cheesy,
            "min_hours": 1.0,
            "max_hours": 5.0,
            "ceiling": 0.1,
            "horizon": slots[-1][1],
            "rank": "offsun",
        }
        chosen = choose(slots, 1.0, 5.0, 0.1, base + SLOT, prev, "offsun")
        assert_eq(chosen["reason"], "planned", "join is a replan, not a slide")
        assert_eq(len(chosen["windows"]), 1, "15 min pause joined")
        assert_eq(chosen["windows"][0]["start"], base, "start of the first bottom held")
        assert_eq(chosen["windows"][0]["end"], base + 9 * SLOT, "run now 2:15 h")

    def test_offsun_does_not_fill_blocked_hour():
        prices = [0.04] * 8 + [0.04] * 4 + [0.04] * 8
        slots = ts_slots(base, prices)
        blocked = [(base + 8 * SLOT, base + 12 * SLOT)]
        chosen = choose(slots, 2.0, 5.0, 0.1, base - 3600, None, "offsun", blocked)
        assert_eq(len(chosen["windows"]), 2, "dropped surplus hour stays a gap")
        assert_eq(chosen["windows"][0]["end"], base + 8 * SLOT, "first window stops at block")
        assert_eq(chosen["windows"][1]["start"], base + 12 * SLOT, "second window after block")

    def test_nordpool_fi_2026_09_03_night():
        # FI day-ahead 15-min, EUR/MWh / 1000, 2026-09-03 21:45Z–05:45Z.
        # Min 1 h: take bottoms, join pauses shorter than 1 h, do not charge
        # every quarter under 0.1 (that would be a simple ceiling).
        prices = [
            0.01999, 0.03891, 0.03487, 0.03168, 0.02920, 0.03982, 0.03554, 0.03020,
            0.02783, 0.03842, 0.03785, 0.03270, 0.02800, 0.03042, 0.03563, 0.03299,
            0.03468, 0.03304, 0.03277, 0.02960, 0.03803, 0.01622, 0.02492, 0.04428,
            0.07148, 0.02800, 0.04044, 0.07237, 0.08670, 0.07874, 0.09451, 0.10181,
            0.11292,
        ]
        slots = ts_slots(base, prices)
        windows = pick_all(slots, 1 * 3600, 5 * 3600, 0.1, base - 3600, "offsun")
        assert_true(windows, "at least one bottom")
        for w in windows:
            assert_true(dur_h(w) + 0.01 >= 1.0, "no session shorter than 1 h")
            assert_true(dur_h(w) <= 5.0 + 0.01, "no session longer than max")
        assert_true(now_in_windows(windows, base), "includes the 19.99 €/MWh bottom")
        assert_true(
            not now_in_windows(windows, base + 31 * SLOT),
            "still off at 101.81 €/MWh",
        )
        assert_true(len(windows) >= 1, "at least one bottom")
        # Contiguous 7.75 h under the cap becomes 5 h + leftover, not 15-min islands.

    def test_idle_after_window_same_horizon():
        slots = ts_slots(base, [0.04] * 16)
        planned = pick_all(slots, 2 * 3600, 5 * 3600, 0.1, base - 3600)
        prev = {
            "windows": planned,
            "min_hours": 2.0,
            "max_hours": 5.0,
            "ceiling": 0.1,
            "horizon": slots[-1][1],
        }
        chosen = choose(slots, 2.0, 5.0, 0.1, planned[0]["end"] + 60, prev)
        assert_eq(chosen["reason"], "idle_after_window", "no extra from same horizon")

    def test_replan_when_ceiling_changes():
        slots = ts_slots(base, [0.08] * 16)
        planned = pick_all(slots, 2 * 3600, 5 * 3600, 0.1, base - 3600)
        prev = {
            "windows": planned,
            "min_hours": 2.0,
            "max_hours": 5.0,
            "ceiling": 0.1,
            "horizon": slots[-1][1],
        }
        chosen = choose(slots, 2.0, 5.0, 0.05, base, prev)
        assert_eq(chosen["reason"], "no_window", "stricter ceiling replans empty")

    def test_end_to_end_script():
        prices = [0.04] * 8 + [0.2] + [0.06] * 16
        now_dt = datetime.datetime.fromtimestamp(base - 1800, tz=timezone.utc)
        out = run_plan(now_dt, {"raw_today": slots_from(base, prices), "tomorrow_valid": False})
        assert_eq(out["reason"], "planned", "script planned")
        assert_eq(out["count"], 2, "two windows")
        assert_eq(out["min_hours"], 2.0, "min")
        assert_eq(out["max_hours"], 5.0, "max")
        assert_eq(out["ceiling"], 0.1, "ceiling")
        assert_true(out["window_1_start"] is not None, "window 1")
        assert_true(out["window_2_start"] is not None, "window 2")
        assert_true(out["window_3_start"] is None, "no third")
        assert_true(out["avg"] is not None, "active/next avg")

    def test_no_source():
        out = plan(Clock(now), None)
        assert_eq(out["reason"], "no_source", "missing entity")

    def test_script_freeze_no_slide():
        prices = [0.05] * 32
        now0 = datetime.datetime.fromtimestamp(base, tz=timezone.utc)
        first = run_plan(now0, {"raw_today": slots_from(base, prices)})
        assert_eq(first["reason"], "planned", "first plan")
        now1 = datetime.datetime.fromtimestamp(base + SLOT, tz=timezone.utc)
        second = run_plan(
            now1,
            {"raw_today": slots_from(base, prices)},
            prev={
                "windows": first["raw_windows"],
                "min_hours": first["min_hours"],
                "max_hours": first["max_hours"],
                "ceiling": first["ceiling"],
                "horizon": first["horizon_ts"],
                "rank": "cheapest",
            },
        )
        assert_eq(second["reason"], "frozen", "script frozen")
        assert_eq(second["window_1_start"], first["window_1_start"], "no slide")
        assert_eq(second["window_2_start"], first["window_2_start"], "second frozen")

    def test_string_params_from_templates():
        now_dt = datetime.datetime.fromtimestamp(base - 3600, tz=timezone.utc)
        out = run_plan(
            now_dt,
            {"raw_today": slots_from(base, [0.04] * 16)},
            data={"min_hours": "2", "max_hours": "5", "ceiling": "0.1"},
        )
        assert_eq(out["count"], 1, "string params parse")
        assert_eq(out["min_hours"], 2.0, "min from string")
        assert_eq(out["ceiling"], 0.1, "ceiling from string")

    def test_increasing_prices_prefer_min():
        prices = [0.01] * 8 + [0.05] * 8 + [0.09] * 8
        windows = pick_all(ts_slots(base, prices), 2 * 3600, 5 * 3600, 0.1, base - 3600)
        assert_eq(len(windows), 3, "three 2h bands")
        assert_true(abs(windows[0]["avg"] - 0.01) < 1e-9, "cheapest 2h first")
        assert_eq(round(dur_h(windows[0]), 2), 2.0, "min length on rising curve")
        assert_eq(windows[0]["start"], base, "earliest cheap band")

    def test_flanks_shorter_than_min_dropped():
        prices = [0.09] * 6 + [0.01] * 8 + [0.09] * 6
        windows = pick_all(ts_slots(base, prices), 2 * 3600, 5 * 3600, 0.1, base - 3600)
        assert_eq(len(windows), 1, "1.5h flanks dropped")
        assert_eq(windows[0]["start"], base + 6 * SLOT, "only the dip")

    def test_longest_vs_cheapest():
        prices = [0.01] * 8 + [0.08] * 20
        slots = ts_slots(base, prices)
        cheap_w = pick_all(slots, 2 * 3600, 5 * 3600, 0.1, base - 3600, "cheapest")
        long_w = pick_all(slots, 2 * 3600, 5 * 3600, 0.1, base - 3600, "longest")
        assert_eq(round(dur_h(cheap_w[0]), 2), 2.0, "cheapest takes the 2h dip")
        assert_true(abs(cheap_w[0]["avg"] - 0.01) < 1e-9, "dip avg")
        assert_eq(round(dur_h(long_w[0]), 2), 5.0, "longest takes 5h")
        assert_eq(long_w[0]["start"], base, "longest 5h from start of run")

    def test_earliest_vs_cheapest():
        prices = [0.09] * 8 + [0.01] * 8
        slots = ts_slots(base, prices)
        cheap_w = pick_all(slots, 2 * 3600, 5 * 3600, 0.1, base - 3600, "cheapest")
        early_w = pick_all(slots, 2 * 3600, 5 * 3600, 0.1, base - 3600, "earliest")
        assert_eq(cheap_w[0]["start"], base + 8 * SLOT, "cheapest is the 0.01 dip")
        assert_eq(early_w[0]["start"], base, "earliest starts now")
        assert_eq(round(dur_h(early_w[0]), 2), 4.0, "earliest fills from the start")

    def test_rank_change_replans():
        prices = [0.01] * 8 + [0.08] * 20
        slots = ts_slots(base, prices)
        cheap_w = pick_all(slots, 2 * 3600, 5 * 3600, 0.1, base - 3600, "cheapest")
        prev = {
            "windows": cheap_w,
            "min_hours": 2.0,
            "max_hours": 5.0,
            "ceiling": 0.1,
            "rank": "cheapest",
            "horizon": slots[-1][1],
        }
        chosen = choose(slots, 2.0, 5.0, 0.1, base, prev, "longest")
        assert_eq(chosen["reason"], "planned", "rank change is not frozen")
        assert_eq(round(dur_h(chosen["windows"][0]), 2), 5.0, "replanned longest")

    def test_force_policy_ranks_as_cheapest():
        assert_eq(norm_rank("Force on"), "cheapest", "force on")
        assert_eq(norm_rank("Force off"), "cheapest", "force off")
        assert_eq(norm_rank("Force on until unplug"), "cheapest", "legacy until unplug")
        assert_eq(norm_rank("Supercheap"), "cheapest", "Supercheap is a policy, not a rank")
        assert_eq(norm_rank("offsun"), "offsun", "offsun")
        assert_eq(norm_rank("off-sun"), "offsun", "off-sun")
        assert_eq(norm_rank("off sun"), "offsun", "off sun")
        assert_eq(norm_rank("Longest"), "longest", "longest")
        assert_eq(norm_rank("Earliest"), "earliest", "earliest")

    def test_until_unplug_overrides_policy():
        step = planner.until_unplug_step
        full = planner.charger_full_power
        results = {"cheapest": {"raw_windows": [{"start": 1000, "end": 2000}]}}
        assert_eq(full("Force off", results, 1500), False, "force off")
        assert_eq(
            full("Force off", results, 1500, until_unplug=True),
            True,
            "until unplug overrides Force off",
        )
        assert_eq(
            full("Cheapest", results, 1500, until_unplug=True),
            True,
            "until unplug overrides Cheapest",
        )
        assert_eq(
            full("Supercheap", results, 1500, enough_solar=True, until_unplug=True),
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

    def test_end_to_end_longest_rank():
        prices = [0.01] * 8 + [0.08] * 20
        now_dt = datetime.datetime.fromtimestamp(base - 1800, tz=timezone.utc)
        out = run_plan(now_dt, {"raw_today": slots_from(base, prices)}, data={"rank": "Longest"})
        assert_eq(out["rank"], "longest", "rank stored")
        start = datetime.datetime.fromisoformat(out["window_1_start"]).timestamp()
        end = datetime.datetime.fromisoformat(out["window_1_end"]).timestamp()
        assert_eq(round((end - start) / 3600.0, 2), 5.0, "script longest 5h")

    def test_same_horizon_falling_longest_stays_frozen():
        prices = [0.09] * 8 + [0.04] * 24
        slots = ts_slots(base, prices)
        first = pick_all(slots, 2 * 3600, 5 * 3600, 0.1, base, "longest")
        prev = {
            "windows": first,
            "min_hours": 2.0,
            "max_hours": 5.0,
            "ceiling": 0.1,
            "rank": "longest",
            "horizon": slots[-1][1],
        }
        chosen = choose(slots, 2.0, 5.0, 0.1, base + SLOT, prev, "longest")
        assert_eq(chosen["reason"], "frozen", "slide is not a switch")
        assert_eq(chosen["windows"][0]["start"], first[0]["start"], "longest start held")

    case("split_on_ceiling", test_split_on_ceiling)
    case("exact_ceiling_allowed", test_exact_ceiling_allowed)
    case("uniform_6h_picks_max", test_uniform_6h_picks_max)
    case("uniform_8h_then_leftover", test_uniform_8h_then_leftover)
    case("dip_taken_first_flanks_kept", test_dip_taken_first_flanks_kept)
    case("short_region_skipped", test_short_region_skipped)
    case("min_gt_max_swapped", test_min_gt_max_swapped)
    case("all_above_ceiling", test_all_above_ceiling)
    case("join_current_slot", test_join_current_slot)
    case("freeze_does_not_slide", test_freeze_does_not_slide)
    case("switch_when_tomorrow_cheaper", test_switch_when_tomorrow_cheaper)
    case("min_drops_short_bottoms", test_min_drops_short_bottoms)
    case("one_hour_min_joins_short_pauses", test_one_hour_min_joins_short_pauses)
    case("adjacent_bands_stay_split", test_adjacent_bands_stay_split)
    case("over_ceiling_pause_not_joined", test_over_ceiling_pause_not_joined)
    case("join_respects_max_hours", test_join_respects_max_hours)
    case("frozen_short_pause_is_joined", test_frozen_short_pause_is_joined)
    case("offsun_does_not_fill_blocked_hour", test_offsun_does_not_fill_blocked_hour)
    case("nordpool_fi_2026_09_03_night", test_nordpool_fi_2026_09_03_night)
    case("idle_after_window_same_horizon", test_idle_after_window_same_horizon)
    case("replan_when_ceiling_changes", test_replan_when_ceiling_changes)
    case("end_to_end_script", test_end_to_end_script)
    case("no_source", test_no_source)
    case("script_freeze_no_slide", test_script_freeze_no_slide)
    case("string_params_from_templates", test_string_params_from_templates)
    case("increasing_prices_prefer_min", test_increasing_prices_prefer_min)
    case("flanks_shorter_than_min_dropped", test_flanks_shorter_than_min_dropped)
    case("longest_vs_cheapest", test_longest_vs_cheapest)
    case("earliest_vs_cheapest", test_earliest_vs_cheapest)
    case("rank_change_replans", test_rank_change_replans)
    case("force_policy_ranks_as_cheapest", test_force_policy_ranks_as_cheapest)
    case("until_unplug_overrides_policy", test_until_unplug_overrides_policy)
    case("end_to_end_longest_rank", test_end_to_end_longest_rank)
    case("same_horizon_falling_longest_stays_frozen", test_same_horizon_falling_longest_stays_frozen)

    surplus = load_mod("surplus")
    leftover_w = surplus.leftover_w
    budget = surplus.budget
    car_plugged = surplus.car_plugged

    def test_supercheap_forecast():
        energy_kwh = surplus.energy_kwh
        upcoming = surplus.upcoming_solar_kwh
        enough = surplus.enough_solar
        full = planner.charger_full_power
        assert_eq(energy_kwh("unknown"), None, "unknown forecast")
        assert_eq(energy_kwh("50", "kWh"), 50.0, "kWh")
        assert_eq(energy_kwh("50000", "Wh"), 50.0, "Wh to kWh")
        assert_eq(energy_kwh("5000", "W"), None, "power is not energy")
        assert_eq(upcoming(None, None), None, "no sensors")
        assert_eq(upcoming(8.0, 6.0), 8.0, "max remaining vs tomorrow")
        assert_eq(upcoming(None, 50.0), 50.0, "tomorrow only")
        assert_eq(upcoming(0.0, 50.0), 50.0, "evening remaining 0, use tomorrow")
        assert_eq(enough(None, 40), False, "unknown is not enough")
        assert_eq(enough(8.0, 40), False, "winter 8 kWh is not enough")
        assert_eq(enough(40.0, 40), True, "at the 40 kWh threshold")
        assert_eq(enough(50.0, 40), True, "50 kWh is enough")
        assert_eq(enough(50.0, 0), False, "threshold 0 is never enough")
        results = {
            "cheapest": {"raw_windows": [{"start": 1000, "end": 2000}]},
            "offsun": {"raw_windows": [{"start": 3000, "end": 4000}]},
        }
        assert_eq(full("Cheapest", results, 1500), True, "Cheapest uses cheapest windows")
        assert_eq(full("Supercheap", results, 1500), False, "Supercheap ignores cheapest")
        assert_eq(full("Supercheap", results, 3500), True, "Supercheap uses offsun")
        assert_eq(
            full("Supercheap", results, 3500, enough_solar=True),
            False,
            "enough solar skips Supercheap even in offsun",
        )
        assert_eq(full("Cheapest", results, 3500, enough_solar=True), False, "Cheapest ignores offsun")
        assert_eq(full("Cheapest", results, 1500, enough_solar=True), True, "Cheapest ignores enough solar")
        assert_eq(full("Force off", results, 1500), False, "force off")

    def test_offsun_drops_surplus_hours():
        night = [0.05] * 32
        midday = [0.01] * 32
        rest = [0.2] * 32
        attrs = {"raw_today": slots_from(base, night + midday + rest)}
        midday_block = [(base + 8 * 3600, base + 16 * 3600)]
        clock0 = Clock(datetime.datetime.fromtimestamp(base, tz=timezone.utc))
        cheap = plan(clock0, attrs, rank="cheapest", blocked=midday_block)
        off = plan(clock0, attrs, rank="offsun", blocked=midday_block)
        assert_eq(cheap["rank"], "cheapest", "cheapest rank")
        assert_eq(off["rank"], "offsun", "offsun rank")
        assert_eq(cheap["raw_windows"][0]["start"], base + 8 * 3600, "cheapest is midday")
        assert_eq(off["raw_windows"][0]["start"], base, "offsun is the night")
        assert_eq(cheap["blocked"], [], "cheapest ignores blocked")
        assert_eq(len(off["blocked"]), 1, "offsun stores surplus hours")
        full = planner.charger_full_power
        results = {"cheapest": cheap, "offsun": off}
        assert_eq(full("Cheapest", results, base + 10 * 3600), True, "Cheapest midday")
        assert_eq(full("Supercheap", results, base + 10 * 3600), False, "Supercheap skips midday")
        assert_eq(full("Supercheap", results, base + 3600), True, "Supercheap night")
        none = plan(clock0, attrs, rank="offsun")
        assert_eq(
            none["raw_windows"][0]["start"],
            cheap["raw_windows"][0]["start"],
            "unknown surplus hours: offsun matches cheapest",
        )
        prev = planner.prev_from_result(clock0, off)
        frozen = plan(
            clock0, attrs, rank="offsun", prev=prev, blocked=midday_block
        )
        assert_eq(frozen["reason"], "frozen", "offsun freeze")
        replanned = plan(clock0, attrs, rank="offsun", prev=prev, blocked=[])
        assert_eq(replanned["reason"], "planned", "blocked change replans")
        assert_eq(
            replanned["raw_windows"][0]["start"],
            cheap["raw_windows"][0]["start"],
            "without surplus hours offsun becomes cheapest",
        )

    def test_surplus_hour_ranges():
        from zoneinfo import ZoneInfo

        hel = ZoneInfo("Europe/Helsinki")
        clock = Clock(datetime.datetime(2026, 3, 15, 12, 0, tzinfo=hel), tz=hel)
        hours = surplus.expected_hour_kwh(
            clock,
            clock.now(),
            clock.start_of_local_day(clock.now()) + datetime.timedelta(days=1),
            50.0,
            60.17,
            24.94,
        )
        by_hour = {
            datetime.datetime.fromtimestamp(start, tz=hel).hour: kwh
            for start, _end, kwh in hours
        }
        assert_true(by_hour.get(12, 0) >= 1.0, "noon 50 kWh remaining is ≥ 1 kWh")
        assert_true(by_hour.get(23, 1) < 1.0, "night hour is under 1 kWh")
        ranges = surplus.surplus_hour_ranges(clock, 50.0, None, 1.0, 60.17, 24.94)
        assert_true(len(ranges) >= 1, "50 kWh remaining excludes productive hours")
        noon = datetime.datetime(2026, 3, 15, 12, 0, tzinfo=hel).timestamp()
        assert_true(
            any(start <= noon < end for start, end in ranges),
            "noon is excluded from Off-sun",
        )
        night = datetime.datetime(2026, 3, 15, 23, 0, tzinfo=hel).timestamp()
        assert_true(
            not any(start <= night < end for start, end in ranges),
            "23:00 is off-sun",
        )
        dawn_only = surplus.surplus_hour_ranges(clock, 4.0, None, 1.0, 60.17, 24.94)
        assert_true(
            not any(start <= night < end for start, end in dawn_only),
            "4 kWh remaining does not exclude night",
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
        leftover = setp(10, 1, 10, n_full=0, eco_lot=50)
        assert_eq(leftover, (10, 1, 10), "pure surplus uses leftover lot")
        assert_eq(
            setp(10, 1, 10, n_full=1, eco_lot=50),
            (50, 1, 10),
            "mixed: keep group lot 50, leftover amp",
        )
        assert_eq(
            setp(25, 2, 25, n_full=1, eco_lot=50),
            (50, 2, 25),
            "mixed: do not cap leftover amp to reserve 32 A",
        )
        assert_eq(
            setp(6, 1, 6, n_full=1, eco_lot=50),
            (50, 1, 6),
            "mixed: 6 A leftover amp beside full-power",
        )
        assert_eq(
            setp(10, 1, 10, n_full=1, eco_lot=32),
            (32, 1, 10),
            "mixed: keep eco_lot, leftover amp; app lop splits the group",
        )

    case("supercheap_forecast", test_supercheap_forecast)
    case("offsun_drops_surplus_hours", test_offsun_drops_surplus_hours)
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
                17, {a: 9000, b: 3000}, min_amp=6, max_amp=32, eco_lot=50, volts=230, phase3_min_w=4140
            ),
            26,
            "9+3 kW: raise group lot to 13 A + 13 A so both amp caps fit",
        )
        assert_eq(
            surplus.group_lot_for_allocations(
                17, {a: 12000, b: 12000}, min_amp=6, max_amp=32, eco_lot=50, volts=230, phase3_min_w=4140
            ),
            17,
            "same leftover on both: keep leftover lot",
        )
        assert_eq(surplus.parse_lop("1"), 1, "lop 1")
        assert_eq(surplus.parse_lop("50"), 50, "lop 50")
        assert_eq(surplus.parse_lop("unknown"), None, "unknown lop")
        assert_eq(surplus.parse_lop("0"), None, "0 is out of app range")
        assert_eq(surplus.nrg_total_w([230, 230, 230, 0, 10, 10, 10, 0, 2300, 2300, 2300, 6900]), 6900, "nrg[11] watts")
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
