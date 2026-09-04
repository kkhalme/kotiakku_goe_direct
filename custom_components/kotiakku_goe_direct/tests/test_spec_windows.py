"""Spec lock for charge windows. A planner rewrite must keep these contracts.

Windows are contiguous cheap bottoms in [min, max] (default 2–5 h, step
0.25 h). Every 15-minute slot is ≤ the ceiling. The ceiling is a hard cap,
not “charge whenever under it”. Greedy disjoint ranks: cheapest, longest,
earliest, off-sun (cheapest after dropping surplus-forecast hours). Cap 16.
Min hours is the shortest session: isolated shorter bottoms are dropped;
pauses shorter than min between bottoms are joined if still under the
ceiling. Freeze is per rank and does not slide 15 minutes.
"""

from __future__ import annotations

import datetime
from datetime import timezone

from harness import Clock, SLOT, assert_eq, assert_true, case_runner, load_mod, slots_from

planner = load_mod("planner", "_spec_win")
pick_all = planner.pick_all
choose = planner.choose
clamp_hours = planner.clamp_hours
now_in_windows = planner.now_in_windows
plan = planner.plan
collect_slots = planner.collect_slots
drop_blocked = planner.drop_blocked
current_or_next = planner.current_or_next
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


def spans(windows):
    return [(round(w["start"] - 0, 0), round(w["end"] - 0, 0)) for w in windows]


