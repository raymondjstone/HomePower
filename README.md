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
| EcoFlow STREAM AC / Pro / Ultra / Ultra X / Max | EcoFlow's own **developer API**: create an Access Key and Secret Key at developer.ecoflow.com; HomePower lists the batteries on your account. | Auto, Hold, Charge (on/off, own rate) |
| EcoFlow STREAM AC 5000 (ES5000, serial ES21/ES22) | The same developer API (EcoFlow's "STREAM 5000 Series" settings) | Auto, Hold, Charge (experimental) |
| Victron GX | Modbus TCP, register map built in | Monitor only |
| Any Modbus TCP battery | You describe its registers as a small JSON map ([see below](#generic-modbus-register-map)) | Auto, plus Charge/Discharge/Hold if the map has a setpoint and mode register |
| Anything else | Advanced: choose Home Assistant entities that read and control it | Auto, Charge, Discharge, Hold |

The four modes mean: **Auto** = the battery runs its own logic; **Charge** / **Discharge** = at a power HomePower sets;
**Hold** = neither charge nor discharge from the battery's store.

- **EcoFlow limitation:** EcoFlow's developer API has no charge/discharge power setpoint, and the ES5000's own commands are
  not published there. Auto = self-powered. Hold = the backup reserve is raised to the battery's current charge level, so
  it stops discharging and is not told to top up from the grid (its own panels still charge it). Charge = the reserve is
  raised to the target (95 % at most), so the battery tops itself up from the grid at its own rate: the power you set is
  not sent, and solar-only / follow-the-meter charging holds the battery instead of grid-charging it. There is no force discharge.
- **Regions:** Anker control is local and works in any region; grid feed-in is off by default in IT/ES/TH/VN/PH. For EcoFlow
  pick your account's region (`api-e.ecoflow.com` Europe, `api-a.ecoflow.com` Americas, `api.ecoflow.com` elsewhere).
- **Anker's X1 register list:** Anker publishes the X1's Modbus registers as [Anker SOLIX X1 Series Modbus Protocol V1.0.0](https://salesforce-knowledge-download.s3.us-west-2.amazonaws.com/000029136/en_US/000029136.pdf) (PDF). HomePower's X1 support and the register compare names are based on it; on a real X1 its 32-bit numbers come low word first and its text registers hold their characters low byte first, which HomePower handles.
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
- **Leave room for the solar forecast** (on by default). Batteries with panels are charged overnight only as far as the
  next day's solar allows, so the sun never has to be wasted on a full battery:
  - The day's forecast (Forecast.Solar or Solcast, found automatically, or a "tomorrow" sensor you pick) is spread over
    the hours between sunrise and sunset (worked out from your home location, as the Sun integration does), most at
    midday.
  - From the end of the cheap rate, the house's **base load** (Settings, in watts) is taken from the batteries outside
    cheap-rate hours, and from solar first once the sun is up. So the hours between the cheap rate ending and the sun
    getting strong use some of the battery, which makes room for the afternoon's solar.
  - HomePower works through the day in 15-minute steps and picks the **highest charge level at which the battery never
    goes over 100%**. A battery cannot go below its minimum charge; from there the grid runs the house.
  - The forecast is shared between the batteries with panels by capacity (10 kWh and 5 kWh batteries get 2/3 and 1/3); the
    base load is shared between all batteries by capacity.
  - Grid charging from schedules and goals stops at that level (the reason says "room left for solar"), and a battery
    charging itself from the cheap rate (its own time-of-use setting) is held there. Manual overrides and backup mode are
    not limited. The battery card shows "Room for solar: up to X%", and Settings > Solar forecast explains the figures.
  - Without a forecast, solar counts as zero and batteries charge fully; the setup check suggests adding Forecast.Solar or
    Solcast. Only batteries marked as having panels (or that have reported solar power) are limited.
- **Solar forecast for goals:** a goal can also charge less when tomorrow's forecast is good, using the share of forecast
  solar that reaches the battery.
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
- **Test run (dry run):** Settings > Test run. HomePower works out its plan as normal but sends nothing; each command it would have sent is listed under Events as `DRY RUN: would send ...`. Unlike Pause, the plan keeps being worked out, so it is the safe way to try new schedules, goals or a new battery.
- **Grid down (Anker X1):** when an X1 reports the grid is lost it is running the house itself, so HomePower sends it no commands until the grid is back, shows a *Grid down* chip and raises an alert. Backup mode is not switched on for you, because it would stop the battery discharging below the reserve during the outage.
- **Tariff comparison (Insights):** HomePower records your grid import and export every half-hour (about 200 days; the half-hourly house load and solar are kept for the what-if below). List other tariffs in Settings > Compare tariffs, one per line (`Octopus Go; peak=0.28; cheap=0.07; window=00:30-05:30; export=0.15; standing=0.45`; rates per kWh, standing per day), and Insights prices the same recorded usage on each. It is what your house and battery actually did, so a tariff that suits a different battery schedule could do better than shown. A *Month by month* chart shows each tariff's cost per month, and if another tariff would have been cheaper than the first one you list (taken to be the one you are on) for the last three or more complete months, Insights says so and sends one heads-up a day.
- **Surplus load in cheap hours:** the solar-surplus switch can also be switched on during cheap-rate hours (Settings > Solar-surplus load).
- **Anker X1 own limits (experimental, off by default):** mirrors HomePower's import and export limit into the X1's own limit registers so they hold if Home Assistant is down. It only sets a limit that is stricter than one already active and never switches one off. The addresses come from a community integration, so check them with Registers compare first.
- **Register compare (Anker, Diagnostics tab):** for finding which register an app setting lives in. *Registers: save baseline* reads the battery's registers (read-only), you change one setting in the Anker app, then *Registers: compare* lists what changed, naming each register from Anker's [register list](https://salesforce-knowledge-download.s3.us-west-2.amazonaws.com/000029136/en_US/000029136.pdf) where it is in there (an X1, or the Solarbank limits). Power, energy and temperature readings move by themselves, so they are tucked into a collapsed list; what is left is what you changed. A register listed as *not in Anker's list* is one the document does not cover.
- **Registers: show all (Anker, Diagnostics tab):** reads every register the battery answers (read-only) and lists them with the register number, its name where Anker's list has one, the holding and input value and, for text registers such as the model and software version, the text. Filter by number or name, tick *Only registers in Anker's list*, or press *Download CSV* to save the lot. A 32-bit value is two registers (the X1 sends the low half first) and negative numbers show as 65536 minus their size.
- **Registers: watch (Anker, Diagnostics tab):** pick registers (for example `10000-10015, 10062-10080`), press Start and change one setting in the vendor app. HomePower reads them every 3 seconds (read-only), highlights the ones that move and keeps a list of the changes, with power and charge readings dimmed. It is the quickest way to find which register an app setting lives in, for example the grid-failure sensitivity. *Show all* also adds a *32-bit value* column that joins the two registers of a pair for you.
- **Register labels:** in *Registers: watch*, press *Label* beside a register (or in the list of changes) to give it your own name, such as "grid fail sensitivity". Labels are saved per battery, shown in the watch and in *Show all* (and its CSV), and *Share findings* downloads them as a small JSON file you can post for others.
- **If the grid went down now (Overview):** how long the batteries could run the house: the usable energy (above each battery's minimum) against your learned usual use for the coming hours, or the current load until that is learned. It ignores solar, so it is the cautious figure. The same estimate is added to the grid-down alert.
- **Why it did that (Diagnostics):** each time a battery's instruction changes, HomePower keeps what it was told, the reason, and the readings behind it (charge level, grid power, price, cheap rate, backup / away / pause, solar forecast, reserve floor, room-for-solar limit). Click one to open it.
- **What if I changed the battery settings? (Insights):** HomePower now also records your house load and solar every half-hour. Once it has at least 3 days it replays them through a simple battery model on the first tariff in Compare tariffs, with two sets of reserve and cheap-rate top-up percentages side by side, and shows the cost difference. It is an estimate (95% one-way efficiency, all batteries as one, none of HomePower's other rules).
- **Weekly digest:** tick *Also send a weekly digest* in Settings > Alerts for one message every Monday (at the daily summary time, or 08:00): savings, grid use, outages, battery efficiency and health, and any cheaper-tariff flag.
- **Downloads and charts:** the tariff comparison (with its monthly table) and the grid outage list have a *Download CSV* link, and the temperature and pack balance card draws each battery's daily peak temperature as a line over up to 60 days.
- **Grid outages (Insights):** each time an X1 reports the grid down, HomePower records when it started and ended and the charge level at both, so you can see how often the grid drops and whether the battery rode through.
- **Battery temperature and pack balance (Insights, X1):** the hottest temperature each day and how far apart the battery pack voltages are (a percentage of their average) for the last 14 days. HomePower alerts you if the spread doubles against its usual or the X1's internal temperature passes 65 °C.
- **Reserve by solar forecast (Settings > Backup & away):** set a forecast threshold in kWh and a reserve percentage. On a day forecast below the threshold (today's forecast before midday, tomorrow's after) batteries are not drained below the reserve; on a sunny day they go down to their normal minimum. Unlike backup mode it never charges from the grid, it only stops discharging. Needs a solar forecast.
- **Hold window (Schedules):** a one-day *Hold* schedule with a date, start and end, for an expensive evening event or a planned outage. Batteries neither charge nor discharge for the time. Recurring holds use an ordinary schedule with the Hold mode.
- **What HomePower reads from Anker batteries:** X1: on-grid / off-grid status, internal temperature, the alarms the X1 reports (inverter, battery or meter communication failure, EEPROM / flash error) and its software version, with an alert if the software changes. Solarbanks (E5000 Pro and others): the battery's own charge limit, discharge limit and backup reserve, shown on its card. All read-only.
- **Automatic backup copies:** once a day HomePower writes your batteries, schedules, goals, scenes and settings (no secrets) to `homepower_backups/` in the Home Assistant config folder, keeping the newest 7. Settings > Backup lists them with a *Restore* button each, and *Back up now* makes an extra one. *Download backup* saves a copy you can restore on another Home Assistant.
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
| Solar forecast | Leave room for the solar forecast, house base load, tomorrow's forecast sensor (optional), share that reaches the battery (goals) |
| Backup & away | Backup reserve, reserve by solar forecast, entity that turns backup mode on, away entity |
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

Savings are counted when the stored energy is **used**, not when it is stored:

- Each battery keeps the average cost of the energy in it. Grid charging adds its price; solar charging adds nothing; the
  energy lost going in and out raises the average.
- Each kWh discharged saves the price of the grid energy it replaced (or earns your export price, if it went out to the
  grid) **minus** what that kWh cost to store.

So a night of cheap charging does not show as a loss the next morning; it shows as savings during the day as the charge is
used. Today's figure only goes negative if the batteries are discharged at a cheaper rate than they were charged at.

Which charging came from the grid: all of it for a battery told to charge from the grid (schedule, goal, override, backup);
for other batteries, the grid import beyond that, shared between them; with no grid reading, it is counted as solar. A
battery first seen part-full is assumed to hold energy bought at the cheapest rate around. It is an estimate, not a bill.

## Removing HomePower

Deleting HomePower in Settings > Devices & services hands any battery it is controlling back to its own control. Your
batteries, EcoFlow keys, schedules, goals, scenes and history stay saved, so adding HomePower again restores everything.
To erase all of it instead, tick **Erase everything when HomePower is deleted** (HomePower panel > Settings > If you remove
HomePower) before deleting. Home Assistant cannot ask at the moment of deleting, so the choice is made there.

## Known limits

- Sending commands has only been run against simulators, not real hardware. New batteries are monitor-only until you allow
  control; run Test control before relying on it.
- EcoFlow STREAM batteries can be set to Auto, Hold or Charge (no set power, no discharge), Charge has not been tried on a real battery.
- **ES5000:** controlled through the same backup-reserve idea as the Ultra X, using EcoFlow's "STREAM 5000 Series" commands (reserve switch + level, operating mode). While HomePower holds or charges it, the unit is put in Self-consumption, and its own reserve and operating mode (for example Timer tasks) are put back on Auto. Untested on hardware: keep control off until Test control passes. An ES22 may be refused by EcoFlow's API. The reserve and mode HomePower replaced are saved, so a Home Assistant restart while a battery is held does not lose them.
- **EcoFlow checks:** Test control on an EcoFlow battery waits for the cloud and then reports whether it really started charging (an info line, not a pass/fail). Diagnostics lists the settings that changed between readings (`recent_setting_changes`), which shows what the EcoFlow app changed. After error 1006 (a model that refuses the commands) or a rate limit, HomePower backs off for five minutes and says so. An alert is raised if a battery told to charge does not for 20 minutes, and another if an EcoFlow battery's grid reading has the opposite sign to your grid meter.
- **Self-calibrating EcoFlow:** HomePower measures the power an EcoFlow battery really charges at (the median of recent readings while it was told to charge) and plans with that instead of the per-model estimate. A cloud battery gets one cycle to show a command's effect before it counts as unconfirmed, and at most 150 commands a day are sent to each (handing it back to Auto is never blocked). Each battery card can show "Why idle", the main thing in the way, and "Settings changed" with what the EcoFlow app changed.
- **Plan versus actual:** the Overview shows, per battery, what HomePower told it to do over the last 24 hours (dashed) against what it really did (solid), with a "followed the plan" percentage. A low figure means the battery is not following commands or something else is overriding them. The last 8 days are kept.
- **Proving EcoFlow control:** every Hold or Charge sent to an EcoFlow battery is watched for ten minutes and logged ("Command results" on its card, `command_traces` in Diagnostics): did it react, how fast, the most power seen. After three Charge commands in a row with no effect, HomePower stops relying on Charge for that battery (it holds instead) and says so; a Test control that sees it charge turns Charge back on. Test control now checks Hold first, then Charge. Until a battery has passed Test control, its first command waits for you to press "Allow first command" on its card. Each last command shows the inputs behind it (reason, charge level, price, grid). Alert notifications on a phone (a `notify.mobile_app_...` service) carry "Pause HomePower" and "Hand back to Auto" buttons.
- **Safe mode** (header button): hands every battery back to its own control, then sends nothing until you leave it. Pause only stops new commands and leaves batteries as they are.
- **EcoFlow control report** (Insights): per battery, how many commands worked, the average reaction time, the real charge rate, how well it followed the plan, and a plain verdict. The wait before a slow battery counts as "not following commands" adapts to how long it has really taken to react (up to four retries).
- **Warnings on save:** saving a schedule or goal still works, but you are told if it cannot do anything (a battery that cannot discharge), if its power setting is ignored (EcoFlow picks its own rate), or if its target is not above the battery's minimum charge.
- **Price changes:** if the cheap hours in the price history differ clearly from the ones in Settings for six hours, you get an alert to update them. **Keep stored energy for dearer hours** (Settings, off by default) holds a battery when the price in the next six hours is clearly higher than now, but not while solar is charging it. When several batteries compete for a limited import, the one with the better measured efficiency gets the cheapest slots.
- **Debug file:** Diagnostics > Download debug file saves readings, command results, plan against actual and recent events in one file with no keys, serial numbers or addresses. The same scrubbing covers error messages and the event log, where addresses and serials otherwise end up, and "Copy debug info" gives the same text. The on-screen Diagnostics keep your own address and serial but never show keys. After each restart the Events list and Diagnostics show what was restored and which batteries could not be reached. The Setup check lists "first command allowed" for EcoFlow batteries.
- **Cold-weather target** (Settings > Cold weather, off by default): when tomorrow is forecast to be colder than the last week, goal targets are raised by the extra energy the house is expected to use, at most 25 points (an auto-target goal stays within the target you set). The plan says why, for example "target raised to 90% for the cold (-3 °C forecast vs about 6 °C lately: about 2.1 kWh more use)". It needs two things. Tomorrow's temperature comes from the first of: your weather entity (any weather integration, daily or hourly forecast), a sensor holding tomorrow's temperature, or, only if you switch it on, Open-Meteo, which is sent your home's location rounded to about 10 km. How much more your house uses per degree of cold is learned from its own recorded days (about a week, with a spread of temperatures; today's outdoor temperature comes from your sensor or the weather entity). Until a clear link is found nothing changes, and the Settings note says how many usable days it has.
- **Planning with EcoFlow batteries:** a battery whose charge power cannot be set is planned in whole slots at its own rate, never at a made-up fraction. The Health card shows whether cycling each battery is worth it over the next 24 hours, using its measured round-trip efficiency.
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
