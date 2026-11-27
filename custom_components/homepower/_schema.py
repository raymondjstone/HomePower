"""Use the schema library Home Assistant itself uses.

Older cores use `voluptuous`; newer ones moved to the API-compatible `probatio`. Following the
core's own choice keeps websocket/service/form schemas valid on both.
"""
from __future__ import annotations

from typing import Any

from homeassistant.helpers import config_validation as _cv

vol: Any = getattr(_cv, "vol", None)      # whichever of the two libraries: typed as Any
if vol is None or not hasattr(vol, "Required"):
    try:
        import probatio as vol  # type: ignore[no-redef]
    except ImportError:  # pragma: no cover
        import voluptuous as vol  # type: ignore[no-redef]
