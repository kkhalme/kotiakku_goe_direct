"""Leftover watts and combined lot/psm/amp. Matches automation_amp.yaml."""

from __future__ import annotations

import datetime
import json
import math

PLUGGED_STATES = {
    "2",
    "3",
    "4",
    "5",
    "charging",
    "waitcar",
    "complete",
    "error",
    "waitforcar",
    "waitforvehicle",
    "finished",
    "connected",
}

CHARGING_STATES = {
    "2",
    "charging",
}

FINISHED_STATES = {
    "4",
    "complete",
    "finished",
}


def watts(state, in_kw, default=0):
    try:
        value = abs(float(state))
    except (TypeError, ValueError):
        return default
    return int(value * (1000 if in_kw else 1))


def house_includes_ev(house_w, ev_w):
    """True when house watts already contain the EV take.

    Leftover is ``solar − house + EV`` only in that case (house CT includes
    the car, so EV must be added back). If house is clearly below the EV
    take — house excludes the charger, or the Controller 5-min mean still
    has a car that just unplugged — adding EV invents surplus and will
    keep charging from the grid.
    """
    house_w = int(house_w)
    ev_w = int(ev_w)
    if ev_w <= 0:
        return True
    margin = max(1000, ev_w // 5)
    return house_w >= ev_w - margin


def effective_ev_w(controller_w, nrg_w=None, *, controller_usable=True):
    """EV watts for leftover: instant charger ``nrg`` when known.

    Controller Car-power is a 5-min mean. After a car unplugs or amp
    drops, that mean stays high while house has already fallen. Prefer
    the lower of Controller and summed charger ``nrg`` so leftover is
    not inflated. Unknown Controller with ``nrg`` uses ``nrg``. Missing
    ``nrg`` keeps Controller (or 0).
    """
    nrg = None if nrg_w is None else max(int(nrg_w), 0)
    ctrl = max(int(controller_w or 0), 0) if controller_usable else None
    if nrg:
        if ctrl:
            return min(ctrl, nrg)
        return nrg
    return ctrl or 0


def leftover_w(solar_w, house_w, ev_w):
    solar_w = int(solar_w)
    house_w = int(house_w)
    ev_w = max(int(ev_w), 0)
    if ev_w > 0 and not house_includes_ev(house_w, ev_w):
        return solar_w - house_w
    return solar_w - house_w + ev_w


UNUSABLE_STATES = ("", "unknown", "unavailable", "none", "nan")


def sensor_usable(state):
    """True if a Kotiakku sensor state can be used as a number."""
    if state is None:
        return False
    text = str(state).strip().lower()
    if text in UNUSABLE_STATES:
        return False
    try:
        value = float(state)
    except (TypeError, ValueError):
        return False
    return value == value  # NaN


POWER_UNITS = ("w", "kw", "watt", "kwatt", "kwatts", "watts")
WH_UNITS = ("wh", "watthour", "watthours")
MWH_UNITS = ("mwh",)


def energy_kwh(state, unit=None):
    """Parse an energy-forecast sensor to kWh. None if unusable or power."""
    if not sensor_usable(state):
        return None
    value = float(state)
    text = str(unit or "").strip().lower().replace(" ", "").replace("_", "")
    if text in POWER_UNITS:
        return None
    if text in WH_UNITS:
        return value / 1000.0
    if text in MWH_UNITS:
        return value * 1000.0
    return value


def upcoming_solar_kwh(today_kwh, tomorrow):
    """Headline forecast kWh: max of today's full-day estimate and tomorrow.

    Missing values are ignored; both missing → None.
    """
    values = [v for v in (today_kwh, tomorrow) if v is not None]
    if not values:
        return None
    return max(values)


def enough_solar(upcoming_kwh, threshold_kwh):
    """True when ``upcoming_kwh`` is at least the threshold.

    Unknown energy or a non-positive threshold is not enough.
    """
    try:
        threshold = float(threshold_kwh)
    except (TypeError, ValueError):
        return False
    if threshold <= 0 or upcoming_kwh is None:
        return False
    return float(upcoming_kwh) >= threshold


# Home Assistant latitude/longitude override these. Fallback is southern
# Finland (Helsinki) when the HA instance has no site location.
DEFAULT_LAT = 60.17
DEFAULT_LON = 24.94


def last_sun_end_ts(
    clock,
    day_start,
    day_end,
    lat=DEFAULT_LAT,
    lon=DEFAULT_LON,
    step_s=900,
):
    """Exclusive end of the last local sample today with sun above the horizon.

    None if the sun never rises (polar night). Polar day: last sample before
    ``day_end``. Uses the same elevation model as off-sun hour weights.
    """
    try:
        t = day_start
        end_ts = float(clock.as_timestamp(day_end))
    except Exception:
        return None
    step = datetime.timedelta(seconds=int(step_s))
    last = None
    while True:
        try:
            ts = float(clock.as_timestamp(t))
        except Exception:
            break
        if ts >= end_ts - 1:
            break
        if _solar_weight(t, lat, lon) > 0:
            last = min(ts + float(step_s), end_ts)
        t = t + step
    return last


def gating_solar_kwh(
    clock,
    today_kwh,
    tomorrow_kwh,
    lat=DEFAULT_LAT,
    lon=DEFAULT_LON,
):
    """kWh that gates 22 kW: today's full-day estimate until sunset, then tomorrow.

    Before today's last sun (including pre-dawn): ``today_kwh``. After sunset,
    polar night, or if sunset cannot be computed: ``tomorrow_kwh``.
    """
    now = clock.now()
    try:
        today_start = clock.start_of_local_day(now)
        today_end = today_start + datetime.timedelta(days=1)
        now_ts = float(clock.as_timestamp(now))
    except Exception:
        return tomorrow_kwh
    sunset = last_sun_end_ts(clock, today_start, today_end, lat, lon)
    if sunset is None or now_ts >= sunset:
        return tomorrow_kwh
    return today_kwh


def gating_solar_day(clock, lat=DEFAULT_LAT, lon=DEFAULT_LON):
    """``today`` until sunset; ``tomorrow`` after sunset or polar night."""
    now = clock.now()
    try:
        today_start = clock.start_of_local_day(now)
        today_end = today_start + datetime.timedelta(days=1)
        now_ts = float(clock.as_timestamp(now))
    except Exception:
        return "tomorrow"
    sunset = last_sun_end_ts(clock, today_start, today_end, lat, lon)
    if sunset is None or now_ts >= sunset:
        return "tomorrow"
    return "today"


def enough_solar_now(
    clock,
    today_kwh,
    tomorrow_kwh,
    threshold_kwh,
    lat=DEFAULT_LAT,
    lon=DEFAULT_LON,
):
    """Skip 22 kW when the gating day's full-day kWh is at least the threshold."""
    return enough_solar(
        gating_solar_kwh(clock, today_kwh, tomorrow_kwh, lat, lon),
        threshold_kwh,
    )


def solar_elevation_deg(when, lat=DEFAULT_LAT, lon=DEFAULT_LON):
    """Approximate solar elevation in degrees (no refraction)."""
    utc = when.astimezone(datetime.timezone.utc)
    n = when.timetuple().tm_yday
    decl = 23.45 * math.sin(math.radians(360.0 / 365.0 * (n - 81)))
    hour = utc.hour + utc.minute / 60.0 + utc.second / 3600.0
    ha = 15.0 * (hour + lon / 15.0 - 12.0)
    sin_el = math.sin(math.radians(lat)) * math.sin(math.radians(decl)) + math.cos(
        math.radians(lat)
    ) * math.cos(math.radians(decl)) * math.cos(math.radians(ha))
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_el))))


