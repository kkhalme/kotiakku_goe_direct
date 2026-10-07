"""Charging decisions. Pure; no Home Assistant. ``decide`` chooses commands; surplus watts are paired here first."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .model import (
    CAR_CHARGING,
    CAR_ERROR,
    CAR_WAITCAR,
    KEEP_PROBE_S,
    MIN_AMP,
    MIN_W,
    OFF,
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
    psm_of,
)
from .planner import local_midnight

_LOGGER = logging.getLogger(__name__)


def meter_gap(ev: int) -> int:
    """How far below the draw a house average must sit before the meter omits the car."""
    return max(1000, abs(int(ev)) // 5)


def leftover_w(solar_w: int, house_w: int, ev_w: int) -> int:
    """Surplus for a pair already accepted: solar minus the non-EV base.

    A closed pair (house >= ev) has base = house - ev. When house is far below
    the draw, the meter does not contain the car and base is the house reading.
    """
    solar, house, ev = abs(int(solar_w)), abs(int(house_w)), abs(int(ev_w))
    if ev > 0 and house < ev - meter_gap(ev):
        return solar - house
    return solar - house + ev


def _window_steps(trace: list[tuple[datetime, int]], start: datetime, end: datetime):
    """Held value at ``start`` and later steps inside ``[start, end)``.

    ``None`` when no point is at or before ``start``: the unknown prefix is not invented.
    """
    if end <= start:
        return None
    held = None
    inside: list[tuple[datetime, int]] = []
    for moment, value in sorted(trace, key=lambda point: point[0]):
        if moment <= start:
            held = int(value)
        elif moment < end:
            inside.append((moment, int(value)))
    if held is None:
        return None
    return held, inside


def window_mean(trace: list[tuple[datetime, int]], start: datetime, end: datetime) -> int | None:
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
    seconds = (end - start).total_seconds()
    total += value * (end - cursor).total_seconds()
    return int(round(total / seconds))


def window_span(trace: list[tuple[datetime, int]], start: datetime, end: datetime) -> int | None:
    """Max minus min of the draw in effect over ``[start, end)``."""
    steps = _window_steps(trace, start, end)
    if steps is None:
        return None
    held, inside = steps
    values = [held, *(value for _, value in inside)]
    return max(values) - min(values)


def ev_for_window(
    trace: list[tuple[datetime, int]], start: datetime, end: datetime, controller: int | None
) -> tuple[int | None, bool]:
    """Window mean and whether the draw was steady. A partial trace does not fall back to the Controller."""
    mean = window_mean(trace, start, end)
    if mean is None:
        if trace or controller is None:
            return None, False
        return abs(int(controller)), False
    span = window_span(trace, start, end)
    return mean, span is not None and span <= meter_gap(mean)


def pair_base(house_w: int, ev_w: int, steady: bool) -> int | None:
    """Base from one house/draw pair. ``None`` when the pair does not close and is not a steady meter-miss."""
    house, ev = abs(int(house_w)), abs(int(ev_w))
    if house >= ev:
        return house - ev
    if steady and ev > 0 and house < ev - meter_gap(ev):
        return house
    return None


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
    solar = int(solar_w)
    if house_w is not None and ev_avg is not None and pair_base(house_w, ev_avg, steady) is not None:
        return solar, int(house_w), int(ev_avg)
    if previous is None:
        return None
    return solar, previous.house_w, previous.ev_w


def role(settings: Settings, serial: str, plan: Plan, now: datetime, until: bool, keep: bool) -> str:
    policy = settings.policy.get(serial, POLICY_FORCE_OFF)
    if until or policy == POLICY_FORCE_ON:
        return ROLE_FULL
    if plan.in_window(now) and (
        policy == POLICY_SOLAR_AND_GRID or (policy == POLICY_SOLAR_PRIORITY and not plan.enough)
    ):
        return ROLE_FULL
    if keep:
        return ROLE_KEEP
    return ROLE_SURPLUS if policy in SURPLUS_POLICIES else ROLE_OFF


def _offer_w(share: int, phases: int, cap: int) -> int:
    return min(cap, max(MIN_AMP, share // (VOLTS * phases))) * VOLTS * phases


def wanted_psm(share: int, last_psm: int | None, preferred_psm: int, max_a: int, one_cap: int) -> int:
    """Keep the running phase while it can still offer leftover.

    3-phase stays while the share holds 6 A on three phases. 1-phase stays until
    3-phase would deliver more watts than the capped 1-phase amp.
    """
    three_ok = share >= MIN_W * 3
    more_on_three = three_ok and _offer_w(share, 3, max_a) > _offer_w(share, 1, one_cap)
    if last_psm == 2:
        return 2 if three_ok else 1
    if last_psm == 1:
        return 2 if more_on_three else 1
    if more_on_three:
        return 2
    return preferred_psm if three_ok else 1


def amp_for(share: int, psm: int, max_a: int, one_cap: int, group_lot: int) -> int:
    phases = 3 if psm == 2 else 1
    cap = max_a if psm == 2 else one_cap
    return int(min(cap, group_lot, max(MIN_AMP, share // (VOLTS * phases))))


def _on(settings: Settings, psm: int, amp: float) -> Command:
    # lot stays at the fuse cap: a leftover-sized lot lets go-e load
    # balancing clip the car below its amp and the allowed current bounces.
    return Command(True, int(psm), int(settings.group_lot_a), int(amp))


def _fixed_command(settings: Settings, role_: str) -> Command:
    if role_ == ROLE_FULL:
        return _on(settings, 2, settings.max_a)
    if role_ == ROLE_KEEP:
        keep_a = min(32, max(MIN_AMP, settings.after_charge_complete_keep_a))
        return _on(settings, psm_of(settings.after_charge_complete_keep_phase), keep_a)
    return OFF


def _elapsed(since: datetime | None, now: datetime) -> float:
    return 0.0 if since is None else (now - since).total_seconds()


def _command_w(command: Command) -> int:
    return int(command.amp) * VOLTS * (3 if command.psm == 2 else 1) if command.on else 0


def _car_limited(c: Charger, m: ChargerMemory, now: datetime) -> bool:
    """The car stayed clearly below an offer that has been unchanged for SETTLE_S."""
    if not c.taking or m.offer_w is None or _elapsed(m.offer_since, now) < SETTLE_S:
        return False
    return c.nrg_w + max(500, m.offer_w // 10) < m.offer_w


def _unplugged(serial: str, m: ChargerMemory, until: dict, keep: dict, switches: dict) -> None:
    for name, values in (("until_unplug", until), ("keep", keep)):
        if values[serial]:
            values[serial] = False
            switches.setdefault(serial, {})[name] = False
            _LOGGER.info("%s %s off (unplugged)", serial, name)
    m.cut = False
    m.idle_since = None


def _probe_keep(settings, c: Charger, m: ChargerMemory, now, keep: dict, switches: dict, victim: bool):
    """Auto-on keep after 60 s of idle Complete that HA did not cause."""
    can_arm = (
        not keep[c.serial]
        and c.plugged
        and settings.keep_enable.get(c.serial, True)
        and settings.policy.get(c.serial, POLICY_FORCE_OFF) != POLICY_FORCE_OFF
        and not m.cut
        and c.idle_complete
        and not victim
    )
    if not can_arm:
        m.idle_since = None
        return
    if m.idle_since is None:
        m.idle_since = now
        _LOGGER.info("%s keep probe: idle Complete for %ss arms keep", c.serial, KEEP_PROBE_S)
    elif _elapsed(m.idle_since, now) >= KEEP_PROBE_S:
        keep[c.serial] = True
        switches.setdefault(c.serial, {})["keep"] = True
        m.idle_since = None
        _LOGGER.info("%s after-charge-complete keep on", c.serial)


def _phase(settings: Settings, m: ChargerMemory, share: int, now: datetime, one_cap: int) -> tuple[int, int]:
    """Wanted psm, held for hold_minutes on a 1↔3 change.

    CCS cannot switch phases in-session: go-e pauses charging and Tesla raises
    a charging-interrupted alert, so a phase change waits in both directions.
    """
    preferred = psm_of(settings.surplus_preferred_start_phase)
    want = wanted_psm(share, m.psm, preferred, int(settings.max_a), one_cap)
    if m.psm not in (1, 2) or want == m.psm:
        m.phase_since = None
        return want, want
    if m.phase_since is None:
        m.phase_since = now
        _LOGGER.info("holding psm %s (wants %s) for %s min", m.psm, want, settings.hold_minutes)
    if _elapsed(m.phase_since, now) >= settings.hold_s:
        m.phase_since = None
        _LOGGER.info("psm hold expired, switching to psm %s", want)
        return want, want
    return m.psm, want


def _take_sample(house: HouseReading, chargers, roles, now) -> Sample:
    solar, house_w, controller = house.sample
    nrg = {c.serial: max(c.nrg_w or 0, 0) for c in chargers}
    ev = controller if controller is not None else sum(nrg.values())
    keep_take = sum(w for serial, w in nrg.items() if roles[serial] == ROLE_KEEP)
    return Sample(now, abs(solar), abs(house_w), abs(ev), leftover_w(solar, house_w, ev), keep_take)


def decide(
    settings: Settings,
    chargers: list[Charger],
    house: HouseReading,
    plan: Plan,
    now: datetime,
    memory: Memory,
) -> Decision:
    switches: dict[str, dict[str, bool]] = {}
    until = {c.serial: bool(settings.until_unplug.get(c.serial)) for c in chargers}
    keep = {c.serial: bool(settings.keep.get(c.serial)) for c in chargers}
    for c in chargers:
        m = memory.of(c.serial)
        if m.keep_was_on and not keep[c.serial] and c.plugged and not m.cut:
            m.cut = True
            _LOGGER.info("%s keep turned off while plugged: keep cut", c.serial)
        if c.plugged is not None:
            if m.last_plugged and not c.plugged:
                _unplugged(c.serial, m, until, keep, switches)
            m.last_plugged = c.plugged

    order = sorted(chargers, key=lambda c: (float(settings.priority.get(c.serial, c.slot + 1)), c.slot))
    session = any(memory.of(c.serial).surplus_on for c in chargers)
    roles = {c.serial: role(settings, c.serial, plan, now, until[c.serial], keep[c.serial]) for c in chargers}
    for rank, c in enumerate(order):
        victim = session and any(o.taking and roles[o.serial] == ROLE_SURPLUS for o in order[:rank])
        _probe_keep(settings, c, memory.of(c.serial), now, keep, switches, victim)
    roles = {c.serial: role(settings, c.serial, plan, now, until[c.serial], keep[c.serial]) for c in chargers}

    if house.sample is not None:
        memory.sample = _take_sample(house, chargers, roles, now)
    sample, soc = memory.sample, house.soc
    usable = house.usable and sample is not None
    soc_ok = soc is not None and soc >= settings.soc_on_pct - settings.soc_hyst_pct
    budget = sample.budget_w if usable and soc_ok else 0
    can_open = usable and (session or (soc is not None and soc >= settings.soc_on_pct))

    one_cap = int(min(max(MIN_AMP, min(32, settings.max_1phase_amp)), settings.max_a))
    start_w, hold_s = settings.surplus_start_w, settings.hold_s
    priced_out = plan.priced_out_at(now) is not None
    decisions: dict[str, ChargerDecision] = {}
    remaining, higher_on = budget, False
    for c in order:
        m, r = memory.of(c.serial), roles[c.serial]
        if r != ROLE_SURPLUS or (c.idle_complete and not m.cut):
            m.low_since = m.phase_since = None
            m.on_since = m.offer_w = m.offer_since = m.start_armed_at = None
            decisions[c.serial] = ChargerDecision(r, _fixed_command(settings, r))
            continue
        running, share, hold, pending, target_w = m.surplus_on, None, False, False, 0
        if priced_out:
            if running:
                _LOGGER.info("%s surplus off: spot above surplus charging price ceiling", c.serial)
            m.low_since = None
        elif running and m.low_since is not None:
            if remaining >= start_w:
                m.low_since = None
                share = remaining
                _LOGGER.info("%s low hold cancelled at %s W", c.serial, remaining)
            elif _elapsed(m.low_since, now) >= hold_s:
                m.low_since = None
                _LOGGER.info("%s low hold expired: surplus off", c.serial)
            else:
                hold = True
        elif running and remaining < MIN_W:
            m.low_since, hold = now, True
            _LOGGER.info("%s low hold: 6 A for %s min (share %s W)", c.serial, settings.hold_minutes, remaining)
        elif running:
            share = remaining
        elif can_open and remaining >= start_w:
            if not higher_on or c.taking or (m.start_armed_at is not None and sample.at > m.start_armed_at):
                share = remaining
            else:
                m.start_armed_at = m.start_armed_at or sample.at
                pending = True
        if not pending:
            m.start_armed_at = None
        caps = (int(settings.max_a), one_cap, int(settings.group_lot_a))
        if hold:
            m.phase_since = None
            command = _on(settings, m.psm or 1, MIN_AMP)
            target_w = _command_w(command)
        elif share is not None:
            psm, want = _phase(settings, m, int(share), now, one_cap)
            command = _on(settings, psm, amp_for(int(share), psm, *caps))
            target_w = max(_command_w(command), _command_w(_on(settings, want, amp_for(int(share), want, *caps))))
        else:
            m.phase_since = None
            command = OFF
        if command.on and not running:
            m.on_since = now
        elif not command.on:
            m.on_since = None
        limited = _car_limited(c, m, now)
        if not command.on:
            reserve = c.nrg_w if c.taking else 0
        elif c.taking:
            reserve = min(target_w, c.nrg_w) if limited else target_w
        else:
            reserve = target_w if _elapsed(m.on_since, now) < START_GRACE_S else 0
        offered = _command_w(command)
        if not command.on:
            m.offer_w = m.offer_since = None
        elif offered != m.offer_w:
            m.offer_w, m.offer_since = offered, now
        decisions[c.serial] = ChargerDecision(
            r,
            command,
            share_w=None if share is None else int(share),
            low_hold_until=None if m.low_since is None else m.low_since + timedelta(seconds=hold_s),
            phase_hold_until=None if m.phase_since is None else m.phase_since + timedelta(seconds=hold_s),
            reserve_w=int(reserve),
            limited=limited,
            start_pending=pending,
            priced_out=priced_out,
        )
        if remaining > 0:
            remaining -= min(reserve, remaining)
        higher_on = higher_on or command.on

    for c in chargers:
        m, d = memory.of(c.serial), decisions[c.serial]
        unfinished = c.car in (CAR_CHARGING, CAR_WAITCAR, CAR_ERROR)
        if d.command.on and unfinished and m.cut:
            m.cut = False
            _LOGGER.info("%s charging allowed again before Complete: keep cut cleared", c.serial)
        elif m.last_on and not d.command.on and unfinished and not m.cut:
            m.cut = True
            _LOGGER.info("%s stopped while car %s: keep cut", c.serial, c.car)
        m.last_on = d.command.on
        m.surplus_on = d.role == ROLE_SURPLUS and d.command.on
        m.psm = d.command.psm if d.command.on else None
        m.keep_was_on = keep[c.serial]

    return Decision(decisions, switches, int(budget), _next_wakeup(memory, chargers, plan, now, hold_s))


def _next_wakeup(memory: Memory, chargers, plan: Plan, now: datetime, hold_s: float) -> datetime:
    hold = timedelta(seconds=hold_s)
    times = [local_midnight(now.date() + timedelta(days=1), now.tzinfo) + timedelta(seconds=30)]
    for c in chargers:
        m = memory.of(c.serial)
        times += [t + hold for t in (m.low_since, m.phase_since) if t is not None]
        if m.idle_since is not None:
            times.append(m.idle_since + timedelta(seconds=KEEP_PROBE_S))
        if m.on_since is not None:
            times.append(m.on_since + timedelta(seconds=START_GRACE_S))
    times += [t for t in (plan.next_boundary(now), plan.usable_end) if t is not None]
    return min(t for t in times if t > now)
