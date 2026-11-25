"""Every form HomePower shows (setup, Configure, Repairs) must convert to JSON the way Home Assistant sends it to the browser
(Probatio's to_field_list, or voluptuous_serialize before 2026.9, with config_validation's serializer). A schema that does not convert breaks that form in the UI even
though the flow itself works, which the other flow scripts cannot see. Runs configure_flows.py with every form checked."""
import os, runpy, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

from homeassistant import data_entry_flow
from homeassistant.helpers import config_validation as cv

try:                                    # Home Assistant 2026.9+: Probatio does the conversion
    import probatio

    def to_json(schema):
        return probatio.to_field_list(schema, custom_serializer=cv.custom_serializer)
except ImportError:                     # older releases: voluptuous_serialize
    import voluptuous_serialize

    def to_json(schema):
        return voluptuous_serialize.convert(schema, custom_serializer=cv.custom_serializer)

seen: list[str] = []
broken: list[str] = []
_show = data_entry_flow.FlowHandler.async_show_form


def checked(self, *, step_id=None, data_schema=None, **kw):
    if data_schema is not None:
        try:
            to_json(data_schema)        # what Home Assistant does before sending the form: fails if the UI could not show it
        except Exception as err:  # noqa: BLE001
            broken.append(f"{type(self).__name__}.{step_id}: {err!r}")
    seen.append(f"{type(self).__name__}.{step_id}")
    return _show(self, step_id=step_id, data_schema=data_schema, **kw)


data_entry_flow.FlowHandler.async_show_form = checked
sys.argv = [os.path.join(os.path.dirname(__file__), "configure_flows.py")]
flows = runpy.run_path(sys.argv[0], run_name="__main__")

# The script above fills most forms in directly, so also open every step that starts a form, with no input: each form is
# then shown (and converted) the way the user first sees it.
import asyncio


async def open_each():
    mgr = flows["FakeMgr"](1)
    mgr.devices["a"] = {"device_id": "a", "name": "A", "control_enabled": False}   # so "control battery" has one to offer
    for name in ("init", "add_battery", "add_schedule", "add_goal", "remove_goal", "settings", "money", "remove_battery",
                 "control_battery", "edit_schedule", "remove_schedule"):
        f = flows["mkflow"](mgr)
        try:
            await getattr(f, f"async_step_{name}")(None)
        except Exception as err:  # noqa: BLE001 - a step that needs more of a real Home Assistant than this fake has
            print(f"  (could not open {name} here: {type(err).__name__})")

asyncio.run(open_each())
print("forms    :", len(set(seen)), "distinct forms shown and converted:", ", ".join(sorted(set(seen))))
assert not broken, broken
assert len(set(seen)) >= 12, seen
print("ALL FORMS SERIALISE OK")
