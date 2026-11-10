"""Battery catalogue: which upstream HA integration drives which model, and how.

Sources (read from the projects' own code/README, not guessed):
  anker_solix_official  github.com/anker-charging/ha-anker-solix-official   (local Modbus TCP)
  anker_x1              github.com/afewyards/anker-x1-ha                    (local Modbus TCP)
  anker_solix_x1        github.com/cbource/anker-solix-x1-modbus            (local Modbus TCP)
  ecoflow_cloud         github.com/tolwi/hassio-ecoflow-cloud               (EcoFlow cloud API)
  ecoflow_streamx       github.com/htims1989/ecoflow-public-api             (EcoFlow public API)

HomePower only talks to Home Assistant entities, so it works in every region the upstream
integration works in; the region notes below are about the *upstream* setup, not HomePower.
HA-free and JSON-serialisable (sent to the panel).
"""
from __future__ import annotations

from .models import Mode

A, C, D, H = Mode.AUTO.value, Mode.CHARGE.value, Mode.DISCHARGE.value, Mode.HOLD.value
ALL_MODES = [A, C, D, H]


def _f(k, label, domains, req=False, keys=(), rx="", hint=""):
    """Entity field. `keys` = translation keys (exact match), `rx` = regex fallback on id+name."""
    return {"k": k, "label": label, "domains": list(domains), "req": req, "keys": list(keys), "rx": rx, "hint": hint}


def _m(k, label, entity, rx="", optional=False):
    """Mode-map field: pick one option of `entity` (a select) for the role `k`."""
    return {"k": k, "label": label, "entity": entity, "rx": rx, "optional": optional}


