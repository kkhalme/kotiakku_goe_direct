"""Spec lock for leftover surplus. Independent of the window planner.

Pin the 3 kW / ~300 W leftover cases and the rest of the surplus contract:
house vs EV add-back, Controller vs nrg, start/hold/stop, 3 kW steal,
budget amps, phase hold, group lot beside full-power.
"""

from __future__ import annotations

from harness import assert_eq, assert_true, case_runner, load_mod

surplus = load_mod("surplus", "_spec_sur")
planner = load_mod("planner", "_spec_sur_p")

A, B, C = "111111", "222222", "333333"
VOLTS = 230
MIN_A = 6
MAX_A = 32
ECO_LOT = 50
P3 = 4140
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
        phase3_min_w=P3,
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
            source_w, MIN_A, MAX_A, ECO_LOT, VOLTS, P3,
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

    def test_ev_prefers_nrg_over_lagged_controller():
        ev = surplus.effective_ev_w
        assert_eq(ev(12000, 3000), 3000, "lagged 12 kW Controller vs 3 kW nrg → 3 kW")
        assert_eq(ev(3000, 3000), 3000, "agree")
        assert_eq(ev(0, 3000, controller_usable=False), 3000, "unknown Controller uses nrg")
        assert_eq(ev(3000, None), 3000, "no nrg keeps Controller")
        assert_eq(ev(3000, 0), 3000, "zero nrg is missing, keep Controller")
        assert_eq(ev(0, None, controller_usable=False), 0, "nothing charging")
        leftover = surplus.leftover_w(3800, 3500, ev(12000, 3000))
        assert_eq(leftover, 3300, "nrg 3 kW with house 3500: add EV back")
        assert_eq(surplus.leftover_w(3800, 3500, 12000), 300, "12 kW Controller vs house 3500: reject the lag")
        assert_eq(surplus.leftover_w(3800, 3500, 3000), 3300, "3 kW nrg with house containing it")

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

    def test_three_kw_is_13a_one_phase_not_a_hold():
        lot, psm, amp = surplus.budget(3000, MIN_A, MAX_A, ECO_LOT, VOLTS, P3)
        assert_eq((psm, amp), (1, 13), "3 kW is 1-phase 13 A")
        lot, psm, amp = surplus.budget(300, MIN_A, MAX_A, ECO_LOT, VOLTS, P3)
        assert_eq((psm, amp), (1, 6), "300 W budgets 6 A floor, not 13 A")
        lot, psm, amp = surplus.budget(0, MIN_A, MAX_A, ECO_LOT, VOLTS, P3)
        assert_eq((psm, amp), (1, 6), "0 W floor is 6 A 1-phase")
        lot, psm, amp = surplus.budget(4140, MIN_A, MAX_A, ECO_LOT, VOLTS, P3)
        assert_eq((psm, amp), (2, 6), "4140 W is 3-phase 6 A")
        lot, psm, amp = surplus.budget(2500, MIN_A, MAX_A, ECO_LOT, VOLTS, P3)
        assert_eq((psm, amp), (1, 10), "2500 W is 1-phase 10 A")
        hold3 = surplus.budget(3000, MIN_A, MAX_A, ECO_LOT, VOLTS, P3, force_psm=2)
        assert_eq((hold3[1], hold3[2]), (2, 6), "forced 3-phase below 4140 W stays 6 A")

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
        idle_first = leftover_kw_args(plugged={A: False, B: True})
        assert_eq(
            alloc(
                [A, B], leftover_w=300, split_hold=True,
                take_w={A: 0, B: 3000}, states={A: "Idle", B: "Charging"}, **idle_first,
            ),
            {B: 300},
            "Idle first + stale steal: only 300 W leftover, not 3 kW",
        )
        assert_eq(
            alloc([A, B], leftover_w=8000, split_hold=True, **idle_first),
            {B: 8000},
            "unplugged first: remaining car gets all leftover as first, not 3 kW",
        )
        assert_eq(
            alloc(
                [A, B], leftover_w=8000,
                take_w={A: 0, B: 0}, states={A: "Complete", B: "Charging"}, **both,
            ),
            {B: 8000},
            "Complete first is skipped",
        )
        wait = alloc(
            [A, B], leftover_w=2500, split_hold=True,
            take_w={A: 0, B: 2500}, states={A: "WaitCar", B: "Charging"}, **both,
        )
        assert_eq(wait.get(B), 2500, "WaitCar taking 0 W does not mint a 3 kW steal from 2500 W")
        assert_true(SPLIT_MIN not in wait.values(), "2500 W leftover never allocates exactly 3 kW steal")
        equal = leftover_kw_args(lops={A: 50, B: 50}, plugged={A: False, B: True})
        assert_eq(
            alloc([A, B], leftover_w=8000, **equal),
            {B: 8000},
            "equal priority: unplugged charger is not offered leftover",
        )
        three = leftover_kw_args(lops={A: 1, B: 2, C: 3}, plugged={A: True, B: False, C: True})
        got = alloc(
            [A, B, C], leftover_w=12000,
            take_w={A: 10000, B: 0, C: 0},
            states={A: "Charging", B: "Idle", C: "Charging"},
            **three,
        )
        assert_eq(got, {A: 9000, C: 3000}, "skip Idle middle charger; steal to the next plugged")
        assert_eq(alloc([A], leftover_w=8000, **both), {A: 8000}, "single charger gets leftover")
        none = surplus.surplus_allocation_plan(
            [A, B], leftover_w=8000, **leftover_kw_args(plugged={A: False, B: False})
        )
        assert_eq(none["allocations"], {}, "nobody plugged → no leftover MQTT")

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
        assert_true(A not in cmds, "Idle first charger is not written")
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
            1500, True, hold_active=True, plugged={A: False, B: True}, serials=[A, B]
        )
        assert_true(mid["use_floor_budget"], "hold hysteresis")
        assert_eq(mid_cmds[B]["amp"], 6, "still 6 A at 1500 W while hold is active")

    def test_group_lot_does_not_shrink_full_power():
        setp = surplus.group_surplus_setpoint
        assert_eq(setp(10, 1, 10, n_full=0, eco_lot=50), (10, 1, 10), "pure surplus leftover lot")
        assert_eq(setp(10, 1, 10, n_full=1, eco_lot=50), (50, 1, 10), "mixed: keep group 50, leftover amp")
        assert_eq(setp(25, 2, 25, n_full=1, eco_lot=50), (50, 2, 25), "do not cap leftover amp for 32 A")
        assert_eq(
            surplus.group_lot_for_allocations(
                17, {A: 9000, B: 3000}, min_amp=6, max_amp=32, eco_lot=50, volts=230, phase3_min_w=4140
            ),
            26,
            "9+3 kW raises lot to 13 A + 13 A",
        )
        assert_eq(
            surplus.group_lot_for_allocations(
                17, {A: 12000, B: 12000}, min_amp=6, max_amp=32, eco_lot=50, volts=230, phase3_min_w=4140
            ),
            17,
            "equal leftover does not sum amps",
        )

    def test_phase_hold_tracks_amp_on_held_phase():
        args = (MIN_A, MAX_A, ECO_LOT, VOLTS, P3)
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

    def test_car_states_plugged_charging_finished():
        assert_eq(surplus.car_plugged("Idle"), False, "Idle is unplugged")
        assert_eq(surplus.car_plugged("1"), False, "numeric idle")
        assert_eq(surplus.car_plugged("Charging"), True, "Charging")
        assert_eq(surplus.car_plugged("Complete"), True, "Complete is still plugged")
        assert_eq(surplus.car_plugged("WaitCar"), True, "WaitCar is plugged")
        assert_eq(surplus.car_finished("Complete"), True, "Complete finished")
        assert_eq(surplus.car_charging("WaitCar"), False, "WaitCar not charging")
        assert_eq(surplus.charger_take_w("Idle", 3000, 8000, 22080), 0, "unplugged take 0")
        assert_eq(surplus.charger_take_w("Complete", 8000, 8000, 22080), 0, "finished take 0")
        assert_eq(surplus.charger_take_w("WaitCar", None, 8000, 22080), 0, "WaitCar take 0")
        assert_eq(surplus.charger_take_w("Charging", None, 8000, 22080), 8000, "unknown charging wants leftover")
        assert_eq(surplus.charger_take_w("Charging", 3000, 300, 22080), 300, "take capped by leftover")

    def test_enough_solar_and_offsun_hours():
        assert_eq(surplus.enough_solar(None, 40), False, "unknown is not enough")
        assert_eq(surplus.enough_solar(40, 40), True, "at threshold")
        assert_eq(surplus.enough_solar(50, 0), False, "threshold 0 disables")
        assert_eq(surplus.upcoming_solar_kwh(8, 50), 50, "max remaining vs tomorrow")
        assert_eq(surplus.energy_kwh("50000", "Wh"), 50.0, "Wh → kWh")
        assert_eq(surplus.energy_kwh("4000", "W"), None, "power is not energy")
        result = {"raw_windows": [{"start": 3000, "end": 4000}]}
        full = planner.charger_full_power
        assert_eq(full("SolarPriority", result, 3500, enough_solar=True), False, "skip 22 kW")
        assert_eq(full("Supercheap", result, 3500), True, "legacy Supercheap maps")
        assert_eq(full("Cheapest", result, 3500, enough_solar=True), False, "legacy Cheapest now skips")

    case("leftover_house_must_contain_ev", test_leftover_house_must_contain_ev)
    case("ev_prefers_nrg_over_lagged_controller", test_ev_prefers_nrg_over_lagged_controller)
    case("decision_start_hold_stop_and_hysteresis", test_decision_start_hold_stop_and_hysteresis)
    case("three_kw_is_13a_one_phase_not_a_hold", test_three_kw_is_13a_one_phase_not_a_hold)
    case("unplugged_first_does_not_keep_3kw_steal", test_unplugged_first_does_not_keep_3kw_steal)
    case("reported_300w_surplus_does_not_publish_13a", test_reported_300w_surplus_does_not_publish_13a)
    case("group_lot_does_not_shrink_full_power", test_group_lot_does_not_shrink_full_power)
    case("phase_hold_tracks_amp_on_held_phase", test_phase_hold_tracks_amp_on_held_phase)
    case("car_states_plugged_charging_finished", test_car_states_plugged_charging_finished)
    case("enough_solar_and_offsun_hours", test_enough_solar_and_offsun_hours)
    run()


if __name__ == "__main__":
    main()
