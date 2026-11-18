"""Inputs found by the full audit: junk that used to crash instead of being refused, and addresses that must not be scanned."""
import pytest

from hp.core.modbus_map import MapError, parse_map
from hp.core.serde import ValidationError, goal_from_dict, schedule_from_dict, _int
from hp.core.settings import SettingsError, clean
from hp.drivers.discovery import subnet_hosts


def test_overflow_and_junk_numbers_are_validation_errors():
    for bad in ("inf", "1e999", float("inf"), "nan"):
        with pytest.raises(ValidationError):
            _int(bad, "x", 0, 100)


def test_device_ids_must_be_a_list_of_strings():
    base = {"id": "s", "mode": "charge", "start": "01:00", "end": "05:00", "power_w": 1000}
    assert schedule_from_dict({**base, "device_ids": ["a", "b"]}).device_ids == frozenset({"a", "b"})
    assert schedule_from_dict({**base}).device_ids == frozenset()
    for bad in ("bat1", [1, "a"], [{}], 5):
        with pytest.raises(ValidationError):
            schedule_from_dict({**base, "device_ids": bad})
    with pytest.raises(ValidationError):
        goal_from_dict({"id": "g", "target_soc": 80, "by": "07:00", "device_ids": "bat1"})


def test_settings_refuse_nan_and_infinity_and_odd_rules():
    for key in ("price_peak", "price_offpeak", "export_price"):
        for bad in ("nan", "inf", "-inf"):
            with pytest.raises(SettingsError):
                clean({key: bad})
    for bad in (["x"], [{"entity": "sensor.a", "above_w": "nan"}], [{"entity": "sensor.a", "above_w": "inf"}]):
        with pytest.raises(SettingsError):
            clean({"load_rules": bad})
    with pytest.raises(SettingsError):
        clean({"import_limit_w": "inf"})
    assert clean({"price_peak": "0.3"})["price_peak"] == 0.3


def test_register_map_with_a_junk_function_code_is_a_map_error():
    base = {"soc": {"addr": 1}, "power": {"addr": 2, "fc": "x"}}
    with pytest.raises(MapError):
        parse_map(base)
    with pytest.raises(MapError):
        parse_map({"soc": {"addr": 1}, "power": {"addr": 2, "scale": "nan"}})


def test_network_scan_never_covers_a_public_address():
    assert subnet_hosts("8.8.8.8", 24) == []
    assert subnet_hosts("203.0.113.5", 24) == []
    assert subnet_hosts("::1", 64) == []                              # IPv6 is not scanned
    hosts = subnet_hosts("192.168.1.10", 24)
    assert hosts and "192.168.1.10" not in hosts and all(h.startswith("192.168.1.") for h in hosts)
    assert subnet_hosts("10.1.2.3", 8)[0].startswith("10.1.2.")      # never wider than a /24


def test_currency_symbol_is_short_and_markup_free():
    assert clean({"currency": "£"})["currency"] == "£"
    got = clean({"currency": '<img src=x onerror="a">'})["currency"]
    assert len(got) <= 8 and not any(c in got for c in "<>&\"'`")


def test_a_nan_price_in_a_rates_entity_is_no_price():
    from core.rates import parse_rates
    slots = parse_rates({"rates": [{"start": "2026-11-10T00:00:00Z", "end": "2026-11-10T00:30:00Z", "value_inc_vat": "nan"},
                                   {"start": "2026-11-10T00:30:00Z", "end": "2026-11-10T01:00:00Z", "value_inc_vat": float("inf")},
                                   {"start": "2026-11-10T01:00:00Z", "end": "2026-11-10T01:30:00Z", "value_inc_vat": 12.5}]})
    assert [s.price for s in slots] == [12.5]


def test_a_nan_or_infinite_forecast_leaves_a_goal_target_alone():
    from core.planner import adjust_target_for_forecast
    assert adjust_target_for_forecast(90, float("nan"), 10.0) == 90
    assert adjust_target_for_forecast(90, float("inf"), 10.0) == 90


def test_switch_settings_written_as_text_are_read_as_words():
    import pytest
    from core.settings import SettingsError, clean
    assert clean({"dry_run": "false", "peak_shaving": "on", "solar_room": "0", "weekly_digest": True}) == \
        {"dry_run": False, "peak_shaving": True, "solar_room": False, "weekly_digest": True}
    with pytest.raises(SettingsError):
        clean({"dry_run": "maybe"})


def test_a_setting_of_zero_stays_zero():
    from core.settings import build
    s = build({"divert_min_soc": 0, "divert_export_w": 0, "cheap_soon_below_pct": 0})
    assert (s.divert_min_soc, s.divert_export_w, s.cheap_soon_below_pct) == (0, 0, 0)
    d = build({})
    assert (d.divert_min_soc, d.divert_export_w, d.cheap_soon_below_pct) == (95, 500, 60)


def test_two_tariffs_with_the_same_name_are_refused():
    import pytest
    from core.tariffs import parse_tariffs
    with pytest.raises(ValueError):
        parse_tariffs("Agile; peak=0.30\nagile; peak=0.20")
    assert len(parse_tariffs("Agile; peak=0.30\nGo; peak=0.28; cheap=0.08; window=00:30-04:30")) == 2
