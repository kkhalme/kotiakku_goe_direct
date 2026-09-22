"""Spec lock for leftover surplus. Independent of the window planner.

Pin the 3 kW / ~300 W leftover cases and the rest of the surplus contract:
house vs EV add-back, Controller vs nrg, start/hold/stop, 3 kW steal,
budget amps, phase hold, group lot beside full-power.
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from harness import Clock, assert_eq, assert_true, case_runner, load_mod

surplus = load_mod("surplus", "_spec_sur")
planner = load_mod("planner", "_spec_sur_p")
const = load_mod("const", "_spec_sur_c")

A, B, C = "111111", "222222", "333333"
VOLTS = 230
MIN_A = 6
MAX_A = 32
GROUP_LOT = 50
MAX_1P = 32
SPLIT_MIN = 3000
SPLIT_FLOOR = 500
HOLD_W = 1000
START_W = 2000


def leftover_kw_args(**extra):
    kw = dict(
        lops={A: 1, B: 50},
        plugged={A: True, B: True},
        split_min_w=SPLIT_MIN,
        split_floor_w=SPLIT_FLOOR,
        charger_max_w=MAX_A * VOLTS * 3,
        min_amp=MIN_A,
        volts=VOLTS,
        max_1phase_amp=32,
    )
    kw.update(extra)
    return kw


def mqtt_for(leftover, session, *, soc=96, window_ok=True, floor_expired=False,
             hold_active=False, last_psm=None, **alloc_kw):
    """Same write_on path as the controller: floor uses 6 A, else min(share, leftover)."""
    dec = surplus.surplus_decision(
        session,
        leftover,
        soc,
        window_ok=window_ok,
        start_min_w=START_W,
        hold_min_w=HOLD_W,
        floor_expired=floor_expired,
        hold_active=hold_active,
        hold_exit_w=START_W,
    )
    if not dec["write_on"]:
        return dec, {}
    alloc_w = leftover
    if dec["use_floor_budget"]:
        alloc_w = max(int(alloc_w), MIN_A * VOLTS)
    plan = surplus.surplus_allocation_plan(
        alloc_kw.pop("serials", [A, B]),
        leftover_w=alloc_w,
        **leftover_kw_args(**alloc_kw),
    )
    cmds = {}
    last_psm = last_psm or {}
    for serial, watts_i in plan["allocations"].items():
        source_w = 0 if dec["use_floor_budget"] else min(int(watts_i), max(int(leftover), 0))
        cmds[serial] = surplus.surplus_phase_budget(
            source_w, MIN_A, MAX_A, GROUP_LOT, VOLTS, MAX_1P,
            last_psm=last_psm.get(serial),
        )
    return dec, cmds


def main():
    case, run = case_runner()

    def test_leftover_house_must_contain_ev():
        leftover_w = surplus.leftover_w
        includes = surplus.house_includes_ev
        assert_eq(leftover_w(3000, 1000, 500), 2500, "small EV, house contains it: add back")
        assert_eq(leftover_w(800, 3500, 3000), 300, "house includes 3 kW car: solar−house+EV")
        assert_eq(
            leftover_w(800, 500, 3000),
            300,
            "house 500 W with 3 kW EV: do not add EV (would be 3300 W)",
        )
        assert_true(leftover_w(800, 500, 3000) != 800 - 500 + 3000, "old always-add formula is wrong here")
        assert_eq(
            leftover_w(3800, 3500, 12000),
            300,
            "Controller still counting an unplugged 12 kW car: do not add it",
        )
        assert_eq(leftover_w(1000, 5000, 0), -4000, "do not abs leftover; deficit stays negative")
        assert_eq(includes(3500, 3000), True, "house 3500 contains 3 kW")
        assert_eq(includes(500, 3000), False, "house 500 does not contain 3 kW")
        margin = max(1000, 3000 // 5)
        assert_eq(includes(3000 - margin, 3000), True, "at the include threshold")
        assert_eq(includes(3000 - margin - 1, 3000), False, "one watt under threshold")
        assert_eq(includes(100, 0), True, "no EV: treat as include")
        assert_eq(surplus.watts(-3.2, True), 3200, "kW magnitude")
        assert_eq(surplus.watts(-1500, False), 1500, "inverted CT watts")
        assert_eq(surplus.watts("unknown", False, 0), 0, "unusable → default")

    def test_keep_take_uses_leftover_pool():
        leftover_w = surplus.leftover_w
        pool = leftover_w(5000, 6000, 3000)
        assert_eq(pool, 2000, "house includes 3 kW keep: leftover still 2 kW")
        assert_eq(surplus.keep_take_w(3000), 3000, "keep precondition take")
        assert_eq(surplus.keep_take_w(80), 80, "keep Sentry / Complete trickle still counts")
        assert_eq(surplus.keep_take_w(None), 0, "unknown nrg")
        assert_eq(surplus.leftover_for_surplus(pool), pool, "no keep cars")
        assert_eq(
            surplus.leftover_for_surplus(pool, 80),
            1920,
            "keep 80 W trickle is subtracted from leftover for other cars",
        )
        assert_eq(
            surplus.leftover_for_surplus(pool, 3000),
            -1000,
            "3 kW keep during 2 kW leftover: all surplus used, 1 kW grid",
        )
        assert_eq(
            surplus.leftover_for_surplus(8000, 3000),
            5000,
            "keep 3 kW during 8 kW leftover: 5 kW left for surplus cars",
        )
        assert_eq(surplus.leftover_for_surplus(2000, 0), 2000, "keep not taking")
        assert_eq(
            surplus.leftover_for_surplus(2000, 1500, 1500),
            -1000,
            "two keep cars both taking 1.5 kW",
        )
        assert_eq(
            surplus.surplus_decision(False, -1000, 92, window_ok=True)["write_on"],
            False,
            "deficit after keep does not start leftover on other cars",
        )

    def test_steal_victim_worse_priority_while_other_takes():
        victim = surplus.surplus_steal_victim
        assert_eq(
            victim(B, leftover_on=True, taking=[A], lops={A: 1, B: 2}),
            True,
            "worse prio than a taking surplus charger is steal victim",
        )
        assert_eq(
            victim(A, leftover_on=True, taking=[A], lops={A: 1, B: 2}),
            False,
            "the taking car is not a victim",
        )
        assert_eq(
            victim(B, leftover_on=True, taking=[], lops={A: 1, B: 2}),
            False,
            "nobody taking: not a victim",
        )
        assert_eq(
            victim(B, leftover_on=False, taking=[A], lops={A: 1, B: 2}),
            False,
            "leftover off: not a victim",
        )
        assert_eq(
            victim(A, leftover_on=True, taking=[B], lops={A: 1, B: 2}),
            False,
            "better prio than the taker can still keep",
        )
        assert_eq(
            victim(B, leftover_on=True, taking=[A], lops={A: 1, B: 1}),
            False,
            "equal HA leftover priority is out of scope",
        )
        one_phase = MIN_A * VOLTS
        both = leftover_kw_args(
            lops={A: 1, B: 2},
            plugged={A: True, B: True},
        )
        idle_b = surplus.surplus_allocation_plan(
            [A, B],
            leftover_w=one_phase,
            take_w={A: one_phase, B: 0},
            states={A: "Charging", B: "Complete"},
            **both,
        )
        assert_eq(idle_b["allocations"].get(A), one_phase, "A keeps the 1-phase leftover")
        assert_true(B not in idle_b["allocations"], "idle Complete B is not leftover-offered")
        live_b = surplus.surplus_allocation_plan(
            [A, B],
            leftover_w=12000,
            take_w={A: 5000, B: 5000},
            states={A: "Charging", B: "Complete"},
            **both,
        )
        assert_eq(live_b["allocations"].get(B), 7000, "live Complete ≥400 W is leftover-eligible")
        assert_eq(live_b["taking"], [A, B], "live Complete is taking")

    def test_leftover_ev_uses_controller_not_instant_nrg():
        ev = surplus.effective_ev_w
        assert_eq(ev(5000, 3680), 5000, "16 A nrg during 5 kW leftover keeps Controller")
        assert_eq(ev(5000, 4830), 5000, "21 A nrg does not raise leftover EV either")
        assert_eq(ev(12000, 3000), 12000, "instant nrg must not pull leftover EV down")
        assert_eq(ev(3000, 3000), 3000, "agree")
        assert_eq(ev(0, 3000, controller_usable=False), 3000, "unknown Controller uses nrg")
        assert_eq(ev(3000, None), 3000, "no nrg keeps Controller")
        assert_eq(ev(3000, 0), 3000, "zero nrg is ignored while Controller is usable")
        assert_eq(ev(0, None, controller_usable=False), 0, "nothing charging")
        leftover = surplus.leftover_w(8000, 8000, ev(5000, 3680))
        assert_eq(leftover, 5000, "5 kW leftover stays 5 kW while Tesla is still at 16 A")
        lot, psm, amp = surplus.budget(leftover, MIN_A, MAX_A, GROUP_LOT, VOLTS, MAX_1P)
        assert_eq((lot, psm, amp), (50, 1, 21), "5 kW publishes 21 A with lot at the 50 A fuse cap")
        lot21, psm21, amp21 = surplus.budget(
            surplus.leftover_w(8000, 8000, ev(5000, 4830)),
            MIN_A, MAX_A, GROUP_LOT, VOLTS, MAX_1P,
        )
        assert_eq((lot21, psm21, amp21), (lot, psm, amp), "amp does not follow nrg 16 A ↔ 21 A")
        assert_eq(
            surplus.leftover_w(3800, 3500, ev(12000, 3000)),
            300,
            "12 kW Controller vs house 3500: reject the lag",
        )
        assert_eq(surplus.leftover_w(3800, 3500, 12000), 300, "12 kW Controller vs house 3500: reject the lag")
        assert_eq(surplus.leftover_w(3800, 3500, 3000), 3300, "3 kW Controller with house containing it")

    def test_nrg_reschedule_only_on_take_or_idle_band():
        resched = surplus.nrg_should_reschedule
        assert_eq(resched(3680, 4830), False, "watt-by-watt while taking does not retune amp")
        assert_eq(resched(4830, 3680), False, "21 A → 16 A nrg does not retune leftover amp")
        assert_eq(resched(50, 150), True, "crossing 100 W take starts leftover has-started")
        assert_eq(resched(150, 50), True, "dropping under 100 W ends has-started")
        assert_eq(resched(500, 350), True, "crossing 400 W idle-Complete band")
        assert_eq(resched(350, 200), False, "already idle Complete, still above 100 W")
        assert_eq(resched(80, 50), False, "already under take and idle band")
        assert_eq(resched(None, 3000), True, "first nrg while taking must apply")
        assert_eq(resched(None, 50), False, "first nrg still under take and idle band")
        assert_eq(resched(3000, 3000), False, "same watts")

    def test_surplus_held_w_kotiakku_cadence():
        hold = surplus.surplus_held_w
        assert_eq(surplus.SURPLUS_SETPOINT_S, 300, "leftover amp follows 5 min Kotiakku cadence")
        first = hold(6900, None, None, 0)
        assert_eq(first, (6900, 0.0), "first sample is live leftover")
        assert_eq(
            hold(0, 6900, 0, 10),
            (6900, 0.0),
            "house tick 10 s later does not retune leftover amp",
        )
        assert_eq(
            hold(0, 6900, 0, 299, allow_sample=True),
            (6900, 0.0),
            "1 s under cadence still holds even with a Kotiakku sample",
        )
        assert_eq(
            hold(0, 6900, 0, 300),
            (6900, 0.0),
            "cadence elapsed still holds without a Kotiakku sample",
        )
        assert_eq(
            hold(5000, 6900, 0, 300, allow_sample=True),
            (5000, 300.0),
            "Kotiakku sample after 5 min takes live leftover",
        )
        assert_eq(
            hold(0, 6900, 0, 10, refresh=True),
            (0, 10.0),
            "session start / 6 A hold expiry takes live leftover",
        )
        assert_eq(
            hold(1234, 6900, 0, 10, cadence_s=0),
            (1234, 10.0),
            "cadence 0 is always live",
        )
        collapsed = surplus.surplus_decision(True, 0, 96, window_ok=True)
        assert_true(collapsed["use_floor_budget"], "live 0 W would bounce to 6 A")
        held_dec = surplus.surplus_decision(True, 6900, 96, window_ok=True)
        assert_true(
            held_dec["write_on"] and not held_dec["arm_floor"],
            "held 6900 W keeps tracking leftover, not the 6 A floor",
        )
        lot, psm, amp = surplus.budget(6900, MIN_A, MAX_A, GROUP_LOT, VOLTS, MAX_1P)
        assert_eq((lot, psm, amp), (50, 1, 30), "held 6900 W is 30 A, lot 50")
        floor_lot, floor_psm, floor_amp = surplus.budget(
            0, MIN_A, MAX_A, GROUP_LOT, VOLTS, MAX_1P
        )
        assert_eq(floor_amp, 6, "live 0 W would publish the 6 A floor")
        sawtooth = [0, 2300, 6900, 1380, 7360, 0]
        w, ts = 6900, 0.0
        for i, live in enumerate(sawtooth, start=1):
            w, ts = hold(live, w, ts, i * 10)
            assert_eq(
                (w, ts),
                (6900, 0.0),
                "sawtooth house leftover %s W at +%ss still holds 30 A watts" % (live, i * 10),
            )
        samples = surplus.surplus_sensor_samples
        solar, ctrl, house, soc = (
            "sensor.solar",
            "sensor.controller",
            "sensor.house",
            "sensor.soc",
        )
        kotiakku = (soc, solar, house)
        assert_true(samples(solar, *kotiakku), "Kotiakku solar may resample leftover amp")
        assert_true(samples(house, *kotiakku), "Kotiakku house may resample leftover amp")
        assert_true(samples(soc, *kotiakku), "Kotiakku SoC may resample leftover amp")
        assert_true(
            not samples(ctrl, *kotiakku),
            "fast Controller must not resample leftover amp",
        )
        assert_true(not samples(house), "no Kotiakku ids does not sample house")
        early = hold(0, 6900, 0, 30, allow_sample=True)
        assert_eq(early, (6900, 0.0), "Kotiakku report inside 5 min does not move amp")
        assert_eq(
            hold(0, early[0], early[1], 300),
            (6900, 0.0),
            "that early report must not arm a later Controller tick",
        )

    def test_decision_start_hold_stop_and_hysteresis():
        decide = surplus.surplus_decision
        start = decide(False, 2000, 92, window_ok=True)
        assert_true(start["write_on"] and not start["arm_floor"], "start at 2000 W / 92%")
        assert_true(not decide(False, 1999, 92, window_ok=True)["write_on"], "no start under 2000 W")
        assert_true(not decide(False, 3000, 91, window_ok=True)["write_on"], "no start under 92%")
        track = decide(True, 1500, 96, window_ok=True)
        assert_true(track["write_on"] and not track["arm_floor"], "1500 W tracks leftover")
        low = decide(True, 300, 96, window_ok=True)
        assert_true(low["write_on"] and low["use_floor_budget"], "300 W is 6 A hold, not off yet")
        stopped = decide(True, 300, 96, window_ok=True, floor_expired=True)
        assert_true(stopped["write_off"] and not stopped["write_on"], "hold expired → off")
        chatter = decide(True, 1500, 96, window_ok=True, hold_active=True, hold_exit_w=2000)
        assert_true(chatter["use_floor_budget"], "1500 W does not cancel an active hold")
        leave = decide(True, 2100, 96, window_ok=True, hold_active=True, hold_exit_w=2000)
        assert_true(leave["write_on"] and not leave["arm_floor"], "2000 W start leftover cancels hold")
        soc_hold = decide(True, 4000, 89, window_ok=True)
        assert_true(soc_hold["use_floor_budget"], "SoC 89% is 6 A hold")
        dead = decide(True, 4000, 96, window_ok=False)
        assert_true(dead["use_floor_budget"], "unusable Kotiakku is 6 A hold")
        assert_true(not decide(False, 4000, 96, window_ok=False)["write_on"], "cannot start unusable")
        idle = decide(False, 300, 96, window_ok=True)
        assert_true(not idle["write_on"] and not idle["write_off"], "300 W does not start a new session")
        at_hold = decide(True, 1000, 96, window_ok=True)
        assert_true(at_hold["write_on"] and not at_hold["arm_floor"], "exactly hold_min_w still tracks leftover")
        under_hold = decide(True, 999, 96, window_ok=True)
        assert_true(under_hold["use_floor_budget"], "1 W under hold_min_w is 6 A hold")
        exit_exact = decide(True, 2000, 96, window_ok=True, hold_active=True, hold_exit_w=2000)
        assert_true(exit_exact["write_on"] and not exit_exact["arm_floor"], "leftover == hold_exit_w cancels hold")
        exit_under = decide(True, 1999, 96, window_ok=True, hold_active=True, hold_exit_w=2000)
        assert_true(exit_under["use_floor_budget"], "1 W under hold_exit_w stays in hold")
        start_exact = decide(False, 2000, 92, window_ok=True)
        assert_true(start_exact["write_on"], "leftover == start_min_w starts")
        hyst_edge = decide(True, 4000, 90, window_ok=True)
        assert_true(hyst_edge["write_on"] and not hyst_edge["arm_floor"], "SoC == soc_on-hyst is not low hold")
        assert_true(not decide(False, 3000, 90, window_ok=True)["write_on"], "SoC 90 cannot start")
        default_exit = decide(True, 1500, 96, window_ok=True, hold_active=True)
        assert_true(
            default_exit["write_on"] and not default_exit["arm_floor"],
            "hold_exit_w None uses hold_min_w: 1500 W already cancels",
        )
        raised_exit = decide(True, 999, 96, window_ok=True, hold_active=True, hold_exit_w=500)
        assert_true(raised_exit["use_floor_budget"], "hold_exit_w below hold_min_w is raised")
        still_on = decide(True, 4000, 96, window_ok=True, floor_expired=True)
        assert_true(still_on["write_on"] and not still_on["write_off"], "expired hold does not stop while leftover is healthy")
        sensors_dead = decide(True, 5000, 96, window_ok=False, hold_active=True, hold_exit_w=2000)
        assert_true(sensors_dead["use_floor_budget"], "unusable Kotiakku stays in hold even at 5 kW")
        recovered_ok = decide(True, 4000, 96, window_ok=True, hold_active=True, hold_exit_w=2000)
        assert_true(recovered_ok["write_on"] and not recovered_ok["arm_floor"], "usable + leftover ≥ exit cancels hold")
        deficit = decide(True, -200, 96, window_ok=True)
        assert_true(deficit["use_floor_budget"], "negative leftover is a low hold, not abs'd")

    def test_three_kw_is_13a_one_phase_not_a_hold():
        lot, psm, amp = surplus.budget(3000, MIN_A, MAX_A, GROUP_LOT, VOLTS, MAX_1P)
        assert_eq((psm, amp), (1, 13), "3 kW is 1-phase 13 A")
        lot, psm, amp = surplus.budget(300, MIN_A, MAX_A, GROUP_LOT, VOLTS, MAX_1P)
        assert_eq((psm, amp), (1, 6), "300 W budgets 6 A floor, not 13 A")
        lot, psm, amp = surplus.budget(0, MIN_A, MAX_A, GROUP_LOT, VOLTS, MAX_1P)
        assert_eq((psm, amp), (1, 6), "0 W floor is 6 A 1-phase")
        lot, psm, amp = surplus.budget(4140, MIN_A, MAX_A, GROUP_LOT, VOLTS, MAX_1P)
        assert_eq((psm, amp), (1, 18), "4140 W first start stays 1-phase 18 A")
        stay3 = surplus.budget(4140, MIN_A, MAX_A, GROUP_LOT, VOLTS, MAX_1P, last_psm=2)
        assert_eq((stay3[1], stay3[2]), (2, 6), "active 3-phase keeps 6 A at the 3-phase floor")
        lot, psm, amp = surplus.budget(2500, MIN_A, MAX_A, GROUP_LOT, VOLTS, MAX_1P)
        assert_eq((psm, amp), (1, 10), "2500 W is 1-phase 10 A")
        hold3 = surplus.budget(3000, MIN_A, MAX_A, GROUP_LOT, VOLTS, MAX_1P, force_psm=2)
        assert_eq((hold3[1], hold3[2]), (2, 6), "forced 3-phase below 4140 W stays 6 A")
        lot, psm, amp = surplus.budget(4139, MIN_A, MAX_A, GROUP_LOT, VOLTS, MAX_1P)
        assert_eq((psm, amp), (1, 17), "1 W under 4140 W stays 1-phase")
        lot, psm, amp = surplus.budget(7589, MIN_A, MAX_A, GROUP_LOT, VOLTS, MAX_1P)
        assert_eq((psm, amp), (1, 32), "just under 3-phase-better stays 1-phase 32 A")
        lot, psm, amp = surplus.budget(7590, MIN_A, MAX_A, GROUP_LOT, VOLTS, MAX_1P)
        assert_eq((psm, amp), (2, 11), "7590 W 3-phase delivers more than 32 A 1-phase")
        lot, psm, amp = surplus.budget(-100, MIN_A, MAX_A, GROUP_LOT, VOLTS, MAX_1P)
        assert_eq((psm, amp), (1, 6), "negative leftover still floors at 6 A")
        lot, psm, amp = surplus.budget(100000, MIN_A, MAX_A, GROUP_LOT, VOLTS, MAX_1P)
        assert_eq((lot, amp), (50, 32), "group_lot then max_amp clip a huge leftover")
        lot, psm, amp = surplus.budget(3000, MIN_A, MAX_A, GROUP_LOT, VOLTS, MAX_1P)
        assert_eq(lot, 50, "3 kW leftover keeps group lot at the fuse cap")
        assert_eq((psm, amp), (1, 13), "3 kW is 1-phase 13 A")
        five = surplus.budget(5000, MIN_A, MAX_A, GROUP_LOT, VOLTS, MAX_1P)
        assert_eq(five, (50, 1, 21), "5 kW is 21 A leftover amp, lot 50")
        lot, psm, amp = surplus.budget(8000, MIN_A, MAX_A, GROUP_LOT, VOLTS, MAX_1P, force_psm="x")
        assert_eq((psm, amp), (2, 11), "bad force_psm is auto")
        assert_eq(surplus.min_charge_w(0, MIN_A, VOLTS, MAX_1P), 1380, "0 W floor is 1-phase 6 A")
        assert_eq(surplus.min_charge_w(4139, MIN_A, VOLTS, MAX_1P), 1380, "just under 3-phase min")
        assert_eq(surplus.min_charge_w(4140, MIN_A, VOLTS, MAX_1P), 1380, "4140 W first start is still 1-phase 6 A")
        assert_eq(surplus.min_charge_w(7590, MIN_A, VOLTS, MAX_1P), 4140, "3-phase better than 32 A 1-phase uses 6 A × 3")

    def test_unplugged_first_does_not_keep_3kw_steal():
        alloc = surplus.surplus_allocations
        both = leftover_kw_args()
        assert_eq(
            alloc([A, B], leftover_w=12000, take_w={A: 10000, B: 0}, states={A: "Charging", B: "Charging"}, **both),
            {A: 9000, B: 3000},
            "12 kW high taking 10 kW → 9+3 steal",
        )
        assert_eq(
            alloc([A, B], leftover_w=8000, take_w={A: 7500, B: 0}, states={A: "Charging", B: "Charging"}, **both),
            {A: 7500},
            "500 W remainder is dead: do not start the second car",
        )
        assert_eq(
            alloc(
                [A, B], leftover_w=8000, split_hold=True,
                take_w={A: 7500, B: 0}, states={A: "Charging", B: "Charging"}, **both,
            ),
            {A: 5000, B: 3000},
            "grace: dead zone keeps 3 kW while split_hold",
        )
        assert_eq(
            alloc(
                [A, B], leftover_w=8000, split_hold=True, split_expired=True,
                take_w={A: 7500, B: 0}, states={A: "Charging", B: "Charging"}, **both,
            ),
            {A: 7500},
            "after 15 min grace, drop the second car",
        )
        idle_first_take = leftover_kw_args(
            plugged={A: False, B: True},
            take_w={A: 0, B: 3000},
            states={A: "Idle", B: "Charging"},
        )
        assert_eq(
            alloc(
                [A, B], leftover_w=300, split_hold=True,
                **idle_first_take,
            ),
            {A: 300, B: 300},
            "Idle first + stale steal: 300 W leftover, not 3 kW; Idle still armed",
        )
        idle_first_plan = surplus.surplus_allocation_plan(
            [A, B], leftover_w=8000, split_hold=True, **idle_first_take,
        )
        assert_eq(
            idle_first_plan["allocations"],
            {A: 8000, B: 8000},
            "Idle first is armed; next taking car gets leftover as first, not 3 kW",
        )
        assert_eq(idle_first_plan["taking"], [B], "split follows the taking car, not Idle-armed")
        assert_eq(idle_first_plan["arm_split_hold"], False, "one taking car does not arm steal grace")
        assert_eq(idle_first_plan["overdraw"], False, "after the wait, Idle arm is not a lot share")
        assert_eq(
            idle_first_plan["lot_allocations"],
            {B: 8000},
            "group lot uses the taking share, not the Idle arm",
        )
        assert_eq(surplus.OFFER_WAIT_S, 15, "wait 15 s after offering leftover")
        waiting = surplus.surplus_allocation_plan(
            [A, B], leftover_w=8000, split_hold=True, offer_pending={A}, **idle_first_take,
        )
        assert_eq(
            waiting["allocations"],
            {A: 8000, B: 8000},
            "first 15 s: keep leftover on the offered car and do not cut the taking car",
        )
        assert_true(B in waiting["allocations"], "taking car stays on during the offer wait")
        assert_eq(waiting["taking"], [B], "pending offer is not a taking split")
        assert_eq(waiting["overdraw"], True, "15 s wait allows leftover on both (over-draw)")
        assert_eq(
            waiting["lot_allocations"],
            {A: 8000, B: 8000},
            "pending high is a group-lot share until the wait expires",
        )
        assert_eq(
            surplus.group_lot_for_allocations(
                11, waiting["lot_allocations"],
                min_amp=6, max_amp=32, group_lot=50, volts=230, max_1phase_amp=32,
                overdraw=True,
            ),
            22,
            "over-draw raises lot so both leftover amps fit",
        )
        assert_eq(
            alloc(
                [A, B], leftover_w=8000,
                take_w={A: 0, B: 0}, states={A: "Complete", B: "Charging"}, **both,
            ),
            {B: 8000},
            "idle Complete first is skipped",
        )
        wait = alloc(
            [A, B], leftover_w=2500, split_hold=True,
            take_w={A: 0, B: 2500}, states={A: "WaitCar", B: "Charging"}, **both,
        )
        assert_eq(wait.get(B), 2500, "WaitCar taking 0 W does not mint a 3 kW steal from 2500 W")
        assert_eq(wait.get(A), 2500, "WaitCar still gets leftover MQTT so it can start")
        assert_true(SPLIT_MIN not in wait.values(), "2500 W leftover never allocates exactly 3 kW steal")
        equal = leftover_kw_args(lops={A: 50, B: 50}, plugged={A: False, B: True})
        assert_eq(
            alloc([A, B], leftover_w=8000, **equal),
            {A: 8000, B: 8000},
            "equal priority: unplugged charger is offered leftover",
        )
        three = leftover_kw_args(lops={A: 1, B: 2, C: 3}, plugged={A: True, B: False, C: True})
        three_plan = surplus.surplus_allocation_plan(
            [A, B, C], leftover_w=12000,
            take_w={A: 10000, B: 0, C: 0},
            states={A: "Charging", B: "Idle", C: "Charging"},
            **three,
        )
        assert_eq(
            three_plan["allocations"],
            {A: 9000, B: 3000},
            "Idle middle is next in priority: steal 9+3 to it, not skip to C",
        )
        assert_eq(three_plan["taking"], [A], "Idle steal share is not a taking car yet")
        assert_eq(three_plan["arm_split_hold"], False, "steal to a non-taking car does not arm grace")
        assert_true(C not in three_plan["allocations"], "third waits until leftover is unused")
        idle_lead = leftover_kw_args(lops={A: 1, B: 2, C: 3})
        lead_plan = surplus.surplus_allocation_plan(
            [A, B, C], leftover_w=12000,
            take_w={A: 0, B: 10000, C: 0},
            states={A: "Idle", B: "Charging", C: "Charging"},
            **idle_lead,
        )
        assert_eq(
            lead_plan["allocations"],
            {A: 12000, B: 9000, C: 3000},
            "Idle first is armed; steal runs on the taking remainder",
        )
        assert_eq(
            lead_plan["lot_allocations"],
            {B: 9000, C: 3000},
            "Idle-armed leftover watts are not a group-lot share",
        )
        assert_eq(lead_plan["taking"], [B], "only the car actually taking is in split")
        assert_eq(
            surplus.group_lot_for_allocations(
                17, lead_plan["lot_allocations"],
                min_amp=6, max_amp=32, group_lot=50, volts=230, max_1phase_amp=32,
            ),
            26,
            "9+3 steal raises lot to 13 A + 13 A without the Idle arm",
        )
        wait_mid = surplus.surplus_allocation_plan(
            [A, B, C], leftover_w=18000,
            take_w={A: 5000, B: 0, C: 0},
            states={A: "Charging", B: "Idle", C: "Charging"},
            offer_pending={B},
            **three,
        )
        assert_eq(
            wait_mid["allocations"],
            {A: 5000, B: 13000},
            "15 s after offering remainder: do not start the third car yet",
        )
        assert_true(C not in wait_mid["allocations"], "third waits for the offered next car")
        wait_lead = surplus.surplus_allocation_plan(
            [A, B, C], leftover_w=12000,
            take_w={A: 0, B: 10000, C: 0},
            states={A: "Idle", B: "Charging", C: "Charging"},
            offer_pending={A},
            **idle_lead,
        )
        assert_eq(
            wait_lead["allocations"],
            {A: 12000, B: 12000},
            "15 s after offering high: steal waits; taking leftover stays (over-draw)",
        )
        assert_true(B in wait_lead["allocations"], "do not frc=1 a taking car during the wait")
        assert_true(C not in wait_lead["allocations"], "steal and the third car wait 15 s")
        assert_eq(wait_lead["overdraw"], True, "pending high plus taking leftover is over-draw")
        assert_eq(
            wait_lead["lot_allocations"],
            {A: 12000, B: 12000},
            "pending high is a group-lot share with taking leftover during the wait",
        )
        steal_pending = surplus.surplus_allocation_plan(
            [A, B], leftover_w=12000,
            take_w={A: 10000, B: 0},
            states={A: "Charging", B: "Idle"},
            offer_pending={B},
            **both,
        )
        assert_eq(
            steal_pending["allocations"].get(A),
            10000,
            "pending next car: do not cut taking high to mint 3 kW",
        )
        assert_true(
            SPLIT_MIN not in steal_pending["allocations"].values(),
            "no 9+3 steal while the next offer is still pending",
        )
        steal_hold = surplus.surplus_allocation_plan(
            [A, B], leftover_w=12000, split_hold=True,
            take_w={A: 0, B: 3000}, states={A: "Idle", B: "Charging"},
            **both,
        )
        assert_true(A in steal_hold["allocations"], "steal/next leftover: Idle high stays on leftover MQTT")
        assert_true(B in steal_hold["allocations"], "lower still has leftover")
        assert_eq(
            steal_hold["lot_allocations"],
            {B: 12000},
            "Idle high arm is not a group-lot share",
        )
        keep = surplus.surplus_higher_keep_on
        assert_eq(keep(A, {B: 8000}, {A: 1, B: 50}), True, "worse-priority leftover: do not frc=1 high")
        assert_eq(keep(B, {A: 8000}, {A: 1, B: 50}), False, "better-priority leftover: low may be off")
        assert_eq(keep(A, {A: 9000, B: 3000}, {A: 1, B: 50}), False, "already allocated")
        assert_eq(
            keep(A, {B: 8000}, {A: 1, B: 50}, states={A: "Complete", B: "Charging"}),
            False,
            "idle Complete high stays skipped",
        )
        assert_eq(keep(A, {}, {A: 1, B: 50}), False, "nobody allocated")
        assert_eq(
            alloc(
                [A, B, C], leftover_w=8000,
                take_w={A: 0, B: 0, C: 0},
                states={A: "Complete", B: "Charging", C: "Charging"},
                **leftover_kw_args(lops={A: 1, B: 50, C: 50}),
            ),
            {B: 8000, C: 8000},
            "idle Complete dropped from ranks: remaining equal-priority cars share leftover",
        )
        assert_eq(alloc([A], leftover_w=8000, **both), {A: 8000}, "single charger gets leftover")
        none = surplus.surplus_allocation_plan(
            [A, B], leftover_w=8000,
            **leftover_kw_args(
                plugged={A: False, B: False},
                take_w={A: 0, B: 0},
                states={A: "Idle", B: "Idle"},
            ),
        )
        assert_eq(
            none["allocations"],
            {A: 8000, B: 8000},
            "nobody taking → leftover MQTT still arms both",
        )
        assert_eq(none["taking"], [], "Idle arms are not a split session")

    def test_reported_300w_surplus_does_not_publish_13a():
        invented = 800 - 500 + 3000
        assert_eq(invented, 3300, "always-add-EV leftover that would look like 3.3 kW")
        leftover = surplus.leftover_w(800, 500, 3000)
        assert_eq(leftover, 300, "house-missing-car leftover is 300 W")
        dec, cmds = mqtt_for(
            leftover,
            True,
            serials=[A, B],
            plugged={A: False, B: True},
            take_w={A: 0, B: 3000},
            states={A: "Idle", B: "Charging"},
            split_hold=True,
        )
        assert_true(dec["write_on"] and dec["use_floor_budget"], "session continues as 6 A hold")
        assert_eq(cmds[B]["amp"], 6, "publishes 6 A, not 13 A")
        assert_eq(cmds[B]["psm"], 1, "1-phase floor")
        assert_eq(cmds[A]["amp"], 6, "Idle first is armed at 6 A floor, not 13 A")
        # Same sensors, old leftover 3300 W would have tracked 14 A:
        old_dec, old_cmds = mqtt_for(
            3300,
            True,
            serials=[A, B],
            plugged={A: False, B: True},
            take_w={A: 0, B: 3000},
            states={A: "Idle", B: "Charging"},
            split_hold=True,
        )
        assert_true(not old_dec["use_floor_budget"], "3300 W is above the 1000 W hold")
        assert_eq(old_cmds[B]["amp"], 14, "3300 W would be 14 A — why 3 kW stuck if leftover is wrong")
        stopped, off_cmds = mqtt_for(leftover, True, floor_expired=True, plugged={A: False, B: True})
        assert_true(stopped["write_off"], "after 15 min hold, stop")
        assert_eq(off_cmds, {}, "no surplus MQTT after stop")
        # House includes the 3 kW car: leftover also 300 W.
        assert_eq(surplus.leftover_w(800, 3500, 3000), 300, "include-EV path is also 300 W")
        # Recovered 1500 W during an active hold stays 6 A, does not jump to 13 A.
        mid, mid_cmds = mqtt_for(
            1500, True, hold_active=True, plugged={A: False, B: True}, serials=[A, B],
            take_w={A: 0, B: 1500}, states={A: "Idle", B: "Charging"},
        )
        assert_true(mid["use_floor_budget"], "hold hysteresis")
        assert_eq(mid_cmds[B]["amp"], 6, "still 6 A at 1500 W while hold is active")

    def test_group_lot_stays_at_fuse_cap():
        setp = surplus.group_surplus_setpoint
        assert_eq(setp(10, 1, 10, n_full=0, group_lot=50), (50, 1, 10), "pure surplus keeps group lot 50")
        assert_eq(setp(10, 1, 10, n_full=1, group_lot=50), (50, 1, 10), "mixed: keep group 50, leftover amp")
        assert_eq(setp(25, 2, 25, n_full=1, group_lot=50), (50, 2, 25), "do not cap leftover amp for 32 A")
        assert_eq(
            surplus.group_lot_for_allocations(
                17, {A: 9000, B: 3000}, min_amp=6, max_amp=32, group_lot=50, volts=230, max_1phase_amp=32
            ),
            26,
            "helper can still raise a leftover lot; MQTT does not use it",
        )
        assert_eq(
            surplus.group_lot_for_allocations(
                17, {A: 12000, B: 12000}, min_amp=6, max_amp=32, group_lot=50, volts=230, max_1phase_amp=32
            ),
            17,
            "equal leftover does not sum amps",
        )
        pub = surplus.surplus_phase_budget(5000, MIN_A, MAX_A, GROUP_LOT, VOLTS, MAX_1P)
        assert_eq((pub["lot"], pub["psm"], pub["amp"]), (50, 1, 21), "phase budget lot is fuse cap")

    def test_phase_hold_tracks_amp_on_held_phase():
        args = (MIN_A, MAX_A, GROUP_LOT, VOLTS, MAX_1P)
        first = surplus.surplus_phase_budget(8000, *args)
        assert_eq((first["psm"], first["amp"], first["arm_phase"]), (2, 11, False), "first start 3-phase")
        up = surplus.surplus_phase_budget(8000, *args, last_psm=1)
        assert_eq(up["psm"], 1, "1→3 waits")
        assert_eq(up["amp"], 32, "held 1-phase amp is leftover on 1-phase, not 11 A")
        down = surplus.surplus_phase_budget(3000, *args, last_psm=2)
        assert_eq((down["psm"], down["amp"]), (2, 6), "3→1 waits at 6 A 3-phase")
        assert_eq(down["wanted_psm"], 1, "leftover wants 1-phase")
        done = surplus.surplus_phase_budget(3000, *args, last_psm=2, hold_expired=True)
        assert_eq((done["psm"], done["amp"]), (1, 13), "after hold: 13 A 1-phase")
        floor = surplus.surplus_phase_budget(0, *args, last_psm=2)
        assert_eq((floor["psm"], floor["amp"]), (2, 6), "6 A floor stays 3-phase during hold")
        stay = surplus.surplus_phase_budget(6000, *args, last_psm=2)
        assert_eq((stay["psm"], stay["amp"], stay["arm_phase"]), (2, 8, False), "6 kW keeps active 3-phase")
        assert_eq(stay["wanted_psm"], 2, "3-phase can still offer 6 kW")
        first6 = surplus.surplus_phase_budget(6000, *args)
        assert_eq((first6["psm"], first6["amp"], first6["arm_phase"]), (1, 26, False), "first start 6 kW is 1-phase")
        keep1 = surplus.surplus_phase_budget(5000, *args, last_psm=1)
        assert_eq((keep1["psm"], keep1["amp"], keep1["arm_phase"]), (1, 21, False), "5 kW keeps active 1-phase")
        assert_eq(keep1["wanted_psm"], 1, "1-phase can still offer 5 kW")
        cap16 = surplus.surplus_phase_budget(5000, MIN_A, MAX_A, GROUP_LOT, VOLTS, 16, last_psm=1)
        assert_eq(
            (cap16["psm"], cap16["amp"], cap16["wanted_psm"], cap16["arm_phase"]),
            (1, 16, 2, True),
            "16 A 1-phase cap wants 3-phase at 5 kW",
        )
        still3 = surplus.surplus_phase_budget(5000, MIN_A, MAX_A, GROUP_LOT, VOLTS, 16, last_psm=2)
        assert_eq(
            (still3["psm"], still3["wanted_psm"], still3["arm_phase"]),
            (2, 2, False),
            "max 1-phase amp does not force 3→1",
        )
        pref3 = surplus.surplus_phase_budget(6000, *args, preferred_psm=2)
        assert_eq((pref3["psm"], pref3["amp"], pref3["arm_phase"]), (2, 8, False), "preferred 3-phase first start 6 kW")
        keep_pref = surplus.surplus_phase_budget(6000, *args, last_psm=1, preferred_psm=2)
        assert_eq(
            (keep_pref["psm"], keep_pref["amp"], keep_pref["arm_phase"]),
            (1, 26, False),
            "preferred start does not switch an active 1-phase",
        )
        keep3_pref1 = surplus.surplus_phase_budget(6000, *args, last_psm=2, preferred_psm=1)
        assert_eq(
            (keep3_pref1["psm"], keep3_pref1["amp"], keep3_pref1["arm_phase"]),
            (2, 8, False),
            "preferred start does not drop an active 3-phase",
        )
        must3 = surplus.surplus_phase_budget(8000, *args, preferred_psm=1)
        assert_eq((must3["psm"], must3["amp"]), (2, 11), "8 kW first start is 3-phase even if preferred 1-phase")
        below = surplus.surplus_phase_budget(3000, *args, preferred_psm=2)
        assert_eq((below["psm"], below["amp"]), (1, 13), "below 3-phase floor first start is 1-phase")

    def test_car_states_plugged_charging_finished():
        assert_eq(surplus.car_plugged("Idle"), False, "Idle is unplugged")
        assert_eq(surplus.car_plugged("1"), False, "numeric idle")
        assert_eq(surplus.car_plugged("Charging"), True, "Charging")
        assert_eq(surplus.car_plugged("Complete"), True, "Complete is still plugged")
        assert_eq(surplus.car_plugged("WaitCar"), True, "WaitCar is plugged")
        assert_eq(surplus.car_finished("Complete"), True, "Complete finished")
        assert_eq(surplus.car_charging("WaitCar"), False, "WaitCar not charging")
        assert_eq(surplus.charger_take_w("Idle", 3000, 8000, 22080), 0, "unplugged take 0")
        assert_eq(surplus.charger_take_w("Complete", 8000, 8000, 22080), 8000, "live Complete take")
        assert_eq(surplus.charger_take_w("Complete", 350, 8000, 22080), 0, "idle Complete take 0")
        assert_eq(surplus.charger_take_w("Complete", None, 8000, 22080), 0, "unknown Complete take 0")
        assert_eq(surplus.charger_take_w("WaitCar", None, 8000, 22080), 0, "WaitCar take 0")
        assert_eq(surplus.charger_take_w("Charging", None, 8000, 22080), 8000, "unknown charging wants leftover")
        assert_eq(surplus.charger_take_w("Charging", 3000, 300, 22080), 300, "take capped by leftover")

    def test_enough_solar_and_offsun_hours():
        assert_eq(surplus.enough_solar(None, 40), False, "unknown is not enough")
        assert_eq(surplus.enough_solar(40, 40), True, "at threshold")
        assert_eq(surplus.enough_solar(50, 0), False, "threshold 0 disables")
        assert_eq(surplus.upcoming_solar_kwh(8, 50), 50, "max today vs tomorrow")
        assert_eq(surplus.energy_kwh("50000", "Wh"), 50.0, "Wh → kWh")
        assert_eq(surplus.energy_kwh("4000", "W"), None, "power is not energy")
        result = {"raw_windows": [{"start": 3000, "end": 4000}]}
        full = planner.charger_full_power
        leftover = planner.charger_surplus
        assert_eq(full("SolarPriority", result, 3500, enough_solar=True), False, "skip 22 kW")
        assert_eq(leftover("Force off", result, 3500), False, "Force off never leftover")
        assert_eq(leftover("Force off", result, 0), False, "Force off never leftover outside a window")
        assert_eq(
            leftover("SolarPriority", result, 3500, enough_solar=True),
            True,
            "enough solar leaves SolarPriority leftover",
        )
        assert_eq(
            full("SolarAndGrid", result, 3500, enough_solar=True),
            True,
            "SolarAndGrid still 22 kW when enough solar",
        )
        assert_eq(
            leftover("SolarAndGrid", result, 3500, enough_solar=True),
            False,
            "SolarAndGrid in-window is not leftover",
        )
        assert_eq(
            leftover("SolarAndGrid", result, 0),
            True,
            "SolarAndGrid leftover outside a window",
        )
        assert_eq(
            leftover("SolarPriority", result, 0, keep_min=True),
            False,
            "6 A keep after self-finish is not leftover",
        )
        assert_eq(full("Supercheap", result, 3500), True, "legacy Supercheap maps")
        assert_eq(full("Cheapest", result, 3500, enough_solar=True), False, "legacy Cheapest now skips")
        assert_eq(surplus.enough_solar(39.9, 40), False, "just under threshold")
        assert_eq(surplus.enough_solar(0, 40), False, "0 kWh is not enough")
        assert_eq(surplus.enough_solar(50, "x"), False, "bad threshold")
        assert_eq(surplus.upcoming_solar_kwh(10, None), 10, "today only")
        assert_eq(surplus.upcoming_solar_kwh(0, 0), 0, "both zero")
        assert_eq(surplus.energy_kwh("10"), 10.0, "unitless is kWh")
        assert_eq(surplus.energy_kwh("2", "MWh"), 2000.0, "MWh → kWh")
        assert_eq(surplus.energy_kwh("10", "kW"), None, "kW is power")
        assert_eq(surplus.energy_kwh("unknown", "kWh"), None, "unusable energy")

    def test_nrg_parse_and_car_state_edges():
        nrg = surplus.nrg_total_w
        sample = [230, 230, 230, 0, 10, 10, 10, 0, 2300, 2300, 2300, 6900]
        assert_eq(nrg(sample), 6900, "list index 11")
        assert_eq(nrg("[230,230,230,0,10,10,10,0,2300,2300,2300,6900]"), 6900, "JSON list")
        assert_eq(nrg('{"nrg":[230,230,230,0,10,10,10,0,2300,2300,2300,6900]}'), 6900, "JSON dict")
        assert_eq(nrg("230,230,230,0,10,10,10,0,2300,2300,2300,6900"), 6900, "comma string")
        assert_eq(nrg("1500"), 1500, "numeric sensor")
        assert_eq(nrg([1, 2, 3]), None, "short list")
        assert_eq(nrg(""), None, "empty")
        assert_eq(nrg(None), None, "missing")
        assert_eq(surplus.car_plugged("Error"), True, "Error is still plugged")
        assert_eq(surplus.car_plugged("Connected"), True, "Connected")
        assert_eq(surplus.car_plugged("Wait for car"), True, "spaces fold")
        assert_eq(surplus.car_charging("2"), True, "numeric charging")
        assert_eq(surplus.car_finished("Finished"), True, "Finished")
        assert_eq(surplus.car_finished("4"), True, "numeric complete")
        assert_eq(surplus.car_plugged(None), False, "missing state")
        assert_eq(surplus.charger_take_w("Charging", 50, 8000, 22080), 8000, "take under 100 W is not accepting: assume leftover")
        assert_eq(surplus.charger_take_w("Error", 3000, 8000, 22080), 0, "Error is plugged but not charging")
        assert_eq(surplus.charger_take_w("Charging", 5000, 0, 22080), 0, "no leftover")
        assert_eq(surplus.parse_lop("99"), 99, "lop 99")
        assert_eq(surplus.parse_lop("100"), None, "lop 100 out of range")
        assert_eq(surplus.parse_lop("1.6"), 2, "lop rounds")
        assert_eq(surplus.sensor_usable("nan"), False, "nan")
        assert_eq(surplus.sensor_usable(" none "), False, "none")
        assert_eq(surplus.effective_ev_w(3000, 12000), 3000, "higher nrg is ignored while Controller is usable")
        assert_eq(surplus.effective_ev_w(3000, -50), 3000, "negative nrg is missing")
        assert_eq(surplus.leftover_w(0, 0, 0), 0, "all zero")
        assert_eq(surplus.leftover_w(0, 1000, 0), -1000, "house-only deficit")
        margin = max(1000, 12000 // 5)
        assert_eq(surplus.house_includes_ev(12000 - margin, 12000), True, "12 kW EV include threshold")
        assert_eq(surplus.house_includes_ev(12000 - margin - 1, 12000), False, "1 W under 12 kW threshold")

    def test_want_w_and_group_lot_edges():
        want = surplus.surplus_want_w
        assert_eq(want(12000, 50), 50, "take under 100 W is not accepting")
        assert_eq(want(12000, "x"), 12000, "bad take wants leftover")
        assert_eq(want(8000, 3000, last_amp=None, last_psm=2), 8000, "unknown last amp wants leftover")
        assert_eq(
            want(4140, 4140, last_amp=6, last_psm=2),
            4140,
            "at cap but leftover cannot raise amp: stay at take",
        )
        assert_eq(
            want(6000, 5520, last_amp=8, last_psm=2),
            5520,
            "active 3-phase at 8 A cap does not raise toward 1-phase",
        )
        assert_eq(
            surplus.group_lot_for_amps(17, [], 50),
            17,
            "no amps keeps leftover lot",
        )
        assert_eq(
            surplus.group_lot_for_amps(11, [11, 11], 50),
            11,
            "equal leftover amps do not sum",
        )
        assert_eq(
            surplus.group_lot_for_amps(11, [11, 11], 50, overdraw=True),
            22,
            "offer-wait over-draw sums equal leftover amps",
        )
        assert_eq(
            surplus.group_lot_for_allocations(
                17, {A: 8000}, min_amp=6, max_amp=32, group_lot=50, volts=230, max_1phase_amp=32
            ),
            17,
            "single allocation does not raise lot",
        )
        assert_eq(
            surplus.group_surplus_setpoint(10, 1, 10, n_full=2, group_lot=50),
            (50, 1, 10),
            "two full-power chargers still keep group_lot",
        )
        assert_eq(
            surplus.group_surplus_setpoint(21, 1, 21, n_full=0, group_lot=50),
            (50, 1, 21),
            "pure surplus 21 A leftover amp still writes lot 50",
        )
        assert_eq(surplus.phase_hold_psm(2, "x")["arm"], False, "bad last psm is first-start")
        assert_eq(surplus.phase_hold_psm(2, 0), {"psm": 2, "arm": False}, "last psm 0 is not held")

    def test_tiny_leftover_and_steal_below_floor():
        alloc = surplus.surplus_allocations
        both = leftover_kw_args()
        assert_eq(
            alloc([A, B], leftover_w=300, **both),
            {},
            "300 W raw leftover + two unequal plugged cars: allocator offers nobody (below 6 A)",
        )
        assert_eq(
            alloc([A], leftover_w=300, **both),
            {A: 300},
            "300 W on a single plugged car is still offered",
        )
        equal = leftover_kw_args(lops={A: 50, B: 50})
        assert_eq(
            alloc([A, B], leftover_w=300, **equal),
            {A: 300, B: 300},
            "equal priority: both still get the tiny leftover",
        )
        assert_eq(
            alloc([A, B], leftover_w=2999, take_w={A: 2000, B: 0}, states={A: "Charging", B: "Charging"}, **both),
            {A: 2000},
            "leftover under 3 kW cannot steal",
        )
        assert_eq(
            alloc(
                [A, B], leftover_w=5000,
                take_w={A: 4000, B: 0}, states={A: "Charging", B: "Charging"}, **both,
            ),
            {A: 4000},
            "leftover 5 kW is under 6 kW: remainder is not a steal",
        )
        assert_eq(
            alloc(
                [A, B], leftover_w=4000, split_hold=True,
                take_w={A: 3900, B: 0}, states={A: "Charging", B: "Charging"}, **both,
            ),
            {A: 3900},
            "steal would leave high under 3 kW: no steal",
        )
        assert_eq(
            alloc(
                [A, B], leftover_w=4500, split_hold=True,
                take_w={A: 4400, B: 0}, states={A: "Charging", B: "Charging"}, **both,
            ),
            {A: 4400},
            "1.5+3 is not a legal split; leftover 4.5 kW keeps high only",
        )
        assert_eq(
            alloc(
                [A, B], leftover_w=5999, split_hold=True,
                take_w={A: 5900, B: 0}, states={A: "Charging", B: "Charging"}, **both,
            ),
            {A: 5900},
            "1 W under 6 kW leftover: still no steal",
        )
        assert_eq(
            alloc(
                [A, B], leftover_w=6000, split_hold=True,
                take_w={A: 5900, B: 0}, states={A: "Charging", B: "Charging"}, **both,
            ),
            {A: 3000, B: 3000},
            "6 kW leftover is the 3+3 minimum split",
        )
        assert_eq(
            alloc(
                [A, B], leftover_w=8000,
                take_w={A: 99, B: 0}, states={A: "Charging", B: "Charging"}, **both,
            ),
            {A: 8000, B: 8000},
            "high take under 100 W: leftover goes to the next; high is still armed",
        )
        assert_eq(
            surplus.surplus_allocation_plan([], leftover_w=8000, **both)["allocations"],
            {},
            "no serials",
        )
        assert_eq(
            alloc([A, B], leftover_w=0, **both),
            {},
            "0 W leftover + unequal: nothing to offer",
        )
        assert_eq(
            alloc([A, B], leftover_w=12000, take_w={A: 5000, B: 0}, states={A: "Charging", B: "Charging"}, **both),
            {A: 5000, B: 7000},
            "high taking 5 kW leaves 7 kW ≥ 3 kW for the next car",
        )
        tiny_max = leftover_kw_args(charger_max_w=5000)
        assert_eq(
            alloc([A, B], leftover_w=12000, take_w={A: 5000, B: 0}, states={A: "Charging", B: "Charging"}, **tiny_max),
            {A: 5000, B: 5000},
            "per-charger max 5 kW caps both shares",
        )

    def test_mqtt_start_floor_and_steal_amps():
        hold, hold_cmds = mqtt_for(300, True, serials=[A, B])
        assert_true(hold["write_on"] and hold["use_floor_budget"], "300 W session is the 6 A hold")
        assert_eq(hold_cmds[A]["amp"], 6, "high car still gets the 6 A floor MQTT")
        assert_eq(hold_cmds[A]["psm"], 1, "6 A floor is 1-phase")
        assert_true(B not in hold_cmds, "low car is not offered leftover during the floor")
        idle, idle_cmds = mqtt_for(300, False, serials=[A, B])
        assert_true(not idle["write_on"], "300 W does not start a new session")
        assert_eq(idle_cmds, {}, "no leftover on publish when still idle")
        dec, cmds = mqtt_for(2000, False, serials=[A], plugged={A: True})
        assert_true(dec["write_on"] and not dec["use_floor_budget"], "start at 2000 W tracks leftover")
        assert_eq(cmds[A]["amp"], 8, "2000 W publishes 8 A, not the 6 A floor")
        assert_eq(cmds[A]["psm"], 1, "2000 W is 1-phase")
        assert_eq(cmds[A]["lot"], GROUP_LOT, "leftover MQTT lot is the fuse cap")
        five, five_cmds = mqtt_for(5000, True, serials=[A], plugged={A: True})
        assert_true(five["write_on"] and not five["use_floor_budget"], "5 kW tracks leftover")
        assert_eq(five_cmds[A]["amp"], 21, "5 kW publishes 21 A")
        assert_eq(five_cmds[A]["lot"], GROUP_LOT, "5 kW does not shrink lot to 21 A")
        dec, cmds = mqtt_for(
            12000,
            True,
            take_w={A: 10000, B: 0},
            states={A: "Charging", B: "Charging"},
        )
        assert_eq(cmds[A]["amp"], 13, "9 kW steal share is 3-phase 13 A")
        assert_eq(cmds[A]["psm"], 2, "9 kW is 3-phase")
        assert_eq(cmds[B]["amp"], 13, "3 kW steal share is 1-phase 13 A")
        assert_eq(cmds[B]["psm"], 1, "3 kW is 1-phase")
        dec, cmds = mqtt_for(8000, True, last_psm={A: 1}, serials=[A], plugged={A: True})
        assert_eq((cmds[A]["psm"], cmds[A]["amp"]), (1, 32), "MQTT path holds 1-phase at 32 A")
        dec, cmds = mqtt_for(6000, True, last_psm={A: 2}, serials=[A], plugged={A: True})
        assert_eq((cmds[A]["psm"], cmds[A]["amp"]), (2, 8), "MQTT path keeps 3-phase at 6 kW")

    def test_idle_mqtt_is_force_off():
        off = const.charger_off_mqtt()
        on = const.charger_on_mqtt(2, 50, 32)
        assert_eq(off[0], ("frc", const.FRC_OFF), "stop publishes force off first")
        assert_eq(const.FRC_OFF, "1", "force off is frc=1")
        assert_true(("frc", const.FRC_NEUTRAL) not in off, "Neutral is not in the stop payload")
        assert_eq(off, (("frc", "1"), ("fup", "false")), "idle is force off only")
        assert_true("psm" not in {key for key, _ in off}, "stop does not restore psm")
        assert_true("lot" not in {key for key, _ in off}, "stop does not restore lot")
        assert_true("amp" not in {key for key, _ in off}, "stop does not restore amp")
        assert_eq(on[-1], ("frc", const.FRC_ON), "start publishes force on last")
        assert_true(("frc", const.FRC_NEUTRAL) not in on, "Neutral is not in the start payload")

    def test_surplus_mqtt_does_not_wait_for_plug():
        alloc = surplus.surplus_allocations
        idle = leftover_kw_args(plugged={A: False}, lops={A: 1})
        assert_eq(
            alloc([A], leftover_w=8000, states={A: "Idle"}, **idle),
            {A: 8000},
            "single Idle charger is offered leftover",
        )
        missing = leftover_kw_args(plugged={A: False}, lops={A: 1})
        assert_eq(
            alloc([A], leftover_w=8000, states={A: None}, **missing),
            {A: 8000},
            "missing car state is offered leftover",
        )
        finished = leftover_kw_args(plugged={A: True}, lops={A: 1})
        assert_eq(
            alloc([A], leftover_w=8000, states={A: "Complete"}, **finished),
            {},
            "idle Complete is not offered leftover",
        )
        assert_eq(
            alloc(
                [A], leftover_w=8000, take_w={A: 5000}, states={A: "Complete"}, **finished
            ),
            {A: 8000},
            "live Complete ≥400 W is offered leftover",
        )
        dec, cmds = mqtt_for(
            2000, False, serials=[A], plugged={A: False}, states={A: "Idle"}
        )
        assert_true(dec["write_on"], "start does not wait for WaitCar")
        assert_true(A in cmds, "Idle charger gets leftover MQTT")
        on = const.charger_on_mqtt(cmds[A]["psm"], GROUP_LOT, cmds[A]["amp"])
        assert_eq(on[-1], ("frc", const.FRC_ON), "start MQTT ends with frc=2")
        dec, cmds = mqtt_for(
            2000,
            False,
            serials=[A, B],
            plugged={A: False, B: False},
            lops={A: 50, B: 50},
        )
        assert_true(dec["write_on"], "equal priority start with nobody plugged")
        assert_true(A in cmds and B in cmds, "both Idle chargers get leftover MQTT")
        assert_eq(cmds[A]["amp"], cmds[B]["amp"], "equal leftover amp on both")

    def test_keep_cut_idle_complete_leftover_offer():
        """Manual keep off / interrupt (KEEP_CUT) arms leftover while Complete.

        The 60 s keep probe still skips idle Complete so a finished
        high-priority car cannot starve a taking lower car via app lop.
        """
        alloc = surplus.surplus_allocations
        finished = leftover_kw_args(plugged={A: True}, lops={A: 1})
        assert_eq(
            alloc([A], leftover_w=8000, states={A: "Complete"}, **finished),
            {},
            "idle Complete without KEEP_CUT is still skipped",
        )
        assert_eq(
            alloc(
                [A],
                leftover_w=8000,
                states={A: "Complete"},
                offer_complete={A},
                **finished,
            ),
            {A: 8000},
            "KEEP_CUT idle Complete is leftover-offered",
        )
        both = leftover_kw_args()
        assert_eq(
            alloc(
                [A, B],
                leftover_w=8000,
                take_w={A: 0, B: 8000},
                states={A: "Complete", B: "Charging"},
                **both,
            ),
            {B: 8000},
            "keep probe: idle Complete high is skipped so B is not starved",
        )
        cut_high = surplus.surplus_allocation_plan(
            [A, B],
            leftover_w=8000,
            take_w={A: 0, B: 8000},
            states={A: "Complete", B: "Charging"},
            offer_complete={A},
            **both,
        )
        assert_eq(cut_high["allocations"].get(A), 8000, "KEEP_CUT high stays armed")
        assert_eq(cut_high["allocations"].get(B), 8000, "taking lower keeps leftover as first")
        assert_eq(cut_high["taking"], [B], "Complete take 0 is not a taking split")
        assert_eq(
            cut_high["lot_allocations"],
            {B: 8000},
            "armed Complete is not a group-lot share after the wait",
        )
        waiting = surplus.surplus_allocation_plan(
            [A, B],
            leftover_w=8000,
            take_w={A: 0, B: 8000},
            states={A: "Complete", B: "Charging"},
            offer_complete={A},
            offer_pending={A},
            **both,
        )
        assert_eq(waiting["overdraw"], True, "15 s wait: Complete high + taking B over-draw")
        assert_true(A in waiting["lot_allocations"], "pending KEEP_CUT high is a lot share")
        assert_true(B in waiting["lot_allocations"], "taking B stays a lot share during wait")
        steal = surplus.surplus_allocation_plan(
            [A, B],
            leftover_w=12000,
            take_w={A: 0, B: 10000},
            states={A: "Complete", B: "Charging"},
            offer_complete={A},
            **both,
        )
        assert_eq(steal["allocations"].get(A), 12000, "not-taking KEEP_CUT high is Idle-like armed")
        assert_eq(steal["allocations"].get(B), 12000, "lower taking is first until high takes")
        taking_both = surplus.surplus_allocation_plan(
            [A, B],
            leftover_w=12000,
            take_w={A: 10000, B: 3000},
            states={A: "Charging", B: "Charging"},
            offer_complete={A},
            **both,
        )
        assert_eq(
            taking_both["allocations"],
            {A: 9000, B: 3000},
            "once KEEP_CUT high takes, steal 9+3 as usual",
        )
        assert_eq(
            alloc(
                [A, B],
                leftover_w=8000,
                take_w={A: 8000, B: 0},
                states={A: "Charging", B: "Complete"},
                **both,
            ),
            {A: 8000},
            "worse-priority idle Complete without KEEP_CUT still skipped",
        )
        b_cut = surplus.surplus_allocation_plan(
            [A, B],
            leftover_w=12000,
            take_w={A: 10000, B: 0},
            states={A: "Charging", B: "Complete"},
            offer_complete={B},
            **both,
        )
        assert_eq(
            b_cut["allocations"],
            {A: 9000, B: 3000},
            "KEEP_CUT lower Complete can receive the 3 kW steal share",
        )
        three = leftover_kw_args(lops={A: 1, B: 2, C: 3})
        three_plan = surplus.surplus_allocation_plan(
            [A, B, C],
            leftover_w=12000,
            take_w={A: 0, B: 10000, C: 0},
            states={A: "Complete", B: "Charging", C: "Charging"},
            offer_complete={A},
            **three,
        )
        assert_eq(
            three_plan["allocations"],
            {A: 12000, B: 9000, C: 3000},
            "KEEP_CUT Complete high is Idle-like: armed, steal on taking remainder",
        )
        wait_three = surplus.surplus_allocation_plan(
            [A, B, C],
            leftover_w=12000,
            take_w={A: 0, B: 10000, C: 0},
            states={A: "Complete", B: "Charging", C: "Charging"},
            offer_complete={A},
            offer_pending={A},
            **three,
        )
        assert_true(
            C not in wait_three["allocations"],
            "third still waits 15 s while high Complete is pending",
        )
        assert_eq(wait_three["overdraw"], True, "pending KEEP_CUT high plus taking B is over-draw")
        keep = surplus.surplus_higher_keep_on
        assert_eq(
            keep(A, {B: 8000}, {A: 1, B: 50}, states={A: "Complete", B: "Charging"}),
            False,
            "idle Complete high stays skipped without KEEP_CUT",
        )
        assert_eq(
            keep(
                A,
                {B: 8000},
                {A: 1, B: 50},
                states={A: "Complete", B: "Charging"},
                offer_complete={A},
            ),
            True,
            "KEEP_CUT idle Complete high stays armed if lower has leftover",
        )
        dec, cmds = mqtt_for(
            4000,
            False,
            serials=[A],
            plugged={A: True},
            lops={A: 1},
            states={A: "Complete"},
            take_w={A: 0},
            offer_complete={A},
        )
        assert_true(dec["write_on"], "4 kW leftover can start")
        assert_true(A in cmds, "KEEP_CUT Complete gets leftover MQTT")
        dec_skip, cmds_skip = mqtt_for(
            4000,
            False,
            serials=[A],
            plugged={A: True},
            lops={A: 1},
            states={A: "Complete"},
            take_w={A: 0},
        )
        assert_true(dec_skip["write_on"], "write_on does not wait for Complete")
        assert_true(A not in cmds_skip, "without KEEP_CUT there is no leftover setpoint")

    def test_offsun_hour_spread_tomorrow_and_evening():
        hel = ZoneInfo("Europe/Helsinki")
        noon = Clock(datetime.datetime(2026, 3, 15, 12, 0, tzinfo=hel), tz=hel)
        both = surplus.surplus_hour_ranges(noon, 50.0, 50.0, 1.0, 60.17, 24.94)
        tom_noon = datetime.datetime(2026, 3, 16, 12, 0, tzinfo=hel).timestamp()
        assert_true(
            any(start <= tom_noon < end for start, end in both),
            "tomorrow kWh blocks tomorrow midday",
        )
        assert_true(len(both) >= 1, "today + tomorrow productive hours merge into ranges")
        evening = Clock(datetime.datetime(2026, 3, 15, 20, 0, tzinfo=hel), tz=hel)
        today_start = evening.start_of_local_day(evening.now())
        hours = surplus.expected_hour_kwh(
            evening,
            today_start,
            today_start + datetime.timedelta(days=1),
            50.0,
            60.17,
            24.94,
        )
        morning = datetime.datetime(2026, 3, 15, 8, 0, tzinfo=hel).timestamp()
        assert_true(
            any(abs(start - morning) <= 1 for start, _end, _kwh in hours),
            "full-day today is spread from midnight: morning hours are still in the profile",
        )
        evening_ranges = surplus.surplus_hour_ranges(evening, 50.0, None, 1.0, 60.17, 24.94)
        assert_true(
            any(start <= morning < end for start, end in evening_ranges),
            "planning at 20:00 still blocks this morning's sun hours",
        )
        assert_eq(
            surplus.surplus_hour_ranges(noon, -5, None, 1.0, 60.17, 24.94),
            [],
            "negative remaining energy does not invent blocked hours",
        )
        assert_eq(
            surplus.surplus_hour_ranges(noon, 50.0, None, "x", 60.17, 24.94),
            [],
            "bad hour threshold excludes nothing",
        )
        assert_eq(
            surplus.expected_hour_kwh(
                noon, noon.now(), noon.now(), None, 60.17, 24.94
            ),
            [],
            "unknown energy: no hour profile",
        )

    def test_enough_solar_usable_hour_gate():
        hel = ZoneInfo("Europe/Helsinki")
        lat, lon = 60.17, 24.94
        hour_kwh = 1.0
        predawn = Clock(datetime.datetime(2026, 3, 15, 2, 0, tzinfo=hel), tz=hel)
        today_start = predawn.start_of_local_day(predawn.now())
        today_end = today_start + datetime.timedelta(days=1)
        sunset = surplus.last_sun_end_ts(predawn, today_start, today_end, lat, lon)
        usable = surplus.last_usable_solar_end_ts(
            predawn, today_start, today_end, 80, hour_kwh, lat, lon
        )
        assert_true(sunset is not None, "Helsinki 15 Mar has a sunset")
        assert_true(usable is not None, "80 kWh today has a last usable hour")
        assert_true(usable < sunset, "last usable hour ends before sunset")
        assert_eq(
            surplus.last_usable_solar_end_ts(
                predawn, today_start, today_end, None, hour_kwh, lat, lon
            ),
            None,
            "unknown today energy has no usable-hour end",
        )
        assert_eq(
            surplus.last_usable_solar_end_ts(
                predawn, today_start, today_end, 0, hour_kwh, lat, lon
            ),
            None,
            "0 kWh today has no usable hour",
        )
        assert_eq(
            surplus.gating_solar_day(predawn, 80, hour_kwh, lat, lon, False),
            "today",
            "02:00 is before last usable hour and before tomorrow prices",
        )
        assert_eq(
            surplus.enough_solar_now(predawn, 80, 10, 40, lat, lon, hour_kwh, False),
            True,
            "sunny today skips 22 kW before prices-in even if tomorrow is cloudy",
        )
        assert_eq(
            surplus.enough_solar_now(predawn, 20, 80, 40, lat, lon, hour_kwh, False),
            False,
            "cloudy today does not skip before last usable hour",
        )
        afternoon = Clock(datetime.datetime(2026, 3, 15, 14, 0, tzinfo=hel), tz=hel)
        assert_eq(
            surplus.gating_solar_day(afternoon, 80, hour_kwh, lat, lon, True),
            "today",
            "14:00 prices-in still today while later hours are usable",
        )
        assert_eq(
            surplus.gating_solar_day(afternoon, 0, hour_kwh, lat, lon, False),
            "today",
            "no usable hour does not flip before tomorrow prices",
        )
        assert_eq(
            surplus.gating_solar_day(afternoon, 0, hour_kwh, lat, lon, True),
            "tomorrow",
            "no usable hour flips at latest when tomorrow prices arrive",
        )
        after_usable = Clock(datetime.datetime.fromtimestamp(usable, tz=hel), tz=hel)
        assert_eq(
            surplus.gating_solar_day(after_usable, 80, hour_kwh, lat, lon, False),
            "today",
            "after last usable hour still today until tomorrow prices",
        )
        assert_eq(
            surplus.gating_solar_day(after_usable, 80, hour_kwh, lat, lon, True),
            "tomorrow",
            "prices-in and last usable hour ended",
        )
        before_sunset = Clock(
            datetime.datetime.fromtimestamp(sunset - 60, tz=hel), tz=hel
        )
        assert_eq(
            surplus.gating_solar_day(before_sunset, 80, hour_kwh, lat, lon, True),
            "tomorrow",
            "after last usable hour, still before sunset, already tomorrow",
        )
        after = Clock(datetime.datetime.fromtimestamp(sunset, tz=hel), tz=hel)
        assert_eq(
            surplus.gating_solar_day(after, 80, hour_kwh, lat, lon, True),
            "tomorrow",
            "at sunset exclusive still tomorrow",
        )
        assert_eq(
            surplus.enough_solar_now(
                after_usable, 80, 10, 40, lat, lon, hour_kwh, True
            ),
            False,
            "after last usable hour cloudy tomorrow allows night 22 kW",
        )
        assert_eq(
            surplus.enough_solar_now(
                after_usable, 80, 80, 40, lat, lon, hour_kwh, True
            ),
            True,
            "after last usable hour sunny tomorrow still skips",
        )
        assert_eq(
            surplus.enough_solar_now(
                after_usable, 80, None, 40, lat, lon, hour_kwh, True
            ),
            False,
            "missing tomorrow after last usable hour is not enough",
        )
        assert_eq(
            surplus.gating_solar_day(predawn, None, hour_kwh, lat, lon, False),
            "today",
            "missing today forecast stays today until prices",
        )
        assert_eq(
            surplus.gating_solar_day(afternoon, None, hour_kwh, lat, lon, True),
            "tomorrow",
            "missing today forecast flips when prices arrive (no solar left)",
        )
        assert_eq(
            surplus.gating_solar_day(predawn, 0, hour_kwh, lat, lon, False),
            "today",
            "0 kWh predawn stays today until prices",
        )
        offsun_end = surplus.last_usable_solar_end_ts(
            predawn, today_start, today_end, 80, 0, lat, lon
        )
        assert_true(offsun_end is not None, "off-sun 0 still has daylight hours")
        assert_eq(
            surplus.gating_solar_day(predawn, 80, 0, lat, lon, False),
            "today",
            "off-sun disabled: remaining daylight keeps today",
        )
        polar_night = Clock(datetime.datetime(2026, 12, 21, 12, 0, tzinfo=hel), tz=hel)
        assert_eq(
            surplus.last_sun_end_ts(
                polar_night,
                polar_night.start_of_local_day(polar_night.now()),
                polar_night.start_of_local_day(polar_night.now()) + datetime.timedelta(days=1),
                78.0,
                16.0,
            ),
            None,
            "78N midwinter never rises",
        )
        assert_eq(
            surplus.gating_solar_day(polar_night, 80, hour_kwh, 78.0, 16.0, False),
            "today",
            "polar night before tomorrow prices stays today",
        )
        polar_night_pm = Clock(
            datetime.datetime(2026, 12, 21, 14, 0, tzinfo=hel), tz=hel
        )
        assert_eq(
            surplus.gating_solar_day(polar_night_pm, 80, hour_kwh, 78.0, 16.0, True),
            "tomorrow",
            "polar night flips when tomorrow prices arrive",
        )
        assert_eq(
            surplus.enough_solar_now(
                polar_night_pm, 80, 10, 40, 78.0, 16.0, hour_kwh, True
            ),
            False,
            "polar night: tomorrow 10 kWh allows 22 kW",
        )
        polar_day = Clock(datetime.datetime(2026, 6, 21, 2, 0, tzinfo=hel), tz=hel)
        assert_eq(
            surplus.gating_solar_day(polar_day, 80, hour_kwh, 78.0, 16.0, False),
            "today",
            "polar day 02:00 stays on today",
        )
        assert_eq(
            surplus.enough_solar_now(
                polar_day, 80, 10, 40, 78.0, 16.0, hour_kwh, False
            ),
            True,
            "polar day 02:00 still gates on today's 80 kWh",
        )

    case("leftover_house_must_contain_ev", test_leftover_house_must_contain_ev)
    case("keep_take_uses_leftover_pool", test_keep_take_uses_leftover_pool)
    case(
        "steal_victim_worse_priority_while_other_takes",
        test_steal_victim_worse_priority_while_other_takes,
    )
    case("leftover_ev_uses_controller_not_instant_nrg", test_leftover_ev_uses_controller_not_instant_nrg)
    case("nrg_reschedule_only_on_take_or_idle_band", test_nrg_reschedule_only_on_take_or_idle_band)
    case("surplus_held_w_kotiakku_cadence", test_surplus_held_w_kotiakku_cadence)
    case("decision_start_hold_stop_and_hysteresis", test_decision_start_hold_stop_and_hysteresis)
    case("three_kw_is_13a_one_phase_not_a_hold", test_three_kw_is_13a_one_phase_not_a_hold)
    case("unplugged_first_does_not_keep_3kw_steal", test_unplugged_first_does_not_keep_3kw_steal)
    case("reported_300w_surplus_does_not_publish_13a", test_reported_300w_surplus_does_not_publish_13a)
    case("group_lot_stays_at_fuse_cap", test_group_lot_stays_at_fuse_cap)
    case("phase_hold_tracks_amp_on_held_phase", test_phase_hold_tracks_amp_on_held_phase)
    case("car_states_plugged_charging_finished", test_car_states_plugged_charging_finished)
    case("enough_solar_and_offsun_hours", test_enough_solar_and_offsun_hours)
    case("nrg_parse_and_car_state_edges", test_nrg_parse_and_car_state_edges)
    case("want_w_and_group_lot_edges", test_want_w_and_group_lot_edges)
    case("tiny_leftover_and_steal_below_floor", test_tiny_leftover_and_steal_below_floor)
    case("mqtt_start_floor_and_steal_amps", test_mqtt_start_floor_and_steal_amps)
    case("idle_mqtt_is_force_off", test_idle_mqtt_is_force_off)
    case("surplus_mqtt_does_not_wait_for_plug", test_surplus_mqtt_does_not_wait_for_plug)
    case("keep_cut_idle_complete_leftover_offer", test_keep_cut_idle_complete_leftover_offer)
    case("offsun_hour_spread_tomorrow_and_evening", test_offsun_hour_spread_tomorrow_and_evening)
    case("enough_solar_usable_hour_gate", test_enough_solar_usable_hour_gate)
    run()


if __name__ == "__main__":
    main()
