import sys
from pathlib import Path

# Import the HA-free core without triggering the integration's __init__ (needs homeassistant).
sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components" / "homepower"))
