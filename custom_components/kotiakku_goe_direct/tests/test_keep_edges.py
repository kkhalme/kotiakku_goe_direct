"""After-charge-complete keep: 60 s idle Complete until unplug.

Mirrors the controller apply order: leftover first with the current keep
switches, then one keep probe/cut/arm pass. Idle Complete (nrg below
400 W) is not leftover-offered. Auto-on needs 60 s wall-clock of that,
enable on, not KEEP_CUT, and not a steal victim (worse HA leftover
priority than another surplus charger that is taking while leftover is
writing).
"""

from __future__ import annotations

from harness import assert_eq, case_runner, load_mod

planner = load_mod("planner", "_keep_edges")
surplus = load_mod("surplus", "_keep_edges_s")
step = planner.keep_until_unplug_step
role = planner.charger_mqtt_role
cmd = planner.charger_mqtt_command
IDLE = planner.KEEP_IDLE
ALLOWED = planner.KEEP_ALLOWED
CUT = planner.KEEP_CUT
PROBE = planner.KEEP_PROBE_S
BAND = surplus.KEEP_PROBE_TAKE_W

WINDOW = {"raw_windows": [{"start": 1000, "end": 2000}]}
LEFTOVER = {"psm": 2, "lot": 11, "amp": 11}


class KeepSim:
    """One charger. ``apply`` is one controller keep pass after leftover."""

    def __init__(self, enable=True):
        self.on = False
        self.seen = False
        self.phase = IDLE
        self.enable = enable
        self.probe_since = None
        self.now = 0.0

    def apply(
        self,
        car,
        *,
        commanded_on,
        enable=None,
        switch=None,
        steal_victim=False,
        take_w=0,
        dt=0,
        now_ts=None,
    ):
        if enable is not None:
            self.enable = enable
        if now_ts is not None:
            self.now = float(now_ts)
        elif dt:
            self.now += float(dt)
        override = self.on if switch is None else bool(switch)
        was_on = self.on
        plugged = surplus.car_plugged(car)
        finished = surplus.car_finished(car)
        idle = surplus.idle_complete(car, take_w)
        override, self.seen, self.phase, self.probe_since = step(
            override,
            self.seen,
            self.phase,
            plugged=plugged,
            finished=finished,
            commanded_on=commanded_on,
            was_on=was_on,
            enable=self.enable,
            steal_victim=steal_victim,
            idle=idle,
            probe_since=self.probe_since,
            now_ts=self.now,
        )
        self.on = override
        return self

    def finish(self, car="Complete", *, commanded_on=False, take_w=0, **kw):
        """Idle Complete this tick, then 60 s later."""
        self.apply(car, commanded_on=commanded_on, take_w=take_w, **kw)
        return self.apply(
            car, commanded_on=commanded_on, take_w=take_w, dt=PROBE, **kw
        )

    def expect(self, on, phase, seen=None, probe=None, msg=""):
        assert_eq(self.on, on, "%s keep" % msg)
        assert_eq(self.phase, phase, "%s phase" % msg)
        if seen is not None:
            assert_eq(self.seen, seen, "%s seen" % msg)
        if probe is True:
            assert_eq(self.probe_since is not None, True, "%s probe running" % msg)
        elif probe is False:
            assert_eq(self.probe_since, None, "%s probe clear" % msg)
        return self


