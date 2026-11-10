# HomePower

A Home Assistant custom integration that is a **central battery manager** for home batteries. It connects to the
batteries **directly** (it does not need, use or depend on any other Anker or EcoFlow integration), shows them in a
sidebar panel, and schedules and optimises how they charge and discharge.

## Supported batteries

| Battery | How HomePower connects | Modes |
|---|---|---|
| Anker SOLIX Solarbank Max AC / Max, XE AC / XE, Solarbank 4 E5000 Pro, X1 | Local **Modbus TCP** on your network (switch it on in the Anker app: device > Settings > Three-Party Control). HomePower scans your network and finds the battery. | Auto, Charge, Discharge, Hold |
| EcoFlow STREAM AC / Pro / Ultra / Ultra X / Max (and AC 5000, model to confirm) | EcoFlow's own **developer API**: create an Access Key and Secret Key at developer.ecoflow.com; HomePower lists the batteries on your account. | Auto, Hold only |
| Victron GX (read-only) | Modbus TCP, register map built in | Monitor only |
| Any Modbus TCP battery | You describe its registers as a small JSON map (see below) | Auto, plus Charge/Discharge/Hold if the map has a setpoint and mode register |
| Anything else | Advanced: choose Home Assistant entities that read and control it | Auto, Charge, Discharge, Hold |

New batteries start in **monitor-only** mode: HomePower reads and shows them but sends no commands until you allow
control (and **Test control** in Diagnostics checks the battery really follows commands). Capacity, power limits and
minimum charge are read from the battery itself where it reports them.

**EcoFlow limitation:** EcoFlow's API has no charge/discharge power setpoint. Auto = self-powered; Hold = backup reserve
raised to 95% (no discharge; solar still charges).

**Regions:** Anker control is local (works in any region); grid feed-in is off by default in IT/ES/TH/VN/PH. For EcoFlow
pick your account's region (`api-e.ecoflow.com` Europe, `api.ecoflow.com` global/US).

**One controller per battery:** an Anker battery accepts one controlling Modbus client. Switch off any other integration
or tool that talks to it over Modbus.

**Unverified on hardware:** the Anker register map and EcoFlow API calls follow the vendors' published behaviour and are
tested against simulators only. Start monitor-only and check the readings.

## What it does

**Control** (priority, highest first): manual override > backup reserve > schedule > goal plan > zero-import battery;
then peak shaving / export limiting for batteries nobody has instructed; then hand-back to the battery's own control.
Every decision has a visible reason ("Why" chip on each battery).

- **Schedules:** time windows (overnight, weekdays, seasons, one-off dates), priority, follow a tariff/on-off entity,
  *presence* (home/away), and **share one total across several batteries** (split by capacity and charge level).
- **Goals ("be at 90% by 07:00"):** HomePower charges in the cheapest half-hours to get there. Prices come from a rates
  entity (Octopus, Agile, Nord Pool: anything with a list of `{start, end, value}` attributes) or a simple cheap-rate
  tariff. Optionally charge less when tomorrow's **solar forecast** (Solcast / Forecast.Solar) is good.
- **Peak shaving:** with an import limit set, batteries discharge to keep whole-house import under it.
- **Export limit (G98/G99):** trims discharge, then charges, to stay under an export limit.
- **Backup mode:** tops up to a reserve and never discharges below it (manual, or while a weather-alert entity is on).
- **Away mode:** only schedules marked for "away" (or "always") apply.
- **Protect loads:** don't charge/discharge while an EV charger or heat pump is busy.
- **Manual override:** "charge now for 2 hours", beats everything else.
- **Safety:** device limits always apply; software SOC limits with hysteresis; overall charge/discharge caps; house import
  limit caps charging; pause switch; batteries we forced are handed back when no longer managed, on shutdown, and after
  a restart; commands are verified by reading back; a hardware comms watchdog (X1) is fed while forced.
- **Alerts:** offline, failed or unconfirmed commands, manufacturer protection: as Home Assistant notifications and
  optionally to a `notify.*` service.

## Where to find things

- **Sidebar > HomePower** (admin): Overview (live), Plan (goals + the next 24 hours + schedule clashes), Schedules,
  Batteries, Insights (savings, energy, long-term history), Settings, Diagnostics (per-battery status, Test control,
  copy debug info).
- **Settings > Devices & Services > HomePower > Configure:** add batteries, schedules and goals and change the common
  settings with native Home Assistant forms (no panel needed).

## Home Assistant entities and services

- Sensors: battery power, stored energy, grid power, electricity price now, savings today / this month (estimate), and per
  battery: charge, power, target ("why"), energy charged / discharged (kWh, usable in the **Energy dashboard**),
  round-trip efficiency, cycles.
- Switches: **Pause control**, **Backup mode**, **Away mode**.
- Services: `homepower.override`, `clear_override`, `pause`, `resume`, `backup`, `away`, `test_control`.
- Diagnostics download on the integration page (secrets redacted).

## Generic Modbus register map

```json
{
  "soc":   {"addr": 843, "type": "u16", "fc": 3},
  "power": {"addr": 842, "type": "i16", "fc": 3, "positive_is": "charging"},
  "grid":  {"addrs": [820, 821, 822], "type": "i16", "fc": 3, "positive_is": "import"},
  "setpoint": {"addr": 10071, "type": "i32", "positive_is": "discharge"},
  "mode":     {"addr": 10064, "control_value": 3, "auto_value": 0}
}
```
`soc` and `power` are required. `setpoint` + `mode` are optional and must come together; without them the device is
read-only. Types: `u16 i16 u32 i32`; `scale` multiplies the raw value; `word_order` is `big` (default) or `little`.

## How savings are estimated

Savings = value of discharged energy at the price then (avoided import) minus the cost of the part of charging that came
from the grid (all of it for scheduled/goal/override charging, otherwise estimated from the grid reading). Rates in
pence/cents or pounds/euros are detected automatically. It is an estimate, not a bill.

## Known limits

- The Anker Modbus and EcoFlow API drivers have only been run against simulators, not real hardware. New batteries are
  monitor-only until you allow control.
- If Home Assistant is hard-killed (power cut) while a battery is being forced, it stays in that mode until HomePower
  next starts and hands it back (X1 also has its own watchdog).
- Goals plan with the prices known at the time; they re-plan every cycle as prices and charge level change.

## Development

```
python -m pip install pytest
python -m pytest
```
`custom_components/homepower/core/` must stay free of `homeassistant` imports so it tests without HA. The decision logic
is in `core/engine.py` (one pure function, fuzz-tested against safety invariants). Checks against the real Home Assistant
classes are in `tests/ha_harness/` (see its README). See [docs/DESIGN.md](docs/DESIGN.md).
