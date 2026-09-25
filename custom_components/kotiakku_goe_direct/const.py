DOMAIN = "kotiakku_goe_direct"
NAME = "Kotiakku go-e Direct"

CONF_PRICE_ENTITY = "price_entity"
CONF_SOC_ENTITY = "soc_entity"
CONF_SOLAR_ENTITY = "solar_entity"
CONF_HOUSE_ENTITY = "house_entity"
CONF_CONTROLLER_ENTITY = "controller_entity"
CONF_SOLAR_TODAY_ENTITY = "solar_today_entity"
CONF_SOLAR_TOMORROW_ENTITY = "solar_tomorrow_entity"
CHARGER_SLOTS = 4
CONF_CHARGER_SERIALS = tuple(f"charger_{slot}_serial" for slot in range(1, CHARGER_SLOTS + 1))

REQUIRED_ENTITIES = (
    CONF_PRICE_ENTITY,
    CONF_SOC_ENTITY,
    CONF_SOLAR_ENTITY,
    CONF_HOUSE_ENTITY,
    CONF_CONTROLLER_ENTITY,
)
OPTIONAL_ENTITIES = (CONF_SOLAR_TODAY_ENTITY, CONF_SOLAR_TOMORROW_ENTITY)

SERIAL_PATTERN = r"^[A-Za-z0-9]{4,12}$"
STORE_KEY = f"{DOMAIN}.state"
LEGACY_STORE_KEY = DOMAIN