def _solar_weight(when, lat, lon):
    el = solar_elevation_deg(when, lat, lon)
    if el <= 0:
        return 0.0
    return math.sin(math.radians(el))


def _hour_floor(clock, when):
    midnight = clock.start_of_local_day(when)
    elapsed = float(clock.as_timestamp(when)) - float(clock.as_timestamp(midnight))
    hour = int(elapsed // 3600)
    if hour < 0:
        hour = 0
    return midnight + datetime.timedelta(hours=hour)


def _merge_ranges(ranges):
    if not ranges:
        return []
    ranges = sorted((float(start), float(end)) for start, end in ranges if end > start)
    if not ranges:
        return []
    out = [list(ranges[0])]
    for start, end in ranges[1:]:
        if start <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return [(start, end) for start, end in out]


def expected_hour_kwh(
    clock,
    start,
    end,
    energy_kwh,
    lat=DEFAULT_LAT,
    lon=DEFAULT_LON,
    step_s=900,
):
    """Spread ``energy_kwh`` across local hours in ``[start, end)`` by solar weight.

    Each item is ``(hour_start_ts, hour_end_ts, kwh)``. Night hours get 0.
    Unknown or unusable energy → no hours (do not invent a profile).
    """
    if energy_kwh is None:
        return []
    try:
        energy = float(energy_kwh)
    except (TypeError, ValueError):
        return []
    if energy < 0:
        energy = 0.0
    try:
        t = start
        end_ts = float(clock.as_timestamp(end))
    except Exception:
        return []
    step = datetime.timedelta(seconds=int(step_s))
    weights = {}
    order = []
    while True:
        try:
            ts = float(clock.as_timestamp(t))
        except Exception:
            break
        if ts >= end_ts - 1:
            break
        hour = _hour_floor(clock, t)
        key = float(clock.as_timestamp(hour))
        if key not in weights:
            weights[key] = 0.0
            order.append(key)
        weights[key] += _solar_weight(t, lat, lon)
        t = t + step
    total = sum(weights.values())
    out = []
    for hour_ts in order:
        kwh = (energy * weights[hour_ts] / total) if total > 0 else 0.0
        out.append((hour_ts, hour_ts + 3600.0, kwh))
    return out


def surplus_hour_ranges(
    clock,
    today_kwh,
    tomorrow_kwh,
    hour_kwh,
    lat=DEFAULT_LAT,
    lon=DEFAULT_LON,
):
    """Hours whose expected forecast energy is at least ``hour_kwh``.

    Today's full-day kWh is spread over the local day (midnight–midnight);
    tomorrow kWh over the next local day. Spot windows stay independent of
    Kotiakku leftover. Unknown energy or a non-positive hour threshold
    excludes nothing (SolarPriority then searches every price slot).
    """
    try:
        threshold = float(hour_kwh)
    except (TypeError, ValueError):
        return []
    if threshold <= 0:
        return []
    now = clock.now()
    try:
        today_start = clock.start_of_local_day(now)
        today_end = today_start + datetime.timedelta(days=1)
        tomorrow_end = today_end + datetime.timedelta(days=1)
    except Exception:
        return []
    hours = []
    hours.extend(
        expected_hour_kwh(clock, today_start, today_end, today_kwh, lat, lon)
    )
    hours.extend(
        expected_hour_kwh(clock, today_end, tomorrow_end, tomorrow_kwh, lat, lon)
    )
    blocked = [(start, end) for start, end, kwh in hours if kwh >= threshold]
    return _merge_ranges(blocked)


def surplus_decision(
    session,
    leftover,
    soc,
    *,
    window_ok,
    soc_on=92,
    soc_hyst=2,
    start_min_w=2000,
    hold_min_w=1000,
    floor_expired=False,
    hold_active=False,
    hold_exit_w=None,
):
    """Start / hold / stop for leftover surplus.

    Start: SoC ≥ soc_on and leftover ≥ start_min_w.
    Low hold (6 A for hold_min): leftover < hold_min_w, SoC below
    soc_on − hyst, or Kotiakku SoC/solar/house unusable. Recovered
    sensors cancel the timer. Cannot start while sensors are unusable.
    Once the low-hold timer is running, leftover must reach
    ``hold_exit_w`` (default start leftover) before the hold cancels, so
    chatter around 1000 W cannot reset the 15 min forever.
    """
    soc_start = window_ok and soc >= soc_on
    soc_low = window_ok and soc < (soc_on - soc_hyst)
    leftover_low = leftover < hold_min_w
    try:
        exit_w = hold_min_w if hold_exit_w is None else int(hold_exit_w)
    except (TypeError, ValueError):
        exit_w = hold_min_w
    exit_w = max(int(hold_min_w), exit_w)
    if hold_active:
        leftover_low = leftover < exit_w
    in_low_hold = (not window_ok) or leftover_low or soc_low
    write_off = session and floor_expired and in_low_hold
    write_on = not write_off and (
        session or (window_ok and soc_start and leftover >= start_min_w)
    )
    arm_floor = bool((write_on or (session and not write_off)) and in_low_hold)
    return {
        "write_on": write_on,
        "write_off": write_off,
        "arm_floor": arm_floor,
        "use_floor_budget": write_on and in_low_hold,
        "in_low_hold": in_low_hold,
    }


def budget(available_w, min_amp, max_amp, group_lot, volts, phase3_min_w, force_psm=None):
    min_amp = int(min_amp)
    volts = int(volts)
    min_hold_w = min_amp * volts
    target_w = max(int(available_w), min_hold_w)
    try:
        force_psm = None if force_psm is None else int(force_psm)
    except (TypeError, ValueError):
        force_psm = None
    if force_psm == 2:
        phases = 3
        target_w = max(target_w, min_amp * volts * 3)
    elif force_psm == 1:
        phases = 1
    else:
        phases = 3 if target_w >= int(phase3_min_w) else 1
    psm = 2 if phases == 3 else 1
    lot = min(int(group_lot), max(min_amp, target_w // (volts * phases)))
    amp = min(int(max_amp), lot)
    return lot, psm, amp


def phase_hold_psm(wanted_psm, last_psm, hold_expired=False):
    """Keep last ``psm`` until leftover has wanted a new phase for hold_min.

    CCS does not switch 1-phase ↔ 3-phase in-session. go-e ``psm`` therefore
    pauses charging for several seconds. Tesla surfaces that as a customer
    alert (charging stopped / interrupted, often CP_a055). Hold both 1→3
    and 3→1 so leftover chatter does not spam the app. Amp still tracks
    leftover on the held phase.
    """
    wanted_psm = int(wanted_psm)
    if last_psm is None:
        return {"psm": wanted_psm, "arm": False}
    try:
        last_psm = int(last_psm)
    except (TypeError, ValueError):
        return {"psm": wanted_psm, "arm": False}
    if last_psm not in (1, 2):
        return {"psm": wanted_psm, "arm": False}
    if last_psm == wanted_psm:
        return {"psm": wanted_psm, "arm": False}
    if hold_expired:
        return {"psm": wanted_psm, "arm": False}
    return {"psm": last_psm, "arm": True}


def surplus_phase_budget(
    available_w,
    min_amp,
    max_amp,
    group_lot,
    volts,
    phase3_min_w,
    *,
    last_psm=None,
    hold_expired=False,
):
    """``lot`` / ``psm`` / ``amp`` with 1↔3 hold.

    ``psm`` may wait ``hold_min``, but ``amp`` is always leftover on the
    phase we will actually run — not the pending other-phase amp, and
    not the last take. 1→3: 1-phase leftover (capped at max amp).
    3→1: 3-phase min amp. Holding ``psm`` must not freeze amp.
    """
    _lot, wanted_psm, _wanted_amp = budget(
        available_w, min_amp, max_amp, group_lot, volts, phase3_min_w
    )
    hold = phase_hold_psm(wanted_psm, last_psm, hold_expired)
    lot, psm, amp = budget(
        available_w,
        min_amp,
        max_amp,
        group_lot,
        volts,
        phase3_min_w,
        force_psm=hold["psm"],
    )
    return {
        "lot": lot,
        "psm": psm,
        "amp": amp,
        "arm_phase": hold["arm"],
        "wanted_psm": wanted_psm,
    }


def group_lot_for_amps(lot, amps, group_lot):
    """Raise leftover ``lot`` so a held 1-phase ``amp`` still fits.

    Equal leftover already shares one group ``lot``; do not sum identical
    amps or two 17 A cars would raise 12 kW leftover to 34 A. Differing
    amps (priority split or mixed 1-phase / 3-phase hold) still need the
    sum so both caps fit, at most ``group_lot``.
    """
    lot = int(lot)
    amps = [int(amp) for amp in amps]
    if not amps:
        return lot
    if len(set(amps)) <= 1:
        return min(int(group_lot), max(lot, amps[0]))
    return min(int(group_lot), max(lot, sum(amps)))


def group_surplus_setpoint(lot, psm, amp, *, n_full, group_lot):
    """MQTT lot/psm/amp for surplus chargers in a load-balancing group.

    Pure surplus: leftover ``lot`` is the group total when every surplus
    charger gets the same leftover. Differing per-charger ``amp`` shares
    may raise that ``lot`` so both caps fit. go-e load balancing and the
    app's charger priorities (``lop``) still apply to the 50 A group.
    HA leftover split uses HA priority numbers, not app ``lop``. HA does
    not write ``lop``.

    Mixed (another charger is full-power): do not write leftover ``lot`` —
    last writer would shrink the shared group. Keep ``lot`` at group_lot
    and keep leftover ``amp`` / ``psm`` as that charger's leftover cap.
    Combined demand may exceed the group; app priorities split it. Do not
    reserve current for the full-power charger by capping surplus ``amp``.
    """
    lot = int(lot)
    psm = int(psm)
    amp = int(amp)
    if int(n_full) <= 0:
        return lot, psm, amp
    return int(group_lot), psm, amp


def group_lot_for_allocations(
    lot,
    allocations,
    *,
    min_amp,
    max_amp,
    group_lot,
    volts,
    phase3_min_w,
):
    """Keep leftover ``lot`` when every surplus charger gets the same watts.

    Differing shares (priority split / steal) use 1-phase and 3-phase
    ``amp`` together. Raise group ``lot`` to the sum of those amps so
    load balancing can actually deliver both, still at most ``group_lot``.
    """
    lot = int(lot)
    if not isinstance(allocations, dict) or len(allocations) < 2:
        return lot
    watts_values = [max(int(watts_i), 0) for watts_i in allocations.values()]
    if len(set(watts_values)) <= 1:
        return lot
    amp_sum = sum(
        int(budget(watts_i, min_amp, max_amp, group_lot, volts, phase3_min_w)[2])
        for watts_i in watts_values
    )
    return min(int(group_lot), max(lot, amp_sum))


def parse_lop(state):
    """Priority 1–99 (1 is highest). Same scale as go-e ``lop``. None if unknown."""
    if not sensor_usable(state):
        return None
    value = int(round(float(state)))
    if value < 1 or value > 99:
        return None
    return value


def _norm_car(state):
    if state is None:
        return ""
    return str(state).lower().replace(" ", "").replace("_", "")


def car_plugged(state):
    if state is None:
        return False
    return _norm_car(state) in PLUGGED_STATES


def car_charging(state):
    return _norm_car(state) in CHARGING_STATES


def car_finished(state):
    return _norm_car(state) in FINISHED_STATES


def min_charge_w(remaining, min_amp, volts, phase3_min_w):
    """Watts for the official 6 A floor at the leftover's 1- or 3-phase."""
    remaining = max(int(remaining), 0)
    min_amp = int(min_amp)
    volts = int(volts)
    if remaining >= int(phase3_min_w):
        return min_amp * volts * 3
    return min_amp * volts


def nrg_total_w(payload):
    """Total charger watts from go-e ``nrg`` (v2 index 11) or a numeric sensor."""
    if payload is None:
        return None
    values = None
    if isinstance(payload, (list, tuple)):
        values = list(payload)
    elif isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", "replace")
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return None
        if text.startswith("[") or text.startswith("{"):
            try:
                data = json.loads(text)
            except Exception:
                data = None
            if isinstance(data, dict):
                data = data.get("nrg")
            if isinstance(data, (list, tuple)):
                values = list(data)
        elif "," in text:
            try:
                values = [float(part) for part in text.split(",")]
            except (TypeError, ValueError):
                values = None
    if values is not None:
        if len(values) > 11:
            try:
                return abs(int(round(float(values[11]))))
            except (TypeError, ValueError):
                return None
        return None
    if sensor_usable(payload):
        return watts(payload, False)
    return None


def charger_take_w(state, power_w, leftover_w, charger_max_w):
    """Watts this car is taking from leftover. 0 if it is not accepting."""
    leftover_w = max(int(leftover_w), 0)
    cap = min(leftover_w, max(int(charger_max_w), 0))
    if not car_plugged(state) or car_finished(state):
        return 0
    if not car_charging(state):
        return 0
    if power_w is None or int(power_w) < 100:
        return cap
    return min(max(int(power_w), 0), cap)


def surplus_want_w(
    leftover_w,
    take_w,
    *,
    last_amp=None,
    last_psm=None,
    volts=230,
    min_amp=6,
    max_amp=32,
    group_lot=50,
    phase3_min_w=4140,
):
    """Watts the car should be treated as wanting from leftover.

    MQTT ``amp`` must track leftover, not stick at the last take. If the
    car is at the published amp cap and leftover would budget a higher
    amp (or switch 1-phase → 3-phase), treat it as wanting all leftover so
    3-phase is not locked at 6 A. A take below 100 W is not accepting.
    Unknown take (None) wants leftover.
    """
    leftover_w = max(int(leftover_w), 0)
    if take_w is None:
        return leftover_w
    try:
        take_w = int(take_w)
    except (TypeError, ValueError):
        return leftover_w
    if take_w < 100:
        return take_w
    take_w = min(take_w, leftover_w)
    _lot, offer_psm, offer_amp = budget(
        leftover_w, min_amp, max_amp, group_lot, volts, phase3_min_w
    )
    if last_amp is None or last_psm is None:
        return leftover_w
    last_amp = int(last_amp)
    last_psm = int(last_psm)
    phases = 3 if last_psm == 2 else 1
    cap_w = last_amp * int(volts) * phases
    slack = int(volts) * phases
    at_cap = take_w >= cap_w - slack
    can_raise = offer_amp > last_amp or (offer_psm == 2 and last_psm != 2)
    if at_cap and can_raise:
        return leftover_w
    return take_w


def _can_charge(watts, min_amp, volts, phase3_min_w):
    watts = max(int(watts), 0)
    return watts >= min_charge_w(watts, min_amp, volts, phase3_min_w)


def _serial_take(serial, remaining, charger_max_w, take_w, states):
    offered = min(max(int(remaining), 0), max(int(charger_max_w), 0))
    if take_w is not None and serial in take_w:
        return min(max(int(take_w[serial]), 0), remaining, charger_max_w)
    if states is not None:
        return charger_take_w(states.get(serial), None, remaining, charger_max_w)
    return offered


def _steal_keep_w(remaining, prev_take, split_min_w, min_amp, volts, phase3_min_w):
    """Watts the high car keeps after a split_min steal. None unless both
    shares are at least ``split_min_w`` and still meet 6 A."""
    split_min_w = int(split_min_w)
    keep_w = int(remaining) + int(prev_take) - split_min_w
    if keep_w < split_min_w:
        return None
    if not _can_charge(keep_w, min_amp, volts, phase3_min_w):
        return None
    if not _can_charge(split_min_w, min_amp, volts, phase3_min_w):
        return None
    return keep_w


def surplus_allocation_plan(
    serials,
    *,
    lops,
    plugged,
    leftover_w,
    split_min_w,
    charger_max_w,
    take_w=None,
    states=None,
    min_amp=6,
    volts=230,
    phase3_min_w=4140,
    split_floor_w=500,
    split_hold=False,
    split_expired=False,
):
    """Per-charger leftover watts plus next-car hold flags.

    Surplus MQTT does not wait for a car. Every listed surplus charger
    that is not finished (Complete) can be offered leftover, including
    Idle, unknown, or unplugged, so ``frc=2`` can arm the charger before
    WaitCar. Equal or unknown HA priority: those chargers get the same
    leftover (go-e splits). Unequal: steal/take follows actual take
    (≥100 W), not plug-in. ``plugged`` is kept for callers and ignored.

    A high-priority car that is not taking still gets leftover MQTT so
    it can start, but leftover itself goes to the next car in priority
    order as the first. If nobody is taking, every eligible charger is
    armed at leftover watts. Finished chargers are skipped so remaining
    equal-priority cars still share. After a taking first car, unused
    leftover above ``split_floor_w`` (default 500 W) goes to the next
    car in priority — even if that car is not taking yet, so it can
    start. If that remainder is below ``split_min_w`` (default 3 kW),
    cut the high-priority share so the next car still gets 3 kW — only
    if leftover itself is at least ``2 × split_min_w`` (each car keeps
    at least 3 kW), the first is actually taking power, and both shares
    still meet 6 A. Remainder at or below 500 W is a dead zone: do not
    *start* the next car. If the next car was already taking and leftover
    then shrinks so the first would use it all, keep stealing 3 kW for
    the hold minutes unless ``split_expired`` or leftover is below 6 kW.
    ``lops`` is HA charger priority, not app ``lop``. HA does not write
    ``lop``. Group ``lot`` uses steal/share watts only: leading Idle
    arms do not inflate the sum.
    """
    leftover_w = max(int(leftover_w), 0)
    serials = [serial for serial in serials if serial]
    plugged = plugged  # steal follows take; kept for existing callers
    empty = {
        "allocations": {},
        "remainder_w": leftover_w,
        "arm_split_hold": False,
        "taking": [],
        "lot_allocations": {},
    }
    if not serials:
        return empty
    take_w = take_w if isinstance(take_w, dict) else None
    states = states if isinstance(states, dict) else None
    eligible = [
        serial
        for serial in serials
        if states is None or not car_finished(states.get(serial))
    ]
    if not eligible or leftover_w <= 0:
        return empty
    charger_max_w = max(int(charger_max_w), 0)
    split_min_w = int(split_min_w)
    split_floor_w = int(split_floor_w)
    shared = {serial: leftover_w for serial in eligible}

    def _take_of(serial, remaining=leftover_w):
        return _serial_take(serial, remaining, charger_max_w, take_w, states)

    def _is_taking(serial, remaining=leftover_w):
        return _take_of(serial, remaining) >= 100

    def _pack(allocations, remainder_w, arm_split_hold, leading=()):
        lot_allocations = dict(allocations)
        out = dict(allocations)
        for serial in leading:
            out[serial] = leftover_w
        taking = [serial for serial in lot_allocations if _is_taking(serial)]
        return {
            "allocations": out,
            "remainder_w": remainder_w,
            "arm_split_hold": arm_split_hold,
            "taking": taking,
            "lot_allocations": lot_allocations,
        }

    def _shared():
        return _pack(shared, leftover_w, False)

    ranks = []
    for serial in eligible:
        rank = lops.get(serial) if isinstance(lops, dict) else None
        if rank is None:
            return _shared()
        ranks.append(int(rank))
    if len(eligible) == 1 or len(set(ranks)) <= 1:
        return _shared()
    order = sorted(
        eligible, key=lambda serial: (int(lops[serial]), serials.index(serial))
    )
    leading = []
    pool = list(order)
    while pool and not _is_taking(pool[0]):
        leading.append(pool.pop(0))
    if not pool:
        return _shared()
    if len(pool) == 1:
        return _pack({pool[0]: leftover_w}, leftover_w, False, leading)
    remaining = leftover_w
    allocations = {}
    prev = None
    prev_take = 0
    remainder_after_high = leftover_w
    for serial in pool:
        if not allocations:
            if not _can_charge(remaining, min_amp, volts, phase3_min_w):
                break
            offered = min(remaining, charger_max_w)
            take = _take_of(serial, remaining)
            allocations[serial] = offered if take <= 0 else take
            remaining -= take
            remainder_after_high = remaining
            prev = serial
            prev_take = take
            continue
        need = min_charge_w(remaining, min_amp, volts, phase3_min_w)
        if remaining >= split_min_w and remaining >= need:
            offered = min(remaining, charger_max_w)
            take = _take_of(serial, remaining)
            allocations[serial] = offered
            remaining -= take
            prev = serial
            prev_take = take
            continue
        in_dead = remaining <= split_floor_w
        want_steal = (
            leftover_w >= 2 * split_min_w
            and prev_take >= 100
            and ((not in_dead) or (split_hold and not split_expired))
        )
        keep_w = _steal_keep_w(
            remaining, prev_take, split_min_w, min_amp, volts, phase3_min_w
        )
        if want_steal and prev is not None and keep_w is not None:
            allocations[prev] = keep_w
            allocations[serial] = split_min_w
            remaining = 0
            prev = serial
            prev_take = split_min_w
            continue
        if remaining >= need:
            offered = min(remaining, charger_max_w)
            take = _take_of(serial, remaining)
            allocations[serial] = offered
            remaining -= take
            prev = serial
            prev_take = take
            continue
        break
    taking = [serial for serial in allocations if _is_taking(serial)]
    arm_split_hold = bool(
        remainder_after_high <= split_floor_w
        and split_hold
        and not split_expired
        and len(taking) >= 2
    )
    return _pack(allocations, remainder_after_high, arm_split_hold, leading)


def surplus_allocations(*args, **kwargs):
    """Per-charger leftover watts for surplus MQTT."""
    return surplus_allocation_plan(*args, **kwargs)["allocations"]


def surplus_targets(*args, **kwargs):
    """Serials that should get leftover MQTT, in priority order."""
    return list(surplus_allocations(*args, **kwargs))
