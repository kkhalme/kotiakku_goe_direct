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
POLICY_SOLAR_AND_GRID = const.POLICY_SOLAR_AND_GRID
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


class PlanState:
    """Price-day cache and epoch first-seen map, stepped like ``async_plan``."""

    def __init__(self):
        self.days = {}
        self.seen = {}

    def plan(self, clock, attrs, min_hours=2.0, max_hours=5.0, ceiling=0.2, blocked=None, **extra):
        now_dt = clock.now()
        today_kwh = extra.get("today_kwh")
        tomorrow_kwh = extra.get("tomorrow_kwh")
        self.days = planner.remember_price_day(clock, self.days, attrs, now_dt, today_kwh)
        history = {"days": self.days}
        offsun = extra.get("offsun_kwh")
        if offsun is not None:
            past = planner.past_day_kwh(clock, history, now_dt)
            blocked = surplus.surplus_hour_ranges(
                clock,
                today_kwh,
                tomorrow_kwh,
                offsun,
                60.17,
                24.94,
                past_kwh=past if extra.get("past_mask", True) else None,
            )
        epoch_day = planner.price_epoch_day(
            clock, attrs, history=history, today_kwh=today_kwh, tomorrow_kwh=tomorrow_kwh
        )
        self.seen, seen_ts = planner.epoch_seen_step(clock, self.seen, epoch_day, now_dt)
        return planner.plan(
            clock,
            attrs,
            min_hours=min_hours,
            max_hours=max_hours,
            ceiling=ceiling,
            flex_pct=extra.get("flex_pct", 20),
            flex_euro=extra.get("flex_euro", 0.02),
            source_entity="sensor.price",
            blocked=blocked,
            today_kwh=today_kwh,
            tomorrow_kwh=tomorrow_kwh,
            history=history,
            epoch_seen_ts=seen_ts,
        )


def local_day(tz, year, month, day, price_at):
    """One local day of quarter-hour slots; ``price_at(local_dt)`` per slot."""
    start = datetime.datetime(year, month, day, tzinfo=tz)
    end = (start + datetime.timedelta(days=1)).timestamp()
    out = []
    t = start.timestamp()
    while t < end:
        local = datetime.datetime.fromtimestamp(t, tz)
        out.append({"start": iso(t), "end": iso(t + SLOT), "value": price_at(local)})
        t += SLOT
    return out


