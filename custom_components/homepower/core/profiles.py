"""Battery catalogue: which models HomePower can drive, and how it connects to them.

HomePower talks to the batteries directly; it does not use (or need) any other Home Assistant
integration. The open-source Anker and EcoFlow integrations were used only as a reference for the
protocols (register map / API) that the devices speak.

  anker_modbus    Anker SOLIX over local Modbus TCP (Solarbank Max/Max AC, XE/XE AC, E5000 Pro, X1)
  ecoflow_stream  EcoFlow STREAM family over EcoFlow's own developer API (Access Key + Secret Key)
  entities        Advanced: any other battery, via Home Assistant entities you choose

HA-free and JSON-serialisable (sent to the panel).
"""
from __future__ import annotations

from .models import Mode

A, C, D, H = Mode.AUTO.value, Mode.CHARGE.value, Mode.DISCHARGE.value, Mode.HOLD.value
ALL_MODES = [A, C, D, H]


def _f(k, label, domains, req=False, rx=""):
    return {"k": k, "label": label, "domains": list(domains), "req": req, "keys": [], "rx": rx, "hint": ""}


def _m(k, label, entity, rx="", optional=False):
    return {"k": k, "label": label, "entity": entity, "rx": rx, "optional": optional}


# Connection fields per driver (what the setup form asks for).
DRIVERS: dict[str, dict] = {
    "anker_modbus": {
        "modes": ALL_MODES,
        "connection": [
            {"k": "host", "label": "IP address of the battery", "type": "text", "req": True},
            {"k": "port", "label": "Modbus TCP port", "type": "number", "req": False, "default": 502},
            {"k": "unit_id", "label": "Modbus unit ID", "type": "number", "req": False, "default": 1},
        ],
        "discover": "network",   # can scan the LAN
        "setup_help": (
            "In the Anker app open the device > Settings > Three-Party Control Settings and switch on "
            "Modbus TCP. HomePower then finds the battery on your network and reads it directly. "
            "The battery accepts one controlling Modbus client, so switch off any other integration or tool "
            "that talks to it over Modbus (for example Anker's own Home Assistant integration)."
        ),
    },
    "ecoflow_stream": {
        "modes": [A, H],
        "connection": [
            {"k": "region", "label": "Region", "type": "select", "req": True, "default": "eu",
             "options": [{"value": "eu", "label": "Europe (api-e.ecoflow.com)"}, {"value": "america", "label": "Americas (api-a.ecoflow.com)"}, {"value": "global", "label": "Other / global (api.ecoflow.com)"}]},
            {"k": "access_key", "label": "Access Key", "type": "text", "req": True},
            {"k": "secret_key", "label": "Secret Key", "type": "password", "req": True},
            {"k": "sn", "label": "Device", "type": "device", "req": True},
        ],
        "discover": "account",   # lists the devices on the account
        "setup_help": (
            "Create an Access Key and Secret Key at developer.ecoflow.com (sign in with your EcoFlow account), "
            "then HomePower lists the batteries on your account. No other integration is needed."
        ),
    },
    "modbus_generic": {
        "modes": ALL_MODES,   # narrowed to Auto when the map is read-only
        "connection": [
            {"k": "host", "label": "IP address of the device", "type": "text", "req": True},
            {"k": "port", "label": "Modbus TCP port", "type": "number", "req": False, "default": 502},
            {"k": "unit_id", "label": "Modbus unit ID", "type": "number", "req": False, "default": 1},
            {"k": "map", "label": "Register map (JSON)", "type": "json", "req": False},
        ],
        "discover": None,
        "setup_help": (
            "Describe where the battery keeps its charge level and power as a small JSON register map "
            "(see the README). Without a setpoint and mode entry it is read-only: monitored, never controlled."
        ),
    },
    "entities": {
        "modes": ALL_MODES,
        "connection": [],
        "entities": [
            _f("soc", "State of charge (%)", ["sensor"], True, r"soc|state.?of.?charge|battery.?level"),
            _f("power", "Battery power (W, + charging)", ["sensor"], True, r"battery.*power|power"),
            _f("mode", "Operating mode (select)", ["select"], True, r"mode|work|strateg"),
            _f("set_power", "Charge/discharge power setpoint", ["number"], True, r"power|watt"),
            _f("target_soc", "Target SOC (optional)", ["number"], False, r"target|upper|max.*soc"),
            _f("reserve_soc", "Reserve SOC (optional)", ["number"], False, r"reserve|backup|lower|min.*soc"),
            _f("protected", "Protection flag (optional)", ["binary_sensor"], False, r"protect|calibrat"),
        ],
        "mode_map": [
            _m("auto", "Auto / self-consumption", "mode", r"auto|self|smart|default"),
            _m("charge", "Charge", "mode", r"^(?!.*dis).*charg"),
            _m("discharge", "Discharge", "mode", r"discharg"),
            _m("hold", "Hold / idle", "mode", r"hold|idle|standby|stop|pause"),
        ],
        "setup_help": "For batteries HomePower has no driver for: point it at the entities that read and control the battery.",
    },
}

