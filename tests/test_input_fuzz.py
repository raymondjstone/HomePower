"""Every validator a user can reach (panel, services, backup files) is fed random junk. Each must either accept the input
or refuse it with its own "invalid input" error - never a TypeError/KeyError/AttributeError/OverflowError that would surface
as a crash - and whatever it accepts must survive being saved and loaded again (a value that breaks the next start-up is
the real way a user could take the integration down)."""
import json
import math
import random

import pytest

from hp.core.insights import SceneError, clean_scene
from hp.core.modbus_map import MapError, parse_map
from hp.core.serde import (ValidationError, device_from_dict, device_to_dict, goal_from_dict, goal_to_dict, schedule_from_dict,
                           schedule_to_dict)
from hp.core.settings import DEFAULTS, SettingsError, build, clean
from hp.core.tariffs import parse_tariffs

ALLOWED = (ValidationError, SettingsError, SceneError, MapError, ValueError)
SEEDS = range(400)

ATOMS = [None, True, False, 0, -1, 1, 7, 100, 101, 65536, 10**30, -(10**30), 0.5, -0.0, 1e308, float("inf"), float("-inf"),
         float("nan"), "", " ", "0", "1", "-5", "abc", "12:00", "25:61", "00:00", "on", "off", "yes", "nan", "inf", "1e999",
         "x" * 5000, "\x00", "é漢字", "<script>", "a:b:c", "13:00-16:00", "auto", "charge", "discharge", "hold",
         "sensor.x", "notify.mobile", [], {}, [1, 2], ["a", "b"], {"a": 1}, [None], [[1]], {"k": {"k": {}}}]


