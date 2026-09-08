"""Edge-case simulation of after-charge-complete keep.

Mirrors the controller two-pass apply: pass 1 arms keep from an existing
offered flag before leftover allocation (Complete is skipped for leftover),
pass 2 tracks whether HA is still commanding this charger on.
"""

from __future__ import annotations

from harness import assert_eq, case_runner, load_mod

planner = load_mod("planner", "_keep_edges")
surplus = load_mod("surplus", "_keep_edges_s")
step = planner.keep_min_until_unplug_step
role = planner.charger_mqtt_role
cmd = planner.charger_mqtt_command

WINDOW = {"raw_windows": [{"start": 1000, "end": 2000}]}
LEFTOVER = {"psm": 2, "lot": 11, "amp": 11}


class KeepSim:
    """One charger. ``apply`` is one controller ``_apply_chargers`` tick."""

    def __init__(self, enable=True):
        self.on = False
        self.seen = False
        self.offered = False
        self.enable = enable

    def apply(self, car, *, commanded_on, enable=None, switch=None):
        if enable is not None:
            self.enable = enable
        override = self.on if switch is None else bool(switch)
        was_on = self.on
        plugged = surplus.car_plugged(car)
        finished = surplus.car_finished(car)
        override, self.seen, self.offered = step(
            override,
            self.seen,
            self.offered,
            plugged=plugged,
            finished=finished,
            commanded_on=False,
            was_on=was_on,
            track_command=False,
            enable=self.enable,
        )
        was_on = override
        override, self.seen, self.offered = step(
            override,
            self.seen,
            self.offered,
            plugged=plugged,
            finished=finished,
            commanded_on=commanded_on,
            was_on=was_on,
            track_command=True,
            enable=self.enable,
        )
        self.on = override
        return self

    def expect(self, on, offered, seen=None, msg=""):
        assert_eq(self.on, on, "%s keep" % msg)
        assert_eq(self.offered, offered, "%s offered" % msg)
        if seen is not None:
            assert_eq(self.seen, seen, "%s seen" % msg)
        return self


