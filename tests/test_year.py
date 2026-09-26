"""48 h Finnish year-round houses driving the real planner and engine every 15 minutes."""

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from core import planner
from core.engine import decide
from core.model import (
    CAR_CHARGING,
    CAR_WAITCAR,
    POLICY_FORCE_OFF,
    POLICY_FORCE_ON,
    POLICY_SOLAR_AND_GRID,
    POLICY_SOLAR_PRIORITY,
    ROLE_FULL,
    ROLE_SURPLUS,
    VOLTS,
    Charger,
    HouseReading,
    Memory,
    Settings,
)

HEL = ZoneInfo("Europe/Helsinki")
LAT, LON = 60.17, 24.94
TICK = timedelta(minutes=15)
PV_PEAK_W = 10000
PUBLISH_H = 14


def pv_w(t, cloud):
    el = planner.solar_elevation_deg(t.timestamp(), LAT, LON)
    factor = cloud(t) if callable(cloud) else cloud
    return 0 if el <= 0 else int(PV_PEAK_W * math.sin(math.radians(el)) * factor)


def house_base_w(t, outdoor_c):
    h = t.hour + t.minute / 60
    return int(700 + (1500 if 16.5 <= h < 18 else 0) + max(0.0, (17 - outdoor_c) * 200))


def winter_price(t, day):
    h = t.hour + t.minute / 60
    return 0.045 - 0.008 * (day % 3) if 1 <= h < 6 else 0.21 if 16 <= h < 21 else 0.12


def summer_price(t, day):
    h = t.hour + t.minute / 60
    return -0.01 - 0.015 * (day % 3) if 10 <= h < 16 else 0.13 if 17 <= h < 21 else 0.11


def shoulder_price(t, day):
    h = t.hour + t.minute / 60
    if 1 <= h < 5:
        return 0.04 - 0.006 * (day % 3)
    return 0.055 if 11 <= h < 15 else 0.16 if 16.5 <= h < 20.5 else 0.11


def april_clouds(t):
    return 0.25 if 10 <= t.hour < 13 else 0.9


SPECS = {
    "midwinter-clear": (datetime(2026, 12, 21, tzinfo=HEL), -6, 1.0, winter_price),
    "midwinter-overcast": (datetime(2026, 12, 21, tzinfo=HEL), -12, 0.15, winter_price),
    "february": (datetime(2026, 2, 10, tzinfo=HEL), -8, 0.45, winter_price),
    "april-mixed": (datetime(2026, 4, 15, tzinfo=HEL), 5, april_clouds, shoulder_price),
    "midsummer-clear": (datetime(2026, 6, 21, tzinfo=HEL), 17, 1.0, summer_price),
    "midsummer-overcast": (datetime(2026, 6, 21, tzinfo=HEL), 14, 0.18, summer_price),
    "october": (datetime(2026, 10, 10, tzinfo=HEL), 6, 0.35, shoulder_price),
    "dst-spring": (datetime(2026, 3, 28, tzinfo=HEL), 1, 0.5, shoulder_price),
    "dst-autumn": (datetime(2026, 10, 24, tzinfo=HEL), 5, 0.35, shoulder_price),
}


def day_slots(day_start, price_fn, index):
    out, t = [], day_start
    end = datetime.combine(day_start.date() + timedelta(days=1), datetime.min.time(), tzinfo=HEL)
    while t < end:
        nxt = datetime.fromtimestamp(t.timestamp() + 900, HEL)
        out.append({"start": t, "end": nxt, "value": price_fn(t, index)})
        t = nxt
    return out


def day_kwh(day_start, cloud):
    end = day_start + timedelta(days=1)
    t, kwh = day_start, 0.0
    while t < end:
        kwh += pv_w(t, cloud) * 0.25 / 1000
        t = datetime.fromtimestamp(t.timestamp() + 900, HEL)
    return kwh


