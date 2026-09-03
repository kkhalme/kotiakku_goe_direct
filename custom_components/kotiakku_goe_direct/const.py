DOMAIN = "kotiakku_goe_direct"

RANKS = ("cheapest", "longest", "earliest", "offsun")
RANK_LABELS = {
    "cheapest": "Cheapest",
    "longest": "Longest",
    "earliest": "Earliest",
    "offsun": "Off-sun",
}
POLICY_CHEAPEST = "Cheapest"
POLICY_SUPERCHEAP = "Supercheap"
POLICY_LONGEST = "Longest"
POLICY_EARLIEST = "Earliest"
POLICY_FORCE_ON = "Force on"
POLICY_FORCE_OFF = "Force off"
POLICY_UNTIL_UNPLUG = "Force on until unplug"
POLICIES = (
    POLICY_CHEAPEST,
    POLICY_SUPERCHEAP,
    POLICY_LONGEST,
    POLICY_EARLIEST,
    POLICY_FORCE_ON,
    POLICY_FORCE_OFF,
    POLICY_UNTIL_UNPLUG,
)
RANKED_POLICIES = (POLICY_CHEAPEST, POLICY_SUPERCHEAP, POLICY_LONGEST, POLICY_EARLIEST)
FORCE_POLICIES = (POLICY_FORCE_ON, POLICY_UNTIL_UNPLUG)
POLICY_RANK = {
    POLICY_CHEAPEST: "cheapest",
    POLICY_SUPERCHEAP: "offsun",
    POLICY_LONGEST: "longest",
    POLICY_EARLIEST: "earliest",
}


def rank_label(rank):
    return RANK_LABELS.get(rank, str(rank).capitalize())

CONF_PRICE_ENTITY = "price_entity"
CONF_SOC_ENTITY = "soc_entity"
CONF_SOLAR_ENTITY = "solar_entity"
CONF_HOUSE_ENTITY = "house_entity"
CONF_CONTROLLER_ENTITY = "controller_entity"
CONF_SOLAR_REMAINING_ENTITY = "solar_remaining_entity"
CONF_SOLAR_TOMORROW_ENTITY = "solar_tomorrow_entity"
CONF_CHARGERS = "chargers"
CONF_PRIORITY = "priority"
PRIORITY_MIN = 1
PRIORITY_MAX = 99
CONF_SOC_ON = "soc_on"
CONF_SOC_HYST = "soc_hyst"
CONF_START_MIN_W = "start_min_w"
CONF_SPLIT_MIN_W = "split_min_w"
CONF_SPLIT_FLOOR_W = "split_floor_w"
CONF_SETTLE_S = "settle_s"
CONF_HOLD_MIN_W = "hold_min_w"
CONF_HOLD_MIN = "hold_min"
CONF_VOLTS = "volts"
CONF_MIN_AMP = "min_amp"
CONF_MAX_AMP = "max_amp"
CONF_PHASE3_MIN_W = "phase3_min_w"
CONF_ECO_PSM = "eco_psm"
CONF_ECO_LOT = "eco_lot"
CONF_KOTIAKKU_IN_KW = "kotiakku_in_kw"
CONF_CONTROLLER_IN_KW = "controller_in_kw"
CONF_SOLAR_ENOUGH_KWH = "solar_enough_kwh"
CONF_OFFSUN_HOUR_KWH = "offsun_hour_kwh"

DEFAULT_SOC_ON = 92
DEFAULT_SOC_HYST = 2
DEFAULT_START_MIN_W = 2000
DEFAULT_SPLIT_MIN_W = 3000
DEFAULT_SPLIT_FLOOR_W = 500
DEFAULT_SETTLE_S = 5
DEFAULT_HOLD_MIN_W = 1000
DEFAULT_HOLD_MIN = 15
DEFAULT_VOLTS = 230
DEFAULT_MIN_AMP = 6
DEFAULT_MAX_AMP = 32
DEFAULT_PHASE3_MIN_W = 4140
DEFAULT_ECO_PSM = 0
DEFAULT_ECO_LOT = 50
DEFAULT_MIN_HOURS = 2.0
DEFAULT_MAX_HOURS = 5.0
DEFAULT_CEILING = 0.1
DEFAULT_KOTIAKKU_IN_KW = True
DEFAULT_CONTROLLER_IN_KW = False
DEFAULT_SOLAR_ENOUGH_KWH = 40
DEFAULT_OFFSUN_HOUR_KWH = 1

PSM_AUTO = "Auto"
PSM_FORCE_1 = "Force 1-phase"
PSM_FORCE_3 = "Force 3-phase"
PSM_OPTIONS = (PSM_AUTO, PSM_FORCE_1, PSM_FORCE_3)
PSM_TO_INT = {PSM_AUTO: 0, PSM_FORCE_1: 1, PSM_FORCE_3: 2}
INT_TO_PSM = {0: PSM_AUTO, 1: PSM_FORCE_1, 2: PSM_FORCE_3}