def main():
    case, run = case_runner()
    now = datetime.datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)
    base = datetime.datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc).timestamp()

    def test_slot_grid_bounds_and_cap():
        assert_eq(planner.SLOT_SECONDS, 900, "15-minute slots")
        assert_eq(planner.MAX_WINDOWS, 16, "at most 16 windows per rank")
        mn, mx = clamp_hours(None, None)
        assert_eq((mn, mx), (2.0, 5.0), "default 2–5 h")
        mn, mx = clamp_hours(0, 0)
        assert_eq((mn, mx), (2.0, 5.0), "non-positive falls back to 2–5 h")
        mn, mx = clamp_hours(0.1, 30)
        assert_eq((mn, mx), (0.25, 24.0), "clamped to 0.25–24 h")
        mn, mx = clamp_hours(5, 2)
        assert_eq((mn, mx), (2.0, 5.0), "min>max swapped")
        islands = ([0.04] * 4 + [0.2] * 4) * 17
        windows = pick_all(ts_slots(base, islands), 3600, 5 * 3600, 0.1, base - 3600)
        assert_eq(len(windows), 16, "17 one-hour bottoms: cap 16")
        swiss = ([0.04] + [0.2] * 3) * 20
        quarter = pick_all(
            ts_slots(base, swiss), 0.25 * 3600, 5 * 3600, 0.1, base - 3600
        )
        assert_eq(len(quarter), 16, "min 0.25 h allows 15-min islands, still capped at 16")

    def test_ceiling_is_hard_cap_not_selection():
        at_cap = pick_all(ts_slots(base, [0.1] * 8), 2 * 3600, 5 * 3600, 0.1, base - 3600)
        assert_eq(len(at_cap), 1, "price equal to ceiling is allowed")
        over = pick_all(ts_slots(base, [0.1000002] * 8), 2 * 3600, 5 * 3600, 0.1, base - 3600)
        assert_eq(len(over), 0, "a hair over the ceiling is out")
        spike = [0.04] * 12 + [0.25] + [0.05] * 12
        windows = pick_all(ts_slots(base, spike), 2 * 3600, 5 * 3600, 0.1, base - 3600)
        spike_ts = base + 12 * SLOT
        assert_eq(len(windows), 2, "over-ceiling slot splits")
        for w in windows:
            assert_true(not (w["start"] <= spike_ts < w["end"]), "spike not inside a window")
        dip = [0.2] * 4 + [0.01] + [0.2] * 4
        none = pick_all(ts_slots(base, dip), 3600, 5 * 3600, 0.1, base - 3600)
        assert_eq(len(none), 0, "15 min under the cap is not a session when min is 1 h")

    def test_greedy_ranks_on_the_same_curve():
        prices = [0.09] * 8 + [0.01] * 8 + [0.09] * 8
        slots = ts_slots(base, prices)
        cheap = pick_all(slots, 2 * 3600, 5 * 3600, 0.1, base - 3600, "cheapest")
        long = pick_all(slots, 2 * 3600, 5 * 3600, 0.1, base - 3600, "longest")
        early = pick_all(slots, 2 * 3600, 5 * 3600, 0.1, base - 3600, "earliest")
        assert_eq(len(cheap), 3, "cheapest: dip plus two 2 h flanks")
        assert_eq(cheap[0]["start"], base + 8 * SLOT, "cheapest first is the 0.01 dip")
        assert_true(abs(cheap[0]["avg"] - 0.01) < 1e-9, "dip average")
        assert_eq(round(dur_h(long[0]), 2), 5.0, "longest takes max hours")
        assert_eq(long[0]["start"], base, "longest starts at the left of the run")
        assert_eq(early[0]["start"], base, "earliest starts at the first legal slot")
        assert_eq(cheap[1]["start"], base, "left flank is second cheapest")
        assert_eq(cheap[2]["start"], base + 16 * SLOT, "right flank is third")
        touching = sorted(cheap, key=lambda w: w["start"])
        assert_eq(touching[0]["end"], touching[1]["start"], "bands may touch")
        assert_eq(len(touching), 3, "touching bands stay split")

    def test_duration_min_max_and_leftover_tile():
        six = pick_all(ts_slots(base, [0.05] * 24), 2 * 3600, 5 * 3600, 0.1, base - 3600)
        assert_eq(len(six), 1, "6 h: leftover 1 h < min is dropped")
        assert_eq(round(dur_h(six[0]), 2), 5.0, "takes max")
        eight = pick_all(ts_slots(base, [0.05] * 32), 2 * 3600, 5 * 3600, 0.1, base - 3600)
        assert_eq(len(eight), 2, "8 h: 5 h + 3 h leftover")
        assert_eq(round(dur_h(eight[0]), 2), 5.0, "first is max")
        assert_eq(round(dur_h(eight[1]), 2), 3.0, "leftover ≥ min is kept")
        assert_eq(eight[0]["end"], eight[1]["start"], "leftover is contiguous")
        short_flank = [0.09] * 6 + [0.01] * 8 + [0.09] * 6
        only = pick_all(ts_slots(base, short_flank), 2 * 3600, 5 * 3600, 0.1, base - 3600)
        assert_eq(len(only), 1, "1.5 h flanks shorter than min are dropped")
        rising = [0.01] * 8 + [0.05] * 8 + [0.09] * 8
        bands = pick_all(ts_slots(base, rising), 2 * 3600, 5 * 3600, 0.1, base - 3600)
        assert_eq(round(dur_h(bands[0]), 2), 2.0, "rising prices: cheapest takes min length")
        assert_true(abs(bands[0]["avg"] - 0.01) < 1e-9, "cheapest 2 h is the 0.01 band")

    def test_min_hours_joins_short_pauses_only():
        prices = [0.02, 0.05, 0.05, 0.03, 0.03, 0.15, 0.04, 0.04, 0.04, 0.04]
        slots = ts_slots(base, prices)
        for rank in ("cheapest", "offsun"):
            windows = pick_all(slots, 3600, 5 * 3600, 0.1, base - 3600, rank)
            for w in windows:
                assert_true(dur_h(w) + 0.01 >= 1.0, "%s session ≥ min" % rank)
            assert_true(now_in_windows(windows, base), "%s includes the 0.02 bottom" % rank)
            assert_true(
                now_in_windows(windows, base + SLOT),
                "%s joins a 15 min pause inside the first bottom" % rank,
            )
            assert_true(
                not now_in_windows(windows, base + 5 * SLOT),
                "%s does not join across an over-ceiling spike" % rank,
            )
        hour_gap = [0.02] * 4 + [0.2] * 4 + [0.03] * 4
        split = pick_all(
            ts_slots(base, hour_gap), 3600, 5 * 3600, 0.1, base - 3600
        )
        ordered = sorted(split, key=lambda w: w["start"])
        assert_eq(len(ordered), 2, "1 h over-ceiling pause is not joined")
        assert_true(
            not now_in_windows(split, base + 4 * SLOT),
            "the pause stays off",
        )
        too_long = [0.04] * 12 + [0.06] + [0.04] * 12
        capped = pick_all(
            ts_slots(base, too_long), 3600, 5 * 3600, 0.1, base - 3600
        )
        for w in capped:
            assert_true(dur_h(w) <= 5.0 + 0.01, "join still respects max hours")
        assert_true(len(capped) >= 2, "6.25 h does not collapse to one window")

    def test_exclusive_end_and_current_slot():
        w = {"start": base, "end": base + 2 * 3600, "avg": 0.04}
        assert_true(now_in_windows([w], w["start"]), "on at start")
        assert_true(now_in_windows([w], w["end"] - 1), "on 1 s before end")
        assert_true(not now_in_windows([w], w["end"]), "off at exclusive end")
        late = pick_all(ts_slots(base, [0.04] * 16), 2 * 3600, 5 * 3600, 0.1, base + 600)
        assert_eq(late[0]["start"], base, "current quarter is eligible")
        nxt = current_or_next(
            [
                {"start": base, "end": base + 3600, "avg": 0.04},
                {"start": base + 7200, "end": base + 10800, "avg": 0.05},
            ],
            base + 4000,
        )
        assert_eq(nxt["start"], base + 7200, "current_or_next is the next future window")
        cur = current_or_next(
            [{"start": base, "end": base + 7200, "avg": 0.04}],
            base + 100,
        )
        assert_eq(cur["start"], base, "inside a window is current")

    def test_freeze_switch_idle_and_param_replan():
        slots = ts_slots(base, [0.08] * 20 + [0.03] * 16)
        first = pick_all(slots, 2 * 3600, 5 * 3600, 0.1, base)
        prev = {
            "windows": first,
            "min_hours": 2.0,
            "max_hours": 5.0,
            "ceiling": 0.1,
            "horizon": slots[-1][1],
            "rank": "cheapest",
        }
        frozen = choose(slots, 2.0, 5.0, 0.1, base + SLOT, prev)
        assert_eq(frozen["reason"], "frozen", "same curve does not slide")
        assert_eq(frozen["windows"][0]["start"], first[0]["start"], "start held")
        today = ts_slots(base, [0.09] * 16)
        tomorrow = ts_slots(base + 24 * 3600, [0.02] * 16)
        morning = pick_all(today, 2 * 3600, 5 * 3600, 0.1, base + 3600)
        prev_m = {
            "windows": morning,
            "min_hours": 2.0,
            "max_hours": 5.0,
            "ceiling": 0.1,
            "horizon": today[-1][1],
            "rank": "cheapest",
        }
        switched = choose(today + tomorrow, 2.0, 5.0, 0.1, base + 3600, prev_m)
        assert_eq(switched["reason"], "switched", "cheaper tomorrow on a longer horizon")
        four_h = ts_slots(base, [0.04] * 16)
        done = pick_all(four_h, 2 * 3600, 5 * 3600, 0.1, base - 3600)
        prev_d = {
            "windows": done,
            "min_hours": 2.0,
            "max_hours": 5.0,
            "ceiling": 0.1,
            "horizon": four_h[-1][1],
            "rank": "cheapest",
        }
        idle = choose(four_h, 2.0, 5.0, 0.1, done[0]["end"] + 60, prev_d)
        assert_eq(idle["reason"], "idle_after_window", "same horizon, no leftover bottom")
        eight = ts_slots(base, [0.04] * 32)
        both = pick_all(eight, 2 * 3600, 5 * 3600, 0.1, base - 3600)
        assert_eq(len(both), 2, "8 h still two windows when frozen as a set")
        tighter = choose(slots, 2.0, 5.0, 0.05, base + SLOT, prev)
        assert_true(tighter["reason"] != "frozen", "ceiling change is not freeze")
        longer_min = choose(slots, 3.0, 5.0, 0.1, base + SLOT, prev)
        assert_true(longer_min["reason"] != "frozen", "min-hours change is not freeze")
        rank_prev = {
            "windows": pick_all(slots, 2 * 3600, 5 * 3600, 0.1, base, "cheapest"),
            "min_hours": 2.0,
            "max_hours": 5.0,
            "ceiling": 0.1,
            "horizon": slots[-1][1],
            "rank": "cheapest",
        }
        as_long = choose(slots, 2.0, 5.0, 0.1, base, rank_prev, "longest")
        assert_eq(as_long["reason"], "planned", "rank change replans")
        cheesy = [
            {"avg": 0.02, "start": base, "end": base + 4 * SLOT},
            {"avg": 0.03, "start": base + 5 * SLOT, "end": base + 9 * SLOT},
        ]
        swiss = ts_slots(base, [0.02] * 4 + [0.05] + [0.03] * 4 + [0.2] * 4)
        prev_s = {
            "windows": cheesy,
            "min_hours": 1.0,
            "max_hours": 5.0,
            "ceiling": 0.1,
            "horizon": swiss[-1][1],
            "rank": "cheapest",
        }
        joined = choose(swiss, 1.0, 5.0, 0.1, base + SLOT, prev_s)
        assert_eq(joined["reason"], "planned", "joining a frozen 15 min pause is a replan")
        assert_eq(len(joined["windows"]), 1, "swiss-cheese freeze is repaired")

    def test_offsun_drops_surplus_hours_not_cheapest():
        prices = [0.05] * 32 + [0.01] * 32 + [0.2] * 32
        slots = ts_slots(base, prices)
        blocked = [(base + 8 * 3600, base + 16 * 3600)]
        cheap = pick_all(slots, 2 * 3600, 5 * 3600, 0.1, base - 3600, "cheapest")
        off = choose(slots, 2.0, 5.0, 0.1, base - 3600, None, "offsun", blocked)
        assert_eq(cheap[0]["start"], base + 8 * 3600, "cheapest is the midday dip")
        assert_eq(off["windows"][0]["start"], base, "off-sun is the night")
        dropped = drop_blocked(slots, blocked)
        assert_true(len(dropped) < len(slots), "blocked hours are removed for off-sun")
        for slot in dropped:
            assert_true(
                not (slot[0] < blocked[0][1] and slot[1] > blocked[0][0]),
                "no overlapping slot survives drop_blocked",
            )
        unknown = choose(slots, 2.0, 5.0, 0.1, base - 3600, None, "offsun", None)
        assert_eq(
            unknown["windows"][0]["start"],
            cheap[0]["start"],
            "no surplus hours: off-sun matches cheapest",
        )
        even = [0.04] * 20
        gap = choose(
            ts_slots(base, even),
            2.0,
            5.0,
            0.1,
            base - 3600,
            None,
            "offsun",
            [(base + 8 * SLOT, base + 12 * SLOT)],
        )
        assert_eq(len(gap["windows"]), 2, "blocked hour stays a gap")
        assert_eq(gap["windows"][0]["end"], base + 8 * SLOT, "stops at the block")
        assert_eq(gap["windows"][1]["start"], base + 12 * SLOT, "resumes after the block")

    def test_parse_hourly_halfhourly_and_quarter_series():
        clock = Clock(datetime.datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc))
        hourly = collect_slots(clock, {"raw_today": [0.04] * 24}, clock.now())
        assert_eq(len(hourly), 24, "24 values are hourly")
        assert_eq(hourly[0][1] - hourly[0][0], 3600, "hourly slot 1 h")
        half = collect_slots(clock, {"raw_today": [0.04] * 48}, clock.now())
        assert_eq(half[0][1] - half[0][0], 1800, "48 values are 30 min")
        quarter = collect_slots(clock, {"raw_today": [0.04] * 96}, clock.now())
        assert_eq(len(quarter), 96, "96 values are 15 min")
        assert_eq(quarter[0][1] - quarter[0][0], 900, "quarter slot 15 min")
        dicts = collect_slots(
            clock,
            {"raw_today": slots_from(base, [0.04, 0.2, 0.04])},
            clock.now(),
        )
        assert_eq(len(dicts), 3, "Nordpool dict slots")
        today = slots_from(base, [0.09] * 4)
        tom = slots_from(base + 24 * 3600, [0.02] * 8)
        both = collect_slots(
            clock,
            {"raw_today": today, "raw_tomorrow": tom, "tomorrow_valid": True},
            clock.now(),
        )
        assert_eq(len(both), 12, "today plus tomorrow")
        assert_true(planner.tomorrow_ok(clock, {"tomorrow_valid": True}, both), "flag")
        out = plan(
            Clock(datetime.datetime.fromtimestamp(base, tz=timezone.utc)),
            {"raw_today": [0.04] * 24},
            min_hours=2,
            max_hours=5,
            ceiling=0.1,
        )
        assert_eq(out["slot_count"], 24, "plan sees hourly series")
        assert_eq(round(dur_h(out["raw_windows"][0]), 2), 5.0, "hourly 5 h max window")
        empty = plan(Clock(now), None)
        assert_eq(empty["reason"], "no_source", "missing attrs")
        none = choose([], 2.0, 5.0, 0.1, base, None)
        assert_eq(none["reason"], "no_slots", "empty series")

    def test_full_power_policies_and_until_unplug():
        results = {
            "cheapest": {"raw_windows": [{"start": 1000, "end": 2000}]},
            "offsun": {"raw_windows": [{"start": 3000, "end": 4000}]},
        }
        assert_eq(charger_full_power("Force on", results, 0), True, "Force on is 22 kW now")
        assert_eq(charger_full_power("Force off", results, 1500), False, "Force off never")
        assert_eq(charger_full_power("Cheapest", results, 1500), True, "Cheapest in window")
        assert_eq(charger_full_power("Cheapest", results, 2000), False, "exclusive end")
        assert_eq(charger_full_power("Supercheap", results, 1500), False, "Supercheap ignores cheapest")
        assert_eq(charger_full_power("Supercheap", results, 3500), True, "Supercheap uses off-sun")
        assert_eq(
            charger_full_power("Supercheap", results, 3500, enough_solar=True),
            False,
            "enough solar skips Supercheap including off-sun night",
        )
        assert_eq(
            charger_full_power("Cheapest", results, 1500, enough_solar=True),
            True,
            "Cheapest ignores enough solar",
        )
        assert_eq(
            charger_full_power("Force off", results, 1500, until_unplug=True),
            True,
            "until-unplug overrides Force off",
        )
        assert_eq(
            charger_full_power("Supercheap", results, 1500, enough_solar=True, until_unplug=True),
            True,
            "until-unplug overrides enough solar",
        )
        on, seen = until_unplug_step(True, False, False)
        assert_eq((on, seen), (True, False), "unplugged start waits for a plug")
        on, seen = until_unplug_step(True, True, False)
        assert_eq((on, seen), (True, True), "plug arms seen")
        on, seen = until_unplug_step(True, True, True)
        assert_eq((on, seen), (True, True), "Complete is still plugged; override stays")
        on, seen = until_unplug_step(True, False, True)
        assert_eq((on, seen), (False, False), "unplug clears")
        on, seen = until_unplug_step(False, True, True)
        assert_eq((on, seen), (False, False), "manual off clears seen")

    case("slot_grid_bounds_and_cap", test_slot_grid_bounds_and_cap)
    case("ceiling_is_hard_cap_not_selection", test_ceiling_is_hard_cap_not_selection)
    case("greedy_ranks_on_the_same_curve", test_greedy_ranks_on_the_same_curve)
    case("duration_min_max_and_leftover_tile", test_duration_min_max_and_leftover_tile)
    case("min_hours_joins_short_pauses_only", test_min_hours_joins_short_pauses_only)
    case("exclusive_end_and_current_slot", test_exclusive_end_and_current_slot)
    case("freeze_switch_idle_and_param_replan", test_freeze_switch_idle_and_param_replan)
    case("offsun_drops_surplus_hours_not_cheapest", test_offsun_drops_surplus_hours_not_cheapest)
    case("parse_hourly_halfhourly_and_quarter_series", test_parse_hourly_halfhourly_and_quarter_series)
    case("full_power_policies_and_until_unplug", test_full_power_policies_and_until_unplug)
    run()


if __name__ == "__main__":
    main()
