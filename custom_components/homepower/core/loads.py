"""Big loads the batteries should not be emptied into (HA-free): EV chargers, heat pumps, immersion heaters.

HomePower's load rules already stop charging or discharging while such a load runs. This finds the power sensors of the
common chargers and heat pumps so the setup check can point out one that no rule covers yet."""
from __future__ import annotations

import re

# words in an entity id that name a big, shiftable load (EV chargers by brand, evcc, heat pumps, immersion diverters)
_BIG = re.compile(r"(^|[._])(evcc|zappi|eddi|ohme|wallbox|easee|hypervolt|myenergi|pod_?point|go_?e|andersen|indra|"
                  r"ev_?charger|charge_?point|wallbox|car_?charger|heat_?pump|heatpump|ashp|immersion)([._]|$)")
_POWER_UNITS = ("W", "kW")
_NOT = re.compile(r"(energy|kwh|total|today|session|cost|limit|max|min|setpoint|target)")


def big_load_sensors(entities, rules) -> list[str]:
    """`entities`: (entity_id, device_class, unit) of the sensors in Home Assistant. Returns the power sensors that look like
    an EV charger or heat pump and are not yet in a load rule (by entity, or by the same device prefix)."""
    covered = {str(r.get("entity") or "") for r in rules or [] if isinstance(r, dict)}
    prefixes = {c.split(".", 1)[-1].rsplit("_", 1)[0] for c in covered if c}
    out = []
    for eid, dclass, unit in entities:
        if not isinstance(eid, str) or not eid.startswith("sensor.") or eid in covered:
            continue
        if dclass not in (None, "power") or unit not in _POWER_UNITS:
            continue
        name = eid.split(".", 1)[1].lower()
        if not _BIG.search(name) or _NOT.search(name):
            continue
        if any(p and name.startswith(p) for p in prefixes):
            continue                     # another sensor of the same charger already has a rule
        out.append(eid)
    return sorted(out)[:5]