HUB_ID = "kotiakku_goe_direct"
EID_MIN = "number.kotiakku_goe_direct_window_min_h"
EID_MAX = "number.kotiakku_goe_direct_window_max_h"
EID_CEILING = "number.kotiakku_goe_direct_electricity_price_ceiling"
EID_PRICE = "text.kotiakku_goe_direct_electricity_price_sensor"
EID_SOC_ON = "number.kotiakku_goe_direct_soc_on_pct"
EID_SOC_HYST = "number.kotiakku_goe_direct_soc_hyst_pct"
EID_START_MIN_W = "number.kotiakku_goe_direct_surplus_start_w"
EID_SPLIT_MIN_W = "number.kotiakku_goe_direct_next_surplus_min_w"
EID_SPLIT_FLOOR_W = "number.kotiakku_goe_direct_remainder_floor_w"
EID_SETTLE_S = "number.kotiakku_goe_direct_settle_s"
EID_HOLD_MIN_W = "number.kotiakku_goe_direct_low_hold_w"
EID_HOLD_MIN = "number.kotiakku_goe_direct_hold_minutes"
EID_VOLTS = "number.kotiakku_goe_direct_voltage_v"
EID_MIN_AMP = "number.kotiakku_goe_direct_min_a"
EID_MAX_AMP = "number.kotiakku_goe_direct_max_a"
EID_PHASE3_MIN_W = "number.kotiakku_goe_direct_phase3_min_w"
EID_ECO_LOT = "number.kotiakku_goe_direct_eco_lot_a"
EID_ECO_PSM = "select.kotiakku_goe_direct_eco_phase"
EID_SOLAR_ENOUGH_KWH = "number.kotiakku_goe_direct_solar_enough_kwh"
EID_OFFSUN_HOUR_KWH = "number.kotiakku_goe_direct_offsun_hour_kwh"

WINDOW_EIDS = (EID_MIN, EID_MAX, EID_CEILING, EID_PRICE)
SURPLUS_EIDS = (
    EID_SOC_ON,
    EID_SOC_HYST,
    EID_START_MIN_W,
    EID_SPLIT_MIN_W,
    EID_SPLIT_FLOOR_W,
    EID_SETTLE_S,
    EID_HOLD_MIN_W,
    EID_HOLD_MIN,
    EID_VOLTS,
    EID_MIN_AMP,
    EID_MAX_AMP,
    EID_PHASE3_MIN_W,
    EID_ECO_LOT,
    EID_ECO_PSM,
    EID_SOLAR_ENOUGH_KWH,
    EID_OFFSUN_HOUR_KWH,
)

