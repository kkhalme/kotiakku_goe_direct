from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from core import planner
from core.model import Settings

UTC = timezone.utc
HEL = ZoneInfo("Europe/Helsinki")
SLOT = 900
NOW = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)
BASE = datetime(2026, 3, 15, tzinfo=UTC).timestamp()


def slots(prices, base=BASE, step=SLOT):
    return [(base + i * step, base + (i + 1) * step, p) for i, p in enumerate(prices)]


def pick(prices, min_h=2, max_h=5, ceiling=0.2, pct=20, eur=0.02, blocked=(), step=SLOT, now=NOW):
    windows, _reason = planner.choose_windows(
        slots(prices, step=step), list(blocked), now, min_h, max_h, ceiling, pct, eur
    )
    return windows


def hours(window):
    return round((window[1] - window[0]) / 3600, 2)


def attrs_for(base, today, tomorrow=None):
    def items(start, prices):
        return [
            {
                "start": datetime.fromtimestamp(start + i * SLOT, UTC).isoformat(),
                "end": datetime.fromtimestamp(start + (i + 1) * SLOT, UTC).isoformat(),
                "value": p,
            }
            for i, p in enumerate(prices)
        ]

    out = {"raw_today": items(base, today)}
    if tomorrow:
        out["raw_tomorrow"] = items(tomorrow[0], tomorrow[1])
    return out


def run_plan(attrs, now=NOW, today_kwh=None, tomorrow_kwh=None, history=None, epoch_seen=None, **knobs):
    settings = Settings(**{"window_flex_pct": 0, "window_flex_eur": 0, **knobs})
    return planner.plan(attrs, now, settings, today_kwh, tomorrow_kwh, 60.17, 24.94, history, epoch_seen)


def test_seed_is_cheapest_min_hours():
    seed = planner.find_seed(slots([0.08] * 8 + [0.01] * 8 + [0.07] * 8), 7200)
    assert seed[1] == 8 and seed[2] == 15


def test_seed_tie_gap_and_weighting():
    assert planner.find_seed(slots([0.03] * 8 + [0.2] * 8 + [0.03] * 8), 7200)[1] == 0
    gapped = slots([0.02] * 8) + slots([0.01] * 8, base=BASE + 8 * SLOT + 3600)
    assert planner.find_seed(gapped, 7200)[1] == 8
    weighted = [(BASE, BASE + 3600, 0.01), (BASE + 3600, BASE + 7200, 0.10), (BASE + 7200, BASE + 14400, 0.06)]
    avg, i, _j = planner.find_seed(weighted, 7200)
    assert i == 0 and round(avg, 4) == 0.055


def test_grow_cheaper_side_under_flex():
    window = pick([0.04] * 4 + [0.05] * 8 + [0.06] * 8)[0]
    assert window[0] == BASE
    assert hours(window) > 2 and window[2] <= 0.07 + 1e-9


def test_no_grow_without_flex_or_when_min_equals_max():
    assert hours(pick([0.01] * 8 + [0.012] * 8, pct=0, eur=0)[0]) == 2
    assert hours(pick([0.04] * 16, pct=0, eur=0)[0]) == 2
    assert hours(pick([0.01] * 8 + [0.012] * 8, max_h=2, pct=50, eur=1)[0]) == 2
    assert hours(pick([0.01] * 8, min_h=0.25, max_h=0.25, pct=50, eur=1)[0]) == 0.25


def test_deep_valley_stays_short():
    window = pick([0.2] * 8 + [0.01] * 8 + [0.2] * 8)[0]
    assert hours(window) == 2 and window[0] == BASE + 8 * SLOT


def test_one_window_even_with_two_nights():
    windows = pick([0.04] * 12 + [0.25] * 48 + [0.01] * 12)
    assert len(windows) == 1 and windows[0][0] == BASE + 60 * SLOT


def test_ceiling_abort_and_hard_no():
    assert pick([0.25] * 8 + [0.22] * 8 + [0.3] * 8) == []
    assert pick([0.21] * 8) == []
    assert len(pick([0.2] * 8, pct=0, eur=0)) == 1
    assert pick([0.05] * 8 + [0.25] + [0.05] * 8, pct=50, eur=1)[0][1] == BASE + 8 * SLOT
    assert pick([0.05] * 8 + [0.2] + [0.4] * 8, pct=50, eur=1)[0][1] == BASE + 9 * SLOT
    spike = pick([0.01] * 7 + [0.25] + [0.4] * 8, pct=0, eur=0)
    assert len(spike) == 1 and spike[0][2] < 0.2


