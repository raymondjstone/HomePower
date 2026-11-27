# Design

## Principles
- **One controller.** HomePower owns the settings it manages; no other automation should write them at the same time
  (Modbus devices often allow a single client at a time, which is also why a failed read is first shown as "Readings stale").
- **Read before write, verify every command.** An accepted request is not proof the battery acted on it: every command is
  read back, and slow (cloud) batteries are watched for minutes before a command counts as followed.
- **Desired vs actual.** Per battery: what HomePower wants, what the battery reports, the last command and its result.
- **Manufacturer protection wins.** Calibration / protection / grid-down states are never overridden
  (`Decision.SKIP_PROTECTED`); re-applies are rate-limited and capped per day.
- **Device-specific limits.** Every command is clamped to that battery's `Capabilities`.
- **Fail safe.** An offline battery gets no writes; a failure in one battery, one step or one view never stops the others.
- **Hand back exactly what was there.** Before its first write HomePower reads and saves the battery's own settings and
  puts exactly those back; when the original is unknown it leaves the battery alone rather than write a default.
- **Nothing lands after a hand-back.** Sending commands and handing back take the same lock; a cycle re-checks pause,
  safe mode and unload before it sends, and safe mode / the hand-back button pause first, then hand back.
- **Export protection is not a substitute for certified G100 hardware.**

## Layers
```
panel (frontend/*.js)  <- websocket API (websocket_api.py, admin only) ->  manager (stored config, flows, backup)
                                                                              |
                     coordinator (coordinator.py + coord_*.py): the 10 s control loop
                     read all batteries -> decide (core/engine.py, pure) -> send and verify commands
                                                                              |
                     DeviceAdapter (adapters/)  ->  drivers/: Anker Modbus TCP, EcoFlow cloud API (+ optional MQTT live
                                                    feed), EcoFlow local Modbus, generic Modbus map, or Home Assistant
                                                    entities of another integration ("Other battery")
```
- `core/` is pure Python with no Home Assistant imports, so the decision logic is unit- and fuzz-tested on its own.
- A loaded HomePower keeps its manager and coordinator on its config entry (`runtime.py`, `entry.runtime_data`).
- Actions are registered in `async_setup`; the sidebar panel and the panel's websocket API are admin only.

## How a cycle decides
Highest priority first (`core/engine.py`): manual override > backup reserve > grid event > schedule (`core/schedule.py`) >
goal plan (charge to X % by a time in the cheapest slots: `core/planner.py`) > zero-import battery > paid export; then,
for batteries still without an instruction, peak shaving / export limiting, and finally handing back the ones HomePower
forced earlier. The day plan (`core/dayplan.py`: where stored energy is worth most until refilling is cheapest) decides
the paid-export half-hours and when to hold charge for dearer hours later. Safety clamps always apply:
device limits, charge-level limits with hysteresis, load rules and the overall caps. Batteries that pick their own rate
(EcoFlow's cloud) get Hold / Charge through their backup reserve, with the measured charge rate used in plans.

## Open questions
- Hardware: commands have been run against simulators and published register maps only; Test control and the hardware
  report are how real batteries get proven (README, "Known limits").
- EcoFlow STREAM 5000 series over the cloud: the command set is taken from EcoFlow's document but untested on hardware.
- EcoFlow local Modbus: the sign of the power setpoint is assumed (+ charging); Test control checks it.
