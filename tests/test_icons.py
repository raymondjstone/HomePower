"""Every icon key an entity asks for is in icons.json, and the other way round; every action has an icon."""
import json
import re
from pathlib import Path

C = Path(__file__).parent.parent / "custom_components" / "homepower"


def test_icons_match_the_entities_and_actions():
    icons = json.loads((C / "icons.json").read_text(encoding="utf-8"))
    sensor = (C / "sensor.py").read_text(encoding="utf-8")
    used = set(re.findall(r'_attr_translation_key = "(\w+)"', sensor)) | set(re.findall(r'MONEY_\w+, "[^"]+", "(\w+)"', sensor))
    assert used == set(icons["entity"]["sensor"])
    switch = (C / "switch.py").read_text(encoding="utf-8")
    assert set(re.findall(r'^    \("(\w+)", "', switch, re.M)) == set(icons["entity"]["switch"])
    assert set(re.findall(r"^(\w+):", (C / "services.yaml").read_text(encoding="utf-8"), re.M)) == set(icons["services"])
