from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from core.engine import decide, leftover_w
from core.model import (
    CAR_CHARGING,
    CAR_COMPLETE,
    CAR_IDLE,
    CAR_WAITCAR,
    PHASE_3,
    POLICY_FORCE_OFF,
    POLICY_SOLAR_AND_GRID,
    POLICY_SOLAR_PRIORITY,
    ROLE_FULL,
    ROLE_KEEP,
    ROLE_SURPLUS,
    Charger,
    HouseReading,
    Memory,
    Plan,
    Settings,
    Window,
)

HEL = ZoneInfo("Europe/Helsinki")
T0 = datetime(2026, 6, 1, 12, tzinfo=HEL)
HOLD = 15 * 60


def make_plan(windows=(), enough=False):
    return Plan(list(windows), [], "planned", False, None, None, None, "today", None, enough)


class Sim:
    def __init__(self, *serials, policy=POLICY_SOLAR_PRIORITY, **knobs):
        serials = serials or ("A",)
        self.settings = Settings(**knobs)
        for slot, serial in enumerate(serials):
            self.settings.policy[serial] = policy
            self.settings.priority[serial] = slot + 1
        self.chargers = {s: Charger(s, slot, car=CAR_WAITCAR, nrg_w=0) for slot, s in enumerate(serials)}
        self.memory = Memory()
        self.now = T0
        self.plan = make_plan()
        self.soc = 96.0
        self.usable = True
        self.decision = None

    def car(self, serial, car="same", nrg="same"):
        charger = self.chargers[serial]
        if car != "same":
            charger.car = car
        if nrg != "same":
            charger.nrg_w = nrg
        return self

    def step(self, leftover=None, dt=0, controller=0, house_w=1000):
        self.now += timedelta(seconds=dt)
        sample = None if leftover is None else (leftover + house_w, house_w, controller)
        house = HouseReading(self.soc, self.usable, sample)
        self.decision = decide(self.settings, list(self.chargers.values()), house, self.plan, self.now, self.memory)
        for serial, changes in self.decision.switches.items():
            for name, value in changes.items():
                getattr(self.settings, name)[serial] = value
        return self

    def cmd(self, serial="A"):
        command = self.decision.chargers[serial].command
        return (command.psm, command.amp) if command.on else "off"

    def role(self, serial="A"):
        return self.decision.chargers[serial].role


def test_leftover_formula():
    assert leftover_w(5000, 4000, 3000) == 4000
    assert leftover_w(5000, 800, 3000) == 4200
    assert leftover_w(0, 2000, 0) == -2000
    assert leftover_w(-5000, -1000, 0) == 4000


def test_controller_unknown_falls_back_to_charger_nrg():
    sim = Sim().car("A", CAR_CHARGING, 3000)
    sim.step(controller=None, leftover=2000, house_w=4000)
    sample = sim.memory.sample
    assert sample.ev_w == 3000 and sample.leftover_w == 5000
    small_house = Sim().car("A", CAR_CHARGING, 3000).step(controller=None, leftover=2000)
    assert small_house.memory.sample.leftover_w == 2000


def test_force_off_never_charges():
    sim = Sim("A", "B", policy=POLICY_FORCE_OFF).step(8000)
    assert sim.cmd("A") == "off" and sim.cmd("B") == "off"


def test_first_start_arms_unplugged_charger_on_preferred_phase():
    sim = Sim().car("A", CAR_IDLE).step(6000)
    assert sim.cmd() == (1, 26)
    assert sim.decision.chargers["A"].command.lot == 50
    assert Sim(surplus_preferred_start_phase=PHASE_3).step(6000).cmd() == (2, 8)


def test_start_needs_soc_and_start_leftover():
    sim = Sim()
    sim.soc = 91
    assert sim.step(8000).cmd() == "off"
    sim.soc = 96
    assert sim.step(1900).cmd() == "off"
    assert sim.step(2000).cmd() == (1, 8)


def test_no_leftover_no_window_is_off():
    assert Sim().step(0).cmd() == "off"


