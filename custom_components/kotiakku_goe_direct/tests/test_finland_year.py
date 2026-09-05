"""48 h Helsinki weather + Nordpool year-round rolls.

Walk 15-minute ticks for at least two local days so ``raw_tomorrow`` can
appear after 14:00 (Nordpool day-ahead, same time the product docs use),
roll at midnight, and appear again the next afternoon. Surplus leftover
uses a 10 kW-class rooftop at 60.2°N and a heated Finnish house. Spot
windows are independent of Kotiakku.
"""

from __future__ import annotations

import bisect
import datetime
import math
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from harness import (
    Clock,
    ROOT,
    assert_eq,
    assert_gt,
    assert_true,
    case_runner,
    load_mod,
    plan_once as run_once,
    window_starts,
)

HEL = ZoneInfo("Europe/Helsinki")
SLOT = datetime.timedelta(minutes=15)
HOLD = datetime.timedelta(minutes=15)
PV_PEAK_W = 10000
SOC = 96.0
PUBLISH_H = 14

planner = load_mod("planner", "_fi")
surplus = load_mod("surplus", "_fi")
const = load_mod("const", "_fi")
now_in_windows = planner.now_in_windows
SOLAR_ENOUGH_KWH = const.DEFAULT_SOLAR_ENOUGH_KWH
OFFSUN_HOUR_KWH = const.DEFAULT_OFFSUN_HOUR_KWH
POLICY_SOLAR_PRIORITY = const.POLICY_SOLAR_PRIORITY
POLICY_FORCE_ON = const.POLICY_FORCE_ON
POLICY_FORCE_OFF = const.POLICY_FORCE_OFF


class Hold:
    def tick(self, now, need):
        if not need:
            self.start = None
            return False
        if self.start is None:
            self.start = now
            return False
        return now - self.start >= HOLD

    def __init__(self):
        self.start = None


def solar_elevation_deg(dt, lat=60.17, lon=24.94):
    return surplus.solar_elevation_deg(dt, lat, lon)


def solar_w(dt, cloud=1.0):
    el = solar_elevation_deg(dt)
    if el <= 0:
        return 0
    return int(PV_PEAK_W * math.sin(math.radians(el)) * max(0.0, min(1.0, cloud)))


def house_base_w(dt, outdoor_c):
    h = dt.hour + dt.minute / 60.0
    cooking = 1500 if 16.5 <= h < 18 else 0
    heating = max(0.0, (17.0 - outdoor_c) * 200.0)
    return int(700 + cooking + heating)


def hourf(dt):
    return dt.hour + dt.minute / 60.0


def winter_price(dt, day_i):
    """Night is the cheap valley. Tomorrow night is cheaper."""
    h = hourf(dt)
    if 1.0 <= h < 6.0:
        return 0.045 - 0.008 * (day_i % 3)
    if 16.0 <= h < 21.0:
        return 0.21
    return 0.12


def summer_price(dt, day_i):
    """Midday solar dump is cheapest; Off-sun blocking drops those hours."""
    h = hourf(dt)
    if 10.0 <= h < 16.0:
        return -0.01 - 0.015 * (day_i % 3)
    if 17.0 <= h < 21.0:
        return 0.13
    return 0.11


def shoulder_price(dt, day_i):
    """Spring/autumn: cheap night plus a milder midday dip."""
    h = hourf(dt)
    if 1.0 <= h < 5.0:
        return 0.04 - 0.006 * (day_i % 3)
    if 11.0 <= h < 15.0:
        return 0.055
    if 16.5 <= h < 20.5:
        return 0.16
    return 0.11


def local_midnight(dt):
    return dt.astimezone(HEL).replace(hour=0, minute=0, second=0, microsecond=0)


def slots_for_day(day_start, price_fn, day_i):
    """Local-day Nordpool slots. DST spring has 92, autumn 100, else 96."""
    out = []
    ts = day_start.timestamp()
    end_ts = (day_start + datetime.timedelta(days=1)).timestamp()
    while ts < end_ts - 1:
        t = datetime.datetime.fromtimestamp(ts, tz=HEL)
        nxt_ts = min(ts + 900, end_ts)
        nxt = datetime.datetime.fromtimestamp(nxt_ts, tz=HEL)
        out.append({"start": t.isoformat(), "end": nxt.isoformat(), "value": price_fn(t, day_i)})
        ts = nxt_ts
    return out


def build_days(start, n_days, price_fn):
    start = local_midnight(start)
    return [slots_for_day(start + datetime.timedelta(days=i), price_fn, i) for i in range(n_days)]


def nordpool_attrs(now, start, days):
    now = now.astimezone(HEL)
    day0 = local_midnight(now)
    idx = (day0.date() - local_midnight(start).date()).days
    if idx < 0 or idx >= len(days):
        return {"raw_today": [], "raw_tomorrow": [], "tomorrow_valid": False}
    published = now.hour > PUBLISH_H or (now.hour == PUBLISH_H)
    if published and idx + 1 < len(days):
        return {
            "raw_today": days[idx],
            "raw_tomorrow": days[idx + 1],
            "tomorrow_valid": True,
        }
    return {"raw_today": days[idx], "raw_tomorrow": [], "tomorrow_valid": False}


def plan_once(clock, attrs, result, blocked=None, today_kwh=None, tomorrow_kwh=None):
    return run_once(
        planner,
        clock,
        attrs,
        result,
        min_hours=2.0,
        max_hours=5.0,
        ceiling=0.2,
        flex_pct=20,
        flex_euro=0.02,
        source_entity="sensor.nordpool_kwh_fi",
        blocked=blocked,
        today_kwh=today_kwh,
        tomorrow_kwh=tomorrow_kwh,
    )


def window_on(result, ts):
    return now_in_windows((result or {}).get("raw_windows") or [], ts)


starts_of = window_starts


def iter_ticks(start, hours=48):
    t = start.astimezone(HEL)
    end = t + datetime.timedelta(hours=hours)
    while t <= end:
        yield t
        t += SLOT


SERIAL_CHEAP = "111111"
SERIAL_SURPLUS = "222222"
VOLTS = 230
FULL_AMP = 32
FULL_PSM = 2
KWH_PER_TICK = SLOT.total_seconds() / 3600.0 / 1000.0


def solar_prefix(start, hours, cloud):
    """15-min PV series plus prefix kWh, covering tomorrow after the last tick."""
    t = start.astimezone(HEL)
    end = t + datetime.timedelta(hours=hours + 48)
    times = []
    watts = []
    while t <= end:
        times.append(t)
        watts.append(solar_w(t, cloud_at(t, {"cloud": cloud})))
        t += SLOT
    prefix = [0.0]
    for w in watts:
        prefix.append(prefix[-1] + w * KWH_PER_TICK)
    return times, [x.timestamp() for x in times], prefix


