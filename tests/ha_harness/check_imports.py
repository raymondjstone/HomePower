import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]
import importlib, inspect, sys, traceback
ok = True
for m in ["__init__", "config_flow", "const", "coordinator", "manager", "sensor", "switch", "diagnostics", "services", "websocket_api", "options_flow", "adapters.ha_entities", "drivers.modbus_generic"]:
    name = "custom_components.homepower" + ("" if m == "__init__" else "." + m)
    try:
        importlib.import_module(name); print("import ok:", name)
    except Exception:
        ok = False; print("IMPORT FAIL", name); traceback.print_exc()
from homeassistant.components import websocket_api as wa, panel_custom, frontend, http
from homeassistant.helpers.storage import Store
from homeassistant.config_entries import ConfigEntries
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, CoordinatorEntity
for label, fn, need in [
    ("panel_custom.async_register_panel", panel_custom.async_register_panel, ["webcomponent_name","frontend_url_path","module_url","sidebar_title","sidebar_icon","require_admin","config"]),
    ("frontend.async_remove_panel", frontend.async_remove_panel, ["warn_if_unknown"]),
    ("ConfigEntries.async_forward_entry_setups", ConfigEntries.async_forward_entry_setups, ["entry", "platforms"]),
    ("ConfigEntries.async_unload_platforms", ConfigEntries.async_unload_platforms, ["entry", "platforms"]),
    ("ConfigEntries.async_update_entry", ConfigEntries.async_update_entry, ["options"]),
    ("http.async_register_static_paths", http.HomeAssistantHTTP.async_register_static_paths, ["configs"]),
]:
    params = inspect.signature(fn).parameters
    miss = [n for n in need if n not in params]
    print(("OK  " if not miss else "BAD ") + label, miss or "")
    ok &= not miss
print("StaticPathConfig fields:", list(inspect.signature(http.StaticPathConfig).parameters))
for n in ["websocket_command","require_admin","async_response","event_message","async_register_command"]:
    print(n, hasattr(wa, n)); ok &= hasattr(wa, n)
print("coordinator async_add_listener / async_update_listeners:", hasattr(DataUpdateCoordinator, "async_add_listener"), hasattr(DataUpdateCoordinator, "async_update_listeners"))
print("ALL OK" if ok else "PROBLEMS")