def keep_cmd(**extra):
    return cmd(planner.ROLE_KEEP, surplus_on=False, **extra)


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

    case("car_flags_match_goe_states", test_car_flags_match_goe_states)

    def test_leftover_waitcar_then_complete_skips_leftover():
        sim = KeepSim()
        sim.apply("Idle", commanded_on=True).expect(
            False, False, msg="unplugged leftover force-on"
        )
        sim.apply("WaitCar", commanded_on=True).expect(
            False, True, seen=False, msg="leftover WaitCar"
        )
        sim.apply("Complete", commanded_on=False).expect(
            True, True, seen=True, msg="Complete leftover-skip"
        )

    case("leftover_waitcar_then_complete_skips_leftover", test_leftover_waitcar_then_complete_skips_leftover)

    def test_leftover_charging_then_complete():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True).expect(False, True, msg="leftover Charging")
        sim.apply("Complete", commanded_on=False).expect(True, True, msg="Complete after leftover Charging")

    case("leftover_charging_then_complete", test_leftover_charging_then_complete)

    def test_leftover_waitcar_charging_complete():
        sim = KeepSim()
        sim.apply("WaitCar", commanded_on=True)
        sim.apply("Charging", commanded_on=True).expect(False, True, msg="still leftover")
        sim.apply("Complete", commanded_on=False).expect(True, True, msg="self-finish after leftover")

    case("leftover_waitcar_charging_complete", test_leftover_waitcar_charging_complete)

    def test_window_charging_complete_while_still_22kw():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True).expect(False, True, msg="22 kW Charging")
        sim.apply("Complete", commanded_on=True).expect(
            True, True, msg="Complete during cheap window"
        )

    case("window_charging_complete_while_still_22kw", test_window_charging_complete_while_still_22kw)

    def test_window_then_leftover_then_complete():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True)
        sim.apply("Charging", commanded_on=True).expect(False, True, msg="window still on")
        sim.apply("Charging", commanded_on=True).expect(False, True, msg="leftover takes over")
        sim.apply("Complete", commanded_on=False).expect(True, True, msg="self-finish after leftover")

    case("window_then_leftover_then_complete", test_window_then_leftover_then_complete)

    def test_plug_already_complete_does_not_arm():
        sim = KeepSim()
        sim.apply("Idle", commanded_on=True)
        sim.apply("Complete", commanded_on=False).expect(
            False, False, msg="plug in already Complete"
        )

    case("plug_already_complete_does_not_arm", test_plug_already_complete_does_not_arm)

    def test_leftover_starts_on_already_complete():
        sim = KeepSim()
        sim.apply("Complete", commanded_on=False).expect(
            False, False, msg="Complete with leftover skip, never offered"
        )
        sim.apply("Complete", commanded_on=True).expect(
            False, False, msg="22 kW while already Complete still needs prior offered"
        )

    case("leftover_starts_on_already_complete", test_leftover_starts_on_already_complete)

    def test_leftover_cut_while_charging_then_complete():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True)
        sim.apply("Charging", commanded_on=False).expect(
            False, False, msg="leftover cut while Charging"
        )
        sim.apply("Complete", commanded_on=False).expect(
            False, False, msg="Complete after leftover cut"
        )

    case("leftover_cut_while_charging_then_complete", test_leftover_cut_while_charging_then_complete)

    def test_leftover_cut_while_waitcar_then_complete():
        sim = KeepSim()
        sim.apply("WaitCar", commanded_on=True)
        sim.apply("WaitCar", commanded_on=False).expect(
            False, False, msg="leftover stolen/cut while WaitCar"
        )
        sim.apply("Complete", commanded_on=False).expect(
            False, False, msg="Complete after WaitCar cut"
        )

    case("leftover_cut_while_waitcar_then_complete", test_leftover_cut_while_waitcar_then_complete)

    def test_window_cut_no_leftover_then_complete():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True)
        sim.apply("Charging", commanded_on=False).expect(False, False, msg="window ended, leftover not writing")
        sim.apply("Complete", commanded_on=False).expect(False, False, msg="Complete after window cut")

    case("window_cut_no_leftover_then_complete", test_window_cut_no_leftover_then_complete)

    def test_leftover_stops_same_tick_as_complete():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True)
        sim.apply("Complete", commanded_on=False).expect(
            True, True, msg="Complete as leftover stops is still a self-finish"
        )

    case("leftover_stops_same_tick_as_complete", test_leftover_stops_same_tick_as_complete)

    def test_error_while_leftover_then_complete():
        sim = KeepSim()
        sim.apply("Error", commanded_on=True).expect(False, True, msg="Error while commanded on")
        sim.apply("Complete", commanded_on=False).expect(True, True, msg="Complete after Error leftover")

    case("error_while_leftover_then_complete", test_error_while_leftover_then_complete)

    def test_enable_off_skips_auto_on():
        sim = KeepSim(enable=False)
        sim.apply("Charging", commanded_on=True).expect(False, True, msg="offered still tracked")
        sim.apply("Complete", commanded_on=False).expect(False, True, msg="enable off no auto-on")

    case("enable_off_skips_auto_on", test_enable_off_skips_auto_on)

    def test_enable_on_after_complete_arms_from_offered():
        sim = KeepSim(enable=False)
        sim.apply("Charging", commanded_on=True)
        sim.apply("Complete", commanded_on=False).expect(False, True, msg="waiting for enable")
        sim.apply("Complete", commanded_on=False, enable=True).expect(
            True, True, msg="enable on while still Complete"
        )

    case("enable_on_after_complete_arms_from_offered", test_enable_on_after_complete_arms_from_offered)

    def test_enable_off_does_not_clear_keep_already_on():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True)
        sim.apply("Complete", commanded_on=False).expect(True, True, msg="auto-on")
        sim.apply("Complete", commanded_on=False, enable=False).expect(
            True, True, msg="enable off leaves keep running"
        )

    case("enable_off_does_not_clear_keep_already_on", test_enable_off_does_not_clear_keep_already_on)

    def test_enable_off_manual_keep_until_unplug():
        sim = KeepSim(enable=False)
        sim.apply("WaitCar", commanded_on=False, switch=True).expect(
            True, False, seen=True, msg="manual keep"
        )
        sim.apply("Complete", commanded_on=False).expect(True, False, msg="still keep, enable off")
        sim.apply("Idle", commanded_on=False).expect(False, False, seen=False, msg="unplug")

    case("enable_off_manual_keep_until_unplug", test_enable_off_manual_keep_until_unplug)

    def test_manual_on_without_complete():
        sim = KeepSim()
        sim.apply("WaitCar", commanded_on=False, switch=True).expect(
            True, False, seen=True, msg="manual on WaitCar"
        )
        sim.apply("Charging", commanded_on=False).expect(True, False, msg="manual keep during Charging")
        sim.apply("Idle", commanded_on=False).expect(False, False, msg="unplug clears manual keep")

    case("manual_on_without_complete", test_manual_on_without_complete)

    def test_manual_on_while_unplugged_waits_for_plug():
        sim = KeepSim()
        sim.apply("Idle", commanded_on=False, switch=True).expect(
            True, False, seen=False, msg="manual on unplugged waits"
        )
        sim.apply("WaitCar", commanded_on=False).expect(True, False, seen=True, msg="plug arms seen")
        sim.apply("Idle", commanded_on=False).expect(False, False, seen=False, msg="unplug after seen")

    case("manual_on_while_unplugged_waits_for_plug", test_manual_on_while_unplugged_waits_for_plug)

    def test_manual_off_while_complete_does_not_rearm():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True)
        sim.apply("Complete", commanded_on=False).expect(True, True, msg="auto-on")
        sim.apply("Complete", commanded_on=False, switch=False).expect(
            False, False, msg="manual off clears offered"
        )
        sim.apply("Complete", commanded_on=True).expect(
            False, False, msg="Complete does not re-arm after manual off"
        )

    case("manual_off_while_complete_does_not_rearm", test_manual_off_while_complete_does_not_rearm)

    def test_manual_off_while_charging_does_not_cancel_later_auto_on():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True).expect(False, True, msg="offered")
        sim.apply("Charging", commanded_on=True, switch=True).expect(True, True, msg="manual on")
        sim.apply("Charging", commanded_on=True, switch=False).expect(
            False, True, msg="manual off while leftover still on: offered returns"
        )
        sim.apply("Complete", commanded_on=False).expect(
            True, True, msg="self-finish still auto-on; use enable to skip"
        )

    case("manual_off_while_charging_does_not_cancel_later_auto_on", test_manual_off_while_charging_does_not_cancel_later_auto_on)

    def test_enable_off_while_charging_skips_auto_on_after_manual_off():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True, switch=True)
        sim.apply("Charging", commanded_on=True, switch=False, enable=False).expect(
            False, True, msg="offered tracked, keep off"
        )
        sim.apply("Complete", commanded_on=False).expect(
            False, True, msg="enable off skips auto-on"
        )

    case("enable_off_while_charging_skips_auto_on_after_manual_off", test_enable_off_while_charging_skips_auto_on_after_manual_off)

    def test_unplug_clears_and_replug_complete_stays_off():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True)
        sim.apply("Complete", commanded_on=False)
        sim.apply("Idle", commanded_on=False).expect(False, False, seen=False, msg="unplug")
        sim.apply("Complete", commanded_on=False).expect(
            False, False, msg="replug already Complete"
        )
        sim.apply("WaitCar", commanded_on=True).expect(False, True, msg="new leftover session")
        sim.apply("Complete", commanded_on=False).expect(True, True, msg="new self-finish")

    case("unplug_clears_and_replug_complete_stays_off", test_unplug_clears_and_replug_complete_stays_off)

    def test_precondition_charging_after_keep():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True)
        sim.apply("Complete", commanded_on=False)
        sim.apply("Charging", commanded_on=False).expect(
            True, True, msg="cabin precondition Charging"
        )
        sim.apply("Complete", commanded_on=False).expect(True, True, msg="back to Complete")
        sim.apply("Idle", commanded_on=False).expect(False, False, msg="unplug after precondition")

    case("precondition_charging_after_keep", test_precondition_charging_after_keep)

    def test_keep_survives_leftover_and_window_end():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True)
        sim.apply("Complete", commanded_on=False)
        sim.apply("Complete", commanded_on=False).expect(True, True, msg="leftover gone, keep stays")
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
        sim.apply("Complete", commanded_on=False)
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

    def test_keep_command_wins_over_leftover_watts():
        assert_eq(
            cmd(planner.ROLE_KEEP, surplus_on=True, surplus_pub=LEFTOVER),
            ("on", 2, 50, 6),
            "keep not leftover amp",
        )
        assert_eq(
            cmd(planner.ROLE_KEEP, surplus_on=False, leftover_session=True),
            ("on", 2, 50, 6),
            "keep not leftover-session force-off",
        )
        assert_eq(
            keep_cmd(keep_psm=1, keep_amp=8),
            ("on", 1, 50, 8),
            "keep knobs",
        )
        assert_eq(
            cmd(planner.ROLE_SURPLUS, surplus_on=True, surplus_pub=LEFTOVER),
            ("on", 2, 11, 11),
            "leftover without keep",
        )

    case("keep_command_wins_over_leftover_watts", test_keep_command_wins_over_leftover_watts)

    def test_two_chargers_keep_skips_leftover_for_that_serial():
        a = KeepSim()
        b = KeepSim()
        a.apply("Charging", commanded_on=True)
        b.apply("WaitCar", commanded_on=True)
        a.apply("Complete", commanded_on=False)
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
        sim.apply("3", commanded_on=True).expect(False, True, msg="numeric WaitCar")
        sim.apply("2", commanded_on=True).expect(False, True, msg="numeric Charging")
        sim.apply("4", commanded_on=False).expect(True, True, msg="numeric Complete")

    case("numeric_car_states_leftover_self_finish", test_numeric_car_states_leftover_self_finish)

    def test_pass1_arms_before_leftover_so_complete_is_not_surplus():
        sim = KeepSim()
        sim.apply("Charging", commanded_on=True)
        plugged = surplus.car_plugged("Complete")
        finished = surplus.car_finished("Complete")
        on, seen, offered = step(
            sim.on,
            sim.seen,
            sim.offered,
            plugged=plugged,
            finished=finished,
            commanded_on=False,
            was_on=sim.on,
            track_command=False,
            enable=True,
        )
        assert_eq((on, seen, offered), (True, True, True), "pass 1 keep")
        assert_eq(
            role("SolarPriority", WINDOW, 0, keep_min=on),
            planner.ROLE_KEEP,
            "leftover allocation sees keep not surplus",
        )

    case("pass1_arms_before_leftover_so_complete_is_not_surplus", test_pass1_arms_before_leftover_so_complete_is_not_surplus)

    run()


if __name__ == "__main__":
    main()
