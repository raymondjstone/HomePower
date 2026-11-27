"""Constants for HomePower."""
DOMAIN = "homepower"
SIGNAL_UPDATE = "homepower_update"   # sent after every control cycle (the panel's live feed listens)

UPDATE_INTERVAL_S = 10

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.config"
