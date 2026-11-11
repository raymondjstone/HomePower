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
    "grid_import_entity": "", "grid_export_entity": "", "grid_invert": False, "zero_import_device": "",
    "rates_entity": "", "rates_unit": "auto", "price_peak": None, "price_offpeak": None,
    "offpeak_start": "", "offpeak_end": "", "offpeak_auto": True, "currency": "£",
    "forecast_entity": "", "forecast_share": 0.5,
    "storm_entity": "", "backup_reserve_pct": 50, "away_entity": "", "notify_service": "",
    "load_rules": [],
    "ecoflow_live": False, "notify_full": False, "notify_low_pct": 0, "notify_cheap_soon": False, "cheap_soon_below_pct": 60,
}
INT_KEYS = ("import_limit_w", "cooldown_s", "total_charge_limit_w", "total_discharge_limit_w", "export_limit_w", "backup_reserve_pct",
            "notify_low_pct", "cheap_soon_below_pct")
FLOAT_KEYS = ("price_peak", "price_offpeak", "forecast_share")
BOOL_KEYS = ("peak_shaving", "grid_invert", "offpeak_auto", "ecoflow_live", "notify_full", "notify_cheap_soon")
STR_KEYS = ("rates_unit", "grid_import_entity", "grid_export_entity", "zero_import_device", "rates_entity", "offpeak_start", "offpeak_end", "currency",
            "forecast_entity", "storm_entity", "away_entity", "notify_service")


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
                out[k] = None if v in (None, "") else float(v)
            elif k in BOOL_KEYS:
                out[k] = bool(v)
            elif k in STR_KEYS:
                out[k] = str(v or "").strip()
            elif k == "load_rules":
                out[k] = clean_rules(v)
        except (TypeError, ValueError) as err:
            raise SettingsError(f"{k} is not valid") from err
    for k in ("offpeak_start", "offpeak_end"):
        if k in out:
            _t(out[k])
    for k in ("notify_low_pct", "cheap_soon_below_pct"):
        if k in out and out[k] > 100:
            raise SettingsError("percentages must be 0-100")
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
        rules.append({"entity": ent, "block": block, "above_w": None if above in (None, "") else float(above)})
    return rules


@dataclass
class AppSettings:
    engine: EngineSettings = field(default_factory=EngineSettings)
    cooldown_s: float = 120
    grid_entity: str = ""
    grid_export_entity: str = ""
    grid_invert: bool = False
    rates_entity: str = ""
    rates_unit: str = "auto"   # auto | pence | main
    price_peak: float | None = None
    price_offpeak: float | None = None
    offpeak_start: time | None = None
    offpeak_end: time | None = None
    offpeak_auto: bool = True   # learn the cheap window from the rates entity's price history
    currency: str = "£"
    forecast_entity: str = ""
    storm_entity: str = ""
    away_entity: str = ""
    notify_service: str = ""
    load_rules: list[dict] = field(default_factory=list)
    ecoflow_live: bool = False
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
        backup_pct=int(c["backup_reserve_pct"]), forecast_share=float(c["forecast_share"] if c["forecast_share"] is not None else 0.5),
    )
    return AppSettings(
        engine=eng, cooldown_s=c["cooldown_s"], grid_entity=c["grid_import_entity"], grid_export_entity=c["grid_export_entity"], grid_invert=bool(c["grid_invert"]),
        rates_entity=c["rates_entity"], rates_unit=c["rates_unit"] or "auto", price_peak=c["price_peak"],
        price_offpeak=c["price_offpeak"], offpeak_start=_t(c["offpeak_start"]), offpeak_end=_t(c["offpeak_end"]), offpeak_auto=bool(c["offpeak_auto"]),
        currency=c["currency"] or "£", forecast_entity=c["forecast_entity"], storm_entity=c["storm_entity"],
        away_entity=c["away_entity"], notify_service=c["notify_service"], load_rules=list(c["load_rules"] or []),
        ecoflow_live=bool(c["ecoflow_live"]), notify_full=bool(c["notify_full"]), notify_low_pct=int(c["notify_low_pct"] or 0),
        notify_cheap_soon=bool(c["notify_cheap_soon"]), cheap_soon_below_pct=int(c["cheap_soon_below_pct"] or 60),
    )
