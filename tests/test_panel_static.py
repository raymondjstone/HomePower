"""The panel is plain JavaScript that cannot run in CI, so check its internal consistency statically."""
import re
from pathlib import Path

JS = (Path(__file__).parent.parent / "custom_components" / "homepower" / "frontend" / "homepower-panel.js").read_text(encoding="utf8")


def test_every_called_method_is_defined():
    defined = set(re.findall(r"^\s{2}(?:async |get |set )?(_?\w+)\(", JS, re.M)) | {"constructor"}
    assert set(re.findall(r"this\.(_\w+)\(", JS)) - defined == set()


def test_every_looked_up_element_exists_in_a_template():
    used = set(re.findall(r'getElementById\("([\w-]+)"\)', JS))
    templated = set(re.findall(r'id="([\w-]+)"', JS))
    dynamic = ("e-", "mm-", "c-", "st-", "test-", "dl-")
    assert sorted(i for i in used if i not in templated and not i.startswith(dynamic) and i not in ("app", "modal")) == []


def test_every_button_action_has_a_handler():
    handled = set(re.findall(r'case "([\w-]+)":', JS))
    assert sorted(a for a in set(re.findall(r'data-act="([\w-]+)"', JS)) if a not in handled) == []


def test_services_yaml_parses_and_matches_the_translations():
    """hassfest rejects an unparsable services.yaml (an unquoted ': ' in a description is enough)."""
    import json
    import pathlib

    import pytest
    yaml = pytest.importorskip("yaml")
    root = pathlib.Path(__file__).parent.parent / "custom_components" / "homepower"
    services = yaml.safe_load((root / "services.yaml").read_text(encoding="utf8"))
    strings = json.loads((root / "strings.json").read_text(encoding="utf8"))["services"]
    assert set(services) == set(strings), set(services) ^ set(strings)
    for name, spec in services.items():
        assert isinstance(spec.get("description"), str) and spec["name"], name