def test_one_phase_holds_before_switching_up():
    sim = Sim().step(5000)
    assert sim.cmd() == (1, 21)
    sim.step(8000)
    assert sim.cmd() == (1, 32)
    assert sim.decision.chargers["A"].phase_hold_until == T0 + timedelta(seconds=HOLD)
    assert sim.step(dt=HOLD - 1).cmd() == (1, 32)
    assert sim.step(dt=1).cmd() == (2, 11)


def test_three_phase_sticks_while_it_can_offer():
    sim = Sim(surplus_preferred_start_phase=PHASE_3).step(8000)
    assert sim.cmd() == (2, 11)
    assert sim.step(6000).cmd() == (2, 8)
    assert sim.step(3000).cmd() == (2, 6)
    assert sim.step(dt=HOLD).cmd() == (1, 13)


def test_phase_hold_cancelled_when_phase_can_offer_again():
    sim = Sim(surplus_preferred_start_phase=PHASE_3).step(8000).step(3000)
    assert sim.decision.chargers["A"].phase_hold_until is not None
    sim.step(6000, dt=60)
    assert sim.cmd() == (2, 8) and sim.decision.chargers["A"].phase_hold_until is None


def test_soc_hysteresis_then_low_hold_then_off():
    sim = Sim().step(5000)
    sim.soc = 90.5
    assert sim.step(dt=60).cmd() == (1, 21)
    sim.soc = 89.9
    assert sim.step(dt=60).cmd() == (1, 6)
    assert sim.step(dt=HOLD).cmd() == "off"


def test_low_hold_ignores_chatter_and_cancels_at_start_leftover():
    sim = Sim().step(5000)
    assert sim.step(1000).cmd() == (1, 6)
    assert sim.step(1500, dt=60).cmd() == (1, 6)
    assert sim.step(2000, dt=60).cmd() == (1, 8)
    assert sim.decision.chargers["A"].low_hold_until is None


def test_low_hold_keeps_three_phase_then_stops():
    sim = Sim(surplus_preferred_start_phase=PHASE_3).step(8000)
    assert sim.step(1000).cmd() == (2, 6)
    assert sim.step(dt=HOLD - 1).cmd() == (2, 6)
    assert sim.step(dt=1).cmd() == "off"


def test_low_hold_threshold_is_the_6a_minimum():
    sim = Sim().step(5000)
    assert sim.step(1300).cmd() == (1, 6)
    assert sim.decision.chargers["A"].low_hold_until is not None


def test_unusable_holds_then_stops_and_cannot_start():
    sim = Sim().step(5000)
    sim.usable = False
    assert sim.step(dt=60).cmd() == (1, 6)
    assert sim.step(dt=HOLD).cmd() == "off"
    assert sim.step(8000, dt=60).cmd() == "off"


def test_held_sample_does_not_follow_nrg():
    sim = Sim().car("A", CAR_CHARGING, 3700).step(5000)
    assert sim.cmd() == (1, 21)
    sim.car("A", nrg=4800).step(dt=10)
    assert sim.cmd() == (1, 21)


def test_second_car_gets_true_remainder():
    sim = Sim("A", "B").car("A", CAR_CHARGING, 10000)
    sim.settings.policy["B"] = POLICY_FORCE_OFF
    sim.step(12000)
    assert sim.cmd("A") == (2, 17)
    sim.settings.policy["B"] = POLICY_SOLAR_PRIORITY
    sim.step(dt=1)
    assert sim.cmd("A") == (2, 17) and sim.cmd("B") == (1, 8)


def test_idle_higher_priority_stays_armed_until_it_takes():
    sim = Sim("A", "B").car("B", CAR_CHARGING, 7000).step(8000)
    assert sim.cmd("A") == (2, 11) and sim.cmd("B") == (2, 11)
    sim.car("A", CAR_CHARGING, 8000).step(dt=5)
    assert sim.cmd("A") == (2, 11) and sim.cmd("B") == (2, 6)
    assert sim.step(dt=HOLD).cmd("B") == "off"


def test_high_taking_everything_does_not_start_second():
    sim = Sim("A", "B").car("A", CAR_CHARGING, 4000)
    sim.settings.policy["B"] = POLICY_FORCE_OFF
    sim.step(4000)
    sim.settings.policy["B"] = POLICY_SOLAR_PRIORITY
    assert sim.step(dt=1).cmd("B") == "off"


