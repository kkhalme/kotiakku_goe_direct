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

POLICY_SOLAR_PRIORITY = "SolarPriority"
POLICY_SOLAR_AND_GRID = "SolarAndGrid"
POLICY_FORCE_ON = "Force on"
POLICY_FORCE_OFF = "Force off"
POLICIES = (POLICY_SOLAR_PRIORITY, POLICY_SOLAR_AND_GRID, POLICY_FORCE_ON, POLICY_FORCE_OFF)
SURPLUS_POLICIES = (POLICY_SOLAR_PRIORITY, POLICY_SOLAR_AND_GRID)

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
    soc_on_pct: float = 92
    soc_hyst_pct: float = 2
    surplus_start_w: float = 2000
    hold_minutes: float = 15
    max_a: float = 32
    max_1phase_amp: float = 32
    group_lot_a: float = 50
    solar_enough_kwh: float = 40
    offsun_hour_kwh: float = 1
    after_charge_complete_keep_a: float = 6
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
    """Current Kotiakku state. ``sample`` is (solar_w, house_w, controller_w or None)
    only when a new held sample should be taken this cycle."""

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
    windows: list[Window]
    blocked: list[tuple[datetime, datetime]]
    reason: str
    tomorrow_ok: bool
    today_kwh: float | None
    tomorrow_kwh: float | None
    usable_end: datetime | None
    gating_day: str
    gating_kwh: float | None
    enough: bool
    epoch_start: datetime | None = None
    epoch_seen: datetime | None = None
    carried: int = 0

    def in_window(self, now: datetime) -> bool:
        return any(w.start <= now < w.end for w in self.windows)

    def next_boundary(self, now: datetime) -> datetime | None:
        edges = [t for w in self.windows for t in (w.start, w.end) if t > now]
        return min(edges, default=None)


@dataclass
class Command:
    on: bool
    psm: int | None = None
    lot: int | None = None
    amp: int | None = None


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


@dataclass
class Decision:
    chargers: dict[str, ChargerDecision]
    switches: dict[str, dict[str, bool]]
    budget_w: int
    next_wakeup: datetime | None
