# HomePower

A Home Assistant custom integration that acts as a **central battery manager** for:

HomePower connects to your batteries **directly**. It does not need, use or depend on any other Anker or
EcoFlow integration (the open-source ones were only a reference for the protocols).

| Battery | How HomePower connects | Modes |
|---|---|---|
| Anker SOLIX Solarbank Max AC / Max, XE AC / XE, Solarbank 4 E5000 Pro, X1 | Local **Modbus TCP** on your network (switch it on in the Anker app: device > Settings > Three-Party Control). HomePower scans your network and finds the battery. | Auto, Charge, Discharge, Hold |
| EcoFlow STREAM AC / Pro / Ultra / Ultra X / Max (and AC 5000, model to confirm) | EcoFlow's own **developer API**: create an Access Key and Secret Key at developer.ecoflow.com; HomePower lists the batteries on your account. | Auto, Hold only |
| Anything else | Advanced: choose Home Assistant entities that read and control it. | Auto, Charge, Discharge, Hold |

New batteries start in **monitor-only** mode: HomePower reads and shows them but sends no commands until you allow control.

**EcoFlow limitation:** EcoFlow's API has no charge/discharge power setpoint. Auto = self-powered; Hold = backup reserve raised to 95% (no discharge; solar still charges).

**Regions:** Anker control is local (works in any region); grid feed-in is off by default in IT/ES/TH/VN/PH. For EcoFlow pick your account's region (`api-e.ecoflow.com` Europe, `api.ecoflow.com` global/US).

**Unverified on hardware:** the Anker register map and EcoFlow API calls follow the vendors' published behaviour and are tested against simulators only. Use monitor-only first and check the readings.

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

- The Anker Modbus and EcoFlow API drivers have only been run against simulators, not real hardware. New batteries are monitor-only until you allow control.
- If Home Assistant is hard-killed (power cut) while a battery is being forced, it stays in that mode until HomePower next starts and releases it. The X1's own comms watchdog (upstream `set_watchdog` service) is not configured by HomePower.

## Development

```
python -m pip install pytest
python -m pytest
```
`custom_components/homepower/core/` must stay free of `homeassistant` imports so it tests without HA.
