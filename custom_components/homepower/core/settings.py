"""All user settings in one place: parsing, defaults and validation (HA-free)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import time

from .engine import Settings as EngineSettings

# key -> default. Everything the panel / Configure flow can change lives under these keys.
DEFAULTS: dict = {
    "import_limit_w": 7000, "cooldown_s": 120, "total_charge_limit_w": 0, "total_discharge_limit_w": 0,
    "export_limit_w": 0, "peak_shaving": False,
    "grid_import_entity": "", "grid_export_entity": "", "grid_invert": False, "grid_order": "sensor,battery,glow,cloud", "zero_import_device": "",
    "rates_entity": "", "rates_unit": "auto", "price_peak": None, "price_offpeak": None, "export_price": None,
    "offpeak_start": "", "offpeak_end": "", "offpeak_auto": True, "currency": "£",
    "forecast_entity": "", "forecast_share": 0.5,
    "storm_entity": "", "backup_reserve_pct": 50, "away_entity": "", "notify_service": "",
    "load_rules": [],
    "ecoflow_live": False, "dry_run": False, "erase_on_remove": False, "solar_room": True, "base_load_w": 0, "notify_full": False, "offpeak_extra": "", "save_for_peak": False, "hold_for_dearer": False,
    "weather_boost": False, "weather_entity": "", "temp_sensor_entity": "", "forecast_temp_entity": "", "weather_online": False,
    "compare_tariffs": "", "weekly_digest": False, "forecast_reserve_kwh": 0, "forecast_reserve_pct": 50, "divert_cheap": False, "x1_grid_limits": False, "daily_summary_time": "", "divert_entity": "", "divert_export_w": 500, "divert_min_soc": 95, "notify_low_pct": 0, "notify_cheap_soon": False, "cheap_soon_below_pct": 60,
}
INT_KEYS = ("import_limit_w", "cooldown_s", "total_charge_limit_w", "total_discharge_limit_w", "export_limit_w", "backup_reserve_pct",
            "notify_low_pct", "cheap_soon_below_pct", "forecast_reserve_kwh", "forecast_reserve_pct", "divert_export_w", "divert_min_soc", "base_load_w")
FLOAT_KEYS = ("price_peak", "price_offpeak", "forecast_share", "export_price")
BOOL_KEYS = ("weekly_digest", "x1_grid_limits", "divert_cheap", "dry_run", "peak_shaving", "grid_invert", "offpeak_auto", "ecoflow_live", "erase_on_remove", "solar_room", "save_for_peak", "hold_for_dearer", "weather_boost", "weather_online", "notify_full", "notify_cheap_soon")
STR_KEYS = ("rates_unit", "grid_import_entity", "grid_export_entity", "zero_import_device", "rates_entity", "offpeak_start", "offpeak_end", "currency",
            "forecast_entity", "storm_entity", "away_entity", "notify_service", "weather_entity", "temp_sensor_entity", "forecast_temp_entity",
            "offpeak_extra", "daily_summary_time", "divert_entity", "grid_order", "compare_tariffs")


class SettingsError(ValueError):
    pass


def _t(v: str) -> time | None:
    if not v:
        return None
    try:
        h, m = str(v).split(":")[:2]
        return time(int(h), int(m))
    except (ValueError, TypeError) as e:
        raise SettingsError(f"'{v}' is not a time (HH:MM)") from e


GRID_SOURCES = ("sensor", "battery", "glow", "cloud")


def parse_grid_order(v) -> list[str]:
    """'battery, sensor' -> ['battery', 'sensor']: which grid readings to use, most trusted first. Left-out ones are never used."""
    out: list[str] = []
    for part in str(v or "").replace(";", ",").split(","):
        part = part.strip().lower()
        if not part:
            continue
        if part not in GRID_SOURCES:
            raise SettingsError(f"'{part}' is not a grid reading source ({', '.join(GRID_SOURCES)})")
        if part not in out:
            out.append(part)
    if not out:
        raise SettingsError("choose at least one source for the grid reading")
    return out


def parse_windows(v: str) -> list[tuple[time, time]]:
    """'13:00-16:00, 22:00-00:00' -> [(13:00, 16:00), (22:00, 00:00)]."""
    out = []
    for part in str(v or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.replace("–", "-").split("-")
        if len(bits) != 2:
            raise SettingsError(f"'{part}' is not a time range (HH:MM-HH:MM)")
        a, b = _t(bits[0].strip()), _t(bits[1].strip())
        if a is None or b is None or a == b:
            raise SettingsError(f"'{part}' is not a time range (HH:MM-HH:MM)")
        out.append((a, b))
    return out


def clean(raw: dict) -> dict:
    """Validate/normalise user-supplied settings; unknown keys are dropped. Partial updates allowed."""
    out: dict = {}
    for k, v in raw.items():
        if k not in DEFAULTS:
            continue
        try:
            if k in INT_KEYS:
                out[k] = max(0, int(float(v or 0)))
            elif k in FLOAT_KEYS:
                out[k] = None if v in (None, "") else _finite(v)
            elif k in BOOL_KEYS:
                out[k] = bool(v)
            elif k in STR_KEYS:
                out[k] = str(v or "").strip()
                if k == "compare_tariffs":
                    from .tariffs import parse_tariffs
                    try:
                        parse_tariffs(out[k])
                    except ValueError as err:
                        raise SettingsError(str(err)) from err
            elif k == "load_rules":
                out[k] = clean_rules(v)
        except (TypeError, ValueError, OverflowError, AttributeError) as err:
            raise SettingsError(f"{k} is not valid") from err
    for k in ("offpeak_start", "offpeak_end", "daily_summary_time"):
        if k in out:
            _t(out[k])
    if "grid_order" in out:
        out["grid_order"] = ",".join(parse_grid_order(out["grid_order"]))
    if "offpeak_extra" in out:
        parse_windows(out["offpeak_extra"])
    if "base_load_w" in out and not 0 <= out["base_load_w"] <= 50000:
        raise SettingsError("base load must be 0-50000 W")
    if "divert_min_soc" in out and out["divert_min_soc"] > 100:
        raise SettingsError("percentages must be 0-100")
    for k in ("notify_low_pct", "cheap_soon_below_pct"):
        if k in out and out[k] > 100:
            raise SettingsError("percentages must be 0-100")
    if "forecast_reserve_pct" in out and out["forecast_reserve_pct"] > 100:
        raise SettingsError("reserve must be 0-100")
    if "backup_reserve_pct" in out and out["backup_reserve_pct"] > 100:
        raise SettingsError("backup reserve must be 0-100")
    if "forecast_share" in out and out["forecast_share"] is not None and not 0 <= out["forecast_share"] <= 1:
        raise SettingsError("forecast share must be between 0 and 1")
    if "rates_unit" in out and out["rates_unit"] not in ("auto", "pence", "main"):
        raise SettingsError("rates unit must be auto, pence or main")
    if out.get("notify_service") and "." not in out["notify_service"]:
        raise SettingsError("notify service must look like notify.mobile_app_phone")
    return out


def clean_rules(v) -> list[dict]:
    if isinstance(v, str):
        v = json.loads(v) if v.strip() else []
    rules = []
    for r in v or []:
        ent = str(r.get("entity") or "").strip()
        block = str(r.get("block") or "discharge")
        if not ent:
            continue
        if block not in ("charge", "discharge", "both"):
            raise SettingsError("a load rule must block charge, discharge or both")
        above = r.get("above_w")
        rules.append({"entity": ent, "block": block, "above_w": None if above in (None, "") else _finite(above)})
    return rules


def _finite(v) -> float:
    """float(v), but NaN and infinity are refused (they would flow into prices, savings and comparisons)."""
    f = float(v)
    if f != f or f in (float("inf"), float("-inf")):
        raise ValueError("not a finite number")
    return f


@dataclass
class AppSettings:
    engine: EngineSettings = field(default_factory=EngineSettings)
    cooldown_s: float = 120
    grid_entity: str = ""
    grid_export_entity: str = ""
    grid_invert: bool = False
    grid_order: list = field(default_factory=lambda: list(GRID_SOURCES))   # grid reading sources, most trusted first
    rates_entity: str = ""
    rates_unit: str = "auto"   # auto | pence | main
    price_peak: float | None = None
    price_offpeak: float | None = None
    export_price: float | None = None   # what you are paid per kWh exported (same unit as the import prices)
    offpeak_start: time | None = None
    offpeak_end: time | None = None
    offpeak_auto: bool = True   # learn the cheap window from the rates entity's price history
    currency: str = "£"
    forecast_entity: str = ""
    weather_boost: bool = False     # raise the overnight target before a cold day
    weather_entity: str = ""        # a Home Assistant weather entity (tomorrow's forecast)
    temp_sensor_entity: str = ""    # outdoor temperature sensor (learning how cold affects use)
    forecast_temp_entity: str = ""  # a sensor holding tomorrow's temperature (fallback)
    weather_online: bool = False    # also ask Open-Meteo (sends the home's rounded location to it)
    storm_entity: str = ""
    away_entity: str = ""
    notify_service: str = ""
    load_rules: list[dict] = field(default_factory=list)
    ecoflow_live: bool = False
    x1_grid_limits: bool = False   # mirror the import/export limits into an Anker X1's own registers
    divert_cheap: bool = False     # also run the surplus load during cheap-rate hours
    compare_tariffs: str = ""
    weekly_digest: bool = False    # a summary message every Monday
    forecast_reserve_kwh: int = 0   # on a day forecast below this many kWh of solar, keep the batteries above forecast_reserve_pct (0 = off)
    forecast_reserve_pct: int = 50
    dry_run: bool = False          # work everything out and log it, but send no commands
    solar_room: bool = True        # cap overnight charging of batteries with panels to leave room for the solar forecast
    base_load_w: int = 0           # the house's steady use, assumed taken from the batteries outside cheap hours
    offpeak_extra: list = field(default_factory=list)   # further cheap windows [(start, end)]
    daily_summary_time: time | None = None
    divert_entity: str = ""
    divert_export_w: int = 500
    divert_min_soc: int = 95
    notify_full: bool = False
    notify_low_pct: int = 0
    notify_cheap_soon: bool = False
    cheap_soon_below_pct: int = 60


def build(cfg: dict) -> AppSettings:
    c = {**DEFAULTS, **{k: v for k, v in cfg.items() if k in DEFAULTS}}
    eng = EngineSettings(
        charge_cap_w=c["total_charge_limit_w"] or None, discharge_cap_w=c["total_discharge_limit_w"] or None,
        import_limit_w=c["import_limit_w"] or None, export_limit_w=c["export_limit_w"] or None,
        zero_import_device=c["zero_import_device"] or None, peak_shaving=bool(c["peak_shaving"]),
        backup_pct=int(c["backup_reserve_pct"]), save_for_peak=bool(c["save_for_peak"]), hold_for_dearer=bool(c["hold_for_dearer"]), forecast_share=float(c["forecast_share"] if c["forecast_share"] is not None else 0.5),
    )
    return AppSettings(
        engine=eng, cooldown_s=c["cooldown_s"], grid_entity=c["grid_import_entity"], grid_export_entity=c["grid_export_entity"], grid_invert=bool(c["grid_invert"]), grid_order=parse_grid_order(c["grid_order"] or ",".join(GRID_SOURCES)),
        rates_entity=c["rates_entity"], rates_unit=c["rates_unit"] or "auto", price_peak=c["price_peak"],
        price_offpeak=c["price_offpeak"], export_price=c["export_price"], offpeak_start=_t(c["offpeak_start"]), offpeak_end=_t(c["offpeak_end"]), offpeak_auto=bool(c["offpeak_auto"]),
        currency=c["currency"] or "£", forecast_entity=c["forecast_entity"], weather_boost=bool(c["weather_boost"]), weather_entity=c["weather_entity"], temp_sensor_entity=c["temp_sensor_entity"], forecast_temp_entity=c["forecast_temp_entity"], weather_online=bool(c["weather_online"]), storm_entity=c["storm_entity"],
        away_entity=c["away_entity"], notify_service=c["notify_service"], load_rules=list(c["load_rules"] or []),
        ecoflow_live=bool(c["ecoflow_live"]), dry_run=bool(c["dry_run"]), divert_cheap=bool(c["divert_cheap"]), x1_grid_limits=bool(c["x1_grid_limits"]), compare_tariffs=c["compare_tariffs"], weekly_digest=bool(c["weekly_digest"]), forecast_reserve_kwh=int(c["forecast_reserve_kwh"] or 0), forecast_reserve_pct=int(c["forecast_reserve_pct"] or 0), solar_room=bool(c["solar_room"]), base_load_w=int(c["base_load_w"] or 0), offpeak_extra=parse_windows(c["offpeak_extra"]),
        daily_summary_time=_t(c["daily_summary_time"]), divert_entity=c["divert_entity"],
        divert_export_w=int(c["divert_export_w"] or 500), divert_min_soc=int(c["divert_min_soc"] or 95), notify_full=bool(c["notify_full"]), notify_low_pct=int(c["notify_low_pct"] or 0),
        notify_cheap_soon=bool(c["notify_cheap_soon"]), cheap_soon_below_pct=int(c["cheap_soon_below_pct"] or 60),
    )