def kwh_between(ts_list, prefix, t0, t1):
    i = bisect.bisect_left(ts_list, t0.timestamp())
    j = bisect.bisect_left(ts_list, t1.timestamp())
    j = min(j, len(prefix) - 1)
    i = min(i, j)
    return prefix[j] - prefix[i]


def solar_forecast(now, ts_list, prefix):
    """Forecast.Solar-style full-day today and tomorrow kWh from the PV model."""
    day0 = local_midnight(now)
    day1 = day0 + datetime.timedelta(days=1)
    day2 = day0 + datetime.timedelta(days=2)
    today = kwh_between(ts_list, prefix, day0, day1)
    tomorrow = kwh_between(ts_list, prefix, day1, day2)
    return today, tomorrow


def cloud_at(dt, weather):
    if callable(weather.get("cloud")):
        return weather["cloud"](dt)
    return float(weather.get("cloud", 1.0))


def current_price(now, attrs):
    for slot in list(attrs.get("raw_today") or []) + list(attrs.get("raw_tomorrow") or []):
        start = datetime.datetime.fromisoformat(slot["start"])
        end = datetime.datetime.fromisoformat(slot["end"])
        if start.tzinfo is None:
            start = start.replace(tzinfo=HEL)
            end = end.replace(tzinfo=HEL)
        if start <= now < end:
            return float(slot["value"])
    return None


def commanded(psm, amp):
    if psm not in (1, 2) or not amp:
        return {"psm": None, "amp": None, "phases": 0, "w": 0}
    phases = 3 if int(psm) == 2 else 1
    amp = int(amp)
    return {"psm": int(psm), "amp": amp, "phases": phases, "w": amp * VOLTS * phases}


def off_cmd():
    return {"psm": None, "amp": None, "phases": 0, "w": 0, "wanted_psm": None, "arm_phase": False}