def junk(rng: random.Random, depth: int = 0):
    r = rng.random()
    if depth < 2 and r < 0.12:
        return [junk(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    if depth < 2 and r < 0.2:
        return {rng.choice(["a", "id", "mode", "x"]): junk(rng, depth + 1) for _ in range(rng.randint(0, 3))}
    return rng.choice(ATOMS)


def mutate(rng: random.Random, base: dict, keys) -> dict:
    """A plausible dict with some fields replaced by junk, some dropped and some unknown ones added."""
    d = dict(base)
    for k in keys:
        if rng.random() < 0.35:
            d[k] = junk(rng)
        elif rng.random() < 0.08:
            d.pop(k, None)
    if rng.random() < 0.2:
        d[rng.choice(["zzz", "__proto__", "extra"])] = junk(rng)
    return d


def _json_round_trip(v):
    return json.loads(json.dumps(v, allow_nan=False))      # HA's store refuses NaN/inf: an accepted value must not hold one


SCHEDULE = {"id": "Night", "start": "00:30", "end": "05:30", "mode": "charge", "power_w": 2000, "target_soc": 80,
            "device_ids": ["a"], "days": [0, 1, 2], "priority": 1, "enabled": True}
GOAL = {"id": "g", "target_soc": 80, "by": "07:00", "source": "grid", "device_ids": ["a"], "enabled": True}
SCENE = {"name": "Holiday", "backup": False, "away": True, "paused": False, "settings": {"import_limit_w": 5000}}
DEVICE = {"device_id": "a", "name": "A", "kind": "anker_e5000_pro", "driver": "anker_modbus", "capacity_wh": 5000,
          "max_charge_w": 3000, "max_discharge_w": 3000, "min_soc": 10, "control_enabled": False,
          "connection": {"host": "192.168.1.20", "port": 502, "unit_id": 1}}


@pytest.mark.parametrize("seed", SEEDS)
def test_schedule_goal_scene_device_validators_only_refuse_cleanly(seed):
    rng = random.Random(seed)
    for base, parse, dump in ((SCHEDULE, schedule_from_dict, schedule_to_dict), (GOAL, goal_from_dict, goal_to_dict),
                              (DEVICE, device_from_dict, device_to_dict)):
        raw = mutate(rng, base, list(base))
        try:
            out = dump(parse(raw))
        except ALLOWED:
            continue
        again = dump(parse(_json_round_trip(out)))          # saved, then loaded at the next start: same result, no error
        assert again == out, (raw, out, again)
    raw = mutate(rng, SCENE, list(SCENE))
    try:
        s = clean_scene(raw)
    except ALLOWED:
        return
    assert clean_scene(_json_round_trip(s)) == s
    build({**DEFAULTS, **s["settings"]})                    # applying the scene must give settings that load


@pytest.mark.parametrize("seed", SEEDS)
def test_settings_only_refuse_cleanly_and_what_is_accepted_always_loads(seed):
    rng = random.Random(seed)
    keys = rng.sample(sorted(DEFAULTS), rng.randint(1, 8))
    raw = {k: junk(rng) for k in keys}
    try:
        out = clean(raw)
    except ALLOWED:
        return
    stored = _json_round_trip(out)
    build({**DEFAULTS, **stored})                           # the next start-up builds the settings from what was saved
    assert clean(stored) == out


@pytest.mark.parametrize("seed", range(150))
def test_tariff_text_and_register_maps_only_refuse_cleanly(seed):
    rng = random.Random(seed)
    text = rng.choice(["Go; peak=0.28; cheap=0.07; window=00:30-05:30", "", "x", ";;;", "a; peak=", "b; window=99:99-00:00",
                       "c; peak=nan", "d; peak=1e999; cheap=-1", "e\nf; standing=abc"]) + rng.choice(["", "; export=0.1", "\n" * 3])
    try:
        parse_tariffs(text)
    except ALLOWED:
        pass
    m = {"soc": junk(rng), "power": junk(rng), "mode": junk(rng)} if rng.random() < 0.7 else junk(rng)
    try:
        parse_map(m if not isinstance(m, (int, float)) or isinstance(m, bool) else str(m))
    except ALLOWED:
        pass


def test_nothing_accepted_holds_a_number_json_cannot_store():
    for v in (float("nan"), float("inf"), "nan", "inf", "1e999"):
        for k in ("import_limit_w", "price_peak", "forecast_share", "wear_cost", "offline_after_min"):
            try:
                out = clean({k: v})
            except ALLOWED:
                continue
            assert all(not (isinstance(x, float) and not math.isfinite(x)) for x in out.values()), (k, v, out)


OTHER_DEVICES = [
    {"device_id": "e", "name": "E", "kind": "custom", "capacity_wh": 5000, "max_charge_w": 2000, "max_discharge_w": 2000, "min_soc": 10,
     "entities": {"soc": "sensor.soc", "power": "sensor.p", "mode": "select.mode", "set_power": "number.sp"},
     "mode_map": {"auto": "Auto", "charge": "Charge", "discharge": "Discharge", "hold": "Hold"}, "invert_power": False},
    {"device_id": "f", "name": "F", "kind": "ecoflow_stream_ultra_x", "capacity_wh": 5000, "max_charge_w": 2300, "min_soc": 10,
     "connection": {"region": "eu", "access_key": "a", "secret_key": "b", "sn": "BK61"}},
    {"device_id": "g", "name": "G", "kind": "modbus_map", "capacity_wh": 5000, "max_charge_w": 2000, "max_discharge_w": 2000,
     "min_soc": 10, "connection": {"host": "10.0.0.5", "port": 502, "unit_id": 1,
                                   "map": {"soc": {"addr": 843, "type": "u16"}, "power": {"addr": 842, "type": "s16"}}}},
]


@pytest.mark.parametrize("seed", SEEDS)
def test_every_kind_of_battery_and_its_nested_fields_only_refuse_cleanly(seed):
    rng = random.Random(seed)
    for base in OTHER_DEVICES:
        raw = mutate(rng, base, list(base))
        for nested in ("connection", "entities", "mode_map"):
            if isinstance(raw.get(nested), dict) and rng.random() < 0.5:
                raw[nested] = mutate(rng, raw[nested], list(raw[nested]))
                if nested == "connection" and isinstance(raw[nested].get("map"), dict) and rng.random() < 0.5:
                    raw[nested]["map"] = mutate(rng, raw[nested]["map"], list(raw[nested]["map"]))
        try:
            out = device_to_dict(device_from_dict(raw))
        except ALLOWED:
            continue
        assert device_to_dict(device_from_dict(_json_round_trip(out))) == out, (raw, out)


def test_a_new_battery_needs_a_safe_id_and_a_short_name_but_existing_ones_keep_theirs():
    from hp.core.serde import check_new_device

    existing = {"old:id": {}}
    for bad in ({"device_id": "a:b", "name": "A"}, {"device_id": "", "name": "A"}, {"device_id": "x" * 97, "name": "A"},
                {"device_id": "ok", "name": "n" * 81}):
        with pytest.raises(ValidationError):
            check_new_device(bad, existing)
    check_new_device({"device_id": "old:id", "name": "Old"}, existing)        # saved before: keeps its id
    check_new_device({"device_id": "garage_battery_2", "name": "Garage battery"}, existing)
    check_new_device({"device_id": "b" * 84, "name": "n" * 80}, existing)     # the longest name the panel allows still fits
