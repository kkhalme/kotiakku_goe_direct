from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from core import planner
from core.model import Settings

HEL = ZoneInfo("Europe/Helsinki")
LAT, LON = 60.17, 24.94
SLOT = 900


def day_items(day, prices):
    start = datetime(day.year, day.month, day.day, tzinfo=HEL)
    return [
        {"start": start + timedelta(seconds=i * SLOT), "end": start + timedelta(seconds=(i + 1) * SLOT), "value": p}
        for i, p in enumerate(prices)
    ]


def solar_curve(prices_night=0.03, prices_day=-0.02):
    """96 local slots: cheap noon (solar dump), cheaper-than-evening night."""
    return [prices_day if 40 <= i < 64 else prices_night if i < 24 else 0.12 for i in range(96)]


def run(now, today_kwh, tomorrow_kwh, attrs, **knobs):
    return planner.plan(attrs, now, Settings(**knobs), today_kwh, tomorrow_kwh, LAT, LON)


def test_hour_weights_sum_and_night_is_zero():
    hours = planner.hour_kwh(date(2026, 6, 21), HEL, 60.0, LAT, LON)
    assert abs(sum(k for *_x, k in hours) - 60.0) < 1e-6
    midwinter = planner.hour_kwh(date(2026, 12, 21), HEL, 2.0, LAT, LON)
    by_hour = {datetime.fromtimestamp(s, HEL).hour: k for s, _e, k in midwinter}
    assert by_hour[0] == 0 and by_hour[12] > 0
    assert planner.hour_kwh(date(2026, 6, 21), HEL, None, LAT, LON) == []


def test_blocked_hours_threshold():
    now = datetime(2026, 6, 21, 8, tzinfo=HEL)
    blocked = planner.blocked_hours(now, 60.0, None, 1.0, LAT, LON)
    assert len(blocked) == 1
    start, end = (datetime.fromtimestamp(t, HEL) for t in blocked[0])
    assert start.hour < 12 < end.hour
    assert planner.blocked_hours(now, 60.0, 60.0, 0, LAT, LON) == []
    assert planner.blocked_hours(now, 8.0, 6.0, 1.0, LAT, LON) == []


def test_offsun_drops_noon_from_search():
    now = datetime(2026, 6, 21, 8, tzinfo=HEL)
    result = run(now, 60.0, None, {"raw_today": day_items(now, solar_curve())})
    assert result.windows and result.windows[0].start.hour < 10
    unblocked = run(now, 60.0, None, {"raw_today": day_items(now, solar_curve())}, offsun_hour_kwh=0)
    assert 10 <= unblocked.windows[0].start.hour <= 16


def test_missing_tomorrow_forecast_still_plans_after_midnight():
    now = datetime(2026, 4, 10, 15, tzinfo=HEL)
    today = [0.12] * 96
    tomorrow = [0.02 if i < 16 else 0.12 for i in range(96)]
    attrs = {"raw_today": day_items(now, today), "raw_tomorrow": day_items(now + timedelta(days=1), tomorrow)}
    result = run(now, 20.0, None, attrs)
    midnight = datetime(2026, 4, 11, tzinfo=HEL)
    assert result.windows[0].start <= midnight < result.windows[0].end


def test_gating_stays_today_until_prices_and_usable_solar_end():
    attrs_today = {"raw_today": day_items(date(2026, 6, 21), [0.1] * 96)}
    attrs_both = dict(attrs_today, raw_tomorrow=day_items(date(2026, 6, 22), [0.1] * 96), tomorrow_valid=True)
    noon = datetime(2026, 6, 21, 12, tzinfo=HEL)
    assert run(noon, 80.0, 10.0, attrs_both).gating_day == "today"
    evening = datetime(2026, 6, 21, 23, tzinfo=HEL)
    no_prices = run(evening, 80.0, 10.0, attrs_today)
    assert no_prices.gating_day == "today" and no_prices.enough
    flipped = run(evening, 80.0, 10.0, attrs_both)
    assert flipped.gating_day == "tomorrow" and flipped.gating_kwh == 10.0 and not flipped.enough
    assert flipped.usable_end is not None and flipped.usable_end < evening
    assert run(evening, 10.0, 50.0, attrs_both).enough


def test_enough_threshold_zero_and_unknown():
    now = datetime(2026, 6, 21, 12, tzinfo=HEL)
    attrs = {"raw_today": day_items(now, [0.1] * 96)}
    assert not run(now, 80.0, None, attrs, solar_enough_kwh=0).enough
    assert not run(now, None, None, attrs).enough
    assert run(now, 40.0, None, attrs).enough


def test_winter_night_under_threshold_stays_searchable():
    now = datetime(2026, 1, 15, 15, tzinfo=HEL)
    result = run(now, 8.0, 6.0, {"raw_today": day_items(now, [0.02 if i < 20 else 0.12 for i in range(96)])})
    assert result.windows[0].start.hour == 0 and not result.enough
