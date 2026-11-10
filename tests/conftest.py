import importlib
import pkgutil
import sys
import types
from pathlib import Path

PKG_DIR = Path(__file__).parent.parent / "custom_components" / "homepower"

# Import the HA-free core without triggering the integration's __init__ (needs homeassistant).
sys.path.insert(0, str(PKG_DIR))

# Also expose the package as "hp" (without running its __init__) so modules that use
# package-relative imports (drivers/*) load, sharing the very same core module objects.
hp = types.ModuleType("hp")
hp.__path__ = [str(PKG_DIR)]
sys.modules["hp"] = hp
core = importlib.import_module("core")
sys.modules["hp.core"] = core
for m in pkgutil.iter_modules(core.__path__):
    sys.modules[f"hp.core.{m.name}"] = importlib.import_module(f"core.{m.name}")