def simulate(name, policy=POLICY_SOLAR_PRIORITY, b_policy=POLICY_FORCE_OFF, n_chargers=2, soc=96.0):
    start, outdoor_c, cloud, price_fn = SPECS[name]
    days = [datetime.combine(start.date() + timedelta(days=i), datetime.min.time(), tzinfo=HEL) for i in range(4)]
    slots = [day_slots(d, price_fn, i) for i, d in enumerate(days)]
    kwh = [day_kwh(d, cloud) for d in days]
    serials = ["A", "B"][:n_chargers]
    settings = Settings(policy={"A": policy, "B": b_policy}, priority={"A": 1, "B": 2})
    chargers = [Charger(s, i, car=CAR_WAITCAR, nrg_w=0) for i, s in enumerate(serials)]
    memory, ticks, now = Memory(), [], start
    end = datetime.fromtimestamp(start.timestamp() + 48 * 3600, HEL)
    while now <= end:
        index = (now.date() - start.date()).days
        attrs = {"raw_today": slots[index]}
        if now.hour >= PUBLISH_H:
            attrs.update(raw_tomorrow=slots[index + 1], tomorrow_valid=True)
        plan = planner.plan(attrs, now, settings, kwh[index], kwh[index + 1], LAT, LON)
        ev_w = sum(c.nrg_w for c in chargers)
        solar, base = pv_w(now, cloud), house_base_w(now, outdoor_c)
        house = HouseReading(soc, True, (solar, base + ev_w, ev_w))
        decision = decide(settings, chargers, house, plan, now, memory)
        for c in chargers:
            command = decision.chargers[c.serial].command
            phases = 3 if command.psm == 2 else 1
            c.nrg_w = command.amp * VOLTS * phases if command.on else 0
            c.car = CAR_CHARGING if command.on else CAR_WAITCAR
        ticks.append(
            {
                "now": now,
                "leftover": solar - base,
                "plan": plan,
                "roles": {s: d.role for s, d in decision.chargers.items()},
                "cmd": {s: d.command for s, d in decision.chargers.items()},
                "surplus_on": any(
                    d.role == ROLE_SURPLUS and d.command.on for d in decision.chargers.values()
                ),
            }
        )
        now = datetime.fromtimestamp(now.timestamp() + TICK.total_seconds(), HEL)
    return ticks, slots


def full(t, serial="A"):
    return t["roles"].get(serial) == ROLE_FULL


def hours(ticks, pred):
    return sum(0.25 for t in ticks if pred(t))


def surplus_amp(t, serial):
    c = t["cmd"].get(serial)
    return c.amp if c is not None and c.on and t["roles"][serial] == ROLE_SURPLUS else None


def assert_day_ahead_publication(ticks):
    start = ticks[0]["now"]
    assert len(ticks) >= 192
    for t in ticks:
        publishes = t["now"].hour >= PUBLISH_H
        assert t["plan"].tomorrow_ok == publishes, t["now"]
    prev = None
    for t in ticks:
        same_env = prev is not None and prev["now"].date() == t["now"].date()
        if same_env and prev["plan"].tomorrow_ok == t["plan"].tomorrow_ok:
            starts = [w.start for w in t["plan"].windows]
            assert starts == [w.start for w in prev["plan"].windows], t["now"]
        prev = t
    assert start.hour == 0


def test_sun_model_sanity():
    dec = planner.solar_elevation_deg(datetime(2026, 12, 21, 12, tzinfo=HEL).timestamp(), LAT, LON)
    jun = planner.solar_elevation_deg(datetime(2026, 6, 21, 13, tzinfo=HEL).timestamp(), LAT, LON)
    assert 3 < dec < 12 and 45 < jun < 58


def test_midwinter_clear():
    ticks, _ = simulate("midwinter-clear")
    assert_day_ahead_publication(ticks)
    assert hours(ticks, lambda t: t["surplus_on"]) == 0
    assert hours(ticks, lambda t: full(t) and 1 <= t["now"].hour < 6) > 3
    for t in ticks:
        assert not t["plan"].enough
        assert full(t) == t["plan"].in_window(t["now"])
        if 16 <= t["now"].hour < 21:
            assert not full(t)