def test_expired_low_hold_needs_start_leftover_to_restart():
    sim = Sim("A", "B").car("A", CAR_CHARGING, 6000).car("B", CAR_CHARGING, 1500).step(8000)
    sim.step(6500, dt=1)
    assert sim.cmd("B") == (1, 6)
    sim.step(dt=HOLD)
    assert sim.cmd("B") == "off"
    assert sim.step(7500, dt=60).cmd("B") == "off"
    assert sim.step(8000, dt=60).cmd("B") == (1, 8)


def test_window_end_keeps_three_phase_for_surplus():
    sim = Sim(policy=POLICY_SOLAR_AND_GRID).car("A", CAR_CHARGING, 11000)
    sim.plan = make_plan([Window(T0 - timedelta(hours=1), T0 + timedelta(minutes=10), 0.02)])
    sim.step(6000)
    assert sim.role() == ROLE_FULL and sim.cmd() == (2, 32)
    sim.step(6000, dt=11 * 60)
    assert sim.role() == ROLE_SURPLUS and sim.cmd() == (2, 8)


def test_cheap_window_is_full_power_even_when_kotiakku_unusable():
    sim = Sim("A", "B")
    sim.plan = make_plan([Window(T0 - timedelta(hours=1), T0 + timedelta(hours=1), 0.02)])
    sim.usable = False
    sim.step()
    assert sim.cmd("A") == (2, 32) and sim.cmd("B") == (2, 32)


def test_enough_solar_skips_solarpriority_window_only():
    window = [Window(T0 - timedelta(hours=1), T0 + timedelta(hours=1), 0.02)]
    sim = Sim("A", "B")
    sim.settings.policy["B"] = POLICY_SOLAR_AND_GRID
    sim.plan = make_plan(window, enough=True)
    sim.step(0)
    assert sim.role("A") == ROLE_SURPLUS and sim.role("B") == ROLE_FULL


def test_keep_take_uses_the_leftover_pool():
    sim = Sim("A", "B").car("A", CAR_COMPLETE, 3000)
    sim.settings.keep["A"] = True
    sim.step(2000)
    assert sim.role("A") == ROLE_KEEP and sim.cmd("A") == (2, 6)
    assert sim.memory.sample.keep_take_w == 3000 and sim.decision.budget_w == -1000
    assert sim.cmd("B") == "off"


def test_idle_complete_is_skipped_then_keeps_after_60s():
    sim = Sim("A", "B").car("A", CAR_COMPLETE, 0).step(8000)
    assert sim.cmd("A") == "off" and sim.cmd("B") == (2, 11)
    assert sim.decision.next_wakeup == T0 + timedelta(seconds=60)
    sim.step(dt=59)
    assert not sim.settings.keep.get("A")
    sim.step(dt=1)
    assert sim.settings.keep["A"] and sim.role("A") == ROLE_KEEP and sim.cmd("A") == (2, 6)


def test_live_complete_drawing_is_still_eligible():
    sim = Sim().car("A", CAR_COMPLETE, 450).step(8000)
    assert sim.cmd("A") == (2, 11)


def test_interrupt_while_charging_cuts_keep_and_offers_complete():
    sim = Sim().car("A", CAR_CHARGING, 5000).step(5000)
    sim.step(0).step(dt=HOLD)
    assert sim.cmd() == "off" and sim.memory.of("A").cut
    sim.car("A", CAR_COMPLETE, 0).step(8000, dt=60)
    assert sim.cmd() == (2, 11)
    sim.step(dt=120)
    assert not sim.settings.keep.get("A")


def test_resume_after_interrupt_then_self_finish_keeps():
    sim = Sim().car("A", CAR_CHARGING, 5000).step(5000)
    sim.step(0).step(dt=HOLD)
    assert sim.memory.of("A").cut
    sim.step(5000, dt=60)
    assert not sim.memory.of("A").cut
    sim.car("A", CAR_COMPLETE, 0).step(dt=5).step(dt=60)
    assert sim.settings.keep["A"]