def test_grow_sides_and_caps():
    right = pick([0.25] * 8 + [0.02] * 8 + [0.03] * 8)[0]
    assert right[0] == BASE + 8 * SLOT and right[1] > BASE + 16 * SLOT
    equal = pick([0.06] * 8 + [0.04] * 8 + [0.06] * 8, max_h=3)[0]
    assert equal[0] == BASE + 4 * SLOT and hours(equal) == 3
    assert hours(pick([0.04] * 32, max_h=3, pct=50, eur=1)[0]) == 3
    assert hours(pick([0.04] * 16, max_h=2.1, pct=50, eur=1)[0]) == 2
    assert hours(pick([0.04] * 16, max_h=2.25, pct=50, eur=1)[0]) == 2.25
    assert hours(pick([0.1] * 8 + [0.11] * 8, pct=20, eur=0)[0]) > 2
    assert hours(pick([0.1] * 8 + [0.11] * 8, pct=0, eur=0.02)[0]) > 2
    negative = pick([-0.05] * 8 + [-0.04] * 8, pct=0, eur=0.02)[0]
    assert hours(negative) > 2 and negative[2] <= -0.03 + 1e-9


def test_hourly_and_half_hour_native_steps():
    hourly = pick([0.1] * 3 + [0.02] * 2 + [0.05] * 3, step=3600)[0]
    assert hourly[0] == BASE + 3 * 3600 and hours(hourly) == 5
    half = pick([0.04] * 4 + [0.05] * 6, max_h=4, pct=50, eur=1, step=1800)[0]
    assert hours(half) == 4 and (half[1] - half[0]) % 1800 == 0


def test_blocked_hours_split_islands():
    prices = [0.05] * 32 + [0.01] * 32 + [0.05] * 32
    window = pick(prices, blocked=[(BASE + 8 * 3600, BASE + 16 * 3600)])[0]
    assert window[0] == BASE and window[1] <= BASE + 8 * 3600 + 1
    later = pick([0.03] * 8 + [0.01] * 8 + [0.02] * 8, pct=0, eur=0, blocked=[(BASE + 8 * SLOT, BASE + 16 * SLOT)])
    assert later[0][0] == BASE + 16 * SLOT


def test_value_lists_map_onto_local_day():
    now = datetime(2026, 3, 15, 12, tzinfo=HEL)
    day = datetime(2026, 3, 15, tzinfo=HEL).timestamp()
    hourly = planner.price_slots({"today": [0.1] * 24}, now)
    assert len(hourly) == 24 and hourly[0][0] == day and hourly[0][1] - hourly[0][0] == 3600
    half = planner.price_slots({"raw_today": [0.1] * 48}, now)
    assert half[1][0] - half[0][0] == 1800


def test_plan_reasons():
    assert run_plan(None).reason == "no_source"
    assert run_plan({}).reason == "no_slots"
    assert run_plan(attrs_for(BASE, [0.3] * 16)).reason == "no_window"
    assert run_plan(attrs_for(BASE, [0.04] * 16)).reason == "planned"


def test_ticks_do_not_slide_and_finished_window_stays():
    attrs = attrs_for(BASE, [0.02] * 8 + [0.2] * 16 + [0.04] * 8)
    early = run_plan(attrs, now=datetime.fromtimestamp(BASE + 1800, UTC))
    late = run_plan(attrs, now=datetime.fromtimestamp(BASE + 5 * 3600, UTC))
    assert early.windows[0].start == late.windows[0].start
    assert not late.in_window(datetime.fromtimestamp(BASE + 5 * 3600, UTC))


def test_cheaper_tomorrow_is_a_new_environment():
    today = [0.04] * 16
    held = run_plan(attrs_for(BASE, today, (BASE + 86400, [0.10] * 16)))
    assert held.windows[0].start.timestamp() == BASE and len(held.windows) == 2
    switched = run_plan(attrs_for(BASE, today, (BASE + 86400, [0.01] * 16)))
    assert switched.windows[0].start.timestamp() >= BASE + 86400 - 1


def test_followup_when_first_misses_overnight():
    now = datetime.fromtimestamp(BASE + 15 * 3600, UTC)
    today_22 = BASE + 22 * 3600
    tomorrow = BASE + 86400
    morning = [0.01] * 8 + [0.2] * 8
    attrs = attrs_for(BASE, morning, (tomorrow, [0.05] * 16 + [0.2] * 16))
    attrs["raw_today"] += attrs_for(BASE + 21 * 3600, [0.04] * 12)["raw_today"]
    two = run_plan(attrs, now=now, window_flex_pct=20, window_flex_eur=0.02)
    assert len(two.windows) == 2 and two.windows[0].start.timestamp() == BASE
    follow = two.windows[1]
    assert follow.end.timestamp() > tomorrow
    assert follow.start.timestamp() >= BASE + 21 * 3600 - 1

    covered = run_plan(attrs_for(BASE + 21 * 3600, [0.02] * 20, (tomorrow, [0.1] * 16)), now=now)
    assert len(covered.windows) == 1
    ends_at_22 = run_plan(attrs_for(BASE + 20 * 3600, [0.02] * 8, (tomorrow, [0.1] * 16)), now=now)
    assert ends_at_22.windows[0].end.timestamp() == today_22 and len(ends_at_22.windows) == 2
    starts_at_22 = run_plan(attrs_for(today_22, [0.02] * 8, (tomorrow + 7200, [0.1] * 16)), now=now)
    assert len(starts_at_22.windows) == 1
    dear = run_plan(attrs_for(BASE, morning, (tomorrow, [0.4] * 16)), now=now)
    assert len(dear.windows) == 1


