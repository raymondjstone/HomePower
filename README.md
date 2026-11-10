# HomePower

A Home Assistant custom integration that acts as a **central battery manager** for:

| Battery | Upstream HA integration (HomePower drives its entities) | Modes HomePower can use |
|---|---|---|
| Anker SOLIX Solarbank Max AC / Max, XE AC / XE, Solarbank 4 E5000 Pro | [anker-charging/ha-anker-solix-official](https://github.com/anker-charging/ha-anker-solix-official) (local Modbus TCP) | Auto, Charge, Discharge, Hold |
| Anker SOLIX X1 | [afewyards/anker-x1-ha](https://github.com/afewyards/anker-x1-ha) or [cbource/anker-solix-x1-modbus](https://github.com/cbource/anker-solix-x1-modbus) (local Modbus TCP) | Auto, Charge, Discharge, Hold |
| EcoFlow STREAM AC / Pro / Ultra / Ultra X, STREAM AC 5000 (ES5000, to confirm) | [tolwi/hassio-ecoflow-cloud](https://github.com/tolwi/hassio-ecoflow-cloud) | Auto, Hold only |
| EcoFlow STREAM Ultra X / Max | [htims1989/ecoflow-public-api](https://github.com/htims1989/ecoflow-public-api) | Auto, Hold only |
| Anything else with a mode select + power number | any | Auto, Charge, Discharge, Hold |

**EcoFlow limitation:** those integrations expose no charge/discharge power setpoint. Auto = self-powered; Hold = backup reserve raised to 95% (no discharge; solar still charges). Cheap-rate grid charging is not possible on STREAM through them. Unverified on hardware.

**Regions:** HomePower only uses HA entities, so it works wherever the upstream integration does. Use the matching EcoFlow API host (`api-e.ecoflow.com` EU, `api.ecoflow.com` global/US). Anker grid feed-in is off by default in IT/ES/TH/VN/PH.

HomePower does **not** reimplement the vendor protocols. Each device gets an adapter that drives the
entities of the upstream integration; the scheduling, coordination and settings-protection logic lives here.

## Status: stage 0 (scaffold)

- [x] HA-free core: schedule resolution, desired-vs-actual reconcile, limit clamping, cheap-rate charge allocator (tested)
- [x] Generic entity-driven adapter, control loop (read -> decide -> apply -> verify -> log)
- [x] Config flow (global import limit + re-apply cooldown)
- [x] Overall charge / discharge caps: total power across all batteries and all schedules combined (lower `priority` number keeps its power, others are trimmed, then HOLD)
- [x] Zero-import battery: pick one battery (options flow); when no schedule applies it discharges just enough to hold grid import at 0 W and charges with solar surplus when exporting (never from the grid; HOLD when full), ignores other batteries' deliberate charging, respects SOC floor, falls back to HOLD if the grid sensor is unavailable
- [x] Stage 1: **HomePower sidebar panel** (admin only): live overview (SOC rings, power, grid, 30-min chart, today's plan timeline, activity log), add/edit batteries with entity auto-fill, settings
- [x] Stage 2: persistent batteries + schedules (`Store`), visual schedule editor (days, overnight, season, one-off date, priority)
- [x] Tariff windows: a schedule can follow any HA on/off entity (cheap-rate sensor), optionally all day
- [x] HA entities + services: `sensor` totals and per-battery target; `homepower.pause/resume/override/clear_override`
- [x] Safety: batteries are handed back to their own control when a schedule ends or HA shuts down (persisted across restarts); software SOC limits with hysteresis; house import limit caps total charging; pause switch
- [ ] Stage 3: multi-battery optimisation, solar-surplus, peak avoidance
- [ ] Optional: Windows C# management app over HA WebSocket/REST

See [docs/DESIGN.md](docs/DESIGN.md).

## Services and sensors

- `sensor.homepower_battery_power` (+ charging / - discharging), `sensor.homepower_stored_energy`, and a `<battery> target` sensor per battery (state = auto/charge/discharge/hold/unmanaged/paused, attributes explain why).
- `homepower.override` (mode, power_w, target_soc, minutes, device_ids), `homepower.clear_override`, `homepower.pause`, `homepower.resume`.

## Known limits

- Entity names and option strings come from reading each upstream integration's source; none of it has been run against real hardware yet.
- If Home Assistant is hard-killed (power cut) while a battery is being forced, it stays in that mode until HomePower next starts and releases it. The X1's own comms watchdog (upstream `set_watchdog` service) is not configured by HomePower.

## Development

```
python -m pip install pytest
python -m pytest
```
`custom_components/homepower/core/` must stay free of `homeassistant` imports so it tests without HA.
