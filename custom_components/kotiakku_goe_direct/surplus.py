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

# Seconds to wait after leftover MQTT before cutting a charger. Over-draw is allowed.
OFFER_WAIT_S = 15

# Below this, leftover still offers (nrg often 0 at start). Surplus treats a
# Charging car as taking leftover (steal / offer-wait). Keep pool subtract
# counts every watt of keep ``nrg``; it does not use this floor.
TAKE_MIN_W = 100

# Idle Complete / Sentry band. Below this, leftover does not offer a
# Complete car and keep may start its 60 s probe. Live Complete at or
# above this is leftover-eligible as taking. Do not use this as leftover
# has-started (that stays TAKE_MIN_W) or as keep-pool subtract.
KEEP_PROBE_TAKE_W = 400


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
    take — house excludes the charger, or Kotiakku house has not yet
    caught a car the Controller already sees — adding EV invents surplus
    and will keep charging from the grid.
    """
    house_w = int(house_w)
    ev_w = int(ev_w)
    if ev_w <= 0:
        return True
    margin = max(1000, ev_w // 5)
    return house_w >= ev_w - margin


def effective_ev_w(controller_w, nrg_w=None, *, controller_usable=True):
    """EV watts for leftover: go-e Controller, not instant charger ``nrg``.

    Controller Car-power updates much faster than Kotiakku. Instant
    ``nrg`` follows the car even more closely. Feeding either straight
    into surplus ``amp`` made the pilot track the take (16 A ↔ 21 A,
    or 30 A ↔ 6 A) so Tesla stayed at the lower value. Use Controller
    while it is usable; the Kotiakku sample hold decides when that
    number may change ``amp``. Instant ``nrg`` is the fallback when
    Controller is unknown. Missing ``nrg`` then is 0.
    """
    nrg = None if nrg_w is None else max(int(nrg_w), 0)
    if controller_usable:
        return max(int(controller_w or 0), 0)
    return nrg or 0


def leftover_w(solar_w, house_w, ev_w):
    solar_w = int(solar_w)
    house_w = int(house_w)
    ev_w = max(int(ev_w), 0)
    if ev_w > 0 and not house_includes_ev(house_w, ev_w):
        return solar_w - house_w
    return solar_w - house_w + ev_w


def keep_take_w(power_w):
    """Watts a keep charger is pulling from the house pool.

    Unknown ``nrg`` is 0. Every known watt counts, including Complete
    trickle and Sentry; ``TAKE_MIN_W`` is only leftover has-started.
    """
    if power_w is None:
        return 0
    try:
        power_w = int(power_w)
    except (TypeError, ValueError):
        return 0
    return max(power_w, 0)


def surplus_sensor_samples(entity_id, *kotiakku_ids):
    """True when this sensor may resample leftover watts for ``amp``.

    Pass Kotiakku SoC, solar, and house. Those update about every
    5 min. The go-e Controller and charger ``nrg`` update much faster
    and must not be passed: sampling them retunes ``amp`` between
    Kotiakku reports.
    """
    if not entity_id:
        return False
    return entity_id in {eid for eid in kotiakku_ids if eid}


def surplus_sample_armed(old_state, new_state, entity_id, *kotiakku_ids):
    """True when a Kotiakku state change may move leftover ``amp``.

    Same state string (attribute-only refresh) and an unusable new
    state do not arm a sample. Those updates are much faster than the
    5 min Kotiakku value and would bounce ``amp`` and the surplus sensor.
    """
    if old_state == new_state:
        return False
    if not surplus_sensor_samples(entity_id, *kotiakku_ids):
        return False
    return sensor_usable(new_state)


def surplus_sensor_w(held_w, *, usable=True):
    """Watts for ``sensor.kotiakku_goe_direct_available_surplus``.

    This is the held Kotiakku leftover, after keep take. ``None`` until
    that hold exists, and whenever Kotiakku cannot be read. Controller
    and charger ``nrg`` must not change this number between reports.
    """
    if not usable or held_w is None:
        return None
    try:
        return int(held_w)
    except (TypeError, ValueError):
        return None


def charger_lot_needs_restore(live_lot, group_lot):
    """True when charger ``lot`` is not the fuse cap.

    A leftover-sized ``lot`` (for example 19 A) lets go-e load balancing
    clip the car and bounce allowed current. ``None`` has not been seen
    yet, so there is nothing to correct.
    """
    if live_lot is None:
        return False
    try:
        return int(live_lot) != int(group_lot)
    except (TypeError, ValueError):
        return False


def surplus_held_w(
    live_w,
    held_w,
    held_ts,
    now_ts,
    *,
    refresh=False,
    allow_sample=False,
):
    """Leftover watts for surplus amp and start/hold/stop.

    First sample (empty hold), ``refresh``, and ``allow_sample`` take
    ``live_w``. ``allow_sample`` is a Kotiakku SoC, solar, or house
    report whose state value changed (``surplus_sample_armed``). Those
    sensors are already about every 5 min, so a report updates ``amp``
    immediately — including one that arrives while the 6 A floor is
    holding an older leftover. Controller and charger ``nrg`` must not
    set ``allow_sample``. A session gap must not set ``refresh`` and
    must not clear the hold: the next fast tick would otherwise reseed
    from live leftover and bounce the pilot (30 A ↔ 6 A).
    """
    live_w = int(live_w)
    try:
        now_ts = float(now_ts)
    except (TypeError, ValueError):
        now_ts = 0.0
    if refresh or allow_sample or held_w is None or held_ts is None:
        return live_w, now_ts
    try:
        held_w = int(held_w)
        held_ts = float(held_ts)
    except (TypeError, ValueError):
        return live_w, now_ts
    return held_w, held_ts


def leftover_for_surplus(leftover_w, *keep_power_w):
    """Leftover still free for surplus chargers after keep take.

    The held result is what ``sensor.kotiakku_goe_direct_available_surplus``
    shows after a Kotiakku sample. Pass each
    keep charger's ``nrg``. Keep MQTT stays at keep amp so leftover does
    not charge that pack, but keep and leftover are the same house pool.
    Subtract the full keep ``nrg`` (0 if unknown). A keep car
    preconditioning at 3 kW during 2 kW leftover has already used that
    leftover (and 1 kW from the grid). Surplus chargers only get the
    remainder; a negative remainder is a deficit.
    """
    leftover_w = int(leftover_w)
    take = sum(keep_take_w(power_w) for power_w in keep_power_w)
    return leftover_w - take


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


def last_usable_solar_end_ts(
    clock,
    day_start,
    day_end,
    energy_kwh,
    hour_kwh,
    lat=DEFAULT_LAT,
    lon=DEFAULT_LON,
):
    """Exclusive end of the last local hour today with expected kWh ≥ ``hour_kwh``.

    None when no hour qualifies (all hours under the threshold, including
    ``energy_kwh == 0`` or polar night) or when energy is unknown. A
    non-positive / invalid hour threshold uses any hour with expected
    energy above 0 (off-sun disabled: remaining daylight still counts).
    """
    try:
        threshold = float(hour_kwh)
    except (TypeError, ValueError):
        threshold = 0.0
    if energy_kwh is None:
        return None
    if threshold <= 0:
        threshold = 1e-12
    last = None
    for _start, end, kwh in expected_hour_kwh(
        clock, day_start, day_end, energy_kwh, lat, lon
    ):
        if kwh >= threshold:
            last = end
    return last


def _gating_until_ts(clock, today_kwh, hour_kwh, lat, lon):
    """``(now_ts, until_ts)`` for remaining usable solar today.

    ``until_ts`` is None when no hour qualifies.
    """
    now = clock.now()
    today_start = clock.start_of_local_day(now)
    today_end = today_start + datetime.timedelta(days=1)
    now_ts = float(clock.as_timestamp(now))
    until = last_usable_solar_end_ts(
        clock, today_start, today_end, today_kwh, hour_kwh, lat, lon
    )
    return now_ts, until


def _gating_use_tomorrow(clock, today_kwh, hour_kwh, lat, lon, tomorrow_ok):
    """True when tomorrow's prices are in and today's usable solar is gone.

    This is the latest the gate flips: not at midnight just because no hour
    meets the threshold, and not at sunset. Stay on today until both.
    """
    if not tomorrow_ok:
        return False
    try:
        now_ts, until = _gating_until_ts(clock, today_kwh, hour_kwh, lat, lon)
    except Exception:
        return False
    if until is not None and now_ts < until:
        return False
    return True


def gating_solar_kwh(
    clock,
    today_kwh,
    tomorrow_kwh,
    lat=DEFAULT_LAT,
    lon=DEFAULT_LON,
    hour_kwh=1,
    tomorrow_ok=False,
):
    """kWh that gates 22 kW: today until usable solar ends and tomorrow's prices are in.

    Stay on ``today_kwh`` while a later hour today still has expected energy
    ≥ ``hour_kwh``, or while the next day's spot curve is missing. After
    both (prices in and no usable solar left, including no qualifying hour):
    ``tomorrow_kwh``.
    """
    if _gating_use_tomorrow(clock, today_kwh, hour_kwh, lat, lon, tomorrow_ok):
        return tomorrow_kwh
    return today_kwh


def gating_solar_day(
    clock,
    today_kwh,
    hour_kwh=1,
    lat=DEFAULT_LAT,
    lon=DEFAULT_LON,
    tomorrow_ok=False,
):
    """``today`` until tomorrow's prices are in and usable solar today is gone."""
    if _gating_use_tomorrow(clock, today_kwh, hour_kwh, lat, lon, tomorrow_ok):
        return "tomorrow"
    return "today"


