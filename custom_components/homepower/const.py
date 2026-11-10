"""Constants for HomePower."""
DOMAIN = "homepower"

CONF_IMPORT_LIMIT_W = "import_limit_w"
CONF_COOLDOWN_S = "cooldown_s"

DEFAULT_IMPORT_LIMIT_W = 7000
DEFAULT_COOLDOWN_S = 120
UPDATE_INTERVAL_S = 30

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.config"