# How HomePower drives each family of integrations.
STRATEGIES: dict[str, dict] = {
    # Official Anker: operating_mode select (+ third_party_control), absolute power setpoint
    # number and a separate charge/discharge direction select, split charge/discharge sensors.
    "direction_setpoint": {
        "modes": ALL_MODES,
        "entities": [
            _f("soc", "Battery SOC (%)", ["sensor"], True, ["battery_soc"], r"soc|state.?of.?charge|battery.?level"),
            _f("charge_power", "Battery charging power (W)", ["sensor"], True, ["battery_charging_power"], r"charging.?power"),
            _f("discharge_power", "Battery discharging power (W)", ["sensor"], True, ["battery_discharging_power"], r"discharging.?power"),
            _f("work_mode", "Operating mode (select)", ["select"], True, ["operating_mode"], r"operating.?mode|work.?mode"),
            _f("direction", "Power direction (select)", ["select"], True, ["battery_power_direction"], r"direction"),
            _f("setpoint", "Battery power setpoint (W)", ["number"], True, ["battery_power_setpoint"], r"setpoint|power"),
            _f("target_soc", "Charging limit SOC (optional)", ["number"], False, ["charging_limit_soc"], r"charging.?limit"),
            _f("reserve_soc", "Discharge limit SOC (optional)", ["number"], False, ["discharge_limit_soc"], r"discharge.?limit"),
        ],
        "mode_map": [
            _m("auto", "Hand back to device (self-consumption)", "work_mode", r"self.?consum"),
            _m("control", "HomePower control (third-party)", "work_mode", r"third|3rd|vpp"),
            _m("charge", "Direction: charge", "direction", r"^charg"),
            _m("discharge", "Direction: discharge", "direction", r"^discharg"),
        ],
    },
    # Anker X1: one signed setpoint number (- charge / + discharge); control is engaged
    # either by a "Modbus Control" switch (afewyards) or by selecting the 3rd-party work mode.
    "signed_setpoint": {
        "modes": ALL_MODES,
        "entities": [
            _f("soc", "Battery SOC (%)", ["sensor"], True, [], r"battery.?soc|soc$|state.?of.?charge"),
            _f("power", "Battery power (W)", ["sensor"], True, [], r"battery.?power", "Raw sign is + discharging, so 'flip the sign' is on by default"),
            _f("setpoint", "Battery setpoint (W)", ["number"], True, [], r"setpoint|charge.?discharge.?control"),
            _f("engage", "Modbus Control switch (if your integration has one)", ["switch"], False, [], r"modbus.?control"),
            _f("work_mode", "Work mode (select)", ["select"], False, [], r"work.?mode"),
        ],
        "mode_map": [
            _m("auto", "Mode to restore when handing back", "work_mode", r"self.?consum|app", True),
            _m("control", "Mode that accepts HomePower control", "work_mode", r"3rd|third|vpp", True),
        ],
        "needs_one_of": ["engage", "work_mode"],
        "defaults": {"invert_power": True, "discharge_positive": True},
    },
    # EcoFlow STREAM (cloud/public API integrations): no power setpoint exists. Only the
    # operating mode and the backup reserve can be written, so Charge/Discharge are unavailable;
    # Hold = raise the backup reserve so the battery will not discharge (solar still charges).
    "ecoflow_stream": {
        "modes": [A, H],
        "entities": [
            _f("soc", "Battery level (%)", ["sensor"], True, [], r"^(?!.*(precise|health|max|min)).*(battery.?level|\bsoc\b)|battery.?level"),
            _f("power", "Battery power (W, discharge negative)", ["sensor"], True, [], r"battery.?power|pow.?get.?bp"),
            _f("backup_reserve", "Backup reserve level (number)", ["number"], True, [], r"backup.?re"),
            _f("self_powered", "Self-powered mode switch (optional)", ["switch"], False, [], r"self.?powered"),
            _f("operating_mode", "Operating mode select (optional)", ["select"], False, [], r"operating.?mode"),
        ],
        "mode_map": [_m("auto", "Option for self-powered", "operating_mode", r"self", True)],
        "defaults": {"invert_power": False, "hold_reserve": 95},
    },
    # Any other battery that exposes a mode select + a power number.
    "generic": {
        "modes": ALL_MODES,
        "entities": [
            _f("soc", "State of charge (%)", ["sensor"], True, [], r"soc|state.?of.?charge|battery.?level"),
            _f("power", "Battery power (W, + charging)", ["sensor"], True, [], r"battery.*power|power"),
            _f("mode", "Operating mode (select)", ["select"], True, [], r"mode|work|strateg"),
            _f("set_power", "Charge/discharge power setpoint", ["number"], True, [], r"power|watt"),
            _f("target_soc", "Target SOC (optional)", ["number"], False, [], r"target|upper|max.*soc"),
            _f("reserve_soc", "Reserve SOC (optional)", ["number"], False, [], r"reserve|backup|lower|min.*soc"),
            _f("protected", "Protection flag (optional)", ["binary_sensor"], False, [], r"protect|calibrat"),
        ],
        "mode_map": [
            _m("auto", "Auto / self-consumption", "mode", r"auto|self|smart|default"),
            _m("charge", "Charge", "mode", r"^(?!.*dis).*charg"),
            _m("discharge", "Discharge", "mode", r"discharg"),
            _m("hold", "Hold / idle", "mode", r"hold|idle|standby|stop|pause"),
        ],
    },
}

VENDORS: dict[str, dict] = {
    "Anker": {
        "regions": (
            "HomePower works in every region the Anker integration works in; it only uses Home Assistant "
            "entities. Anker's Modbus TCP control is local (enable it in the Anker app under device settings > "
            "Three-Party Control > Modbus TCP). Grid feed-in is off by default in Italy, Spain, Thailand, Vietnam "
            "and the Philippines, so export/zero-import behaviour may need feed-in enabled in the app. The Anker "
            "app's Backup SOC, SOC calibration, Fast Charging and Storm Guard override commands from Home Assistant."
        ),
    },
    "EcoFlow": {
        "regions": (
            "Use the same API host as your EcoFlow integration: api-e.ecoflow.com for Europe, api.ecoflow.com for "
            "global/US. A wrong host fails with \"Account doesn't exist or incorrect password\". HomePower itself "
            "is region-independent. EcoFlow STREAM batteries cannot be force-charged or force-discharged through "
            "these integrations, so HomePower offers Auto and Hold only for them."
        ),
    },
    "Other": {"regions": "Any battery exposing a mode select and a power number in Home Assistant."},
}


