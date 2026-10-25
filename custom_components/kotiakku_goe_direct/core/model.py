"""Plain data shared by the hub and the pure core. No Home Assistant imports."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

VOLTS = 230
MIN_AMP = 6
MIN_W = MIN_AMP * VOLTS
TAKE_MIN_W = 100
IDLE_COMPLETE_W = 400
KEEP_PROBE_S = 60
SETTLE_S = 90
START_GRACE_S = 120
KOTIAKKU_AVG_S = 300
DRAW_TRACE_S = 600
MIDNIGHT_WAKE_S = 30
METER_GAP_MIN_W = 1000
METER_GAP_DIVISOR = 5
LIMITED_MARGIN_MIN_W = 500
LIMITED_MARGIN_DIVISOR = 10

POLICY_SOLAR_PRIORITY = "SolarPriority"
POLICY_SOLAR_AND_GRID = "SolarAndGrid"
POLICY_DAILY_TRIP = "DailyTrip"
POLICY_FORCE_ON = "Force on"
POLICY_FORCE_OFF = "Force off"
POLICIES = (POLICY_SOLAR_PRIORITY, POLICY_SOLAR_AND_GRID, POLICY_DAILY_TRIP, POLICY_FORCE_ON, POLICY_FORCE_OFF)
SURPLUS_POLICIES = (POLICY_SOLAR_PRIORITY, POLICY_SOLAR_AND_GRID, POLICY_DAILY_TRIP)

PHASE_1 = "1-phase"
PHASE_3 = "3-phase"
PHASE_OPTIONS = (PHASE_1, PHASE_3)

ROLE_FULL = "full"
ROLE_KEEP = "keep"
ROLE_SURPLUS = "surplus"
ROLE_OFF = "off"

CAR_IDLE, CAR_CHARGING, CAR_WAITCAR, CAR_COMPLETE, CAR_ERROR = 1, 2, 3, 4, 5
PLUGGED_CARS = (CAR_CHARGING, CAR_WAITCAR, CAR_COMPLETE, CAR_ERROR)


def psm_of(phase: str) -> int:
    return 1 if phase == PHASE_1 else 2


@dataclass
class Settings:
    """Knob values. Field names are the entity keys that edit them."""

    window_min_h: float = 2.0
    window_max_h: float = 5.0
    electricity_price_ceiling: float = 0.2
    window_flex_pct: float = 20.0
    window_flex_eur: float = 0.02
    daily_trip_charge_after_h: float = 17.0
    daily_trip_charge_before_h: float = 7.0
    daily_trip_price_flex_pct: float = 5.0
    daily_trip_price_flex_eur: float = 0.03
    soc_on_pct: float = 92
    soc_hyst_pct: float = 2
    surplus_start_w: float = 2000
    surplus_charging_price_ceiling: float = 0.25
    hold_minutes: float = 15
    max_a: int = 32
    max_1phase_amp: int = 32
    group_lot_a: int = 50
    solar_enough_kwh: float = 40
    offsun_kw: float = 1
    after_charge_complete_keep_a: int = 6
    after_charge_complete_keep_phase: str = PHASE_3
    surplus_preferred_start_phase: str = PHASE_1
    policy: dict[str, str] = field(default_factory=dict)
    priority: dict[str, float] = field(default_factory=dict)
    until_unplug: dict[str, bool] = field(default_factory=dict)
    keep: dict[str, bool] = field(default_factory=dict)
    keep_enable: dict[str, bool] = field(default_factory=dict)

    @property
    def hold_s(self) -> float:
        return float(self.hold_minutes) * 60

    @property
    def one_phase_cap_a(self) -> int:
        return min(self.max_1phase_amp, self.max_a)

    def add_charger(self, serial: str, slot: int) -> None:
        """Per-charger defaults; values already set are kept."""
        self.policy.setdefault(serial, POLICY_FORCE_OFF)
        self.priority.setdefault(serial, slot + 1)
        self.until_unplug.setdefault(serial, False)
        self.keep.setdefault(serial, False)
        self.keep_enable.setdefault(serial, True)

    def apply(self, switches: dict[str, dict[str, bool]]) -> None:
        """Write switch changes from a ``Decision`` back into the per-charger fields."""
        for serial, changes in switches.items():
            for name, value in changes.items():
                getattr(self, name)[serial] = value


@dataclass
class Charger:
    serial: str
    slot: int
    car: int | None = None
    nrg_w: int | None = None

    @property
    def plugged(self) -> bool | None:
        return None if self.car is None else self.car in PLUGGED_CARS

    @property
    def taking(self) -> bool:
        return (self.nrg_w or 0) >= TAKE_MIN_W

    @property
    def idle_complete(self) -> bool:
        return self.car == CAR_COMPLETE and (self.nrg_w or 0) < IDLE_COMPLETE_W


@dataclass
class HouseReading:
    """Current Kotiakku state. ``sample`` is (solar_w, house_w, ev_w or None)
    only when a publication should replace the held surplus this cycle.
    ``ev_w`` is the draw averaged over the same window as that house value."""

    soc: float | None
    usable: bool
    sample: tuple[int, int, int | None] | None = None


@dataclass
class Sample:
    at: datetime
    solar_w: int
    house_w: int
    ev_w: int
    leftover_w: int
    keep_take_w: int

    @property
    def budget_w(self) -> int:
        return self.leftover_w - self.keep_take_w


@dataclass
class Window:
    start: datetime
    end: datetime
    avg: float


@dataclass
class Plan:
    windows: list[Window] = field(default_factory=list)
    blocked: list[tuple[datetime, datetime]] = field(default_factory=list)
    reason: str = "planned"
    tomorrow_ok: bool = False
    today_kwh: float | None = None
    tomorrow_kwh: float | None = None
    usable_end: datetime | None = None
    gating_day: str = "today"
    gating_kwh: float | None = None
    enough: bool = False
    epoch_start: datetime | None = None
    epoch_seen: datetime | None = None
    carried: int = 0
    surplus_priced_out: list[Window] = field(default_factory=list)
    trip_windows: list[Window] = field(default_factory=list)

    def in_window(self, now: datetime) -> bool:
        return any(w.start <= now < w.end for w in self.windows)

    def priced_out_at(self, now: datetime) -> Window | None:
        return next((w for w in self.surplus_priced_out if w.start <= now < w.end), None)

    def next_boundary(self, now: datetime) -> datetime | None:
        spans = self.windows + self.trip_windows + self.surplus_priced_out
        edges = [t for w in spans for t in (w.start, w.end) if t > now]
        return min(edges, default=None)


@dataclass
class Command:
    on: bool
    psm: int | None = None
    lot: int | None = None
    amp: int | None = None

    @property
    def watts(self) -> int:
        return self.amp * VOLTS * (3 if self.psm == 2 else 1) if self.on else 0


OFF = Command(False)


@dataclass
class ChargerMemory:
    surplus_on: bool = False
    last_on: bool = False
    psm: int | None = None
    low_since: datetime | None = None
    phase_since: datetime | None = None
    idle_since: datetime | None = None
    cut: bool = False
    last_plugged: bool | None = None
    keep_was_on: bool = False
    offer_w: int | None = None
    offer_since: datetime | None = None
    on_since: datetime | None = None
    start_armed_at: datetime | None = None


@dataclass
class Memory:
    chargers: dict[str, ChargerMemory] = field(default_factory=dict)
    sample: Sample | None = None

    def of(self, serial: str) -> ChargerMemory:
        return self.chargers.setdefault(serial, ChargerMemory())


@dataclass
class ChargerDecision:
    role: str
    command: Command
    share_w: int | None = None
    low_hold_until: datetime | None = None
    phase_hold_until: datetime | None = None
    reserve_w: int | None = None
    limited: bool = False
    start_pending: bool = False
    priced_out: bool = False
    windows: list[Window] = field(default_factory=list)


@dataclass
class Decision:
    chargers: dict[str, ChargerDecision]
    switches: dict[str, dict[str, bool]]
    budget_w: int
    next_wakeup: datetime | None