def test_manual_keep_off_while_complete_does_not_rearm():
    sim = Sim().car("A", CAR_COMPLETE, 0).step(8000).step(dt=60)
    assert sim.settings.keep["A"]
    sim.settings.keep["A"] = False
    sim.step(dt=1)
    assert sim.memory.of("A").cut and sim.cmd() == (2, 11)
    sim.step(dt=120)
    assert not sim.settings.keep["A"]


def test_manual_keep_off_while_charging_does_not_block_later_auto_on():
    sim = Sim().car("A", CAR_CHARGING, 5000).step(5000)
    sim.settings.keep["A"] = True
    sim.step(dt=1)
    sim.settings.keep["A"] = False
    sim.step(dt=1)
    assert sim.role() == ROLE_SURPLUS and not sim.memory.of("A").cut
    sim.car("A", CAR_COMPLETE, 0).step(dt=1).step(dt=60)
    assert sim.settings.keep["A"]


def test_steal_victim_never_arms_keep():
    sim = Sim("A", "B").car("A", CAR_CHARGING, 5000).car("B", CAR_COMPLETE, 0)
    sim.step(8000).step(dt=60).step(dt=60)
    assert not sim.settings.keep.get("B")
    better = Sim("A", "B").car("B", CAR_CHARGING, 5000).car("A", CAR_COMPLETE, 0)
    better.step(8000).step(dt=60)
    assert better.settings.keep["A"]


def test_force_off_and_enable_off_skip_auto_on_but_manual_works():
    sim = Sim(policy=POLICY_FORCE_OFF).car("A", CAR_COMPLETE, 0).step().step(dt=120)
    assert not sim.settings.keep.get("A")
    sim = Sim().car("A", CAR_COMPLETE, 0)
    sim.settings.keep_enable["A"] = False
    sim.step().step(dt=120)
    assert not sim.settings.keep.get("A")
    sim.settings.keep["A"] = True
    assert sim.step(dt=1).role() == ROLE_KEEP


def test_until_unplug_survives_complete_and_unknown_car():
    sim = Sim(policy=POLICY_FORCE_OFF).car("A", CAR_CHARGING, 11000)
    sim.settings.until_unplug["A"] = True
    assert sim.step().cmd() == (2, 32)
    assert sim.car("A", CAR_COMPLETE, 0).step(dt=60).role() == ROLE_FULL
    assert sim.car("A", None).step(dt=60).role() == ROLE_FULL
    sim.car("A", CAR_IDLE).step(dt=60)
    assert sim.decision.switches == {"A": {"until_unplug": False}}
    assert sim.cmd() == "off"


def test_unplug_during_restart_is_detected_from_persisted_state():
    sim = Sim(policy=POLICY_FORCE_OFF).car("A", CAR_IDLE)
    sim.settings.until_unplug["A"] = True
    sim.memory.of("A").last_plugged = True
    sim.step()
    assert not sim.settings.until_unplug["A"]


def test_manual_keep_while_unplugged_waits_for_plug():
    sim = Sim().car("A", CAR_IDLE)
    sim.settings.keep["A"] = True
    assert sim.step().role() == ROLE_KEEP
    assert sim.car("A", CAR_WAITCAR).step(dt=1).role() == ROLE_KEEP
    sim.car("A", CAR_IDLE).step(dt=1)
    assert not sim.settings.keep["A"]


def test_unknown_car_never_arms_keep():
    sim = Sim().car("A", None, 0).step().step(dt=120)
    assert not sim.settings.keep.get("A")


def test_priority_ties_break_by_slot():
    sim = Sim("A", "B").car("A", CAR_CHARGING, 8000).car("B", CAR_CHARGING, 8000)
    sim.settings.priority["B"] = 1
    sim.step(8000)
    assert sim.cmd("A") == (2, 11)
    assert sim.cmd("B") == "off"


def test_next_wakeup_defaults_to_after_midnight():
    sim = Sim().step(0)
    assert sim.decision.next_wakeup == datetime(2026, 6, 2, 0, 0, 30, tzinfo=HEL)