VENDORS: dict[str, dict] = {
    "Anker": {"regions": (
        "Works in every region: HomePower connects to the battery on your own network (Modbus TCP, no cloud). "
        "Grid feed-in is off by default in Italy, Spain, Thailand, Vietnam and the Philippines, which affects "
        "export/zero-import behaviour. The Anker app's Backup SOC, SOC calibration, Fast Charging and Storm Guard "
        "override commands sent by HomePower.")},
    "EcoFlow": {"regions": (
        "Pick the region of your EcoFlow account: Europe uses api-e.ecoflow.com, the Americas use api-a.ecoflow.com, anywhere else api.ecoflow.com. "
        "STREAM batteries cannot be force-charged or force-discharged through EcoFlow's API, so HomePower offers "
        "Auto and Hold only for them.")},
    "Other": {"regions": "Any battery that exposes a mode select and a power number in Home Assistant."},
}

_ANKER = ("Connects directly over Modbus TCP. Control uses the device's third-party control mode and a signed power "
          "setpoint; HomePower hands the battery back to its previous mode when no schedule applies.")
_EF = ("Uses EcoFlow's developer API. Auto = self-powered; Hold = backup reserve raised to the current charge level so the battery will "
       "not discharge (solar can still charge it). No force charge/discharge is available.")


def _p(pid, vendor, label, driver, notes="", **extra):
    return {"id": pid, "vendor": vendor, "label": label, "driver": driver, "notes": notes, **extra}


PROFILES: list[dict] = [
    _p("anker_solarbank_max_ac", "Anker", "Anker SOLIX Solarbank Max AC", "anker_modbus", _ANKER, restore_mode=0, watchdog=False),
    _p("anker_solarbank_max", "Anker", "Anker SOLIX Solarbank Max", "anker_modbus", _ANKER, restore_mode=0, watchdog=False),
    _p("anker_xe_ac", "Anker", "Anker SOLIX XE AC", "anker_modbus", _ANKER, restore_mode=0, watchdog=False),
    _p("anker_xe", "Anker", "Anker SOLIX XE", "anker_modbus", _ANKER, restore_mode=0, watchdog=False),
    _p("anker_e5000_pro", "Anker", "Anker SOLIX Solarbank 4 E5000 Pro", "anker_modbus", _ANKER, restore_mode=0, watchdog=False),
    _p("anker_x1", "Anker", "Anker SOLIX X1", "anker_modbus",
       _ANKER + " X1 also gets a comms watchdog so it reverts to the app's mode if HomePower stops talking to it.",
       restore_mode=20, watchdog=True, house_meter=True),   # installed at the grid connection: its meter sees the whole house
    _p("ecoflow_stream_ac", "EcoFlow", "EcoFlow STREAM AC", "ecoflow_stream", _EF),
    _p("ecoflow_stream_pro", "EcoFlow", "EcoFlow STREAM Pro", "ecoflow_stream", _EF),
    _p("ecoflow_stream_ultra", "EcoFlow", "EcoFlow STREAM Ultra", "ecoflow_stream", _EF),
    _p("ecoflow_stream_ultra_x", "EcoFlow", "EcoFlow STREAM Ultra X", "ecoflow_stream", _EF),
    _p("ecoflow_stream_max", "EcoFlow", "EcoFlow STREAM Max", "ecoflow_stream", _EF),
    _p("ecoflow_stream_ac_5000", "EcoFlow", "EcoFlow STREAM AC 5000 / ES5000 (model to confirm)", "ecoflow_stream",
       _EF + " Whether EcoFlow's developer API covers this exact model is unconfirmed; the setup test will tell you."),
    _p("victron_gx", "Other", "Victron GX system (read-only)", "modbus_generic",
       "Reads the GX system registers over Modbus TCP (enable it in the GX device: Settings > Services > Modbus/TCP). "
       "Read-only: shown and recorded, never controlled. Unverified on hardware.",
       default_unit=100, preset_map={
           "soc": {"addr": 843, "type": "u16", "fc": 3},
           "power": {"addr": 842, "type": "i16", "fc": 3, "positive_is": "charging"},
           "grid": {"addrs": [820, 821, 822], "type": "i16", "fc": 3, "positive_is": "import"}}),
    _p("modbus_map", "Other", "Other battery via Modbus register map (advanced)", "modbus_generic"),
    _p("custom", "Other", "Other battery (use Home Assistant entities)", "entities"),
]
PROFILE_BY_ID = {p["id"]: p for p in PROFILES}


def catalogue() -> dict:
    return {"profiles": PROFILES, "drivers": DRIVERS, "vendors": VENDORS}