# unit: percent | W | s | min | V | A | kWh
SURPLUS_NUMBER_SPECS = (
    {
        "entity_id": EID_SOC_ON,
        "unique_id": "kotiakku_goe_direct_soc_on_pct",
        "name": "Surplus SoC on",
        "conf": CONF_SOC_ON,
        "default": DEFAULT_SOC_ON,
        "min": 0,
        "max": 100,
        "step": 1,
        "unit": "percent",
        "icon": "mdi:battery-charging-80",
    },
    {
        "entity_id": EID_SOC_HYST,
        "unique_id": "kotiakku_goe_direct_soc_hyst_pct",
        "name": "Surplus SoC hysteresis",
        "conf": CONF_SOC_HYST,
        "default": DEFAULT_SOC_HYST,
        "min": 0,
        "max": 20,
        "step": 1,
        "unit": "percent",
        "icon": "mdi:battery-minus",
    },
    {
        "entity_id": EID_START_MIN_W,
        "unique_id": "kotiakku_goe_direct_surplus_start_w",
        "name": "Surplus start leftover",
        "conf": CONF_START_MIN_W,
        "default": DEFAULT_START_MIN_W,
        "min": 0,
        "max": 50000,
        "step": 50,
        "unit": "W",
        "icon": "mdi:lightning-bolt",
    },
    {
        "entity_id": EID_SPLIT_MIN_W,
        "unique_id": "kotiakku_goe_direct_next_surplus_min_w",
        "name": "Next surplus min",
        "conf": CONF_SPLIT_MIN_W,
        "default": DEFAULT_SPLIT_MIN_W,
        "min": 0,
        "max": 50000,
        "step": 50,
        "unit": "W",
        "icon": "mdi:call-split",
    },
    {
        "entity_id": EID_SPLIT_FLOOR_W,
        "unique_id": "kotiakku_goe_direct_remainder_floor_w",
        "name": "Surplus remainder floor",
        "conf": CONF_SPLIT_FLOOR_W,
        "default": DEFAULT_SPLIT_FLOOR_W,
        "min": 0,
        "max": 50000,
        "step": 50,
        "unit": "W",
        "icon": "mdi:gauge-empty",
    },
    {
        "entity_id": EID_HOLD_MIN_W,
        "unique_id": "kotiakku_goe_direct_low_hold_w",
        "name": "Surplus low hold leftover",
        "conf": CONF_HOLD_MIN_W,
        "default": DEFAULT_HOLD_MIN_W,
        "min": 0,
        "max": 50000,
        "step": 50,
        "unit": "W",
        "icon": "mdi:gauge-low",
    },
    {
        "entity_id": EID_SETTLE_S,
        "unique_id": "kotiakku_goe_direct_settle_s",
        "name": "Surplus settle",
        "conf": CONF_SETTLE_S,
        "default": DEFAULT_SETTLE_S,
        "min": 0,
        "max": 120,
        "step": 1,
        "unit": "s",
        "icon": "mdi:timer-sand",
    },
    {
        "entity_id": EID_HOLD_MIN,
        "unique_id": "kotiakku_goe_direct_hold_minutes",
        "name": "Hold",
        "conf": CONF_HOLD_MIN,
        "default": DEFAULT_HOLD_MIN,
        "min": 1,
        "max": 120,
        "step": 1,
        "unit": "min",
        "icon": "mdi:timer-outline",
    },
    {
        "entity_id": EID_VOLTS,
        "unique_id": "kotiakku_goe_direct_voltage_v",
        "name": "Voltage",
        "conf": CONF_VOLTS,
        "default": DEFAULT_VOLTS,
        "min": 100,
        "max": 400,
        "step": 1,
        "unit": "V",
        "icon": "mdi:sine-wave",
    },
    {
        "entity_id": EID_MIN_AMP,
        "unique_id": "kotiakku_goe_direct_min_a",
        "name": "Minimum amp",
        "conf": CONF_MIN_AMP,
        "default": DEFAULT_MIN_AMP,
        "min": 6,
        "max": 32,
        "step": 1,
        "unit": "A",
        "icon": "mdi:current-ac",
    },
    {
        "entity_id": EID_MAX_AMP,
        "unique_id": "kotiakku_goe_direct_max_a",
        "name": "Per-charger amp cap",
        "conf": CONF_MAX_AMP,
        "default": DEFAULT_MAX_AMP,
        "min": 6,
        "max": 32,
        "step": 1,
        "unit": "A",
        "icon": "mdi:current-ac",
    },
    {
        "entity_id": EID_PHASE3_MIN_W,
        "unique_id": "kotiakku_goe_direct_phase3_min_w",
        "name": "3-phase leftover",
        "conf": CONF_PHASE3_MIN_W,
        "default": DEFAULT_PHASE3_MIN_W,
        "min": 0,
        "max": 50000,
        "step": 10,
        "unit": "W",
        "icon": "mdi:numeric-3-circle-outline",
    },
    {
        "entity_id": EID_ECO_LOT,
        "unique_id": "kotiakku_goe_direct_eco_lot_a",
        "name": "ECO lot",
        "conf": CONF_ECO_LOT,
        "default": DEFAULT_ECO_LOT,
        "min": 6,
        "max": 64,
        "step": 1,
        "unit": "A",
        "icon": "mdi:tune",
    },
    {
        "entity_id": EID_SOLAR_ENOUGH_KWH,
        "unique_id": "kotiakku_goe_direct_solar_enough_kwh",
        "name": "Enough solar",
        "conf": CONF_SOLAR_ENOUGH_KWH,
        "default": DEFAULT_SOLAR_ENOUGH_KWH,
        "min": 0,
        "max": 500,
        "step": 1,
        "unit": "kWh",
        "icon": "mdi:solar-power",
    },
    {
        "entity_id": EID_OFFSUN_HOUR_KWH,
        "unique_id": "kotiakku_goe_direct_offsun_hour_kwh",
        "name": "Off-sun hour",
        "conf": CONF_OFFSUN_HOUR_KWH,
        "default": DEFAULT_OFFSUN_HOUR_KWH,
        "min": 0,
        "max": 20,
        "step": 0.1,
        "unit": "kWh",
        "icon": "mdi:weather-sunny-off",
    },
)

STORAGE_VERSION = 1
STORAGE_KEY = "kotiakku_goe_direct"


def psm_option(value):
    try:
        return INT_TO_PSM[int(value)]
    except (KeyError, TypeError, ValueError):
        return PSM_AUTO


def psm_int(option):
    if option in PSM_TO_INT:
        return PSM_TO_INT[option]
    return DEFAULT_ECO_PSM


def priority_entity_id(serial) -> str:
    return f"number.kotiakku_goe_direct_priority_{serial}"


def window_sensor_unique_id(rank) -> str:
    return f"kotiakku_goe_direct_{rank}_window"


def window_sensor_entity_id(rank) -> str:
    return f"sensor.{window_sensor_unique_id(rank)}"


def window_sensor_legacy_unique_id(rank) -> str:
    return f"kotiakku_goe_direct_{rank}_window_start"


def default_charger_priority(slot) -> int:
    """go-e scale: 1 is highest. Slot 0 (charger 1) defaults to 1."""
    try:
        value = int(slot) + 1
    except (TypeError, ValueError):
        value = PRIORITY_MIN
    if value < PRIORITY_MIN:
        return PRIORITY_MIN
    if value > PRIORITY_MAX:
        return PRIORITY_MAX
    return value