def enough_solar_now(
    clock,
    today_kwh,
    tomorrow_kwh,
    threshold_kwh,
    lat=DEFAULT_LAT,
    lon=DEFAULT_LON,
    hour_kwh=1,
    tomorrow_ok=False,
):
    """Skip 22 kW when the gating day's full-day kWh is at least the threshold."""
    return enough_solar(
        gating_solar_kwh(
            clock, today_kwh, tomorrow_kwh, lat, lon, hour_kwh, tomorrow_ok
        ),
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


def three_phase_min_w(min_amp, volts):
    """Watts for official min amp on 3-phase (6 A × V × 3)."""
    return int(min_amp) * int(volts) * 3


def _phase_amp(available_w, phases, min_amp, max_amp, volts):
    return min(
        int(max_amp),
        max(int(min_amp), int(available_w) // (int(volts) * int(phases))),
    )


def _phase_offer_w(available_w, phases, min_amp, max_amp, volts):
    return _phase_amp(available_w, phases, min_amp, max_amp, volts) * int(volts) * int(
        phases
    )


def _clamp_amp(value, min_amp, max_amp):
    try:
        amp = int(value)
    except (TypeError, ValueError):
        amp = int(max_amp)
    min_amp = int(min_amp)
    max_amp = int(max_amp)
    if amp < min_amp:
        return min_amp
    if amp > max_amp:
        return max_amp
    return amp


def _one_phase_cap(min_amp, max_amp, max_1phase_amp):
    """Surplus 1-phase amp ceiling, at most the per-charger cap."""
    return _clamp_amp(max_1phase_amp, min_amp, max_amp)


def surplus_wanted_psm(
    available_w,
    min_amp,
    max_amp,
    volts,
    max_1phase_amp=32,
    last_psm=None,
    preferred_psm=1,
):
    """1- or 3-phase leftover should run.

    Keep the active phase while it can still offer leftover. 1-phase
    stays until 3-phase would deliver more watts (1-phase amp is capped
    at ``max_1phase_amp``). 3-phase stays until leftover cannot hold the
    6 A 3-phase floor. First start: if both phases can run and 1-phase
    still matches leftover, use ``preferred_psm`` (default 1-phase).
    """
    min_amp = int(min_amp)
    max_amp = int(max_amp)
    volts = int(volts)
    available_w = int(available_w)
    one_cap = _one_phase_cap(min_amp, max_amp, max_1phase_amp)
    three_min = three_phase_min_w(min_amp, volts)
    try:
        last = None if last_psm is None else int(last_psm)
    except (TypeError, ValueError):
        last = None
    if last not in (1, 2):
        last = None
    try:
        pref = 1 if preferred_psm is None else int(preferred_psm)
    except (TypeError, ValueError):
        pref = 1
    if pref not in (1, 2):
        pref = 1
    w1 = _phase_offer_w(available_w, 1, min_amp, one_cap, volts)
    three_ok = available_w >= three_min
    w3 = (
        _phase_offer_w(available_w, 3, min_amp, max_amp, volts) if three_ok else 0
    )
    if last == 2:
        return 1 if not three_ok else 2
    if last == 1:
        return 2 if three_ok and w3 > w1 else 1
    if three_ok and w3 > w1:
        return 2
    if three_ok:
        return 2 if pref == 2 else 1
    return 1


def budget(
    available_w,
    min_amp,
    max_amp,
    group_lot,
    volts,
    max_1phase_amp=32,
    force_psm=None,
    last_psm=None,
    preferred_psm=1,
):
    min_amp = int(min_amp)
    volts = int(volts)
    min_hold_w = min_amp * volts
    target_w = max(int(available_w), min_hold_w)
    one_cap = _one_phase_cap(min_amp, max_amp, max_1phase_amp)
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
        psm_i = surplus_wanted_psm(
            target_w,
            min_amp,
            max_amp,
            volts,
            max_1phase_amp,
            last_psm=last_psm,
            preferred_psm=preferred_psm,
        )
        phases = 3 if psm_i == 2 else 1
    psm = 2 if phases == 3 else 1
    leftover_amp = max(min_amp, target_w // (volts * phases))
    amp_cap = one_cap if phases == 1 else int(max_amp)
    # Surplus energy is per-charger ``amp``. Group ``lot`` stays at the
    # fuse cap so load balancing does not clip Tesla below leftover amp.
    amp = min(amp_cap, leftover_amp, int(group_lot))
    lot = int(group_lot)
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
    max_1phase_amp=32,
    *,
    last_psm=None,
    hold_expired=False,
    preferred_psm=1,
):
    """``lot`` / ``psm`` / ``amp`` with sticky phase plus 1↔3 hold.

    Wanted ``psm`` keeps the last phase while it can still offer leftover.
    A real 1↔3 change still waits ``hold_min``. ``amp`` is leftover on the
    phase we will actually run — not the pending other-phase amp, and
    not the last take. ``lot`` stays at ``group_lot`` (fuse cap). 1→3:
    1-phase leftover (capped at max 1-phase amp). 3→1: 3-phase min amp.
    Holding ``psm`` must not freeze amp. First start uses
    ``preferred_psm`` when both phases can still offer leftover.
    """
    _lot, wanted_psm, _wanted_amp = budget(
        available_w,
        min_amp,
        max_amp,
        group_lot,
        volts,
        max_1phase_amp,
        last_psm=last_psm,
        preferred_psm=preferred_psm,
    )
    hold = phase_hold_psm(wanted_psm, last_psm, hold_expired)
    lot, psm, amp = budget(
        available_w,
        min_amp,
        max_amp,
        group_lot,
        volts,
        max_1phase_amp,
        force_psm=hold["psm"],
        preferred_psm=preferred_psm,
    )
    return {
        "lot": lot,
        "psm": psm,
        "amp": amp,
        "arm_phase": hold["arm"],
        "wanted_psm": wanted_psm,
    }


def group_surplus_setpoint(lot, psm, amp, *, n_full, group_lot):
    """MQTT lot/psm/amp for surplus chargers in a load-balancing group.

    ``lot`` is always the group fuse cap (``group_lot``, default 50 A).
    HA already sets each charger's leftover ``amp`` / ``psm``, so load
    balancing must not be used as a surplus energy cap — shrinking
    ``lot`` to leftover amps lets go-e clip Tesla below that ``amp``.
    ``n_full`` is kept for callers; it does not change ``lot``. App
    ``lop`` still applies inside the 50 A group. HA leftover split uses
    HA priority numbers. HA does not write ``lop``. Combined demand may
    exceed the group; app priorities split it. Do not reserve current
    for a full-power charger by capping surplus ``amp``.
    """
    psm = int(psm)
    amp = int(amp)
    return int(group_lot), psm, amp


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


def idle_complete(state, take_w=None):
    """Complete drawing below the Sentry / keep-probe band.

    Unknown or unusable ``nrg`` is 0 W. This is not leftover has-started
    and not keep-pool subtract.
    """
    if not car_finished(state):
        return False
    if take_w is None:
        return True
    try:
        take = int(take_w)
    except (TypeError, ValueError):
        return True
    return take < KEEP_PROBE_TAKE_W


def min_charge_w(remaining, min_amp, volts, max_1phase_amp=32, max_amp=32):
    """Watts for the official 6 A floor at the leftover's 1- or 3-phase."""
    remaining = max(int(remaining), 0)
    min_amp = int(min_amp)
    volts = int(volts)
    if surplus_wanted_psm(remaining, min_amp, max_amp, volts, max_1phase_amp) == 2:
        return min_amp * volts * 3
    return min_amp * volts


def nrg_should_reschedule(old_w, new_w):
    """True when a charger ``nrg`` change should recompute MQTT.

    Watt-by-watt ``nrg`` must not retune leftover ``amp``: leftover is
    held to the Kotiakku SoC/solar/house sample (about every 5 min). Crossing
    leftover has-started (``TAKE_MIN_W``) or the idle-Complete band
    (``KEEP_PROBE_TAKE_W``) still must, for offer-wait, steal, and keep.
    """

    def _take(value):
        if value is None:
            return False
        try:
            return int(value) >= TAKE_MIN_W
        except (TypeError, ValueError):
            return False

    def _idle_band(value):
        if value is None:
            return True
        try:
            return int(value) < KEEP_PROBE_TAKE_W
        except (TypeError, ValueError):
            return True

    return _take(old_w) != _take(new_w) or _idle_band(old_w) != _idle_band(new_w)


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
    """Watts this car is taking from leftover. 0 if it is not accepting.

    Idle Complete (``nrg`` below ``KEEP_PROBE_TAKE_W``) is 0. Live
    Complete still drawing at or above that band uses real ``nrg``.
    Charging below ``TAKE_MIN_W`` still assumes the leftover cap (Tesla
    start). Do not raise ``TAKE_MIN_W`` to the keep-probe band.
    """
    leftover_w = max(int(leftover_w), 0)
    cap = min(leftover_w, max(int(charger_max_w), 0))
    if not car_plugged(state):
        return 0
    if car_finished(state):
        if idle_complete(state, power_w):
            return 0
        try:
            take = int(power_w)
        except (TypeError, ValueError):
            return 0
        return min(max(take, 0), cap)
    if not car_charging(state):
        return 0
    if power_w is None or int(power_w) < TAKE_MIN_W:
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
    max_1phase_amp=32,
    preferred_psm=1,
):
    """Watts the car should be treated as wanting from leftover.

    MQTT ``amp`` must track leftover, not stick at the last take. If the
    car is at the published amp cap and leftover would budget a higher
    amp (or switch 1-phase → 3-phase), treat it as wanting all leftover so
    3-phase is not locked at 6 A. A take below TAKE_MIN_W is not accepting.
    Unknown take (None) wants leftover.
    """
    leftover_w = max(int(leftover_w), 0)
    if take_w is None:
        return leftover_w
    try:
        take_w = int(take_w)
    except (TypeError, ValueError):
        return leftover_w
    if take_w < TAKE_MIN_W:
        return take_w
    take_w = min(take_w, leftover_w)
    _lot, offer_psm, offer_amp = budget(
        leftover_w,
        min_amp,
        max_amp,
        group_lot,
        volts,
        max_1phase_amp,
        last_psm=last_psm,
        preferred_psm=preferred_psm,
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


def _can_charge(watts, min_amp, volts, max_1phase_amp=32):
    watts = max(int(watts), 0)
    return watts >= min_charge_w(watts, min_amp, volts, max_1phase_amp)


def _serial_take(serial, remaining, charger_max_w, take_w, states):
    offered = min(max(int(remaining), 0), max(int(charger_max_w), 0))
    if take_w is not None and serial in take_w:
        return min(max(int(take_w[serial]), 0), remaining, charger_max_w)
    if states is not None:
        return charger_take_w(states.get(serial), None, remaining, charger_max_w)
    return offered


def _steal_keep_w(remaining, prev_take, split_min_w, min_amp, volts, max_1phase_amp=32):
    """Watts the high car keeps after a split_min steal. None unless both
    shares are at least ``split_min_w`` and still meet 6 A."""
    split_min_w = int(split_min_w)
    keep_w = int(remaining) + int(prev_take) - split_min_w
    if keep_w < split_min_w:
        return None
    if not _can_charge(keep_w, min_amp, volts, max_1phase_amp):
        return None
    if not _can_charge(split_min_w, min_amp, volts, max_1phase_amp):
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
    max_1phase_amp=32,
    split_floor_w=500,
    split_hold=False,
    split_expired=False,
    offer_pending=None,
    offer_complete=None,
):
    """Per-charger leftover watts plus next-car hold flags.

    Surplus MQTT does not wait for a car. Every listed surplus charger
    that is not idle Complete can be offered leftover, including Idle,
    unknown, or unplugged, so ``frc=2`` can arm the charger before
    WaitCar. Idle Complete (``nrg`` below ``KEEP_PROBE_TAKE_W``) is not
    offered, backfilled, or used for steal/remainder, unless
    ``offer_complete`` lists that serial (controller: keep phase
    ``KEEP_CUT`` — manual keep off or leftover/window interrupt).
    Complete still drawing at or above that band is leftover-eligible
    as taking. Equal or unknown HA priority: those chargers get the
    same leftover (go-e splits). Unequal: steal/take follows actual
    take (≥100 W), not plug-in. ``plugged`` is kept for callers and
    ignored.

    A high-priority car that is not taking still gets leftover MQTT so
    it can start. If it does not take all leftover, the next car in
    priority still gets leftover as first. While that lower car is
    taking, high stays ``frc=2``. HA only steals from or drops the
    lower car once high is actually taking (≥100 W).

    ``OFFER_WAIT_S`` (15 s) is a cut delay, not an exclusive offer.
    ``offer_pending`` is serials still in that wait (controller: offered
    leftover, take < 100 W, 15 s not expired). Do not ``frc=1`` anyone
    during the wait: leftover can stay on the offered charger and the
    next taking charger together (temporary over-draw; those pending
    arms are group-lot shares until the wait expires). Steal also waits:
    do not cut a taking car to mint 3 kW, and do not start a further car,
    while a higher-priority offer is still pending. After the wait, if
    high is still not taking, leftover belongs to the next as first and
    high stays armed (not a lot share). If nobody is taking, every
    eligible charger is armed at leftover watts. Idle Complete is
    skipped so remaining equal-priority cars still share, except
    ``offer_complete`` serials (KEEP_CUT). After a taking
    first car, unused leftover above ``split_floor_w`` (default 500 W)
    goes to the next car in priority — even if that car is not taking
    yet, so it can start. If that next car is pending, stop there until
    it reacts or the wait expires (do not start a further car yet). If
    that remainder is below ``split_min_w`` (default 3 kW), cut the
    high-priority share so the next car still gets 3 kW — only if
    leftover itself is at least ``2 × split_min_w`` (each car keeps at
    least 3 kW), the first is actually taking power, both shares still
    meet 6 A, and no higher-priority offer is still pending. Remainder
    at or below 500 W is a dead zone: do not *start* the next car. If
    the next car was already taking and leftover then shrinks so the
    first would use it all, keep stealing 3 kW for the hold minutes
    unless ``split_expired`` or leftover is below 6 kW.
    ``lops`` is HA charger priority, not app ``lop``. HA does not write
    ``lop``. Charger ``lot`` stays the fuse cap. ``lot_allocations`` only
    marks which shares count as taking. Whenever a lower-priority eligible
    charger is allocated leftover, every better HA priority that is
    still eligible stays in ``allocations`` (leftover MQTT, ``frc=2``)
    so it can start taking again. Those backfills are not group-lot
    shares unless the offer wait is still running. Idle Complete
    stays skipped unless ``offer_complete``.
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
        "overdraw": False,
    }
    if not serials:
        return empty
    take_w = take_w if isinstance(take_w, dict) else None
    states = states if isinstance(states, dict) else None

    def _idle_of(serial):
        if offer_complete and serial in offer_complete:
            return False
        if states is None:
            return False
        take = None
        if take_w is not None and serial in take_w:
            take = take_w[serial]
        return idle_complete(states.get(serial), take)

    eligible = [serial for serial in serials if not _idle_of(serial)]
    if not eligible or leftover_w <= 0:
        return empty
    charger_max_w = max(int(charger_max_w), 0)
    split_min_w = int(split_min_w)
    split_floor_w = int(split_floor_w)
    shared = {serial: leftover_w for serial in eligible}

    def _take_of(serial, remaining=leftover_w):
        return _serial_take(serial, remaining, charger_max_w, take_w, states)

    def _is_taking(serial, remaining=leftover_w):
        return _take_of(serial, remaining) >= TAKE_MIN_W

    def _backfill_higher(out):
        """Keep every better HA priority on leftover MQTT if a lower one is allocated."""
        if not out:
            return
        worst = None
        for serial in out:
            rank = lops.get(serial) if isinstance(lops, dict) else None
            if rank is None:
                continue
            rank = int(rank)
            if worst is None or rank > worst:
                worst = rank
        if worst is None:
            return
        for serial in eligible:
            rank = lops.get(serial) if isinstance(lops, dict) else None
            if rank is None:
                continue
            if int(rank) < worst and serial not in out:
                out[serial] = leftover_w

    def _pack(allocations, remainder_w, arm_split_hold, leading=(), overdraw_serials=()):
        lot_allocations = dict(allocations)
        out = dict(allocations)
        for serial in leading:
            out[serial] = leftover_w
        taking_now = [serial for serial in lot_allocations if _is_taking(serial)]
        overdraw = False
        if overdraw_serials and taking_now:
            overdraw = True
            for serial in overdraw_serials:
                lot_allocations[serial] = leftover_w
                out[serial] = leftover_w
        _backfill_higher(out)
        taking = [serial for serial in lot_allocations if _is_taking(serial)]
        return {
            "allocations": out,
            "remainder_w": remainder_w,
            "arm_split_hold": arm_split_hold,
            "taking": taking,
            "lot_allocations": lot_allocations,
            "overdraw": overdraw,
        }

    def _shared():
        return _pack(shared, leftover_w, False)

    pending = set(offer_pending or ())

    def _is_pending(serial):
        return serial in pending and not _is_taking(serial)

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
    overdraw_serials = []
    while pool and not _is_taking(pool[0]) and not _is_pending(pool[0]):
        leading.append(pool.pop(0))
    while pool and not _is_taking(pool[0]) and _is_pending(pool[0]):
        serial = pool.pop(0)
        leading.append(serial)
        overdraw_serials.append(serial)
    if not pool:
        return _shared()
    if len(pool) == 1:
        return _pack({pool[0]: leftover_w}, leftover_w, False, leading, overdraw_serials)
    remaining = leftover_w
    allocations = {}
    prev = None
    prev_take = 0
    remainder_after_high = leftover_w
    for serial in pool:
        if not allocations:
            if not _can_charge(remaining, min_amp, volts, max_1phase_amp):
                break
            offered = min(remaining, charger_max_w)
            take = _take_of(serial, remaining)
            allocations[serial] = offered if (take <= 0 or overdraw_serials) else take
            remaining -= take
            remainder_after_high = remaining
            prev = serial
            prev_take = take
            if overdraw_serials:
                break
            continue
        need = min_charge_w(remaining, min_amp, volts, max_1phase_amp)
        if remaining >= split_min_w and remaining >= need:
            offered = min(remaining, charger_max_w)
            take = _take_of(serial, remaining)
            allocations[serial] = offered
            remaining -= take
            prev = serial
            prev_take = take
            if _is_pending(serial):
                remaining = 0
                break
            continue
        in_dead = remaining <= split_floor_w
        want_steal = (
            leftover_w >= 2 * split_min_w
            and prev_take >= TAKE_MIN_W
            and ((not in_dead) or (split_hold and not split_expired))
            and not _is_pending(serial)
            and not overdraw_serials
        )
        keep_w = _steal_keep_w(
            remaining, prev_take, split_min_w, min_amp, volts, max_1phase_amp
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
            if _is_pending(serial):
                remaining = 0
                break
            continue
        break
    taking = [serial for serial in allocations if _is_taking(serial)]
    arm_split_hold = bool(
        remainder_after_high <= split_floor_w
        and split_hold
        and not split_expired
        and len(taking) >= 2
    )
    return _pack(
        allocations, remainder_after_high, arm_split_hold, leading, overdraw_serials
    )


def surplus_steal_victim(serial, *, leftover_on, taking, lops):
    """Worse HA leftover priority than a taking surplus charger.

    Keep must not auto-on while leftover is writing and another surplus
    charger is taking (≥100 W has-started) at a better (lower) HA
    leftover priority. Equal priority is out of scope.
    """
    if not leftover_on or not serial:
        return False
    others = [other for other in (taking or []) if other and other != serial]
    if not others or not isinstance(lops, dict):
        return False
    rank = lops.get(serial)
    if rank is None:
        return False
    rank = int(rank)
    for other in others:
        other_rank = lops.get(other)
        if other_rank is None:
            continue
        if int(other_rank) < rank:
            return True
    return False


def surplus_higher_keep_on(
    serial, allocations, lops, states=None, take_w=None, offer_complete=None
):
    """True when a worse-priority charger has leftover: do not ``frc=1`` this one.

    Idle Complete stays off unless ``offer_complete`` (KEEP_CUT). Live
    Complete still drawing is leftover. Plug-in does not matter.
    """
    if not serial or not isinstance(allocations, dict) or not allocations:
        return False
    if serial in allocations:
        return False
    take = None if take_w is None else take_w.get(serial)
    if (
        states is not None
        and idle_complete(states.get(serial), take)
        and not (offer_complete and serial in offer_complete)
    ):
        return False
    if not isinstance(lops, dict):
        return False
    rank = lops.get(serial)
    if rank is None:
        return False
    rank = int(rank)
    for other in allocations:
        other_rank = lops.get(other)
        if other_rank is None:
            continue
        if int(other_rank) > rank:
            return True
    return False


def surplus_allocations(*args, **kwargs):
    """Per-charger leftover watts for surplus MQTT."""
    return surplus_allocation_plan(*args, **kwargs)["allocations"]


def surplus_targets(*args, **kwargs):
    """Serials that should get leftover MQTT, in priority order."""
    return list(surplus_allocations(*args, **kwargs))