def test_overnight_22_is_local_wall_clock():
    now = datetime(2026, 3, 15, 17, tzinfo=HEL)
    day = datetime(2026, 3, 15, tzinfo=HEL).timestamp()
    tomorrow = datetime(2026, 3, 16, tzinfo=HEL).timestamp()
    ends_local_22 = attrs_for(day + 20 * 3600, [0.02] * 8, (tomorrow, [0.1] * 16))
    result = run_plan(ends_local_22, now=now)
    assert result.windows[0].end == datetime(2026, 3, 15, 22, tzinfo=HEL)
    assert len(result.windows) == 2


def test_union_windows_on_the_seam():
    first = planner.Window(datetime.fromtimestamp(1000, UTC), datetime.fromtimestamp(2000, UTC), 0.01)
    second = planner.Window(datetime.fromtimestamp(2000, UTC), datetime.fromtimestamp(3000, UTC), 0.02)
    result = run_plan(None)
    result.windows = [first, second]
    assert result.in_window(datetime.fromtimestamp(2000, UTC))
    result.windows = [first]
    assert not result.in_window(datetime.fromtimestamp(2000, UTC))


@pytest.mark.parametrize("day,count", [(date(2026, 3, 29), 92), (date(2026, 10, 25), 100)])
def test_dst_days_keep_every_slot(day, count):
    start = datetime(day.year, day.month, day.day, tzinfo=HEL)
    end = start + timedelta(days=1)
    items, t = [], start.timestamp()
    while t < end.timestamp() - 1:
        items.append({"start": datetime.fromtimestamp(t, HEL), "end": datetime.fromtimestamp(t + SLOT, HEL), "value": 0.05})
        t += SLOT
    now = start + timedelta(hours=12)
    assert len(planner.price_slots({"raw_today": items}, now)) == count
    assert run_plan({"raw_today": items}, now=now).reason == "planned"


def _day_attrs(day, prices):
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    items = [
        {"start": start + timedelta(seconds=i * SLOT), "end": start + timedelta(seconds=(i + 1) * SLOT), "value": p}
        for i, p in enumerate(prices)
    ]
    return start, items


def test_midnight_keeps_the_window_from_cached_yesterday():
    cheap = [0.01] * 8
    evening_day = date(2026, 3, 15)
    start, today = _day_attrs(evening_day, [0.2] * 88 + cheap)
    _tomorrow_start, tomorrow = _day_attrs(date(2026, 3, 16), cheap + [0.2] * 88)
    evening = datetime(2026, 3, 15, 20, tzinfo=UTC)
    before = run_plan({"raw_today": today, "raw_tomorrow": tomorrow}, now=evening, window_min_h=4, window_max_h=4, window_flex_pct=0, window_flex_eur=0)
    assert before.windows[0].start == start.replace(hour=22)
    assert before.windows[0].end == datetime(2026, 3, 16, 2, tzinfo=UTC)
    days = planner.remember_day({}, evening, planner.price_slots({"raw_today": today}, evening), None)
    after_midnight = datetime(2026, 3, 16, 0, 30, tzinfo=UTC)
    after = run_plan({"raw_today": tomorrow}, now=after_midnight, history=days, window_min_h=4, window_max_h=4, window_flex_pct=0, window_flex_eur=0)
    assert (after.windows[0].start, after.windows[0].end) == (before.windows[0].start, before.windows[0].end)


def test_new_epoch_keeps_only_the_window_already_running():
    running = (datetime(2026, 3, 16, 12, tzinfo=UTC).timestamp(), datetime(2026, 3, 16, 16, tzinfo=UTC).timestamp(), 0.05)
    later = (datetime(2026, 3, 16, 22, tzinfo=UTC).timestamp(), datetime(2026, 3, 17, 0, tzinfo=UTC).timestamp(), 0.04)
    kept = planner.carry_windows([], [running, later], datetime(2026, 3, 16, 14, tzinfo=UTC).timestamp())
    assert kept == [running]
    assert planner.carry_windows([], [running], None) == []


def test_min_hours_clamped_and_swapped():
    result = run_plan(attrs_for(BASE, [0.04] * 32), window_min_h=5, window_max_h=2)
    assert round((result.windows[0].end - result.windows[0].start).total_seconds() / 3600, 2) == 2
