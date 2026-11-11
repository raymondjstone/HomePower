# HomePower

A Home Assistant custom integration that is a **central battery manager** for home batteries. It connects to the
batteries **directly** (it does not need, use or depend on any other Anker or EcoFlow integration), shows them in a
sidebar panel, and schedules and optimises how they charge and discharge.

**Contents:** [Install](#install) · [Supported batteries](#supported-batteries) · [Getting started](#getting-started) ·
[How HomePower decides](#how-homepower-decides) · [Schedules](#schedules) · [Goals](#goals) ·
[Tariff and prices](#tariff-and-prices) · [Grid reading](#grid-reading) · [Solar](#solar) ·
[Limits and protection](#limits-and-protection) · [Modes, scenes and overrides](#modes-scenes-and-overrides) ·
[Several batteries](#several-batteries) · [Messages and alerts](#messages-and-alerts) · [The panel](#the-panel) ·
[Settings reference](#settings-reference) · [Entities, services and card](#entities-services-and-card) ·
[Generic Modbus map](#generic-modbus-register-map) · [Savings](#how-savings-are-estimated) ·
[Removing HomePower](#removing-homepower) · [Known limits](#known-limits)

## Install

**HACS (recommended):** HACS > three-dot menu > *Custom repositories* > add `https://github.com/raymondjstone/HomePower`
as an *Integration*, download **HomePower Battery Manager**, restart Home Assistant, then *Settings > Devices & Services >
Add integration > HomePower*. Add batteries from the **HomePower** sidebar panel or the integration's **Configure** menu.

**Manual:** copy `custom_components/homepower` into your Home Assistant `custom_components` folder and restart.

HomePower is set up once per Home Assistant (a second copy cannot be added). Tested on Home Assistant Core 2026.2 and
2026.11.

## Supported batteries

| Battery | How HomePower connects | Modes |
|---|---|---|
| Anker SOLIX Solarbank Max AC / Max, XE AC / XE, Solarbank 4 E5000 Pro, X1 | Local **Modbus TCP** on your network (switch it on in the Anker app: device > Settings > Three-Party Control). HomePower scans your network and finds the battery. | Auto, Charge, Discharge, Hold |
| EcoFlow STREAM AC / Pro / Ultra / Ultra X / Max | EcoFlow's own **developer API**: create an Access Key and Secret Key at developer.ecoflow.com; HomePower lists the batteries on your account. | Auto, Hold |
| EcoFlow STREAM AC 5000 (ES5000) | The same developer API | Monitor only |
| Victron GX | Modbus TCP, register map built in | Monitor only |
| Any Modbus TCP battery | You describe its registers as a small JSON map ([see below](#generic-modbus-register-map)) | Auto, plus Charge/Discharge/Hold if the map has a setpoint and mode register |
| Anything else | Advanced: choose Home Assistant entities that read and control it | Auto, Charge, Discharge, Hold |

The four modes mean: **Auto** = the battery runs its own logic; **Charge** / **Discharge** = at a power HomePower sets;
**Hold** = neither charge nor discharge from the battery's store.

- **EcoFlow limitation:** EcoFlow's developer API has no charge/discharge power setpoint, and the ES5000's own commands are
  not published there. Auto = self-powered. Hold = the backup reserve is raised to the battery's current charge level, so
  it stops discharging and is not told to top up from the grid (its own panels still charge it).
- **Regions:** Anker control is local and works in any region; grid feed-in is off by default in IT/ES/TH/VN/PH. For EcoFlow
  pick your account's region (`api-e.ecoflow.com` Europe, `api-a.ecoflow.com` Americas, `api.ecoflow.com` elsewhere).
- **One controller per battery:** an Anker battery accepts one controlling Modbus client. Switch off any other integration
  or tool that talks to it over Modbus.
- **What has been checked on real hardware:** readings from an Anker E5000 Pro, an EcoFlow STREAM Ultra X and an ES5000.
  Sending commands is tested against simulators only, which is why new batteries start monitor-only.

## Getting started

1. **Add a battery** (panel > Batteries > Add, or Configure > Add battery). Anker batteries are found by a network scan;
   EcoFlow batteries are listed from your account once you enter your keys. Capacity, power limits and minimum charge are
   read from the battery itself where it reports them.
2. **Check the readings.** A new battery is **monitor-only**: HomePower reads and shows it but sends no commands.
3. **Allow control** on the battery when you are ready, then run **Test control** (Diagnostics). The test sends a tiny,
   safe command, checks the battery follows it, and hands it back.
4. **Enter your tariff** (Settings > Tariff & prices) and, if you want peak shaving, the export limit or solar-only
   charging, make sure there is a [grid reading](#grid-reading).
5. **Add schedules or goals.**

The **Setup check** (on Overview until everything is done, always on Diagnostics) lists each of these with a *Fix* or
*Test now* button: batteries reporting, control allowed and tested, prices known, cheap-rate hours confirmed, grid reading
available and live.

## How HomePower decides

Every 10 seconds HomePower reads all batteries, decides what each should do, sends a command only where the battery is not
already doing it, and reads back to confirm. For each battery the first of these that applies wins:

1. **Manual override** ("charge now for 2 hours")
2. **Backup reserve** (backup mode)
3. **Schedule**
4. **Goal plan**
5. **Zero-import battery** (the one battery chosen to hold grid import at 0 W)

Then, for batteries nobody has instructed: **peak shaving** and **export limiting**, **hold-back** (reserve role, save for
peak hours), and finally **hand-back** to the battery's own control. Device limits, charge-level limits and the overall
caps are applied to whatever was decided. Every decision has a visible reason: the *Why* chip on each battery, and the
battery's *target* sensor.

## Schedules

A schedule tells one, several or all batteries to Charge, Discharge, Hold or run on Auto during a time window.

- **When:** start and end time (may cross midnight), days of the week, an optional season (for example 1 Nov to 28 Feb),
  or a single date. *All day* ignores the times.
- **Only while an entity is on:** follow a tariff or on/off entity instead of, or as well as, the clock.
- **Presence:** always, only when home, or only when away.
- **Power and limits:** a power in watts, a charge level to stop charging at, and a reserve to stop discharging at.
- **Share one total across several batteries:** give a total power and HomePower splits it by capacity and charge level.
- **Priority:** when two schedules overlap on a battery the higher priority wins. The Plan tab lists clashes.
- **Where a charge schedule takes its energy from:**
  - *Grid:* charge at the set power.
  - *Solar only:* follow the grid meter and charge only with what would otherwise be exported, never importing to charge.
    Needs a live grid reading; while there is no surplus the battery is held. On batteries that only support Hold
    (EcoFlow) the battery is held so its own panels charge it.
  - *Grid + solar:* the set power, plus any spare solar on top.

Schedules can be added and edited in the panel (Schedules tab) or with the Configure forms.

## Goals

A goal is "be at 90% by 07:00". HomePower works out how much energy is missing and charges in the **cheapest half-hours**
before the deadline, re-planning every cycle as prices and the charge level change.

- **Days** the deadline applies to, which batteries, and an optional cap on charging power.
- **Charge from:** *grid* (cheapest half-hours), *solar only* (surplus, nothing from the grid), or *solar first, then the
  cheapest grid half-hours* for what is still missing. Between solar and grid slots the battery is left to its own control,
  so it can still power the house.
- **Use the solar forecast:** charge less from the grid when tomorrow's forecast (Solcast / Forecast.Solar) is good.
- **Work the target out from my usual use:** HomePower learns your house's load for each half-hour (weekdays and weekends
  separately) and charges only what the coming day is expected to need, never above the target you set. Learning takes a
  few days; until then the target you set is used.
- **Export price:** when you are paid more to export than it costs to buy the energy back at your cheapest rate, solar
  goals leave the surplus to export.

The Plan tab shows the goals, a **price chart for the next 24 hours** with the half-hours HomePower will charge in, and a
timeline of what each battery will do.

## Tariff and prices

Prices are needed for goals, savings and the cheap-rate features. Any of these work:

- **A rates sensor** with a list of `{start, end, value}` in its attributes (Octopus, Agile, Nord Pool...) or a plain
  "price now" sensor. The Hildebrand Glow rate sensor is found automatically.
- **A simple two-rate tariff:** type the standard rate, the cheap rate and when the cheap rate starts and ends. Quick-fill
  buttons cover Octopus Go, Intelligent Octopus Go, Economy 7, 23:30 - 07:30 and Cosy Octopus.
- **Several cheap periods a day:** add more windows, for example `13:00-16:00, 22:00-00:00`.
- **Worked out from history:** if you leave the times empty, HomePower estimates the cheap period from two weeks of the
  rates sensor's history. Such sensors can run up to an hour late, so this is an estimate (the end is pulled back half an
  hour to be safe). Times you type always win.

Rates in pence/cents or pounds/euros are detected automatically, or you can say which.

**Save the battery for peak hours** (optional) holds the batteries during cheap-rate periods so their charge is kept for
the expensive hours. It never holds while solar is producing.

## Grid reading

Peak shaving, the export limit, solar-only charging, the zero-import battery and savings need to know what the house is
importing or exporting. In **Settings > Grid meter** you choose which sources to use and in what order; the first one with
a reading is used, and an unticked one is never used:

- **Your own sensor:** an import sensor, optionally an export sensor to subtract. Each may be a power sensor (W/kW) or a
  cumulative energy counter (Wh/kWh). Tick *positive when exporting* if it is the other way round.
- **A battery that measures the whole house.** Each battery has a setting for this. An Anker X1 sits at the grid connection
  and does; plug-in batteries normally only measure themselves and are not used unless you say so.
- **Hildebrand Glow**, found automatically (smart-meter data, which can run late).
- **EcoFlow's cloud figure**, as a last resort.

Settings shows which source is in use. The setup check warns when the reading is more than two minutes old, and a reading
more than five minutes old is not used to steer solar charging.

## Solar

- **Panels per battery:** each battery can be marked as having solar panels (yes / no / detect automatically when it
  reports solar power). Its card shows the solar power.
- **Solar-only and grid + solar charging** for schedules and goals, described above.
- **Solar forecast** for goals (Settings > Solar forecast), with the share of forecast solar that reaches the battery.
- **Solar-surplus load:** switch on a chosen switch (an immersion heater, for example) while the home battery is full and
  you are exporting more than a set power, and off again when it would need grid power. It is released while HomePower is
  paused.

## Limits and protection

- **House import limit:** caps total charging so charging never pushes import over it.
- **Peak shaving:** with it on, batteries also discharge to keep whole-house import under the limit.
- **Export limit (G98/G99):** trims discharge, then charges, to stay under an export limit.
- **Overall caps** on total charge and total discharge power across all batteries.
- **Charge-level limits:** each battery's minimum charge is respected with a margin so it does not flap on and off.
- **Protect loads:** don't charge and/or discharge while a chosen entity is on or above a power (EV charger, heat pump...).
- **Manufacturer protection:** if a battery reports its own protection is active, HomePower does not override it and
  raises an alert.
- **Commands are verified** by reading back, and not re-sent more often than the re-apply cooldown.
- **Hand-back:** a battery HomePower forced is handed back to its own control when it is no longer managed, when control is
  switched off for it, when it is deleted, when HomePower is reloaded or removed, on shutdown, and after a restart.
- **Watchdog:** on batteries with a communications watchdog (X1) it is fed while the battery is forced, so the battery
  reverts by itself if Home Assistant stops.
- **If the decision logic ever fails**, no commands are sent that cycle and a note says so.

## Modes, scenes and overrides

- **Pause:** HomePower keeps reading but sends no commands.
- **Backup mode:** tops up to the backup reserve and never discharges below it. Manual, or automatically while a chosen
  entity (a weather alert, for example) is on.
- **Away mode:** only schedules marked "away" (or "always") apply. Manual, or from a person / tracker / on-off entity.
- **Manual override:** force a mode on some or all batteries for a set time, with an optional power and stop level. It beats
  everything else and ends by itself.
- **Scenes:** saved combinations of backup / away / pause and limits. *Normal*, *Holiday*, *Storm coming* and *Hands off*
  come built in; add your own. One tap in the panel, or the `homepower.apply_scene` service.

## Several batteries

- **Home battery view:** one card on Overview (and the optional dashboard card) shows all batteries as one: total charge,
  stored and usable energy, flow, and how long it will last or take to fill.
- **Priority:** lower-priority-number batteries discharge first.
- **Reserve battery:** give a battery the *Reserve* role and it is held back until the everyday batteries have run down.
  Needs control allowed and a battery that supports Hold; solar still charges it.
- **Zero-import battery:** one battery can be chosen to keep grid import at 0 W whenever nothing else applies to it.
- **Shared schedule power**, split by capacity and charge level.

## Messages and alerts

All appear as Home Assistant notifications, and optionally go to a `notify.*` service as well.

- **Alerts:** a battery is offline, a command failed, a battery is not following commands, manufacturer protection is
  active, or a battery's round-trip efficiency has dropped well below its usual. Each clears itself when resolved.
- **Heads-ups (optional):** the home battery is full, the charge has fallen to a level you choose, or the cheap rate starts
  in 30 minutes and the battery is below a level you choose.
- **Daily summary (optional):** one message at a time you choose with the battery level, yesterday's charging and
  discharging, estimated savings, solar self-consumption and cheap-rate share.

## The panel

**Sidebar > HomePower** (administrators only):

| Tab | What is there |
|---|---|
| Overview | Live state of every battery with its *Why*, the home battery card, grid power, scenes, quick overrides, tariff notice, setup check, and a live power chart with a time scale you can change (15 minutes to 24 hours) |
| Plan | Goals, the price and planned-charging chart, the next 24 hours per battery, schedule clashes |
| Schedules | Add, edit, enable and delete schedules |
| Batteries | Add, edit and delete batteries; allow control; role, priority, solar panels, whether its meter measures the whole house |
| Insights | Savings today and this month, energy totals, solar self-consumption and cheap-rate share, battery health (weekly round-trip efficiency), long-term history charts |
| Settings | Everything in the [settings reference](#settings-reference) |
| Diagnostics | Setup check, per-battery status and last command, Test control, the raw readings received, event log, copy debug info |

**Settings > Devices & Services > HomePower > Configure** offers the essentials as native Home Assistant forms, with no
panel needed: add and remove batteries, allow control, add / change / remove schedules, add and remove goals, and the
common settings.

**Backup & restore** (panel > Settings) saves batteries, schedules, goals, scenes and settings to a file and restores them.
EcoFlow keys are only included if you tick the box.

## Settings reference

| Section | Settings |
|---|---|
| Grid meter | Import sensor, export sensor, positive-when-exporting, order of grid reading sources |
| Limits | House import limit, peak shaving, export limit, max total charge, max total discharge, re-apply cooldown |
| Zero-import battery | Which battery keeps import at 0 W |
| Tariff & prices | Quick-fill presets, rates sensor, rates unit, standard rate, cheap rate, export price, estimate the cheap period from history, cheap-rate start and end, more cheap periods, save the battery for peak hours, currency symbol |
| Solar forecast | Tomorrow's forecast sensor, share that reaches the battery |
| Backup & away | Backup reserve, entity that turns backup mode on, away entity |
| Protect loads | Entities that block charging and/or discharging while busy |
| Alerts & heads-ups | Extra notify service, battery full, battery low level, cheap-rate-soon warning and its level, daily summary time |
| Solar surplus | The switch to run, export needed, minimum home battery charge |
| EcoFlow | Live feed (see below) |
| Backup & restore | Save to file, restore from file |
| If you remove HomePower | Erase everything when HomePower is deleted |

**EcoFlow live feed (opt-in):** makes EcoFlow batteries use EcoFlow's MQTT feed with the same keys, so readings are seconds
old instead of about 30 seconds. It is off by default because it opens a second connection on your EcoFlow account, which
could disturb another EcoFlow integration such as EcoFlow Cloud. If it cannot connect, normal polling continues. It has not
been tested against a live EcoFlow account.

## Entities, services and card

**Sensors** (on one *HomePower* device):

- Whole system: battery power, stored energy, grid power, electricity price now, savings today, savings this month, home
  battery (all batteries as one, with totals in its attributes), solar self-consumption today, cheap-rate share of grid
  import today.
- Energy counters for the **Energy dashboard**: grid energy imported, grid energy exported, solar energy.
- Per battery: charge, power, target (what HomePower wants it to do, with the reason in its attributes), energy charged,
  energy discharged, round-trip efficiency, cycles.

**Switches:** Pause control, Backup mode, Away mode.

**Services:**

| Service | What it does |
|---|---|
| `homepower.override` | Force a mode (`auto`, `charge`, `discharge`, `hold`) on some or all batteries for a number of minutes, with optional power and stop level |
| `homepower.clear_override` | Go back to schedules |
| `homepower.pause` / `homepower.resume` | Stop / resume sending commands |
| `homepower.backup` | Backup mode on or off |
| `homepower.away` | Away mode on or off |
| `homepower.apply_scene` | Switch to a saved scene by name |
| `homepower.test_control` | Run the control test on a battery and return the result |

**Dashboard card (optional):** `type: custom:homepower-card` shows the home battery. It finds the sensor by itself;
`entity:`, `title:` and `show_batteries:` are optional.

**Diagnostics download** on the integration page, with keys, addresses and serial numbers removed.

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
from the grid (all of it for scheduled / goal / override charging, none of it for solar-only charging, otherwise estimated
from the grid reading). It is an estimate, not a bill.

## Removing HomePower

Deleting HomePower in Settings > Devices & services hands any battery it is controlling back to its own control. Your
batteries, EcoFlow keys, schedules, goals, scenes and history stay saved, so adding HomePower again restores everything.
To erase all of it instead, tick **Erase everything when HomePower is deleted** (HomePower panel > Settings > If you remove
HomePower) before deleting. Home Assistant cannot ask at the moment of deleting, so the choice is made there.

## Known limits

- Sending commands has only been run against simulators, not real hardware. New batteries are monitor-only until you allow
  control; run Test control before relying on it.
- EcoFlow batteries can only be set to Auto or Hold, and the ES5000 is read-only.
- If Home Assistant is hard-killed (power cut) while a battery is being forced, it stays in that mode until HomePower
  next starts and hands it back (X1 also has its own watchdog).
- Goals plan with the prices known at the time; they re-plan every cycle as prices and charge level change.
- Solar-only charging is only as good as the grid reading: a slow or late reading makes it track the surplus poorly.

## License

MIT, see [LICENSE](LICENSE).

## Releases

- Work goes to the **`develop`** branch. Every push there runs the tests and publishes a **pre-release** named
  `v<version>-beta.<n>` (in HACS: *Redownload > Show beta versions*). Only the newest five pre-releases are kept.
- Merging `develop` into **`master`** (a pull request) publishes the real release `v<version>` and removes that version's
  pre-releases.
- `<version>` is `"version"` in `custom_components/homepower/manifest.json`. After each release the workflow moves `develop`
  on by one patch number by itself (pull before you carry on working); edit the number yourself for a bigger jump. A
  pre-release build fails with a clear message if the version is not newer than the latest release.

## Development

```
python -m pip install pytest
python -m pytest
```
`custom_components/homepower/core/` must stay free of `homeassistant` imports so it tests without HA. The decision logic
is in `core/engine.py` (one pure function, fuzz-tested against safety invariants). Checks against the real Home Assistant
classes are in `tests/ha_harness/` (see its README). See [docs/DESIGN.md](docs/DESIGN.md).