def _p(pid, vendor, label, integration, strategy, notes="", **extra):
    return {
        "id": pid, "vendor": vendor, "label": label, "integration": integration,
        "strategy": strategy, "notes": notes, "capacity_wh": None,
        "max_charge_w": None, "max_discharge_w": None, **extra,
    }


_OFFICIAL_NOTE = (
    "Official Anker integration (Modbus TCP). Control uses the 'third party control' operating mode; "
    "HomePower hands the battery back to 'self consumption' when no schedule applies. Charging limit SOC "
    "accepts 80-100 and discharge limit 0-20 on the device."
)
_X1_NOTE = (
    "Entities for control are disabled by default in this integration; enable them in Home Assistant first. "
    "Battery power is + when discharging. Tested upstream on the X1-H12K-T (single and three-phase output modes "
    "are both exposed); rated power can be read from the device."
)
_EF_NOTE = (
    "No force-charge/discharge: Auto = self-powered; Hold = backup reserve raised to 95% so the battery will not "
    "discharge (solar can still charge it). Best effort, unverified on hardware."
)

PROFILES: list[dict] = [
    _p("anker_solarbank_max_ac", "Anker", "Anker SOLIX Solarbank Max AC", "anker_solix_official", "direction_setpoint", _OFFICIAL_NOTE),
    _p("anker_solarbank_max", "Anker", "Anker SOLIX Solarbank Max", "anker_solix_official", "direction_setpoint", _OFFICIAL_NOTE),
    _p("anker_xe_ac", "Anker", "Anker SOLIX XE AC", "anker_solix_official", "direction_setpoint", _OFFICIAL_NOTE),
    _p("anker_xe", "Anker", "Anker SOLIX XE", "anker_solix_official", "direction_setpoint", _OFFICIAL_NOTE),
    _p("anker_e5000_pro", "Anker", "Anker SOLIX Solarbank 4 E5000 Pro", "anker_solix_official", "direction_setpoint", _OFFICIAL_NOTE),
    _p("anker_x1_afewyards", "Anker", "Anker SOLIX X1 (anker_x1 integration)", "anker_x1", "signed_setpoint", _X1_NOTE),
    _p("anker_x1_cbource", "Anker", "Anker SOLIX X1 (anker_solix_x1 integration)", "anker_solix_x1", "signed_setpoint", _X1_NOTE),
    _p("ecoflow_stream_ac", "EcoFlow", "EcoFlow STREAM AC", "ecoflow_cloud", "ecoflow_stream", _EF_NOTE),
    _p("ecoflow_stream_pro", "EcoFlow", "EcoFlow STREAM Pro", "ecoflow_cloud", "ecoflow_stream", _EF_NOTE),
    _p("ecoflow_stream_ultra", "EcoFlow", "EcoFlow STREAM Ultra", "ecoflow_cloud", "ecoflow_stream", _EF_NOTE),
    _p("ecoflow_stream_ultra_x", "EcoFlow", "EcoFlow STREAM Ultra X", "ecoflow_cloud", "ecoflow_stream", _EF_NOTE),
    _p("ecoflow_stream_ac_5000", "EcoFlow", "EcoFlow STREAM AC 5000 / ES5000 (to confirm model)", "ecoflow_cloud", "ecoflow_stream",
       _EF_NOTE + " The private-API entity names for this model were not verified; check each entity."),
    _p("ecoflow_streamx_ultra_x", "EcoFlow", "EcoFlow STREAM Ultra X (ecoflow_streamx integration)", "ecoflow_streamx", "ecoflow_stream", _EF_NOTE),
    _p("ecoflow_streamx_max", "EcoFlow", "EcoFlow STREAM Max (ecoflow_streamx integration)", "ecoflow_streamx", "ecoflow_stream", _EF_NOTE),
    _p("custom", "Other", "Other battery (mode select + power number)", "", "generic"),
]
PROFILE_BY_ID = {p["id"]: p for p in PROFILES}


def catalogue() -> dict:
    return {"profiles": PROFILES, "strategies": STRATEGIES, "vendors": VENDORS}