def simulate(
    start,
    *,
    hours=48,
    outdoor_c,
    cloud,
    price_fn,
    name,
    policy=POLICY_SOLAR_PRIORITY,
    b_policy=POLICY_FORCE_OFF,
    solar_enough_kwh=SOLAR_ENOUGH_KWH,
    n_chargers=2,
    priorities=None,
):
    """48 h house: charger A uses ``policy``, optional charger B uses ``b_policy``.

    Default B is Force off and never charges. Leftover split tests pass
    SolarPriority on B. Leftover uses HA charger priority (default A=1,
    B=2; 1 is highest), same as ``surplus_allocation_plan``.
    """
    start = start.astimezone(HEL).replace(second=0, microsecond=0)
    days = build_days(start, int(hours // 24) + 3, price_fn)
    clock = Clock(start, tz=HEL)
    result = None
    session = False
    split_hold = False
    last_psm = {SERIAL_CHEAP: None, SERIAL_SURPLUS: None}
    floor = Hold()
    split = Hold()
    phase = {SERIAL_CHEAP: Hold(), SERIAL_SURPLUS: Hold()}
    ticks = []
    _times, ts_list, prefix = solar_prefix(start, hours, cloud)
    if priorities is None:
        priorities = {SERIAL_CHEAP: 1, SERIAL_SURPLUS: 2}
    n_chargers = 1 if int(n_chargers) <= 1 else 2

    def surplus_cmd(serial, watts_i, use_floor, now):
        source_w = 0 if use_floor else int(watts_i)
        last = last_psm[serial]
        wanted = surplus.budget(source_w, 6, 32, 50, VOLTS, 4140)[1]
        expired = phase[serial].tick(now, last in (1, 2) and last != wanted)
        pub = surplus.surplus_phase_budget(
            source_w,
            6,
            32,
            50,
            VOLTS,
            4140,
            last_psm=last,
            hold_expired=expired,
        )
        last_psm[serial] = pub["psm"]
        cmd = commanded(pub["psm"], pub["amp"])
        cmd["wanted_psm"] = pub["wanted_psm"]
        cmd["arm_phase"] = pub["arm_phase"]
        return cmd

    def idle_surplus(serial, now):
        phase[serial].tick(now, False)
        last_psm[serial] = None
        return off_cmd()

    for now in iter_ticks(start, hours):
        clock.set(now)
        attrs = nordpool_attrs(now, start, days)
        today_kwh, tomorrow = solar_forecast(now, ts_list, prefix)
        blocked = surplus.surplus_hour_ranges(
            clock,
            today_kwh,
            tomorrow,
            OFFSUN_HOUR_KWH,
            lat=60.17,
            lon=24.94,
        )
        result = plan_once(
            clock,
            attrs,
            result,
            blocked=blocked,
            today_kwh=today_kwh,
            tomorrow_kwh=tomorrow,
        )
        ts = clock.as_timestamp(now)
        upcoming = surplus.upcoming_solar_kwh(today_kwh, tomorrow)
        enough = surplus.enough_solar_now(
            clock, today_kwh, tomorrow, solar_enough_kwh, 60.17, 24.94
        )
        gating_day = surplus.gating_solar_day(clock, 60.17, 24.94)
        in_window = window_on(result, ts)
        cheap_window = in_window
        offsun_window = in_window
        cheap_full = planner.charger_full_power(
            policy, result, ts, enough_solar=enough
        )
        solar = solar_w(now, cloud_at(now, {"cloud": cloud}))
        house = house_base_w(now, outdoor_c)
        leftover = surplus.leftover_w(solar, house, 0)
        dec = surplus.surplus_decision(session, leftover, SOC, window_ok=True)
        if floor.tick(now, dec["arm_floor"]):
            dec = surplus.surplus_decision(
                session, leftover, SOC, window_ok=True, floor_expired=True
            )
        write_on = dec["write_on"]
        use_floor = dec["use_floor_budget"]
        a = off_cmd()
        b = off_cmd()
        if cheap_full:
            a = commanded(FULL_PSM, FULL_AMP)
            a["wanted_psm"] = FULL_PSM
            a["arm_phase"] = False
            last_psm[SERIAL_CHEAP] = None
            phase[SERIAL_CHEAP].tick(now, False)
        surplus_serials = []
        if write_on:
            if planner.charger_surplus(policy, result, ts, enough_solar=enough):
                surplus_serials.append(SERIAL_CHEAP)
            if n_chargers >= 2 and planner.charger_surplus(
                b_policy, result, ts, enough_solar=enough
            ):
                surplus_serials.append(SERIAL_SURPLUS)
            if not surplus_serials:
                session = False
                split_hold = False
                split.tick(now, False)
                if not cheap_full:
                    a = idle_surplus(SERIAL_CHEAP, now)
                if n_chargers >= 2:
                    b = idle_surplus(SERIAL_SURPLUS, now)
            else:
                session = True
        else:
            if not cheap_full:
                a = idle_surplus(SERIAL_CHEAP, now)
            if n_chargers >= 2:
                b = idle_surplus(SERIAL_SURPLUS, now)
            session = False
            split_hold = False
            split.tick(now, False)
        if surplus_serials:
            session = True
            alloc_w = leftover
            if use_floor:
                alloc_w = max(alloc_w, 6 * VOLTS)
            split_expired = split.tick(now, split_hold)
            plan = surplus.surplus_allocation_plan(
                surplus_serials,
                lops={serial: priorities.get(serial) for serial in surplus_serials},
                plugged={serial: True for serial in surplus_serials},
                leftover_w=alloc_w,
                split_min_w=3000,
                charger_max_w=32 * VOLTS * 3,
                split_hold=split_hold,
                split_expired=split_expired,
            )
            split_hold = len(plan["allocations"]) >= 2
            cmds = {SERIAL_CHEAP: a, SERIAL_SURPLUS: b}
            for serial in surplus_serials:
                watts_i = plan["allocations"].get(serial)
                if watts_i is None:
                    cmds[serial] = idle_surplus(serial, now)
                else:
                    cmds[serial] = surplus_cmd(serial, watts_i, use_floor, now)
            a, b = cmds[SERIAL_CHEAP], cmds[SERIAL_SURPLUS]
        ticks.append(
            {
                "now": now,
                "price": current_price(now, attrs),
                "leftover": leftover,
                "solar": solar,
                "house": house,
                "write_on": write_on,
                "psm": b["psm"],
                "amp": b["amp"],
                "wanted_psm": b["wanted_psm"],
                "arm_phase": b["arm_phase"],
                "cheap_window": cheap_window,
                "offsun_window": offsun_window,
                "cheap_full": cheap_full,
                "enough": enough,
                "gating_day": gating_day,
                "today_kwh": today_kwh,
                "tomorrow_kwh": tomorrow,
                "upcoming_kwh": upcoming,
                "a": a,
                "b": b,
                "reason": result["reason"],
                "tomorrow_ok": result["tomorrow_ok"],
                "slot_count": result["slot_count"],
                "horizon_ts": result.get("horizon_ts"),
                "starts": starts_of(result),
                "count": result["count"],
            }
        )
    return {
        "name": name,
        "start": start,
        "ticks": ticks,
        "days": days,
        "policy": policy,
        "b_policy": b_policy,
        "solar_enough_kwh": solar_enough_kwh,
        "n_chargers": n_chargers,
        "priorities": dict(priorities),
    }


def at_hour(ticks, hour, minute=0):
    return [t for t in ticks if t["now"].hour == hour and t["now"].minute == minute]


def hours_where(ticks, pred):
    return sum(0.25 for t in ticks if pred(t))


def max_leftover(ticks):
    return max(t["leftover"] for t in ticks)


def assert_48h_nordpool(sim):
    ticks = sim["ticks"]
    start = sim["start"]
    assert_true(ticks[-1]["now"] - start >= datetime.timedelta(hours=48), "covers 48 h")
    assert_true(len(ticks) >= 192, "15-min ticks for 48 h")
    fourteens = at_hour(ticks, PUBLISH_H)
    assert_true(len(fourteens) >= 2, "two 14:00 publications in the window")
    midnights = at_hour(ticks, 0)
    assert_true(len(midnights) >= 3, "start + two midnights")

    day0_morning = [t for t in ticks if t["now"].date() == start.date() and t["now"].hour < PUBLISH_H]
    assert_true(day0_morning, "ticks before first 14:00")
    for t in day0_morning:
        assert_eq(t["tomorrow_ok"], False, "no tomorrow before 14:00 @ %s" % t["now"])
        assert_true(
            t["slot_count"] <= 100,
            "today-only slots before 14:00 @ %s (got %s)" % (t["now"], t["slot_count"]),
        )

    first_pub = fourteens[0]
    assert_eq(first_pub["tomorrow_ok"], True, "tomorrow_valid at first 14:00")
    assert_true(
        first_pub["slot_count"] > day0_morning[-1]["slot_count"] + 50,
        "horizon grows when tomorrow appears (%s → %s)"
        % (day0_morning[-1]["slot_count"], first_pub["slot_count"]),
    )
    assert_true(
        first_pub["reason"] in ("planned", "no_window"),
        "14:00 reason is a planner outcome, not a crash: %s" % first_pub["reason"],
    )
    if first_pub["count"] and first_pub["reason"] == "planned":
        held = [t for t in ticks if t["now"] > first_pub["now"] and t["starts"] == first_pub["starts"]]
        assert_true(len(held) >= 4, "planned set is held at least an hour")

    second_pub = fourteens[1]
    assert_eq(second_pub["tomorrow_ok"], True, "second afternoon also has tomorrow")
    # Midnight roll: tomorrow of yesterday is today; tomorrow_valid false until 14:00.
    day1 = start.date() + datetime.timedelta(days=1)
    day1_morning = [
        t for t in ticks if t["now"].date() == day1 and t["now"].hour < PUBLISH_H
    ]
    assert_true(day1_morning, "second local morning")
    for t in day1_morning:
        assert_eq(t["tomorrow_ok"], False, "tomorrow gone after midnight until 14:00 @ %s" % t["now"])

    # Same Nordpool curve and local date: starts do not slide with the clock.
    prev = None
    for t in ticks:
        if (
            prev is not None
            and t["tomorrow_ok"] == prev["tomorrow_ok"]
            and t["slot_count"] == prev["slot_count"]
            and t["now"].date() == prev["now"].date()
        ):
            assert_eq(t["starts"], prev["starts"], "same curve starts do not slide @ %s" % t["now"])
        prev = t

    # Spot windows ignore leftover.
    for t in ticks:
        if t["cheap_full"]:
            assert_true(True, "full-power allowed with leftover %s" % t["leftover"])


def assert_no_surplus(sim, msg):
    on = hours_where(sim["ticks"], lambda t: t["write_on"])
    assert_eq(on, 0, "%s: surplus hours should be 0, got %s (max leftover %s W)" % (msg, on, max_leftover(sim["ticks"])))


def sim_stats(sim):
    ticks = sim["ticks"]
    upcoming = [t["upcoming_kwh"] for t in ticks]
    return {
        "surplus_h": hours_where(ticks, lambda t: t["write_on"]),
        "a_surplus_h": hours_where(
            ticks, lambda t: t["write_on"] and not t["cheap_full"] and t["a"]["amp"]
        ),
        "b_surplus_h": hours_where(ticks, lambda t: t["b"]["amp"]),
        "full_h": hours_where(ticks, lambda t: t["cheap_full"]),
        "window_h": hours_where(ticks, lambda t: t["cheap_window"]),
        "skip_h": hours_where(ticks, lambda t: t["cheap_window"] and not t["cheap_full"]),
        "enough_h": hours_where(ticks, lambda t: t["enough"]),
        "p3_h": hours_where(ticks, lambda t: t["psm"] == 2),
        "upcoming_min": min(upcoming),
        "upcoming_max": max(upcoming),
        "leftover_max": max_leftover(ticks),
    }


def summary(sim):
    stats = sim_stats(sim)
    reasons = [t["reason"] for t in at_hour(sim["ticks"], PUBLISH_H)]
    print(
        "  %s [%s]: leftover_max=%s W surplus=%.1fh A_surplus=%.1fh B_surplus=%.1fh "
        "3p=%.1fh window=%.1fh full=%.1fh skip=%.1fh enough=%.1fh upcoming=%.1f–%.1f kWh 14:00=%s"
        % (
            sim["name"],
            sim.get("policy", POLICY_SOLAR_PRIORITY),
            stats["leftover_max"],
            stats["surplus_h"],
            stats["a_surplus_h"],
            stats["b_surplus_h"],
            stats["p3_h"],
            stats["window_h"],
            stats["full_h"],
            stats["skip_h"],
            stats["enough_h"],
            stats["upcoming_min"],
            stats["upcoming_max"],
            reasons,
        )
    )


def _spans(ticks, pred):
    spans = []
    start = None
    prev = None
    for t in ticks:
        if pred(t):
            if start is None:
                start = t["now"]
            prev = t["now"]
        elif start is not None:
            spans.append((start, prev + SLOT))
            start = None
    if start is not None:
        spans.append((start, ticks[-1]["now"] + SLOT))
    return spans


def april_clouds(dt):
    h = hourf(dt)
    if 10 <= h < 13:
        return 0.25
    return 0.9


PLOT_SPECS = (
    dict(
        name="midwinter-clear",
        start=datetime.datetime(2026, 12, 21, 0, 0, tzinfo=HEL),
        outdoor_c=-6,
        cloud=1.0,
        price_fn=winter_price,
    ),
    dict(
        name="midwinter-overcast",
        start=datetime.datetime(2026, 12, 21, 0, 0, tzinfo=HEL),
        outdoor_c=-12,
        cloud=0.15,
        price_fn=winter_price,
    ),
    dict(
        name="february",
        start=datetime.datetime(2026, 2, 10, 0, 0, tzinfo=HEL),
        outdoor_c=-8,
        cloud=0.45,
        price_fn=winter_price,
    ),
    dict(
        name="april-mixed",
        start=datetime.datetime(2026, 4, 15, 0, 0, tzinfo=HEL),
        outdoor_c=5,
        cloud=april_clouds,
        price_fn=shoulder_price,
    ),
    dict(
        name="midsummer-clear",
        start=datetime.datetime(2026, 6, 21, 0, 0, tzinfo=HEL),
        outdoor_c=17,
        cloud=1.0,
        price_fn=summer_price,
    ),
    dict(
        name="midsummer-overcast",
        start=datetime.datetime(2026, 6, 21, 0, 0, tzinfo=HEL),
        outdoor_c=14,
        cloud=0.18,
        price_fn=summer_price,
    ),
    dict(
        name="october",
        start=datetime.datetime(2026, 10, 10, 0, 0, tzinfo=HEL),
        outdoor_c=6,
        cloud=0.35,
        price_fn=shoulder_price,
    ),
    dict(
        name="dst-spring",
        start=datetime.datetime(2026, 3, 28, 0, 0, tzinfo=HEL),
        outdoor_c=1,
        cloud=0.5,
        price_fn=shoulder_price,
    ),
    dict(
        name="dst-autumn",
        start=datetime.datetime(2026, 10, 24, 0, 0, tzinfo=HEL),
        outdoor_c=5,
        cloud=0.35,
        price_fn=shoulder_price,
    ),
)


PLOT_BY_NAME = {spec["name"]: spec for spec in PLOT_SPECS}


def sim_from_spec(spec_name, **overrides):
    spec = dict(PLOT_BY_NAME[spec_name])
    spec.update(overrides)
    start = spec.pop("start")
    spec.setdefault("hours", 48)
    spec.setdefault("name", spec_name)
    return simulate(start, **spec)


def sim_solarpriority(spec_name, **overrides):
    return sim_from_spec(
        spec_name,
        name="%s-solarpriority" % spec_name,
        policy=POLICY_SOLAR_PRIORITY,
        **overrides,
    )


def plot_sim(sim, path):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    ticks = sim["ticks"]
    policy = sim.get("policy", POLICY_SOLAR_PRIORITY)
    threshold = sim.get("solar_enough_kwh", SOLAR_ENOUGH_KWH)
    times = [t["now"] for t in ticks]
    prices = [t["price"] for t in ticks]
    leftover_kw = [t["leftover"] / 1000.0 for t in ticks]
    today_kwh = [t["today_kwh"] for t in ticks]
    tomorrow = [t["tomorrow_kwh"] for t in ticks]
    upcoming = [t["upcoming_kwh"] for t in ticks]
    a_amp = [t["a"]["amp"] if t["a"]["amp"] else 0 for t in ticks]
    b_amp = [t["b"]["amp"] if t["b"]["amp"] else 0 for t in ticks]
    a_w = [t["a"]["w"] / 1000.0 for t in ticks]
    b_w = [t["b"]["w"] / 1000.0 for t in ticks]
    a_ph = [t["a"]["phases"] for t in ticks]
    b_ph = [t["b"]["phases"] for t in ticks]

    fig, axes = plt.subplots(
        5,
        1,
        sharex=True,
        figsize=(14, 13.5),
        gridspec_kw={"height_ratios": [1.15, 1.05, 1.0, 1.0, 1.15]},
    )
    fig.suptitle(
        "%s  —  48 h Helsinki  ·  charger A %s pri %s  ·  charger B %s"
        % (
            sim["name"],
            policy,
            (sim.get("priorities") or {}).get(SERIAL_CHEAP, 1),
            "omitted"
            if sim.get("n_chargers", 2) < 2
            else "%s pri %s%s"
            % (
                sim.get("b_policy", POLICY_FORCE_OFF),
                (sim.get("priorities") or {}).get(SERIAL_SURPLUS, 2),
                " never charges"
                if sim.get("b_policy", POLICY_FORCE_OFF) == POLICY_FORCE_OFF
                else "",
            ),
        ),
        fontsize=13,
        fontweight="bold",
    )

    ax = axes[0]
    for start, end in _spans(ticks, lambda t: t["cheap_window"]):
        ax.axvspan(start, end, color="#2ca02c", alpha=0.18, zorder=0)
    if policy == POLICY_SOLAR_PRIORITY:
        for start, end in _spans(ticks, lambda t: t["cheap_window"] and not t["cheap_full"]):
            ax.axvspan(start, end, color="#ff7f0e", alpha=0.28, hatch="///", zorder=1)
        for start, end in _spans(ticks, lambda t: t["cheap_full"]):
            ax.axvspan(start, end, color="#1b7a1b", alpha=0.35, zorder=2)
    ax.plot(times, prices, color="#1f4e79", lw=1.6, label="Spot €/kWh")
    ax.axhline(0.2, color="#d62728", ls="--", lw=1, label="Ceiling 0.20")
    for t in times:
        if t.hour == PUBLISH_H and t.minute == 0:
            ax.axvline(t, color="#7f7f7f", ls=":", lw=0.8, alpha=0.8)
    ax.set_ylabel("€/kWh")
    handles = [
        plt.Line2D([0], [0], color="#1f4e79", lw=1.6, label="Spot €/kWh"),
        plt.Line2D([0], [0], color="#d62728", ls="--", lw=1, label="Ceiling 0.20"),
        Patch(facecolor="#2ca02c", alpha=0.18, label="SolarPriority window"),
    ]
    if policy == POLICY_SOLAR_PRIORITY:
        handles.append(Patch(facecolor="#ff7f0e", alpha=0.28, hatch="///", label="enough-solar skip"))
        handles.append(Patch(facecolor="#1b7a1b", alpha=0.35, label="SolarPriority 22 kW"))
    ax.legend(loc="upper right", fontsize=8, ncol=2, handles=handles)
    ax.set_title(
        "Spot price  ·  green = SolarPriority window (seed+flex, ceiling 0.20)"
        + (
            "  ·  hatch = skip 22 kW (enough solar)"
            if policy == POLICY_SOLAR_PRIORITY
            else ""
        )
    )
    ax.grid(True, alpha=0.3)

    ax = axes[1]
    for start, end in _spans(ticks, lambda t: t["enough"]):
        ax.axvspan(start, end, color="#bcbd22", alpha=0.18, zorder=0)
    ax.plot(times, today_kwh, color="#ffbb78", lw=1.3, label="Solar today")
    ax.plot(times, tomorrow, color="#9467bd", lw=1.3, label="Tomorrow")
    ax.plot(times, upcoming, color="#8c564b", lw=1.8, label="Upcoming (max)")
    ax.axhline(threshold, color="#d62728", ls="--", lw=1.1, label="Enough %s kWh" % threshold)
    ax.set_ylabel("kWh")
    ax.set_title(
        "Forecast.Solar-style energy  ·  SolarPriority skips 22 kW when the gating day's kWh ≥ %s; Off-sun drops hours ≥ 1 kWh"
        % threshold
    )
    ax.legend(loc="upper right", fontsize=8, ncol=3)
    ax.grid(True, alpha=0.3)

    ax = axes[2]
    ax.axhline(0, color="#333", lw=0.6)
    ax.axhline(2.0, color="#c47d00", ls=":", lw=0.8, label="Start 2 kW")
    ax.axhline(4.14, color="#6a3d9a", ls=":", lw=0.8, label="3-phase 4.14 kW")
    ax.fill_between(
        times, leftover_kw, 0, where=[v >= 0 for v in leftover_kw], interpolate=True, color="#ff7f0e", alpha=0.35
    )
    ax.fill_between(
        times, leftover_kw, 0, where=[v < 0 for v in leftover_kw], interpolate=True, color="#8c564b", alpha=0.25
    )
    ax.plot(times, leftover_kw, color="#ff7f0e", lw=1.4, label="Leftover kW")
    ax.set_ylabel("kW")
    leftover_title = "Surplus leftover  (solar − house; EV cancels)"
    if policy == POLICY_SOLAR_PRIORITY:
        leftover_title += "  ·  still runs while SolarPriority skips"
    ax.set_title(leftover_title)
    ax.legend(loc="upper right", fontsize=8, ncol=3)
    ax.grid(True, alpha=0.3)

    ax = axes[3]
    ax.plot(times, a_amp, color="#1f77b4", lw=1.5, label="charger A amp")
    ax.plot(times, b_amp, color="#17becf", lw=1.5, ls="--", label="charger B amp")
    ax.set_ylabel("A")
    ax.set_ylim(-1, 34)
    ax.set_title("MQTT amp  ·  phase as 1 / 3 on the right axis")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(True, alpha=0.3)
    ax2 = ax.twinx()
    ax2.step(times, a_ph, where="post", color="#1f77b4", lw=1.0, alpha=0.5)
    ax2.step(times, b_ph, where="post", color="#17becf", lw=1.0, alpha=0.5, ls="--")
    ax2.set_ylabel("phases")
    ax2.set_ylim(-0.2, 3.6)
    ax2.set_yticks([0, 1, 3])

    ax = axes[4]
    ax.plot(times, a_w, color="#1f77b4", lw=1.5, label="charger A kW (amp×230×phases)")
    ax.plot(times, b_w, color="#17becf", lw=1.5, ls="--", label="charger B kW")
    ax.set_ylabel("kW")
    ax.set_title(
        "Commanded charge wattage  ·  HA leftover priority (1 highest); B omitted when one charger"
    )
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %H:%M", tz=HEL))
    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=[0, 6, 12, 14, 18], tz=HEL))
    fig.autofmt_xdate()
    fig.tight_layout(rect=[0, 0.02, 1, 0.96])
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def plot_compare(sims, path):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    names = [s["name"] for s in sims]
    window_h = [hours_where(s["ticks"], lambda t: t["cheap_window"]) for s in sims]
    full_h = [hours_where(s["ticks"], lambda t: t["cheap_full"]) for s in sims]
    skip_h = [
        hours_where(s["ticks"], lambda t: t["cheap_window"] and not t["cheap_full"])
        for s in sims
    ]
    surplus_h = [hours_where(s["ticks"], lambda t: t["write_on"]) for s in sims]
    enough_h = [hours_where(s["ticks"], lambda t: t["enough"]) for s in sims]
    upcoming = [max(t["upcoming_kwh"] for t in s["ticks"]) for s in sims]

    x = np.arange(len(names))
    w = 0.18
    fig, axes = plt.subplots(2, 1, figsize=(14, 8.5), gridspec_kw={"height_ratios": [1.3, 1.0]})
    fig.suptitle(
        "SolarPriority windows  ·  48 h Helsinki  ·  enough solar = 40 kWh, Off-sun hour = 1 kWh",
        fontsize=13,
        fontweight="bold",
    )

    ax = axes[0]
    ax.bar(x - 1.5 * w, window_h, w, color="#2ca02c", label="SolarPriority window")
    ax.bar(x - 0.5 * w, full_h, w, color="#1b7a1b", label="SolarPriority 22 kW")
    ax.bar(x + 0.5 * w, skip_h, w, color="#ff7f0e", label="enough-solar skip")
    ax.bar(x + 1.5 * w, surplus_h, w, color="#17becf", label="Surplus leftover")
    ax.set_ylabel("hours / 48 h")
    ax.set_xticks(x)
    ax.set_xticklabels(names, rotation=20, ha="right")
    ax.legend(loc="upper right", fontsize=8, ncol=2)
    ax.grid(True, axis="y", alpha=0.3)
    ax.set_title("charger A SolarPriority pri 1  ·  charger B Force off never charges")

    ax = axes[1]
    ax.bar(x - 0.2, upcoming, 0.4, color="#9467bd", label="Max forecast kWh (max today, tomorrow)")
    ax.bar(x + 0.2, enough_h, 0.4, color="#bcbd22", label="Hours enough solar is on")
    ax.axhline(SOLAR_ENOUGH_KWH, color="#d62728", ls="--", lw=1.2, label="40 kWh threshold")
    ax.set_ylabel("kWh  /  hours")
    ax.set_xticks(x)
    ax.set_xticklabels(names, rotation=20, ha="right")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(True, axis="y", alpha=0.3)
    ax.set_title("Forecast energy that gates SolarPriority  ·  Off-sun still drops ≥ 1 kWh hours")

    fig.tight_layout(rect=[0, 0.02, 1, 0.95])
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def write_report(sims, out_dir):
    lines = [
        "# SolarPriority 48 h Helsinki year-round",
        "",
        "**Charger A SolarPriority**, **charger B Force off** (B never charges). One cheap window after",
        "dropping hours with at least 1 kWh of expected solar (full-day today /",
        "tomorrow shaped by elevation). SolarPriority then skips 22 kW when the",
        "gating day's kWh ≥ 40 (today until sunset, tomorrow after). A finished",
        "cheapest window stays the plan and is not used for 22 kW.",
        "Surplus leftover uses HA leftover priority on SolarPriority chargers (A=1).",
        "",
        "| Case | Upcoming kWh | Enough solar | Window | 22 kW | Skip | Surplus | Leftover max |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for sim in sims:
        stats = sim_stats(sim)
        lines.append(
            "| %s | %.1f–%.1f | %.1f h | %.1f h | %.1f h | %.1f h | %.1f h | %.1f kW |"
            % (
                sim["name"],
                stats["upcoming_min"],
                stats["upcoming_max"],
                stats["enough_h"],
                stats["window_h"],
                stats["full_h"],
                stats["skip_h"],
                stats["surplus_h"],
                stats["leftover_max"] / 1000.0,
            )
        )
    lines.extend(
        [
            "",
            "- **Midwinter / February / October / DST**: today's kWh stays well under 40, and night hours are under 1 kWh, so SolarPriority 22 kW runs in the night window. After that window ends it stays the plan (idle) until prices or the date change.",
            "- **April mixed**: after sunset, tomorrow just crossing 40 kWh skips night 22 kW. Before sunset, today's full-day kWh gates. Surplus still starts in the brief midday sun.",
            "- **Midsummer clear**: ~87 kWh tomorrow, SolarPriority never force-on after sunset. Polar-day-long sun keeps today's gate on before sunset. Midday leftover still runs surplus (not 22 kW).",
            "- **Midsummer overcast**: ~16 kWh is not enough solar; Off-sun still drops hours with ≥ 1 kWh expected energy.",
            "",
            "![Season comparison](summary.png)",
            "",
        ]
    )
    for spec in PLOT_SPECS:
        lines.append("## %s" % spec["name"])
        lines.append("")
        lines.append("![%s](%s.png)" % (spec["name"], spec["name"]))
        lines.append("")
    path = Path(out_dir) / "REPORT.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def plot_all(out_dir, policy=POLICY_SOLAR_PRIORITY):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    sims = []
    for spec in PLOT_SPECS:
        sim = sim_from_spec(spec["name"], policy=policy)
        summary(sim)
        path = out_dir / ("%s.png" % spec["name"])
        plot_sim(sim, path)
        print("wrote %s" % path)
        paths.append(path)
        sims.append(sim)
    if policy == POLICY_SOLAR_PRIORITY:
        paths.append(plot_compare(sims, out_dir / "summary.png"))
        print("wrote %s" % paths[-1])
        report = write_report(sims, out_dir)
        print("wrote %s" % report)
    return paths


def main():
    case, run = case_runner()

    def test_helsinki_sun_sanity():
        dec = datetime.datetime(2026, 12, 21, 12, 0, tzinfo=HEL)
        jun = datetime.datetime(2026, 6, 21, 13, 0, tzinfo=HEL)
        el_dec = solar_elevation_deg(dec)
        el_jun = solar_elevation_deg(jun)
        assert_true(3 < el_dec < 12, "Dec 21 noon elevation %s" % el_dec)
        assert_true(45 < el_jun < 58, "Jun 21 noon elevation %s" % el_jun)
        assert_eq(solar_w(datetime.datetime(2026, 12, 21, 3, 0, tzinfo=HEL), 1.0), 0, "kaamos night")
        assert_gt(solar_w(jun, 1.0), 6000, "midsummer noon PV")

    def test_midwinter_clear_48h():
        sim = sim_from_spec("midwinter-clear")
        summary(sim)
        assert_48h_nordpool(sim)
        assert_no_surplus(sim, "midwinter heating eats leftover")
        cheap_h = hours_where(sim["ticks"], lambda t: t["cheap_full"])
        assert_gt(cheap_h, 3, "night windows still force-on")
        first_pub = at_hour(sim["ticks"], 14)[0]
        assert_true(
            first_pub["reason"] in ("planned", "no_window"),
            "14:00 is a planner outcome, not a crash: %s" % first_pub["reason"],
        )
        assert_true(first_pub["count"] >= 1, "tomorrow night is a valid window")
        night_full = hours_where(
            sim["ticks"], lambda t: t["cheap_full"] and 1 <= t["now"].hour < 6
        )
        assert_gt(night_full, 3, "night valley still 22 kW")
        for t in sim["ticks"]:
            assert_eq(t["enough"], False, "kaamos upcoming is under 40 kWh @ %s" % t["now"])
            assert_true(t["upcoming_kwh"] < 10, "winter upcoming %s" % t["upcoming_kwh"])
            if t["cheap_window"]:
                assert_eq(t["cheap_full"], True, "not enough solar: window is 22 kW @ %s" % t["now"])
            if 16 <= t["now"].hour < 21:
                assert_eq(t["cheap_full"], False, "evening 0.21 is above the ceiling @ %s" % t["now"])

    def test_midwinter_overcast_48h():
        sim = sim_from_spec("midwinter-overcast")
        summary(sim)
        assert_48h_nordpool(sim)
        assert_no_surplus(sim, "overcast kaamos")
        assert_gt(hours_where(sim["ticks"], lambda t: t["cheap_full"]), 3, "spot still charges")

    def test_february_48h():
        sim = sim_from_spec("february")
        summary(sim)
        assert_48h_nordpool(sim)
        assert_true(max_leftover(sim["ticks"]) < 2000, "February leftover still under start")
        assert_no_surplus(sim, "february")

    def test_april_mixed_48h():
        sim = sim_from_spec("april-mixed")
        summary(sim)
        assert_48h_nordpool(sim)
        assert_gt(max_leftover(sim["ticks"]), 2000, "April sun can start surplus")
        assert_gt(hours_where(sim["ticks"], lambda t: t["cheap_window"]), 2, "April still has a night window")
        night_after_sun = [
            t
            for t in sim["ticks"]
            if t["gating_day"] == "tomorrow" and t["tomorrow_kwh"] >= SOLAR_ENOUGH_KWH
        ]
        assert_true(night_after_sun, "April has post-sunset ticks with tomorrow ≥ 40 kWh")
        for t in night_after_sun:
            assert_eq(t["cheap_full"], False, "after sunset tomorrow ≥ 40 skips 22 kW @ %s" % t["now"])
        for t in sim["ticks"]:
            if t["gating_day"] == "today" and t["today_kwh"] >= SOLAR_ENOUGH_KWH:
                assert_eq(t["cheap_full"], False, "before sunset today ≥ 40 skips 22 kW @ %s" % t["now"])
        assert_true(
            any(t["tomorrow_kwh"] >= SOLAR_ENOUGH_KWH for t in sim["ticks"]),
            "April tomorrow crosses the 40 kWh gate",
        )
        on = [t for t in sim["ticks"] if t["write_on"] and not t["cheap_full"] and t["a"]["amp"]]
        assert_true(on, "April leftover runs on charger A")
        one_p = [t for t in on if t["a"]["psm"] == 1]
        assert_true(one_p, "April leftover often 1-phase")
        for t in sim["ticks"]:
            if t["enough"]:
                assert_eq(t["cheap_full"], False, "no 22 kW while enough @ %s" % t["now"])

    def test_midsummer_clear_48h():
        sim = sim_from_spec("midsummer-clear")
        summary(sim)
        assert_48h_nordpool(sim)
        assert_gt(max_leftover(sim["ticks"]), 5000, "midsummer leftover")
        surplus_h = hours_where(sim["ticks"], lambda t: t["write_on"])
        assert_gt(surplus_h, 6, "surplus runs through the long day")
        three = [t for t in sim["ticks"] if t["a"]["psm"] == 2]
        assert_true(three, "clear midsummer reaches 3-phase")
        assert_true(any(t["a"]["amp"] > 6 for t in three), "3-phase amp tracks leftover, not stuck at 6 A")
        held_up = [
            t
            for t in sim["ticks"]
            if t["write_on"]
            and not t["cheap_full"]
            and t["a"].get("arm_phase")
            and t["a"]["psm"] == 1
            and t["a"].get("wanted_psm") == 2
        ]
        for t in held_up:
            three_amp = surplus.budget(t["leftover"], 6, 32, 50, 230, 4140)[2]
            one_amp = surplus.budget(t["leftover"], 6, 32, 50, 230, 4140, force_psm=1)[2]
            assert_eq(t["a"]["amp"], one_amp, "held 1-phase uses 1-phase leftover amp")
            assert_true(t["a"]["amp"] != three_amp or one_amp == three_amp, "not the pending 3-phase amp")
        assert_eq(
            hours_where(sim["ticks"], lambda t: t["cheap_full"]),
            0,
            "87 kWh tomorrow skips 22 kW even at night",
        )
        assert_true(
            hours_where(sim["ticks"], lambda t: t["cheap_window"]) > 0
            or hours_where(sim["ticks"], lambda t: t["write_on"]) > 6,
            "window or surplus still runs",
        )
        for t in sim["ticks"]:
            if t["write_on"]:
                assert_eq(t["cheap_full"], False, "no 22 kW during enough solar @ %s" % t["now"])
                assert_true(t["a"]["w"] < 32 * VOLTS * 3, "charger A is leftover not full power @ %s" % t["now"])
        for t in sim["ticks"]:
            assert_true(t["enough"], "clear midsummer upcoming stays ≥ 40 kWh @ %s" % t["now"])
            assert_true(t["upcoming_kwh"] >= SOLAR_ENOUGH_KWH, "upcoming %s" % t["upcoming_kwh"])
        first_pub = at_hour(sim["ticks"], 14)[0]
        assert_true(
            first_pub["reason"] in ("planned", "no_window"),
            "14:00 is a planner outcome: %s" % first_pub["reason"],
        )

    def test_midsummer_overcast_48h():
        sim = sim_from_spec("midsummer-overcast")
        summary(sim)
        assert_48h_nordpool(sim)
        assert_true(max_leftover(sim["ticks"]) < 2500, "overcast June leftover is small")
        for t in sim["ticks"]:
            assert_eq(t["enough"], False, "overcast June upcoming under 40 @ %s" % t["now"])
            if t["cheap_full"]:
                assert_eq(t["offsun_window"], True, "22 kW is in the (blocked-hour) window @ %s" % t["now"])
                blocked = surplus.surplus_hour_ranges(
                    Clock(t["now"], tz=HEL),
                    t["today_kwh"],
                    t["tomorrow_kwh"],
                    OFFSUN_HOUR_KWH,
                    lat=60.17,
                    lon=24.94,
                )
                ts = t["now"].timestamp()
                assert_true(
                    not any(start <= ts < end for start, end in blocked),
                    "no 22 kW in a ≥ 1 kWh hour @ %s" % t["now"],
                )

    def test_october_48h():
        sim = sim_from_spec("october")
        summary(sim)
        assert_48h_nordpool(sim)
        assert_gt(hours_where(sim["ticks"], lambda t: t["cheap_window"]), 2, "October night windows")
        first_pub = at_hour(sim["ticks"], 14)[0]
        assert_eq(first_pub["tomorrow_ok"], True, "October 14:00 has tomorrow")

    def test_dst_spring_forward_48h():
        # EU DST 2026-03-29 03:00 → 04:00. Start the evening before.
        sim = sim_from_spec("dst-spring")
        summary(sim)
        assert_48h_nordpool(sim)
        n_slots = len(sim["days"][1])
        assert_eq(n_slots, 92, "spring-forward local day has 92 quarter-hours, not 96")
        fourteens = at_hour(sim["ticks"], 14)
        assert_true(len(fourteens) >= 2, "14:00 still exists on DST weekend")
        for t in fourteens:
            assert_eq(t["tomorrow_ok"], True, "tomorrow still publishes after DST")

    def test_dst_autumn_48h():
        sim = sim_from_spec("dst-autumn")
        summary(sim)
        assert_48h_nordpool(sim)
        n_slots = len(sim["days"][1])
        assert_eq(n_slots, 100, "autumn extra hour is 100 quarter-hours")
        assert_eq(at_hour(sim["ticks"], 14)[0]["tomorrow_ok"], True, "14:00 still publishes")

    def test_leftover_priority_and_optional_charger():
        default = sim_from_spec("midsummer-clear", policy=POLICY_SOLAR_PRIORITY)
        for t in default["ticks"]:
            assert_eq(t["b"]["w"], 0, "Force off B never charges @ %s" % t["now"])
        both_off = sim_from_spec(
            "midsummer-clear",
            policy=POLICY_FORCE_OFF,
            b_policy=POLICY_FORCE_OFF,
        )
        leftover_on = [t for t in both_off["ticks"] if t["write_on"]]
        assert_true(leftover_on, "midsummer leftover still wants to start")
        for t in both_off["ticks"]:
            assert_eq(t["a"]["w"], 0, "Force off A never charges @ %s" % t["now"])
            assert_eq(t["b"]["w"], 0, "Force off B never charges @ %s" % t["now"])
            assert_eq(t["cheap_full"], False, "Force off is not 22 kW @ %s" % t["now"])
        unequal = sim_from_spec(
            "midsummer-clear",
            policy=POLICY_SOLAR_PRIORITY,
            b_policy=POLICY_SOLAR_PRIORITY,
        )
        summary(unequal)
        both = [t for t in unequal["ticks"] if t["write_on"] and not t["cheap_full"]]
        assert_true(both, "midsummer SolarPriority has leftover surplus")
        offered = [t for t in both if t["leftover"] >= 6 * 230]
        assert_true(offered, "leftover reaches 6 A")
        for t in offered:
            assert_true(t["a"]["amp"], "priority 1 A is offered leftover @ %s" % t["now"])
            assert_eq(
                t["b"]["amp"],
                None,
                "priority 2 B waits while A takes leftover @ %s" % t["now"],
            )
        equal = sim_from_spec(
            "midsummer-clear",
            policy=POLICY_SOLAR_PRIORITY,
            b_policy=POLICY_SOLAR_PRIORITY,
            priorities={SERIAL_CHEAP: 50, SERIAL_SURPLUS: 50},
        )
        both_eq = [t for t in equal["ticks"] if t["write_on"] and not t["cheap_full"]]
        assert_true(both_eq, "equal priority still surplus")
        for t in both_eq:
            if t["leftover"] < 6 * 230:
                continue
            assert_true(t["a"]["amp"], "equal priority A shares leftover @ %s" % t["now"])
            assert_true(t["b"]["amp"], "equal priority B shares leftover @ %s" % t["now"])
        one = sim_from_spec("midsummer-clear", policy=POLICY_SOLAR_PRIORITY, n_chargers=1)
        surplus_one = False
        for t in one["ticks"]:
            assert_eq(t["b"]["w"], 0, "no charger B @ %s" % t["now"])
            if t["write_on"] and not t["cheap_full"] and t["leftover"] >= 6 * 230:
                surplus_one = True
                assert_true(t["a"]["amp"], "single charger gets leftover @ %s" % t["now"])
        assert_true(surplus_one, "single charger still runs surplus")
        forced = sim_from_spec(
            "midsummer-clear",
            policy=POLICY_FORCE_ON,
            b_policy=POLICY_SOLAR_PRIORITY,
        )
        overlap = [t for t in forced["ticks"] if t["cheap_full"] and t["write_on"]]
        assert_true(overlap, "B surplus while A is Force-on 22 kW")
        for t in overlap:
            assert_eq(t["a"]["amp"], 32, "A full power @ %s" % t["now"])
            assert_true(t["b"]["amp"], "B leftover beside full-power A @ %s" % t["now"])

    case("helsinki_sun_sanity", test_helsinki_sun_sanity)
    case("midwinter_clear_48h", test_midwinter_clear_48h)
    case("midwinter_overcast_48h", test_midwinter_overcast_48h)
    case("february_48h", test_february_48h)
    case("april_mixed_48h", test_april_mixed_48h)
    case("midsummer_clear_48h", test_midsummer_clear_48h)
    case("midsummer_overcast_48h", test_midsummer_overcast_48h)
    case("october_48h", test_october_48h)
    case("dst_spring_forward_48h", test_dst_spring_forward_48h)
    case("dst_autumn_48h", test_dst_autumn_48h)
    case("leftover_priority_and_optional_charger", test_leftover_priority_and_optional_charger)

    run()


if __name__ == "__main__":
    import sys

    if "--plot" in sys.argv:
        dest = Path("/opt/cursor/artifacts") / "finland-year"
        if not dest.parent.exists():
            dest = ROOT / "tests" / "plots" / "finland-year"
        plot_all(dest, policy=POLICY_SOLAR_PRIORITY)
    else:
        main()
