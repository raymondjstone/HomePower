"""Every entity's translation key has a name in strings.json (and the other way round), every icon belongs to one of those
entities, every action has an icon, and every translated error the actions raise has its message."""
import json
import re
from pathlib import Path

C = Path(__file__).parent.parent / "custom_components" / "homepower"


def _load(name):
    return json.loads((C / name).read_text(encoding="utf-8"))


def _sensor_keys(code: str) -> set[str]:
    """The translation keys sensor.py hands out: literal keys, keys passed to the shared base classes, the X1's PV-string
    keys (built from the kind), and the savings / daily-share keys chosen at run time."""
    keys = set(re.findall(r'_attr_translation_key = "(\w+)"', code))
    for a, b in re.findall(r'_attr_translation_key = "(\w+)" if [^\n]* else "(\w+)"', code):
        keys |= {a, b}
    keys |= set(re.findall(r'super\(\).__init__\(coord, entry, device_id, name, "\w+", "(\w+)"\)', code))
    keys |= set(re.findall(r'EnergyTotal\(coord, entry, f?"[^"]+", "(\w+)"', code))
    keys |= set(re.findall(r'MoneyTotal\(coord, entry, MONEY_\w+, "(\w+)"\)', code))
    keys |= set(re.findall(r'DailyShare\(coord, entry, "(\w+)"\)', code))
    if 'f"pv_string_{kind}"' in code:
        keys |= {f"pv_string_{k}" for k in re.findall(r'"(voltage|current|power)": \(SensorDeviceClass', code)}
    return keys


def test_every_entity_is_named_from_strings():
    strings, en = _load("strings.json"), _load("translations/en.json")
    assert strings == en, "translations/en.json must be a copy of strings.json"
    names = strings["entity"]
    sensor, switch = (C / "sensor.py").read_text(encoding="utf-8"), (C / "switch.py").read_text(encoding="utf-8")
    assert "_attr_name" not in sensor and "_attr_name" not in switch, "names come from strings.json, not the code"
    assert _sensor_keys(sensor) == set(names["sensor"])
    assert set(re.findall(r'^    \("(\w+)", "', switch, re.M)) == set(names["switch"])
    assert all(v.get("name") for p in names.values() for v in p.values())


def test_icons_belong_to_named_entities_and_every_action_has_one():
    icons, names = _load("icons.json"), _load("strings.json")["entity"]
    for platform, keys in icons["entity"].items():
        assert set(keys) <= set(names[platform]), (platform, set(keys) - set(names[platform]))
    assert set(icons["entity"]["switch"]) == set(names["switch"])
    assert set(re.findall(r"^(\w+):", (C / "services.yaml").read_text(encoding="utf-8"), re.M)) == set(icons["services"])


def test_action_errors_are_translated():
    exceptions = _load("strings.json")["exceptions"]
    code = (C / "services.py").read_text(encoding="utf-8")
    used = set(re.findall(r'_error\("(\w+)"', code))
    assert used and used == set(exceptions), (used, set(exceptions))
    assert "ServiceValidationError(f" not in code and "ServiceValidationError(str(" not in code, "every action error is translated"
    for key, v in exceptions.items():        # the placeholders a message names are the ones the code passes
        wanted = set(re.findall(r"{(\w+)}", v["message"]))
        call = re.search(rf'_error\("{key}", [^\n]*', code).group(0)
        passed = set(re.findall(r"(\w+)=", call))
        assert wanted == passed, (key, wanted, passed)
