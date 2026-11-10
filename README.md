# HomePower

A Home Assistant custom integration that acts as a **central battery manager** for:

| Device | Control path (via existing HA integration) |
|---|---|
| Anker SOLIX X1 | Local Modbus TCP (community integration) |
| Anker SOLIX E5000 (Solarbank 4 Pro) | Local Modbus TCP (official Anker integration) |
| EcoFlow STREAM Ultra X | EcoFlow cloud API / MQTT (community integration) |
| EcoFlow ES5000 (STREAM AC 5000 / ES22 — *to confirm*) | EcoFlow Energy integration |

HomePower does **not** reimplement the vendor protocols. Each device gets an adapter that drives the
entities of the upstream integration; the scheduling, coordination and settings-protection logic lives here.

## Status: stage 0 (scaffold)

- [x] HA-free core: schedule resolution, desired-vs-actual reconcile, limit clamping, cheap-rate charge allocator (tested)
- [x] Generic entity-driven adapter, control loop (read -> decide -> apply -> verify -> log)
- [x] Config flow (global import limit + re-apply cooldown)
- [x] Overall charge / discharge caps: total power across all batteries and all schedules combined (lower `priority` number keeps its power, others are trimmed, then HOLD)
- [x] Zero-import battery: pick one battery (options flow); when no schedule applies it discharges just enough to hold grid import at 0 W and charges with solar surplus when exporting (never from the grid; HOLD when full), ignores other batteries' deliberate charging, respects SOC floor, falls back to HOLD if the grid sensor is unavailable
- [ ] Stage 1: device config UI (entity maps per device), HA entities/sensors, dashboard
- [ ] Stage 2: persistent schedules (`Store`), services, calendar-style editor, tariff window from HA
- [ ] Stage 3: multi-battery optimisation, solar-surplus, peak avoidance
- [ ] Optional: Windows C# management app over HA WebSocket/REST

See [docs/DESIGN.md](docs/DESIGN.md).

## Development

```
python -m pip install pytest
python -m pytest
```
`custom_components/homepower/core/` must stay free of `homeassistant` imports so it tests without HA.
