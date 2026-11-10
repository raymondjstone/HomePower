# Design

## Principles
- **One controller.** HomePower owns the settings it manages; no other automation writes them (Modbus allows a single controlling client).
- **Read before write, verify every command.** An API 200 is not proof the inverter accepted it.
- **Desired vs actual.** Per device: desired config, actual config, last command, last readback, history.
- **Manufacturer protection wins.** Calibration/protection/firmware states are never overridden (`Decision.SKIP_PROTECTED`); re-applies are rate-limited.
- **Device-specific limits.** Every command is clamped to that device's `Capabilities`.
- **Fail safe.** Offline device => no writes; loss of comms must not cause uncontrolled charge/discharge.
- **Export protection is not a substitute for certified G100 hardware.**

## Layers
```
schedules / strategies / tariff window  ->  core (pure Python)
                                          |  Desired (neutral)
                              DeviceAdapter (HaEntityAdapter)
                                          |  select / number service calls
                    upstream HA integrations (Anker Modbus, EcoFlow API/MQTT)
```

## Schedule types
1. Time-based (weekday/weekend, seasonal, date overrides, priority) — `core/schedule.py` (done)
2. SOC-based (floors, targets) — via `Desired.reserve_soc` / `target_soc` (model done, rules TODO)
3. Tariff-based — `core/allocator.py` spreads charge across batteries within the import limit (done); window source from HA TODO

## Strategies (stage 3)
Cheap-rate charging, solar harvesting, peak avoidance, reserve protection, max self-sufficiency, manual override.
Device priorities decide discharge order; topology/shared-bus awareness needed.

## Open questions
- Is the "ES5000" the STREAM AC 5000 (ES22)? Controls differ by model.
- Some EcoFlow commands write a whole-day task rather than a mode; charge-setpoint behaviour unproven — needs validation on hardware.
- Exact entity names/mode option strings per upstream integration (to be captured as per-device profiles).
- Cheap-rate window source (Octopus/HA entity?) and house import limit.
