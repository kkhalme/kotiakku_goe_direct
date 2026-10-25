"""Charging decisions. Pure; no Home Assistant. ``decide`` chooses commands; surplus watts are paired here first."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .model import (
    CAR_CHARGING,
    CAR_ERROR,
    CAR_WAITCAR,
    DRAW_TRACE_S,
    KEEP_PROBE_S,
    LIMITED_MARGIN_DIVISOR,
    LIMITED_MARGIN_MIN_W,
    METER_GAP_DIVISOR,
    METER_GAP_MIN_W,
    MIDNIGHT_WAKE_S,
    MIN_AMP,
    MIN_W,
    OFF,
    POLICY_DAILY_TRIP,
    POLICY_FORCE_OFF,
    POLICY_FORCE_ON,
    POLICY_SOLAR_AND_GRID,
    POLICY_SOLAR_PRIORITY,
    ROLE_FULL,
    ROLE_KEEP,
    ROLE_OFF,
    ROLE_SURPLUS,
    SETTLE_S,
    START_GRACE_S,
    SURPLUS_POLICIES,
    VOLTS,
    Charger,
    ChargerDecision,
    ChargerMemory,
    Command,
    Decision,
    HouseReading,
    Memory,
    Plan,
    Sample,
    Settings,
    Window,
    psm_of,
)
from .planner import day_start

_LOGGER = logging.getLogger(__name__)

Trace = list[tuple[datetime, int]]


def meter_gap(ev: int) -> int:
    """How far below the draw a house average must sit before the meter omits the car."""
    return max(METER_GAP_MIN_W, ev // METER_GAP_DIVISOR)


def _meter_misses_car(house: int, ev: int) -> bool:
    return ev > 0 and house < ev - meter_gap(ev)


def leftover_w(solar_w: int, house_w: int, ev_w: int) -> int:
    """Surplus for a pair already accepted: solar minus the non-EV base.

    A closed pair (house >= ev) has base = house - ev. When house is far below
    the draw, the meter does not contain the car and base is the house reading.
    """
    base = house_w if _meter_misses_car(house_w, ev_w) else house_w - ev_w
    return solar_w - base


def pair_base(house_w: int, ev_w: int, steady: bool) -> int | None:
    """Base from one house/draw pair. ``None`` when the pair does not close and is not a steady meter-miss."""
    if house_w >= ev_w:
        return house_w - ev_w
    if steady and _meter_misses_car(house_w, ev_w):
        return house_w
    return None


def note_draw(trace: Trace, now: datetime, draw: int) -> Trace:
    """``trace`` with ``draw`` appended, keeping ``DRAW_TRACE_S`` plus the last older point."""
    if trace and trace[-1][1] == draw:
        return trace
    trace = [*trace, (now, draw)]
    cutoff = now - timedelta(seconds=DRAW_TRACE_S)
    older = [point for point in trace if point[0] < cutoff]
    recent = [point for point in trace if point[0] >= cutoff]
    return older[-1:] + recent


def _window_steps(trace: Trace, start: datetime, end: datetime):
    """Held value at ``start`` and later steps inside ``[start, end)``.

    ``None`` when no point is at or before ``start``: the unknown prefix is not invented.
    """
    if end <= start:
        return None
    held = None
    inside: Trace = []
    for moment, value in sorted(trace, key=lambda point: point[0]):
        if moment <= start:
            held = value
        elif moment < end:
            inside.append((moment, value))
    if held is None:
        return None
    return held, inside


def window_mean(trace: Trace, start: datetime, end: datetime) -> int | None:
    """Stepwise-constant mean of ``trace`` over ``[start, end)``."""
    steps = _window_steps(trace, start, end)
    if steps is None:
        return None
    held, inside = steps
    total = 0.0
    cursor, value = start, held
    for moment, nxt in inside:
        total += value * (moment - cursor).total_seconds()
        cursor, value = moment, nxt
    total += value * (end - cursor).total_seconds()
    return round(total / (end - start).total_seconds())


def window_span(trace: Trace, start: datetime, end: datetime) -> int | None:
    """Max minus min of the draw in effect over ``[start, end)``."""
    steps = _window_steps(trace, start, end)
    if steps is None:
        return None
    held, inside = steps
    values = [held, *(value for _, value in inside)]
    return max(values) - min(values)


def ev_for_window(trace: Trace, start: datetime, end: datetime, controller: int | None) -> tuple[int | None, bool]:
    """Window mean and whether the draw was steady. A partial trace does not fall back to the Controller."""
    mean = window_mean(trace, start, end)
    if mean is None:
        if trace or controller is None:
            return None, False
        return controller, False
    span = window_span(trace, start, end)
    return mean, span is not None and span <= meter_gap(mean)


def reconciled_reading(
    previous: Sample | None,
    solar_w: int,
    house_w: int | None,
    ev_avg: int | None,
    steady: bool,
) -> tuple[int, int, int] | None:
    """``(solar, house, ev)`` for ``leftover_w``, or ``None`` when no base exists yet.

    A house update that does not close keeps the previous house and ev, and the new solar still applies.
    """
    if house_w is not None and ev_avg is not None and pair_base(house_w, ev_avg, steady) is not None:
        return solar_w, house_w, ev_avg
    if previous is None:
        return None
    return solar_w, previous.house_w, previous.ev_w


def charger_windows(settings: Settings, serial: str, plan: Plan, now: datetime) -> list[Window]:
    """Windows the policy will still run at full power under the current plan, in time order.

    Force On Until Unplug does not change them. Force on is full all the time and
    has none; SolarPriority has none while solar is enough.
    """
    policy = settings.policy[serial]
    if policy == POLICY_DAILY_TRIP:
        spans = plan.windows + plan.trip_windows
    elif policy == POLICY_SOLAR_AND_GRID or (policy == POLICY_SOLAR_PRIORITY and not plan.enough):
        spans = plan.windows
    else:
        return []
    return sorted((w for w in spans if w.end > now), key=lambda w: (w.start, w.end))


def role(settings: Settings, serial: str, plan: Plan, now: datetime, until: bool, keep: bool) -> str:
    policy = settings.policy[serial]
    if until or policy == POLICY_FORCE_ON:
        return ROLE_FULL
    if any(w.start <= now < w.end for w in charger_windows(settings, serial, plan, now)):
        return ROLE_FULL
    if keep:
        return ROLE_KEEP
    return ROLE_SURPLUS if policy in SURPLUS_POLICIES else ROLE_OFF


def _offer_w(share: int, phases: int, cap: int) -> int:
    return min(cap, max(MIN_AMP, share // (VOLTS * phases))) * VOLTS * phases


def wanted_psm(share: int, last_psm: int | None, settings: Settings) -> int:
    """Keep the running phase while it can still offer leftover.

    3-phase stays while the share holds 6 A on three phases. 1-phase stays until
    3-phase would deliver more watts than the capped 1-phase amp.
    """
    three_ok = share >= MIN_W * 3
    more_on_three = three_ok and _offer_w(share, 3, settings.max_a) > _offer_w(share, 1, settings.one_phase_cap_a)
    if last_psm == 2:
        return 2 if three_ok else 1
    if last_psm == 1:
        return 2 if more_on_three else 1
    if more_on_three:
        return 2
    return psm_of(settings.surplus_preferred_start_phase) if three_ok else 1


def amp_for(share: int, psm: int, settings: Settings) -> int:
    phases = 3 if psm == 2 else 1
    cap = settings.max_a if psm == 2 else settings.one_phase_cap_a
    return min(cap, settings.group_lot_a, max(MIN_AMP, share // (VOLTS * phases)))


def _on(settings: Settings, psm: int, amp: int) -> Command:
    # lot stays at the fuse cap: a leftover-sized lot lets go-e load
    # balancing clip the car below its amp and the allowed current bounces.
    return Command(True, psm, settings.group_lot_a, amp)


def _fixed_command(settings: Settings, role_: str) -> Command:
    if role_ == ROLE_FULL:
        return _on(settings, 2, settings.max_a)
    if role_ == ROLE_KEEP:
        return _on(settings, psm_of(settings.after_charge_complete_keep_phase), settings.after_charge_complete_keep_a)
    return OFF


def _elapsed(since: datetime | None, now: datetime) -> float:
    return 0.0 if since is None else (now - since).total_seconds()


def _car_limited(charger: Charger, mem: ChargerMemory, now: datetime) -> bool:
    """The car stayed clearly below an offer that has been unchanged for SETTLE_S."""
    if not charger.taking or mem.offer_w is None or _elapsed(mem.offer_since, now) < SETTLE_S:
        return False
    margin = max(LIMITED_MARGIN_MIN_W, mem.offer_w // LIMITED_MARGIN_DIVISOR)
    return charger.nrg_w + margin < mem.offer_w


def _unplugged(serial: str, mem: ChargerMemory, until: dict, keep: dict, switches: dict) -> None:
    for name, values in (("until_unplug", until), ("keep", keep)):
        if values[serial]:
            values[serial] = False
            switches.setdefault(serial, {})[name] = False
            _LOGGER.info("%s %s off (unplugged)", serial, name)
    mem.cut = False
    mem.idle_since = None


def _track_plugs(chargers: list[Charger], memory: Memory, until: dict, keep: dict, switches: dict) -> None:
    """Cut keep when it was switched off while plugged, and clear the per-car switches on unplug."""
    for charger in chargers:
        mem = memory.of(charger.serial)
        if mem.keep_was_on and not keep[charger.serial] and charger.plugged and not mem.cut:
            mem.cut = True
            _LOGGER.info("%s keep turned off while plugged: keep cut", charger.serial)
        if charger.plugged is not None:
            if mem.last_plugged and not charger.plugged:
                _unplugged(charger.serial, mem, until, keep, switches)
            mem.last_plugged = charger.plugged


def _probe_keep(settings, charger: Charger, mem: ChargerMemory, now, keep: dict, switches: dict, victim: bool):
    """Auto-on keep after 60 s of idle Complete that HA did not cause."""
    serial = charger.serial
    can_arm = (
        not keep[serial]
        and charger.plugged
        and settings.keep_enable[serial]
        and settings.policy[serial] != POLICY_FORCE_OFF
        and not mem.cut
        and charger.idle_complete
        and not victim
    )
    if not can_arm:
        mem.idle_since = None
        return
    if mem.idle_since is None:
        mem.idle_since = now
        _LOGGER.info("%s keep probe: idle Complete for %ss arms keep", serial, KEEP_PROBE_S)
    elif _elapsed(mem.idle_since, now) >= KEEP_PROBE_S:
        keep[serial] = True
        switches.setdefault(serial, {})["keep"] = True
        mem.idle_since = None
        _LOGGER.info("%s after-charge-complete keep on", serial)


def _phase(settings: Settings, mem: ChargerMemory, share: int, now: datetime) -> tuple[int, int]:
    """Wanted psm, held for hold_minutes on a 1↔3 change.

    CCS cannot switch phases in-session: go-e pauses charging and Tesla raises
    a charging-interrupted alert, so a phase change waits in both directions.
    """
    want = wanted_psm(share, mem.psm, settings)
    if mem.psm not in (1, 2) or want == mem.psm:
        mem.phase_since = None
        return want, want
    if mem.phase_since is None:
        mem.phase_since = now
        _LOGGER.info("holding psm %s (wants %s) for %s min", mem.psm, want, settings.hold_minutes)
    if _elapsed(mem.phase_since, now) >= settings.hold_s:
        mem.phase_since = None
        _LOGGER.info("psm hold expired, switching to psm %s", want)
        return want, want
    return mem.psm, want


def _take_sample(house: HouseReading, chargers: list[Charger], roles: dict, now: datetime) -> Sample:
    solar, house_w, controller = house.sample
    nrg = {charger.serial: max(charger.nrg_w or 0, 0) for charger in chargers}
    ev = controller if controller is not None else sum(nrg.values())
    keep_take = sum(w for serial, w in nrg.items() if roles[serial] == ROLE_KEEP)
    return Sample(now, solar, house_w, ev, leftover_w(solar, house_w, ev), keep_take)


def _surplus_share(
    settings: Settings,
    charger: Charger,
    mem: ChargerMemory,
    remaining: int,
    now: datetime,
    *,
    priced_out: bool,
    can_open: bool,
    higher_on: bool,
    sample_at: datetime | None,
) -> tuple[int | None, bool, bool]:
    """``(share, low_hold, start_pending)`` for a surplus charger.

    ``share`` is the leftover it may use, or ``None`` when it gets no share this cycle.
    """
    running = mem.surplus_on
    if priced_out:
        if running:
            _LOGGER.info("%s surplus off: spot above surplus charging price ceiling", charger.serial)
        mem.low_since = None
        return None, False, False
    if running and mem.low_since is not None:
        if remaining >= settings.surplus_start_w:
            mem.low_since = None
            _LOGGER.info("%s low hold cancelled at %s W", charger.serial, remaining)
            return remaining, False, False
        if _elapsed(mem.low_since, now) >= settings.hold_s:
            mem.low_since = None
            _LOGGER.info("%s low hold expired: surplus off", charger.serial)
            return None, False, False
        return None, True, False
    if running and remaining < MIN_W:
        mem.low_since = now
        _LOGGER.info("%s low hold: 6 A for %s min (share %s W)", charger.serial, settings.hold_minutes, remaining)
        return None, True, False
    if running:
        return remaining, False, False
    if can_open and remaining >= settings.surplus_start_w:
        # A lower charger waits for a second Kotiakku report: one lagging house value can miss a fresh EV ramp.
        if not higher_on or charger.taking or (mem.start_armed_at is not None and sample_at > mem.start_armed_at):
            return remaining, False, False
        mem.start_armed_at = mem.start_armed_at or sample_at
        return None, False, True
    return None, False, False


def _surplus_command(
    settings: Settings, mem: ChargerMemory, share: int | None, low_hold: bool, now: datetime
) -> tuple[Command, int]:
    """Command for a surplus charger and the watts it will take once a phase hold ends."""
    if low_hold:
        mem.phase_since = None
        command = _on(settings, mem.psm or 1, MIN_AMP)
        return command, command.watts
    if share is None:
        mem.phase_since = None
        return OFF, 0
    psm, want = _phase(settings, mem, share, now)
    command = _on(settings, psm, amp_for(share, psm, settings))
    after_hold = _on(settings, want, amp_for(share, want, settings))
    return command, max(command.watts, after_hold.watts)


def _reserve_w(charger: Charger, mem: ChargerMemory, command: Command, target_w: int, limited: bool, now) -> int:
    """Watts this charger holds back from lower priorities."""
    if not command.on:
        return charger.nrg_w if charger.taking else 0
    if charger.taking:
        return min(target_w, charger.nrg_w) if limited else target_w
    return target_w if _elapsed(mem.on_since, now) < START_GRACE_S else 0


def _clear_surplus(mem: ChargerMemory) -> None:
    mem.low_since = mem.phase_since = None
    mem.on_since = mem.offer_w = mem.offer_since = mem.start_armed_at = None


def _allocate(
    settings: Settings,
    order: list[Charger],
    roles: dict[str, str],
    planned: dict[str, list[Window]],
    memory: Memory,
    now: datetime,
    budget: int,
    *,
    can_open: bool,
    priced_out: bool,
) -> dict[str, ChargerDecision]:
    """Decisions in priority order. Each surplus charger reserves what it was offered from ``budget``."""
    sample_at = memory.sample.at if memory.sample else None
    hold = timedelta(seconds=settings.hold_s)
    decisions: dict[str, ChargerDecision] = {}
    remaining, higher_on = budget, False
    for charger in order:
        mem, role_ = memory.of(charger.serial), roles[charger.serial]
        if role_ != ROLE_SURPLUS or (charger.idle_complete and not mem.cut):
            _clear_surplus(mem)
            decisions[charger.serial] = ChargerDecision(
                role_, _fixed_command(settings, role_), windows=planned[charger.serial]
            )
            continue
        running = mem.surplus_on
        share, low_hold, pending = _surplus_share(
            settings,
            charger,
            mem,
            remaining,
            now,
            priced_out=priced_out,
            can_open=can_open,
            higher_on=higher_on,
            sample_at=sample_at,
        )
        if not pending:
            mem.start_armed_at = None
        command, target_w = _surplus_command(settings, mem, share, low_hold, now)
        if command.on and not running:
            mem.on_since = now
        elif not command.on:
            mem.on_since = None
        limited = _car_limited(charger, mem, now)
        reserve = _reserve_w(charger, mem, command, target_w, limited, now)
        if not command.on:
            mem.offer_w = mem.offer_since = None
        elif command.watts != mem.offer_w:
            mem.offer_w, mem.offer_since = command.watts, now
        decisions[charger.serial] = ChargerDecision(
            role_,
            command,
            share_w=share,
            low_hold_until=None if mem.low_since is None else mem.low_since + hold,
            phase_hold_until=None if mem.phase_since is None else mem.phase_since + hold,
            reserve_w=reserve,
            limited=limited,
            start_pending=pending,
            priced_out=priced_out,
            windows=planned[charger.serial],
        )
        if remaining > 0:
            remaining -= min(reserve, remaining)
        higher_on = higher_on or command.on
    return decisions


def _remember(chargers: list[Charger], decisions: dict[str, ChargerDecision], memory: Memory, keep: dict) -> None:
    """Carry this cycle's outcome into memory: keep cut, running state and phase."""
    for charger in chargers:
        mem, decision = memory.of(charger.serial), decisions[charger.serial]
        on = decision.command.on
        unfinished = charger.car in (CAR_CHARGING, CAR_WAITCAR, CAR_ERROR)
        if on and unfinished and mem.cut:
            mem.cut = False
            _LOGGER.info("%s charging allowed again before Complete: keep cut cleared", charger.serial)
        elif mem.last_on and not on and unfinished and not mem.cut:
            mem.cut = True
            _LOGGER.info("%s stopped while car %s: keep cut", charger.serial, charger.car)
        mem.last_on = on
        mem.surplus_on = decision.role == ROLE_SURPLUS and on
        mem.psm = decision.command.psm if on else None
        mem.keep_was_on = keep[charger.serial]