def at_local(tz, year, month, day, hour, minute=0):
    return datetime.datetime(year, month, day, hour, minute, tzinfo=tz)


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
                charger_full_power(POLICY_SOLAR_AND_GRID, result, ts),
                active,
                "SolarAndGrid full power follows the window",
            )
            assert_eq(
                charger_surplus(POLICY_SOLAR_AND_GRID, result, ts),
                not active,
                "SolarAndGrid leftover outside the window",
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
            charger_surplus(POLICY_SOLAR_PRIORITY, result, night, enough_solar=True),
            True,
            "SolarPriority leftover when enough solar skips 22 kW",
        )
        assert_eq(
            charger_full_power(POLICY_SOLAR_AND_GRID, result, night, enough_solar=True),
            True,
            "SolarAndGrid 22 kW even when enough solar",
        )
        assert_eq(
            charger_surplus(POLICY_SOLAR_AND_GRID, result, night, enough_solar=True),
            False,
            "SolarAndGrid in-window is not leftover",
        )
        assert_eq(
            charger_surplus(POLICY_SOLAR_AND_GRID, result, midday, enough_solar=True),
            True,
            "SolarAndGrid leftover outside the window",
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
        args = (6, 32, 50, 230, 32)
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
        stay = phase(6000, *args, last_psm=2)
        assert_eq((stay["psm"], stay["amp"], stay["arm_phase"]), (2, 8, False), "8 kW → 6 kW keeps 3-phase")

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
        assert_eq(len(result["raw_windows"]), 2, "today valley misses overnight: follow-up")
        assert_true(
            result["raw_windows"][1]["start"] >= tomorrow_start - 1,
            "follow-up is tomorrow-seeded",
        )
        follow_start = result["raw_windows"][1]["start"]
        for _ in range(4):
            clock.advance(minutes=15)
            result = plan_once(clock, attrs, result, flex_pct=0, flex_euro=0)
            assert_eq(result["reason"], "planned", "still today's cheaper valley")
            assert_eq(result["raw_windows"][0]["start"], start0, "start does not slide")
            assert_eq(result["raw_windows"][1]["start"], follow_start, "follow-up does not slide")

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

    def spans_of(result):
        return [(w["start"], w["end"]) for w in result["raw_windows"]]

    def active_at(result, dt):
        return now_in_windows(result["raw_windows"], dt.timestamp())

    def next_day(ymd, days=1):
        d = datetime.date(*ymd) + datetime.timedelta(days=days)
        return (d.year, d.month, d.day)

    fixed_2h = dict(min_hours=2.0, max_hours=2.0, flex_pct=0, flex_euro=0)

    def midnight_days(ymd):
        d1 = next_day(ymd)
        day_d = local_day(HELSINKI, *ymd, lambda t: 0.02 if t.hour == 23 else 0.10)
        day_d1 = local_day(
            HELSINKI,
            *d1,
            lambda t: 0.02 if t.hour == 0 else (0.03 if t.hour >= 22 else 0.10),
        )
        return d1, day_d, day_d1

    def check_midnight_keeps_window(ymd):
        d1, day_d, day_d1 = midnight_days(ymd)
        state = PlanState()
        clock = Clock(at_local(HELSINKI, *ymd, 21), tz=HELSINKI)
        evening = {"raw_today": day_d, "raw_tomorrow": day_d1, "tomorrow_valid": True}
        result = state.plan(clock, evening, **fixed_2h)
        across = (
            at_local(HELSINKI, *ymd, 23).timestamp(),
            at_local(HELSINKI, *d1, 1).timestamp(),
        )
        assert_eq(spans_of(result), [across], "%s evening: 23:00→01:00" % (ymd,))

        clock.set(at_local(HELSINKI, *d1, 0) + datetime.timedelta(seconds=30))
        rolled = {"raw_today": day_d1, "raw_tomorrow": [], "tomorrow_valid": False}
        result = state.plan(clock, rolled, **fixed_2h)
        assert_eq(spans_of(result), [across], "%s midnight keeps the window" % (ymd,))
        half_past = at_local(HELSINKI, *d1, 0, 30)
        clock.set(half_past)
        result = state.plan(clock, rolled, **fixed_2h)
        assert_true(active_at(result, half_past), "%s active at 00:30" % (ymd,))
        assert_eq(
            charger_full_power(POLICY_SOLAR_PRIORITY, result, half_past.timestamp()),
            True,
            "%s SolarPriority still 22 kW at 00:30" % (ymd,),
        )
        one = at_local(HELSINKI, *d1, 1)
        clock.set(one)
        result = state.plan(clock, rolled, **fixed_2h)
        assert_eq(spans_of(result), [across], "%s plan held after it ends" % (ymd,))
        assert_true(not active_at(result, one), "%s off at 01:00" % (ymd,))

        control = plan_once(clock, rolled, None, **fixed_2h)
        assert_eq(
            control["raw_windows"][0]["start"],
            at_local(HELSINKI, *d1, 22).timestamp(),
            "%s without yesterday's prices the window jumps to 22:00" % (ymd,),
        )
        assert_true(not active_at(control, half_past), "%s control is off at 00:30" % (ymd,))

    def test_midnight_keeps_window_across_midnight():
        check_midnight_keeps_window((2026, 3, 15))

    def test_midnight_keeps_window_dst():
        check_midnight_keeps_window((2026, 3, 28))
        check_midnight_keeps_window((2026, 10, 24))

    def test_midnight_keeps_yesterday_offsun_mask():
        ymd = (2026, 3, 15)
        d1 = next_day(ymd)

        def price_d(t):
            if 11 <= t.hour < 13:
                return 0.01
            return 0.02 if t.hour == 23 else 0.10

        day_d = local_day(HELSINKI, *ymd, price_d)
        _d1, _day_d, day_d1 = midnight_days(ymd)
        kw = dict(fixed_2h, offsun_kwh=1.0)
        state = PlanState()
        clock = Clock(at_local(HELSINKI, *ymd, 21), tz=HELSINKI)
        evening = {"raw_today": day_d, "raw_tomorrow": day_d1, "tomorrow_valid": True}
        result = state.plan(clock, evening, today_kwh=50.0, tomorrow_kwh=50.0, **kw)
        across = (
            at_local(HELSINKI, *ymd, 23).timestamp(),
            at_local(HELSINKI, *d1, 1).timestamp(),
        )
        assert_eq(spans_of(result), [across], "sunny midday dip is off-sun; 23:00 wins")

        half_past = at_local(HELSINKI, *d1, 0, 30)
        rolled = {"raw_today": day_d1, "raw_tomorrow": [], "tomorrow_valid": False}
        clock.set(half_past)
        kept = PlanState()
        kept.days, kept.seen = dict(state.days), dict(state.seen)
        result = kept.plan(clock, rolled, today_kwh=50.0, tomorrow_kwh=None, **kw)
        assert_eq(spans_of(result), [across], "yesterday's off-sun hours still masked")
        assert_true(active_at(result, half_past), "active at 00:30")

        unmasked = PlanState()
        unmasked.days, unmasked.seen = dict(state.days), dict(state.seen)
        result = unmasked.plan(
            clock, rolled, today_kwh=50.0, tomorrow_kwh=None, past_mask=False, **kw
        )
        assert_true(
            not active_at(result, half_past),
            "without yesterday's kWh the finished midday dip would win",
        )

    def test_arrival_keeps_running_window():
        d0, d1, d2 = (2026, 1, 13), (2026, 1, 14), (2026, 1, 15)
        kw = dict(min_hours=3.0, max_hours=3.0, flex_pct=0, flex_euro=0)
        day_d = local_day(HELSINKI, *d0, lambda t: 0.10)
        day_d1 = local_day(HELSINKI, *d1, lambda t: 0.03 if 12 <= t.hour < 15 else 0.10)
        day_d2 = local_day(HELSINKI, *d2, lambda t: 0.01 if 1 <= t.hour < 4 else 0.10)
        state = PlanState()
        clock = Clock(at_local(HELSINKI, *d0, 20), tz=HELSINKI)
        state.plan(clock, {"raw_today": day_d, "raw_tomorrow": day_d1, "tomorrow_valid": True}, **kw)
        noon = (
            at_local(HELSINKI, *d1, 12).timestamp(),
            at_local(HELSINKI, *d1, 15).timestamp(),
        )
        morning = {"raw_today": day_d1, "raw_tomorrow": [], "tomorrow_valid": False}
        clock.set(at_local(HELSINKI, *d1, 13))
        result = state.plan(clock, morning, **kw)
        assert_eq(spans_of(result), [noon], "midday window from last evening")
        assert_true(active_at(result, clock.now()), "running at 13:00")

        arrival = at_local(HELSINKI, *d1, 14, 10)
        clock.set(arrival)
        arrived = {"raw_today": day_d1, "raw_tomorrow": day_d2, "tomorrow_valid": True}
        result = state.plan(clock, arrived, **kw)
        night = (
            at_local(HELSINKI, *d2, 1).timestamp(),
            at_local(HELSINKI, *d2, 4).timestamp(),
        )
        assert_eq(spans_of(result), [night, noon], "new window plus the running one")
        assert_eq(result["carried"], 1, "one carried window")
        assert_eq(result["start"], iso(night[0]), "window 1 is the new epoch's")
        for hour, minute, on in ((14, 30, True), (14, 59, True), (15, 0, False), (22, 0, False)):
            when = at_local(HELSINKI, *d1, hour, minute)
            clock.set(when)
            result = state.plan(clock, arrived, **kw)
            assert_eq(spans_of(result), [night, noon], "carried set holds @ %s" % when)
            assert_eq(active_at(result, when), on, "active @ %s" % when)
        clock.set(at_local(HELSINKI, *d2, 2))
        result = state.plan(clock, arrived, **kw)
        assert_true(active_at(result, clock.now()), "new window runs at 02:00")

        clock.set(at_local(HELSINKI, *d1, 14, 30))
        no_seen = planner.plan(
            clock,
            arrived,
            source_entity="sensor.price",
            history={"days": state.days},
            **kw,
        )
        assert_eq(spans_of(no_seen), [night], "without epoch_seen the running window is replaced")

    def test_arrival_does_not_carry_unstarted_window():
        d0, d1, d2 = (2026, 1, 20), (2026, 1, 21), (2026, 1, 22)
        day_d = local_day(HELSINKI, *d0, lambda t: 0.10)
        day_d1 = local_day(HELSINKI, *d1, lambda t: 0.03 if t.hour >= 22 else 0.10)
        day_d2 = local_day(HELSINKI, *d2, lambda t: 0.01 if 3 <= t.hour < 5 else 0.10)
        state = PlanState()
        clock = Clock(at_local(HELSINKI, *d0, 20), tz=HELSINKI)
        result = state.plan(
            clock, {"raw_today": day_d, "raw_tomorrow": day_d1, "tomorrow_valid": True}, **fixed_2h
        )
        evening_d1 = (
            at_local(HELSINKI, *d1, 22).timestamp(),
            at_local(HELSINKI, *d2, 0).timestamp(),
        )
        assert_eq(spans_of(result), [evening_d1], "previous epoch picks 22:00")
        clock.set(at_local(HELSINKI, *d1, 10))
        morning = {"raw_today": day_d1, "raw_tomorrow": [], "tomorrow_valid": False}
        assert_eq(spans_of(state.plan(clock, morning, **fixed_2h)), [evening_d1], "held overnight")
        clock.set(at_local(HELSINKI, *d1, 14, 10))
        arrived = {"raw_today": day_d1, "raw_tomorrow": day_d2, "tomorrow_valid": True}
        night = (
            at_local(HELSINKI, *d2, 3).timestamp(),
            at_local(HELSINKI, *d2, 5).timestamp(),
        )
        result = state.plan(clock, arrived, **fixed_2h)
        assert_eq(spans_of(result), [night], "not-yet-started 22:00 is replaced")
        late = at_local(HELSINKI, *d1, 22, 30)
        clock.set(late)
        result = state.plan(clock, arrived, **fixed_2h)
        assert_eq(spans_of(result), [night], "22:00 does not come back at its start")
        assert_eq(result["carried"], 0, "nothing carried")
        assert_true(not active_at(result, late), "off at 22:30")

    def test_midnight_without_cache_plans_today_only():
        ymd = (2026, 3, 15)
        d1, _day_d, day_d1 = midnight_days(ymd)
        clock = Clock(at_local(HELSINKI, *d1, 0) + datetime.timedelta(seconds=30), tz=HELSINKI)
        rolled = {"raw_today": day_d1, "raw_tomorrow": [], "tomorrow_valid": False}
        state = PlanState()
        result = state.plan(clock, rolled, **fixed_2h)
        control = plan_once(clock, rolled, None, **fixed_2h)
        assert_eq(spans_of(result), spans_of(control), "no cache: today-only plan")
        assert_eq(result["epoch_seen"], None, "first epoch on record carries nothing")
        assert_eq(result["carried"], 0, "nothing carried")

    def test_started_window_carried_on_arrival():
        day = datetime.datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
        today_start = datetime.datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc).timestamp()
        tomorrow_start = today_start + 24 * 3600
        today = slots_from(today_start + 10 * 3600, [0.09] * 48)
        clock = Clock(day)
        state = PlanState()
        result = state.plan(clock, {"raw_today": today}, flex_pct=0, flex_euro=0)
        started = spans_of(result)[0]
        assert_true(started[0] <= day.timestamp() < started[1], "window has already started")
        clock.advance(minutes=15)
        attrs = {
            "raw_today": today,
            "raw_tomorrow": slots_from(tomorrow_start, [0.02] * 16),
            "tomorrow_valid": True,
        }
        result = state.plan(clock, attrs, flex_pct=0, flex_euro=0)
        assert_true(result["raw_windows"][0]["start"] >= tomorrow_start - 1, "window 1 moved to tomorrow")
        assert_eq(spans_of(result)[1:], [started], "running window carried")
        assert_true(active_at(result, clock.now()), "still 22 kW at 10:15")
        switched = spans_of(result)
        for _ in range(12):
            clock.advance(minutes=15)
            result = state.plan(clock, attrs, flex_pct=0, flex_euro=0)
            assert_eq(spans_of(result), switched, "carried set does not slide")
        assert_true(not active_at(result, clock.now()), "carried window ended at 12:00")

    def test_tomorrow_switch_keeps_running_plateau():
        day = datetime.datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
        today_start = datetime.datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc).timestamp()
        tomorrow_start = today_start + 24 * 3600
        today = slots_from(today_start + 10 * 3600, [0.09] * 48)
        clock = Clock(day)
        state = PlanState()
        attrs = {"raw_today": today, "tomorrow_valid": False}
        plateau = spans_of(state.plan(clock, attrs))[0]
        clock.advance(hours=4)
        attrs = {
            "raw_today": today,
            "raw_tomorrow": slots_from(tomorrow_start, [0.02] * 16),
            "tomorrow_valid": True,
        }
        result = state.plan(clock, attrs)
        assert_true(result["raw_windows"][0]["start"] >= tomorrow_start - 1, "window 1 is tomorrow")
        assert_eq(spans_of(result)[1:], [plateau], "running plateau carried past 14:00")
        assert_true(active_at(result, clock.now()), "still 22 kW at 14:00")

    def test_price_cache_and_epoch_seen():
        clock = Clock(at_local(HELSINKI, 2026, 3, 15, 12), tz=HELSINKI)
        day = local_day(HELSINKI, 2026, 3, 15, lambda t: 0.05)
        days = planner.remember_price_day(clock, {}, {"raw_today": day}, clock.now(), 12.0)
        assert_eq(list(days), ["2026-03-15"], "keyed by local date")
        assert_eq(len(days["2026-03-15"]["slots"]), 96, "whole local day cached")
        assert_eq(days["2026-03-15"]["kwh"], 12.0, "kWh cached")
        again = planner.remember_price_day(clock, days, {"raw_today": day}, clock.now(), None)
        assert_eq(again["2026-03-15"]["kwh"], 12.0, "unknown forecast keeps last kWh")
        empty = planner.remember_price_day(clock, days, {"raw_today": []}, clock.now(), 12.0)
        assert_eq(len(empty["2026-03-15"]["slots"]), 96, "empty curve does not replace the day")
        clock.set(at_local(HELSINKI, 2026, 3, 18, 12))
        pruned = planner.remember_price_day(clock, days, None, clock.now(), None)
        assert_eq(pruned, {}, "days older than two back are dropped")

        today = at_local(HELSINKI, 2026, 3, 18, 0).timestamp()
        tomorrow = at_local(HELSINKI, 2026, 3, 19, 0).timestamp()
        seen, ts = planner.epoch_seen_step(clock, {}, today, clock.now())
        assert_eq(ts, None, "first epoch on record")
        later = clock.as_timestamp(clock.now()) + 3600
        clock.set(datetime.datetime.fromtimestamp(later, tz=HELSINKI))
        seen2, ts2 = planner.epoch_seen_step(clock, seen, today, clock.now())
        assert_eq(seen2, seen, "same epoch is not re-recorded")
        assert_eq(ts2, None, "still the first epoch")
        seen3, ts3 = planner.epoch_seen_step(clock, seen2, tomorrow, clock.now())
        assert_eq(ts3, later, "next epoch is first seen now")
        seen4, ts4 = planner.epoch_seen_step(
            clock, seen3, tomorrow, clock.now() + datetime.timedelta(hours=5)
        )
        assert_eq((seen4, ts4), (seen3, later), "first-seen time does not move")
        far = at_local(HELSINKI, 2026, 3, 30, 12)
        seen5, _ts5 = planner.epoch_seen_step(
            clock, seen4, at_local(HELSINKI, 2026, 3, 31, 0).timestamp(), far
        )
        assert_eq(len(seen5), 1, "old epochs pruned when a new one is recorded")

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
    case("midnight_keeps_window_across_midnight", test_midnight_keeps_window_across_midnight)
    case("midnight_keeps_window_dst", test_midnight_keeps_window_dst)
    case("midnight_keeps_yesterday_offsun_mask", test_midnight_keeps_yesterday_offsun_mask)
    case("arrival_keeps_running_window", test_arrival_keeps_running_window)
    case("arrival_does_not_carry_unstarted_window", test_arrival_does_not_carry_unstarted_window)
    case("midnight_without_cache_plans_today_only", test_midnight_without_cache_plans_today_only)
    case("started_window_carried_on_arrival", test_started_window_carried_on_arrival)
    case("tomorrow_switch_keeps_running_plateau", test_tomorrow_switch_keeps_running_plateau)
    case("price_cache_and_epoch_seen", test_price_cache_and_epoch_seen)

    run()


if __name__ == "__main__":
    main()