@pytest.mark.parametrize("name", ["midwinter-overcast", "february"])
def test_winter_has_no_surplus_but_night_windows(name):
    ticks, _ = simulate(name)
    assert_day_ahead_publication(ticks)
    assert max(t["leftover"] for t in ticks) < 2000
    assert hours(ticks, lambda t: t["surplus_on"]) == 0
    assert hours(ticks, full) > 3


def test_april_mixed():
    ticks, _ = simulate("april-mixed")
    assert_day_ahead_publication(ticks)
    assert max(t["leftover"] for t in ticks) > 2000
    assert hours(ticks, lambda t: t["plan"].in_window(t["now"])) > 2
    for t in ticks:
        if t["plan"].enough:
            assert not full(t), t["now"]
    on = [t for t in ticks if surplus_amp(t, "A")]
    assert on and any(t["cmd"]["A"].psm == 1 for t in on)


def test_midsummer_clear():
    ticks, _ = simulate("midsummer-clear")
    assert_day_ahead_publication(ticks)
    peak = max(t["leftover"] for t in ticks)
    assert 5000 < peak < 7590
    assert hours(ticks, lambda t: t["surplus_on"]) > 6
    on = [t for t in ticks if surplus_amp(t, "A")]
    assert all(t["cmd"]["A"].psm == 1 for t in on)
    assert any(t["cmd"]["A"].amp >= 30 for t in on)
    assert hours(ticks, full) == 0
    assert all(t["plan"].enough for t in ticks)


def test_midsummer_overcast_never_charges_full_in_a_sunny_hour():
    ticks, _ = simulate("midsummer-overcast")
    assert max(t["leftover"] for t in ticks) < 2500
    for t in ticks:
        assert not t["plan"].enough
        if full(t):
            assert not any(b <= t["now"] < e for b, e in t["plan"].blocked)


def test_october_and_dst_days():
    ticks, _ = simulate("october")
    assert hours(ticks, lambda t: t["plan"].in_window(t["now"])) > 2
    spring, spring_slots = simulate("dst-spring")
    assert len(spring_slots[1]) == 92
    assert_day_ahead_publication(spring)
    autumn, autumn_slots = simulate("dst-autumn")
    assert len(autumn_slots[1]) == 100
    assert_day_ahead_publication(autumn)


def test_priorities_force_off_and_single_charger():
    ticks, _ = simulate("midsummer-clear")
    assert all(not t["cmd"]["B"].on for t in ticks)
    both_off, _ = simulate("midsummer-clear", policy=POLICY_FORCE_OFF)
    assert all(not c.on for t in both_off for c in t["cmd"].values())
    shared, _ = simulate("midsummer-clear", b_policy=POLICY_SOLAR_PRIORITY)
    offered = [t for t in shared if t["surplus_on"] and t["leftover"] >= 2 * 6 * VOLTS]
    assert offered
    for t in offered:
        assert surplus_amp(t, "A")
        assert surplus_amp(t, "B") is None or t["cmd"]["B"].amp == 6
    one, _ = simulate("midsummer-clear", n_chargers=1)
    assert any(surplus_amp(t, "A") for t in one)


def test_force_on_beside_surplus_and_both_full_in_window():
    forced, _ = simulate("midsummer-clear", policy=POLICY_FORCE_ON, b_policy=POLICY_SOLAR_PRIORITY)
    overlap = [t for t in forced if full(t) and surplus_amp(t, "B")]
    assert overlap and all(t["cmd"]["A"].amp == 32 for t in overlap)
    winter, _ = simulate("midwinter-clear", b_policy=POLICY_SOLAR_PRIORITY)
    window = [t for t in winter if full(t)]
    assert window and all(t["cmd"]["B"].amp == 32 and t["cmd"]["B"].psm == 2 for t in window)


def test_solarandgrid_still_charges_full_on_enough_solar():
    ticks, _ = simulate("midsummer-clear", policy=POLICY_SOLAR_AND_GRID)
    full_ticks = [t for t in ticks if full(t)]
    assert full_ticks and all(t["plan"].enough and t["plan"].in_window(t["now"]) for t in full_ticks)
    assert any(surplus_amp(t, "A") for t in ticks)