def main():
    case, run = case_runner()

    def test_car_flags_match_goe_states():
        assert_eq(
            (surplus.car_plugged("Idle"), surplus.car_finished("Idle")),
            (False, False),
            "Idle unplugged",
        )
        assert_eq(
            (surplus.car_plugged("WaitCar"), surplus.car_finished("WaitCar")),
            (True, False),
            "WaitCar plugged not finished",
        )
        assert_eq(
            (surplus.car_plugged("Charging"), surplus.car_finished("Charging")),
            (True, False),
            "Charging plugged not finished",
        )
        assert_eq(
            (surplus.car_plugged("Complete"), surplus.car_finished("Complete")),
            (True, True),
            "Complete plugged finished",
        )
        assert_eq(
            (surplus.car_plugged("Error"), surplus.car_finished("Error")),
            (True, False),
            "Error plugged not finished",
        )
        assert_eq(surplus.car_finished("4"), True, "numeric Complete")
        assert_eq(surplus.car_plugged("3"), True, "numeric WaitCar")
        assert_eq(surplus.idle_complete("Complete", 0), True, "idle Complete 0 W")
        assert_eq(surplus.idle_complete("Complete", 350), True, "Sentry band idle")
        assert_eq(surplus.idle_complete("Complete", BAND), False, "400 W live Complete")
        assert_eq(surplus.idle_complete("Complete", None), True, "unknown nrg idle")
        assert_eq(surplus.idle_complete("Charging", 0), False, "Charging is not idle Complete")
        assert_eq(PROBE, 60, "keep probe is 60 s")

    case("car_flags_match_goe_states", test_car_flags_match_goe_states)

    def test_idle_complete_waits_60s_then_keep():
        sim = KeepSim()
        sim.apply("Idle", commanded_on=True).expect(
            False, IDLE, probe=False, msg="unplugged leftover force-on"
        )
        sim.apply("WaitCar", commanded_on=True).expect(
            False, ALLOWED, seen=False, probe=False, msg="leftover WaitCar"
        )
        sim.apply("Complete", commanded_on=False).expect(
            False, ALLOWED, probe=True, msg="t+0 idle Complete no keep"
        )
        sim.apply("Complete", commanded_on=False, dt=PROBE - 1).expect(
            False, ALLOWED, probe=True, msg="t+59 still probing"
        )
        sim.apply("Complete", commanded_on=False, dt=1).expect(
            True, ALLOWED, seen=True, probe=False, msg="t+60 keep"
        )

    case("idle_complete_waits_60s_then_keep", test_idle_complete_waits_60s_then_keep)

    def test_leftover_charging_then_complete():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True).expect(False, ALLOWED, msg="leftover Charging")
        sim.finish("Complete").expect(
            True, ALLOWED, msg="Complete after leftover Charging"
        )

    case("leftover_charging_then_complete", test_leftover_charging_then_complete)

    def test_leftover_waitcar_charging_complete():
        sim = KeepSim()
        sim.apply("WaitCar", commanded_on=True)
        sim.apply("Charging", commanded_on=True).expect(False, ALLOWED, msg="still leftover")
        sim.finish("Complete").expect(True, ALLOWED, msg="self-finish after leftover")

    case("leftover_waitcar_charging_complete", test_leftover_waitcar_charging_complete)

    def test_window_charging_complete_while_still_22kw():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True).expect(False, ALLOWED, msg="22 kW Charging")
        sim.finish("Complete", commanded_on=True).expect(
            True, ALLOWED, msg="Complete during cheap window"
        )

    case("window_charging_complete_while_still_22kw", test_window_charging_complete_while_still_22kw)

    def test_window_then_leftover_then_complete():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True)
        sim.apply("Charging", commanded_on=True).expect(False, ALLOWED, msg="window still on")
        sim.apply("Charging", commanded_on=True).expect(False, ALLOWED, msg="leftover takes over")
        sim.finish("Complete").expect(True, ALLOWED, msg="self-finish after leftover")

    case("window_then_leftover_then_complete", test_window_then_leftover_then_complete)

    def test_complete_without_a_charge_session():
        sim = KeepSim()
        sim.apply("Idle", commanded_on=True)
        sim.finish("Complete").expect(
            True, IDLE, msg="already Complete when plugged in: finished pack"
        )

    case("complete_without_a_charge_session", test_complete_without_a_charge_session)

    def test_leftover_on_already_finished_car():
        sim = KeepSim()
        sim.finish("Complete").expect(
            True, IDLE, msg="already finished: leftover skip is still a full pack"
        )
        sim.apply("Complete", commanded_on=True).expect(
            True, IDLE, msg="22 kW while already Complete keeps the finished pack"
        )

    case("leftover_on_already_finished_car", test_leftover_on_already_finished_car)

    def test_force_off_already_complete_does_not_auto_on():
        sim = KeepSim(enable=False)
        sim.finish("Complete").expect(
            False, IDLE, probe=False, msg="Force off / enable off never auto-on keep"
        )

    case(
        "force_off_already_complete_does_not_auto_on",
        test_force_off_already_complete_does_not_auto_on,
    )

    def test_leftover_interrupt_while_charging_then_complete():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True)
        sim.apply("Charging", commanded_on=False).expect(
            False, CUT, msg="leftover stopped while still Charging"
        )
        sim.finish("Complete").expect(
            False, CUT, probe=False, msg="Complete after interrupt is not a finished pack"
        )

    case("leftover_interrupt_while_charging_then_complete", test_leftover_interrupt_while_charging_then_complete)

    def test_leftover_interrupt_while_waitcar_then_complete():
        sim = KeepSim()
        sim.apply("WaitCar", commanded_on=True)
        sim.apply("WaitCar", commanded_on=False).expect(
            False, CUT, msg="leftover stolen while still WaitCar"
        )
        sim.finish("Complete").expect(
            False, CUT, probe=False, msg="Complete after interrupt is not a finished pack"
        )

    case("leftover_interrupt_while_waitcar_then_complete", test_leftover_interrupt_while_waitcar_then_complete)

    def test_window_interrupt_no_leftover_then_complete():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True)
        sim.apply("Charging", commanded_on=False).expect(
            False, CUT, msg="window ended while still Charging"
        )
        sim.finish("Complete").expect(
            False, CUT, probe=False, msg="Complete after interrupt is not a finished pack"
        )

    case("window_interrupt_no_leftover_then_complete", test_window_interrupt_no_leftover_then_complete)

    def test_leftover_resume_after_interrupt_then_complete():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True)
        sim.apply("Charging", commanded_on=False).expect(False, CUT, msg="interrupt")
        sim.apply("Charging", commanded_on=True).expect(
            False, ALLOWED, msg="leftover returns"
        )
        sim.finish("Complete").expect(
            True, ALLOWED, msg="self-finish after leftover resumed"
        )

    case(
        "leftover_resume_after_interrupt_then_complete",
        test_leftover_resume_after_interrupt_then_complete,
    )

    def test_leftover_stops_same_tick_as_complete():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True)
        sim.apply("Complete", commanded_on=False).expect(
            False, ALLOWED, probe=True, msg="same-tick Complete starts probe, not keep"
        )
        sim.apply("Complete", commanded_on=False, dt=PROBE).expect(
            True, ALLOWED, msg="60 s later is a finished surplus charge"
        )

    case("leftover_stops_same_tick_as_complete", test_leftover_stops_same_tick_as_complete)

    def test_steal_victim_idle_complete_never_keep():
        """Activity log 2026-09-14: A (left) took 1-phase leftover.

        B (right) went Complete while still frc=2. Idle Complete is not
        leftover-offered. Steal victim blocks keep; HA sends frc=1.
        """
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True).expect(
            False, ALLOWED, msg="B leftover Charging, A still unplugged"
        )
        sim.apply("Complete", commanded_on=False, steal_victim=True).expect(
            False, ALLOWED, probe=False, msg="t+0 steal victim, no probe"
        )
        sim.apply("Complete", commanded_on=False, steal_victim=True, dt=PROBE).expect(
            False, ALLOWED, probe=False, msg="t+60 steal victim still no keep"
        )
        assert_eq(
            cmd(planner.ROLE_SURPLUS, surplus_on=True, leftover_session=True),
            ("off",),
            "B leftover MQTT off is frc=1",
        )

    case("steal_victim_idle_complete_never_keep", test_steal_victim_idle_complete_never_keep)

    def test_waitcar_other_car_is_not_steal_victim():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True)
        sim.apply("Complete", commanded_on=False, steal_victim=False).expect(
            False, ALLOWED, probe=True, msg="A WaitCar: B not a steal victim"
        )
        sim.apply("Complete", commanded_on=False, dt=PROBE).expect(
            True, ALLOWED, msg="B keep at 60 s while A is WaitCar"
        )

    case("waitcar_other_car_is_not_steal_victim", test_waitcar_other_car_is_not_steal_victim)

    def test_better_priority_idle_complete_keeps_while_other_takes():
        sim = KeepSim()
        sim.finish("Complete").expect(
            True, IDLE, msg="A finished: keep after 60 s, leftover already on B"
        )
        assert_eq(
            role("SolarPriority", WINDOW, 0, keep_min=sim.on),
            planner.ROLE_KEEP,
            "better-priority idle Complete is keep, not leftover",
        )

    case(
        "better_priority_idle_complete_keeps_while_other_takes",
        test_better_priority_idle_complete_keeps_while_other_takes,
    )

    def test_take_at_probe_band_resets_probe():
        sim = KeepSim()
        sim.apply("Complete", commanded_on=False, take_w=0).expect(
            False, IDLE, probe=True, msg="probe starts"
        )
        sim.apply("Complete", commanded_on=False, take_w=BAND, dt=30).expect(
            False, IDLE, probe=False, msg="≥400 W during probe resets"
        )
        sim.apply("Complete", commanded_on=False, take_w=0).expect(
            False, IDLE, probe=True, msg="idle again restarts probe"
        )
        sim.apply("Complete", commanded_on=False, take_w=0, dt=PROBE - 1).expect(
            False, IDLE, probe=True, msg="59 s after reset is not keep"
        )
        sim.apply("Complete", commanded_on=False, take_w=0, dt=1).expect(
            True, IDLE, msg="60 s after reset is keep"
        )

    case("take_at_probe_band_resets_probe", test_take_at_probe_band_resets_probe)

    def test_error_while_leftover_then_complete():
        sim = KeepSim()
        sim.apply("Error", commanded_on=True).expect(
            False, ALLOWED, msg="Error while commanded on"
        )
        sim.finish("Complete").expect(True, ALLOWED, msg="Complete after Error leftover")

    case("error_while_leftover_then_complete", test_error_while_leftover_then_complete)

    def test_enable_off_skips_auto_on():
        sim = KeepSim(enable=False)
        sim.apply("Charging", commanded_on=True).expect(
            False, ALLOWED, msg="allowed still tracked"
        )
        sim.finish("Complete").expect(
            False, ALLOWED, probe=False, msg="enable off no auto-on"
        )

    case("enable_off_skips_auto_on", test_enable_off_skips_auto_on)

    def test_enable_on_after_complete_arms():
        sim = KeepSim(enable=False)
        sim.apply("Charging", commanded_on=True)
        sim.apply("Complete", commanded_on=False).expect(
            False, ALLOWED, probe=False, msg="waiting for enable"
        )
        sim.apply("Complete", commanded_on=False, enable=True).expect(
            False, ALLOWED, probe=True, msg="enable on starts probe"
        )
        sim.apply("Complete", commanded_on=False, dt=PROBE).expect(
            True, ALLOWED, msg="enable on while still Complete"
        )

    case("enable_on_after_complete_arms", test_enable_on_after_complete_arms)

    def test_enable_off_does_not_clear_keep_already_on():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True)
        sim.finish("Complete").expect(True, ALLOWED, msg="auto-on")
        sim.apply("Complete", commanded_on=False, enable=False).expect(
            True, ALLOWED, msg="enable off leaves keep running"
        )

    case("enable_off_does_not_clear_keep_already_on", test_enable_off_does_not_clear_keep_already_on)

    def test_enable_off_manual_keep_until_unplug():
        sim = KeepSim(enable=False)
        sim.apply("WaitCar", commanded_on=False, switch=True).expect(
            True, IDLE, seen=True, msg="manual keep"
        )
        sim.apply("Complete", commanded_on=False).expect(
            True, IDLE, msg="still keep, enable off"
        )
        sim.apply("Idle", commanded_on=False).expect(
            False, IDLE, seen=False, msg="unplug"
        )

    case("enable_off_manual_keep_until_unplug", test_enable_off_manual_keep_until_unplug)

    def test_manual_on_without_complete():
        sim = KeepSim()
        sim.apply("WaitCar", commanded_on=False, switch=True).expect(
            True, IDLE, seen=True, msg="manual on WaitCar"
        )
        sim.apply("Charging", commanded_on=False).expect(
            True, IDLE, msg="manual keep during Charging"
        )
        sim.apply("Idle", commanded_on=False).expect(
            False, IDLE, msg="unplug clears manual keep"
        )

    case("manual_on_without_complete", test_manual_on_without_complete)

    def test_manual_on_while_unplugged_waits_for_plug():
        sim = KeepSim()
        sim.apply("Idle", commanded_on=False, switch=True).expect(
            True, IDLE, seen=False, msg="manual on unplugged waits"
        )
        sim.apply("WaitCar", commanded_on=False).expect(
            True, IDLE, seen=True, msg="plug arms seen"
        )
        sim.apply("Idle", commanded_on=False).expect(
            False, IDLE, seen=False, msg="unplug after seen"
        )

    case("manual_on_while_unplugged_waits_for_plug", test_manual_on_while_unplugged_waits_for_plug)

    def test_manual_off_while_complete_does_not_rearm():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True)
        sim.finish("Complete").expect(True, ALLOWED, msg="auto-on")
        sim.apply("Complete", commanded_on=False, switch=False).expect(
            False, CUT, probe=False, msg="manual off cuts"
        )
        sim.finish("Complete", commanded_on=True).expect(
            False, CUT, probe=False, msg="Complete does not re-arm after manual off"
        )

    case("manual_off_while_complete_does_not_rearm", test_manual_off_while_complete_does_not_rearm)

    def test_manual_off_while_charging_does_not_cancel_later_auto_on():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True).expect(False, ALLOWED, msg="allowed")
        sim.apply("Charging", commanded_on=True, switch=True).expect(
            True, ALLOWED, msg="manual on"
        )
        sim.apply("Charging", commanded_on=True, switch=False).expect(
            False, ALLOWED, msg="manual off while leftover still on: allowed returns"
        )
        sim.finish("Complete").expect(
            True, ALLOWED, msg="self-finish still auto-on; use enable to skip"
        )

    case("manual_off_while_charging_does_not_cancel_later_auto_on", test_manual_off_while_charging_does_not_cancel_later_auto_on)

    def test_enable_off_while_charging_skips_auto_on_after_manual_off():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True, switch=True)
        sim.apply("Charging", commanded_on=True, switch=False, enable=False).expect(
            False, ALLOWED, msg="allowed tracked, keep off"
        )
        sim.finish("Complete").expect(
            False, ALLOWED, probe=False, msg="enable off skips auto-on"
        )

    case("enable_off_while_charging_skips_auto_on_after_manual_off", test_enable_off_while_charging_skips_auto_on_after_manual_off)

    def test_unplug_clears_and_replug_already_complete_auto_on():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True)
        sim.finish("Complete")
        sim.apply("Idle", commanded_on=False).expect(
            False, IDLE, seen=False, probe=False, msg="unplug"
        )
        sim.finish("Complete").expect(
            True, IDLE, msg="replug already Complete is a finished pack"
        )
        sim.apply("Idle", commanded_on=False).expect(
            False, IDLE, seen=False, msg="unplug keep"
        )
        sim.apply("WaitCar", commanded_on=True).expect(
            False, ALLOWED, msg="new leftover session"
        )
        sim.finish("Complete").expect(True, ALLOWED, msg="new self-finish")

    case(
        "unplug_clears_and_replug_already_complete_auto_on",
        test_unplug_clears_and_replug_already_complete_auto_on,
    )

    def test_precondition_charging_after_keep():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True)
        sim.finish("Complete")
        sim.apply("Charging", commanded_on=False).expect(
            True, ALLOWED, msg="cabin precondition Charging"
        )
        sim.apply("Complete", commanded_on=False).expect(
            True, ALLOWED, msg="back to Complete"
        )
        sim.apply("Idle", commanded_on=False).expect(
            False, IDLE, msg="unplug after precondition"
        )

    case("precondition_charging_after_keep", test_precondition_charging_after_keep)

    def test_keep_survives_leftover_and_window_end():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True)
        sim.finish("Complete")
        sim.apply("Complete", commanded_on=False).expect(
            True, ALLOWED, msg="leftover gone, keep stays"
        )
        assert_eq(
            role("SolarPriority", WINDOW, 0, keep_min=sim.on),
            planner.ROLE_KEEP,
            "outside window is keep",
        )
        assert_eq(
            role("Force off", WINDOW, 0, keep_min=sim.on),
            planner.ROLE_KEEP,
            "Force off still keep",
        )

    case("keep_survives_leftover_and_window_end", test_keep_survives_leftover_and_window_end)

    def test_full_power_wins_over_keep():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True)
        sim.finish("Complete")
        assert_eq(
            role("SolarPriority", WINDOW, 1500, keep_min=sim.on),
            planner.ROLE_FULL,
            "cheap window over keep",
        )
        assert_eq(
            role("Force on", WINDOW, 0, keep_min=sim.on),
            planner.ROLE_FULL,
            "Force on over keep",
        )
        assert_eq(
            role("SolarPriority", WINDOW, 0, until_unplug=True, keep_min=sim.on),
            planner.ROLE_FULL,
            "22 kW until-unplug over keep",
        )
        assert_eq(
            planner.charger_surplus("SolarPriority", WINDOW, 0, keep_min=sim.on),
            False,
            "keep is not leftover",
        )

    case("full_power_wins_over_keep", test_full_power_wins_over_keep)

    def test_two_chargers_keep_skips_leftover_for_that_serial():
        a = KeepSim()
        b = KeepSim()
        a.apply("Charging", commanded_on=True)
        b.apply("WaitCar", commanded_on=True)
        a.finish("Complete")
        b.apply("WaitCar", commanded_on=True)
        assert_eq(
            role("SolarPriority", WINDOW, 0, keep_min=a.on),
            planner.ROLE_KEEP,
            "A keep",
        )
        assert_eq(
            role("SolarPriority", WINDOW, 0, keep_min=b.on),
            planner.ROLE_SURPLUS,
            "B leftover",
        )
        assert_eq(
            cmd(planner.ROLE_KEEP, surplus_on=True, surplus_pub=LEFTOVER),
            ("on", 2, 50, 6),
            "A keep command",
        )
        assert_eq(
            cmd(planner.ROLE_SURPLUS, surplus_on=True, surplus_pub=LEFTOVER),
            ("on", 2, 11, 11),
            "B leftover command",
        )

    case("two_chargers_keep_skips_leftover_for_that_serial", test_two_chargers_keep_skips_leftover_for_that_serial)

    def test_numeric_car_states_leftover_self_finish():
        sim = KeepSim()
        sim.apply("3", commanded_on=True).expect(False, ALLOWED, msg="numeric WaitCar")
        sim.apply("2", commanded_on=True).expect(False, ALLOWED, msg="numeric Charging")
        sim.finish("4").expect(True, ALLOWED, msg="numeric Complete")

    case("numeric_car_states_leftover_self_finish", test_numeric_car_states_leftover_self_finish)

    def test_leftover_first_idle_complete_is_not_keep_role():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True)
        on, seen, phase, probe = step(
            sim.on,
            sim.seen,
            sim.phase,
            plugged=surplus.car_plugged("Complete"),
            finished=surplus.car_finished("Complete"),
            commanded_on=False,
            was_on=sim.on,
            enable=True,
            idle=True,
            now_ts=0,
        )
        assert_eq((on, seen, phase), (False, False, ALLOWED), "t+0 leftover still sees surplus")
        assert_eq(probe, 0, "probe starts at leftover tick")
        assert_eq(
            role("SolarPriority", WINDOW, 0, keep_min=on),
            planner.ROLE_SURPLUS,
            "idle Complete is not keep until 60 s",
        )
        on, seen, phase, probe = step(
            on,
            seen,
            phase,
            plugged=True,
            finished=True,
            commanded_on=False,
            was_on=on,
            enable=True,
            idle=True,
            probe_since=probe,
            now_ts=PROBE,
        )
        assert_eq((on, seen, phase, probe), (True, True, ALLOWED, None), "t+60 keep")
        assert_eq(
            role("SolarPriority", WINDOW, 0, keep_min=on),
            planner.ROLE_KEEP,
            "leftover allocation then sees keep not surplus",
        )

    case(
        "leftover_first_idle_complete_is_not_keep_role",
        test_leftover_first_idle_complete_is_not_keep_role,
    )

    run()


if __name__ == "__main__":
    main()
