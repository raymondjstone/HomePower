"""Constants for HomePower."""
DOMAIN = "homepower"
SIGNAL_UPDATE = "homepower_update"   # sent after every control cycle (the panel's live feed listens)

CONF_IMPORT_LIMIT_W = "import_limit_w"
CONF_COOLDOWN_S = "cooldown_s"

DEFAULT_IMPORT_LIMIT_W = 7000
DEFAULT_COOLDOWN_S = 120
UPDATE_INTERVAL_S = 10

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.config"

CONF_CHARGE_CAP_W = "total_charge_limit_w"        # all schedules combined
CONF_DISCHARGE_CAP_W = "total_discharge_limit_w"  # all schedules combined
CONF_GRID_ENTITY = "grid_import_entity"           # sensor, W
CONF_GRID_INVERT = "grid_invert"                  # True if sensor is + when exporting
CONF_ZERO_IMPORT_DEVICE = "zero_import_device"