def decide(
    settings: Settings,
    chargers: list[Charger],
    house: HouseReading,
    plan: Plan,
    now: datetime,
    memory: Memory,
) -> Decision:
    switches: dict[str, dict[str, bool]] = {}
    until = {charger.serial: settings.until_unplug[charger.serial] for charger in chargers}
    keep = {charger.serial: settings.keep[charger.serial] for charger in chargers}
    _track_plugs(chargers, memory, until, keep, switches)

    def roles() -> dict[str, str]:
        return {c.serial: role(settings, c.serial, plan, now, until[c.serial], keep[c.serial]) for c in chargers}

    order = sorted(chargers, key=lambda c: (settings.priority[c.serial], c.slot))
    session = any(memory.of(charger.serial).surplus_on for charger in chargers)
    before_probe = roles()
    for rank, charger in enumerate(order):
        victim = session and any(o.taking and before_probe[o.serial] == ROLE_SURPLUS for o in order[:rank])
        _probe_keep(settings, charger, memory.of(charger.serial), now, keep, switches, victim)
    after_probe = roles()
    planned = {c.serial: charger_windows(settings, c.serial, plan, now) for c in chargers}

    if house.sample is not None:
        memory.sample = _take_sample(house, chargers, after_probe, now)
    sample, soc = memory.sample, house.soc
    usable = house.usable and sample is not None
    soc_ok = soc is not None and soc >= settings.soc_on_pct - settings.soc_hyst_pct
    budget = sample.budget_w if usable and soc_ok else 0
    session_or_soc = session or (soc is not None and soc >= settings.soc_on_pct)
    decisions = _allocate(
        settings,
        order,
        after_probe,
        planned,
        memory,
        now,
        budget,
        can_open=usable and session_or_soc,
        priced_out=plan.priced_out_at(now) is not None,
    )
    _remember(chargers, decisions, memory, keep)
    return Decision(decisions, switches, budget, _next_wakeup(memory, chargers, plan, now, settings.hold_s))


def _next_wakeup(memory: Memory, chargers: list[Charger], plan: Plan, now: datetime, hold_s: float) -> datetime:
    hold = timedelta(seconds=hold_s)
    times = [day_start(now, 1) + timedelta(seconds=MIDNIGHT_WAKE_S)]
    for charger in chargers:
        mem = memory.of(charger.serial)
        times += [t + hold for t in (mem.low_since, mem.phase_since) if t is not None]
        if mem.idle_since is not None:
            times.append(mem.idle_since + timedelta(seconds=KEEP_PROBE_S))
        if mem.on_since is not None:
            times.append(mem.on_since + timedelta(seconds=START_GRACE_S))
    times += [t for t in (plan.next_boundary(now), plan.usable_end) if t is not None]
    return min(t for t in times if t > now)
