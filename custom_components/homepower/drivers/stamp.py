"""Times the drivers record for people to read (dropouts, settings changes), in Home Assistant's time zone.

The drivers do not otherwise depend on Home Assistant (their tests run without it), so outside it the computer's own zone is
used. Inside Home Assistant the operating system's zone can differ from Home Assistant's (a container left on UTC), which
would put these times hours away from every other time shown."""
from __future__ import annotations

import time
from datetime import datetime, timezone


def stamp(ts: float | None = None) -> str:
    ts = time.time() if ts is None else ts
    try:
        from homeassistant.util import dt as dt_util

        return dt_util.as_local(datetime.fromtimestamp(ts, timezone.utc)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001 - not running inside Home Assistant (tests): the computer's own zone
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
