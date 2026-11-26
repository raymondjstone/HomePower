# HomePower

A Home Assistant custom integration that is a **central battery manager** for home batteries. It connects to the
batteries **directly** (it does not need, use or depend on any other Anker or EcoFlow integration), shows them in a
sidebar panel, and schedules and optimises how they charge and discharge.

**Contents:** [Install](#install) · [Supported batteries](#supported-batteries) · [Getting started](#getting-started) ·
[How HomePower decides](#how-homepower-decides) · [Schedules](#schedules) · [Goals](#goals) ·
[Tariff and prices](#tariff-and-prices) · [Grid reading](#grid-reading) · [Solar](#solar) ·
[Limits and protection](#limits-and-protection) · [Taking and handing back control](#taking-and-handing-back-control) ·
[Modes, scenes and overrides](#modes-scenes-and-overrides) ·
[Several batteries](#several-batteries) · [Messages and alerts](#messages-and-alerts) · [The panel](#the-panel) ·
[Settings reference](#settings-reference) · [Entities, services and card](#entities-services-and-card) ·
[EcoFlow protocol notes](#ecoflow-protocol-notes) · [Generic Modbus map](#generic-modbus-register-map) · [Savings](#how-savings-are-estimated) ·
[Removing HomePower](#removing-homepower) · [Known limits](#known-limits)

## Install

**Marketplace (Home Assistant 2026.11 and newer, recommended):** *Settings > Marketplace*, search for **HomePower Battery
Manager**, install it and restart Home Assistant. Then *Settings > Devices & Services > Add integration > HomePower*.
Add batteries from the **HomePower** sidebar panel or the integration's **Configure** menu.

**HACS (before 2026.11):** open HACS, search for **HomePower Battery Manager**, download it, restart, then add the
integration as above. (From 2026.11 HACS is part of Home Assistant as the Marketplace, and an installed copy is moved
there automatically.)

If the search finds nothing, the catalog does not list HomePower yet: add it once by link instead. Marketplace: *Add from
link* with `https://github.com/raymondjstone/HomePower`. HACS: three-dot menu > *Custom repositories* > add that address as
an *Integration*. Then search and install as above.

**Manual:** copy `custom_components/homepower` into your Home Assistant `custom_components` folder and restart.

HomePower is set up once per Home Assistant (a second copy cannot be added). Tested on Home Assistant Core 2026.2 and
2026.11.3 (Python 3.13 and 3.14).

## Supported batteries

| Battery | How HomePower connects | Modes |
|---|---|---|
| Anker SOLIX Solarbank Max AC / Max, XE AC / XE, Solarbank 4 E5000 Pro, X1 | Local **Modbus TCP** on your network (switch it on in the Anker app: device > Settings > Three-Party Control). HomePower scans your network and finds the battery. | Auto, Charge, Discharge, Hold |
| EcoFlow STREAM AC / Pro / Ultra / Ultra X / Max | EcoFlow's own **developer API**: create an Access Key and Secret Key at developer.ecoflow.com; HomePower lists the batteries on your account. | Auto, Hold, Charge (on/off, own rate) |
| EcoFlow STREAM AC 5000 (ES5000, serial ES21/ES22) | The same developer API (EcoFlow's "STREAM 5000 Series" settings) | Auto, Hold, Charge (experimental) |
| EcoFlow STREAM 5000 / AC 5000 - **local Modbus** (firmware 1.1.7.28 or later) | The battery's own Modbus TCP "third-party control", no cloud | Auto, Hold, Charge and Discharge in watts (not yet verified on hardware) |
| Victron GX | Modbus TCP, register map built in | Monitor only |
| Any Modbus TCP battery | You describe its registers as a small JSON map ([see below](#generic-modbus-register-map)) | Auto, plus Charge/Discharge/Hold if the map has a setpoint and mode register |
| Anything else | Advanced: choose Home Assistant entities that read and control it | Auto, Charge, Discharge, Hold |

The four modes mean: **Auto** = the battery runs its own logic; **Charge** / **Discharge** = at a power HomePower sets;
**Hold** = neither charge nor discharge from the battery's store.

- **EcoFlow limitation:** EcoFlow's cloud developer API has no charge/discharge power setpoint (the ES5000 can use its local Modbus connection
  instead, which does: see the table above and [EcoFlow protocol notes](#ecoflow-protocol-notes)). Auto = the battery's own settings, exactly as they were before HomePower changed them. Hold = the backup reserve is raised to the battery's current charge level, so
  it stops discharging and is not told to top up from the grid (its own panels still charge it). Charge = the reserve is
  raised to the target (95 % at most), so the battery tops itself up from the grid at its own rate: the power you set is
  not sent, and solar-only / follow-the-meter charging holds the battery instead of grid-charging it. There is no force discharge.
  Raising the reserve is the same method other tools use to grid-charge a STREAM (the evcc EV-charging controller documents
  setting the backup reserve to its upper limit for grid charging, and notes that EcoFlow keeps the reserve at least 3 points
  above the discharge limit and that active control overwrites the reserve set in the EcoFlow app; HomePower puts your own
  reserve back on Auto). That is independent support for the idea, not a test on your battery: use Test control, which reports
  whether the battery really started charging.
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
   safe command, checks the battery follows it, and hands it back. Taking control changes settings in the battery itself
   (its work mode, backup reserve or control point): note down your settings in the vendor app first, and check them
   there after HomePower hands the battery back. See [Taking and handing back control](#taking-and-handing-back-control).
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
3. **Grid event** (a saving session: see [Grid events](#grid-events-saving-sessions))
4. **Schedule**
5. **Goal plan**
6. **Zero-import battery** (the one battery chosen to hold grid import at 0 W)
7. **Paid export** (off by default: see [Paid export](#paid-export))

Then, for batteries nobody has instructed: **peak shaving** and **export limiting**, **hold-back** (reserve role, save for
peak hours), and finally **hand-back** to the battery's own control. Device limits, charge-level limits and the overall
caps are applied to whatever was decided. Every decision has a visible reason: the *Why* chip on each battery, and the
battery's *target* sensor (`paused` while HomePower is paused, `monitor_only` for a battery whose control is off).

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

Rates in pence/cents or pounds/euros are detected automatically from the whole price table (if any price is 5 or more it is pence/cents, so a 3.5p slot or a 4.1p export rate is read correctly), or you can say which. Clock-change days (23 and 25 hours) are planned in real time, so a fixed cheap window keeps its true length.

**Save the battery for peak hours** (optional) holds the batteries during cheap-rate periods so their charge is kept for
the expensive hours. It never holds while solar is producing.

**Battery wear** (optional, *Battery wear per kWh*): what cycling one kWh through the battery costs in wear, in the same
unit as the prices. A rough figure is the battery's price divided by (its capacity in kWh × its rated cycles): 4000 / (10 ×
6000) = 0.067, about 6.7p. Paid export and the *is storing worth it* figure in Insights must beat it, and spare solar is
left to export when storing it would earn less than the wear it costs.

### Paid export

Off by default (Settings > Paid export). Batteries discharge to the grid when **exporting pays more than buying the
energy back later**, and in the **best-paid half-hours** for what is stored:

- **The day plan.** From the prices, HomePower works out where the stored energy is worth most between now and the
  cheapest coming import price (when refilling is cheap again): running the house in the dearest half-hours, or exporting
  at full power in the best-paid ones. Uses are taken best-paid first while the energy lasts, so on Agile Outgoing the
  battery is not sold at 15p in the afternoon when the 18:00 peak pays 30p. Exporting in a half-hour also runs the house
  there first, as a battery always does. The house's usual use is learned (400 W is assumed until it is). The plan is
  worked out again every cycle as prices, charge and use change, and the Plan tab's price chart shows it.

- The export price comes from an **export price sensor** (Octopus Agile Outgoing, Flux, Nord Pool...: price slots in its
  attributes, or a plain sensor whose state is the price now), or the fixed export price under Tariff.
- Exporting must beat the cheapest import price in the next 24 hours divided by 85% (the energy lost putting it back into
  the battery), plus the battery wear above.
- A battery never goes below the level you set; the plan itself keeps what the house needs until the cheapest price
  comes round. Several batteries share the export power by the energy each has stored.
- Only batteries that can be told a discharge power take part (not EcoFlow over the cloud). Overrides, backup mode, grid
  events, schedules, goals and the zero-import battery all come first; load rules and the export limit still apply.
- Insights shows what exporting pays now against what buying back costs.

### Grid events (saving sessions)

Settings > Grid events: pick one or more entities (comma-separated) that say when an event runs: the Octopus Energy
integration's saving-session binary sensor or its event entity (which lists the sessions you joined), a calendar (on while
one of its events runs), or an `input_boolean`. While an event runs, every battery that can discharges down to its minimum to run the house, and nothing charges from the grid
(the export limit may still charge from power that would otherwise go out over the limit). A battery that cannot be told
to discharge is handed back to its own control, which powers the house. Manual overrides and backup mode still come
first. The start and end are logged under Events, and the Plan tab's price chart shades the event.

**Charge up before the event** (optional, a percentage): when the entity says when the next event starts (a calendar or the
Octopus Energy saving-session sensor or event entity; a plain on/off helper cannot; all-day calendar entries are ignored), a
goal charges the batteries to that level in the cheapest half-hours before it starts, so there is more to sell during the
session. It is not held back by *leave room for the solar forecast*, since the energy is meant to go out during the event.

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
- **Today's solar forecast** (when Forecast.Solar or Solcast is set up; found automatically): an Overview tile shows the
  day's forecast, how much your batteries have measured so far, how much is still to come (from the hour-by-hour
  forecast) and tomorrow's figure. With both installed Forecast.Solar is used; they are never added together.
- **Forecast against what came** (Insights): each day's forecast (the total first seen that day, which is what the day was
  planned with) beside the solar measured, for the last 30 days, with how close the forecast has been as a percentage;
  and today or yesterday hour by hour (each hour's forecast as it stood when the hour began: later revisions, and the
  estimated actuals Solcast puts in place of past hours, do not overwrite it),
  with a CSV of the daily figures. Measured solar is what your batteries report, so panels on a separate inverter are not
  included. The forecast history is kept for 120 days.
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
- **How much of the forecast to use** (Settings > Solar forecast, 100% by default): 75% means HomePower plans with three
  quarters of every forecast figure, for room for solar, goals, the dull-day reserve and the Overview tile. The Insights
  comparison keeps the forecast as given, so its percentage shows what to set.
- **Solar forecast for goals:** a goal can also charge less when the solar after its deadline is forecast to be good (a goal
  due before sunset uses that day's forecast; one due after sunset, the next day's), using the share of forecast solar that reaches the battery. The forecast is the sensor you pick, or
  else Forecast.Solar / Solcast, found automatically.
- **Solar string health (Anker X1, EcoFlow STREAM):** each PV input's volts, amps and watts are shown in the battery's details sheet and
  under Insights > Solar strings. Every string is compared with the others on the same inverter, so the weather cancels
  out. An alert is raised when a string that normally produces gives almost nothing for half an hour while the others
  produce well (a blown fuse, a pulled connector, a failed optimiser), or when its share of the day's solar is under 60% of
  its usual share two days running (new shading, dirt, a failing panel). The usual share needs three days with at least
  1 kWh of solar; a string never seen above 30 V counts as unused.
- **Solar-surplus load:** switch on a chosen switch (an immersion heater, for example) while the home battery is full and
  you are exporting more than a set power, and off again when it would need grid power. It is released while HomePower is
  paused.

## Limits and protection

- **House import limit:** caps total charging so charging never pushes import over it.
- **Peak shaving:** with it on, batteries also discharge to keep whole-house import under the limit.
- **Export limit (G98/G99):** trims discharge, then charges, to stay under an export limit.
- **Overall caps** on total charge and total discharge power across all batteries. A battery that picks its own charge rate only
  charges when the cap has room for its full rate.
- **Charge-level limits:** each battery's minimum charge is respected with a margin so it does not flap on and off.
- **Protect loads:** don't charge and/or discharge while a chosen entity is on or above a power (EV charger, heat pump...).
  The setup check points out the power sensor of a common EV charger (evcc, zappi, Ohme, Wallbox, Easee, Hypervolt,
  Pod Point, go-e...) or heat pump that no rule covers yet, so a battery is not emptied into the car.
- **Manufacturer protection:** if a battery reports its own protection is active, HomePower does not override it and
  raises an alert.
- **Commands are verified** by reading back, and not re-sent more often than the re-apply cooldown.
- **Hand-back:** a battery HomePower forced is handed back to its own control when it is no longer needed. What is
  changed and what is put back is described in [Taking and handing back control](#taking-and-handing-back-control).
- **Watchdog:** on batteries with a communications watchdog (X1) it is fed while the battery is forced, so the battery
  reverts by itself if Home Assistant stops.
- **If the decision logic ever fails**, no commands are sent that cycle and a note says so.

## Taking and handing back control

> **Warning: check your settings in the vendor app (Anker or EcoFlow) after HomePower hands a battery back.** To control a
> battery HomePower changes settings in the battery itself. It saves what was there and puts it back, but it cannot
> promise the battery ends up exactly as you left it: some settings are not restored (see below), a hand-back can fail or
> be skipped, and the vendor's app or cloud may change things on its own. Before you allow control, note down the settings
> listed below; after HomePower lets go of the battery, open the vendor app and check them.

### What HomePower changes when it takes control

HomePower takes no control while a battery is **monitor-only** (the default for a new battery), while HomePower is paused
or in Safe mode, or during a test run (Settings: *dry run*), though a hand-back is still sent in all of these. Once control
is allowed, the first Charge, Discharge or Hold command changes:

| Battery | Changed while HomePower controls it | Saved first, and put back on hand-back |
|---|---|---|
| Anker (Modbus) | Work mode (register 10064) set to *third-party control*; power setpoint (10071). X1: communications watchdog (10080) set to 600 s | The work mode it had, and on the X1 the watchdog it had |
| EcoFlow STREAM Ultra / Ultra X (cloud) | Backup reserve raised (Hold: to the current charge level; Charge: to the target, 95 % at most) | The backup reserve it had. The energy strategy (self-powered, AI, time-of-use, scheduled) is never changed |
| EcoFlow STREAM 5000 series (cloud) | Operating mode set to *Self-consumption*; backup reserve switched on and raised | The reserve switch and level, and the operating mode it had |
| EcoFlow ES5000 (local Modbus) | Control point set to *battery*, battery power target; keep-alive written every 20 s | The control point another tool had set, if any; otherwise the battery's own energy management (control point 0) |
| Generic Modbus map / Home Assistant entities | The mode and power registers or entities you mapped | Nothing is saved: hand-back writes the *Auto* value or option you mapped |

What the battery had is read from the battery itself just before the first change, and saved with HomePower's other
state, so a Home Assistant restart while a battery is held does not lose it.

### When HomePower hands a battery back

When no schedule, goal or override needs it any more; when you switch control off for it (monitor-only); when you delete
it; on **Safe mode**; when HomePower is reloaded, removed or Home Assistant shuts down; after a restart if it was left
held; from the *Hand back to Auto* button on a phone alert. **Pause** does not hand back: it leaves batteries as they are
(except that an Anker X1 is no longer kept alive while paused, so its watchdog returns it to its own control after 10 minutes).

A hand-back that fails (the battery is offline, EcoFlow's cloud refuses or rate-limits it) is retried until it works,
and a *command failed* alert says so meanwhile. If Home Assistant stops while a battery is held, nothing is handed back
until it starts again: the Anker X1 reverts by itself through its watchdog after 10 minutes; other batteries stay as
HomePower left them.

### What hand-back puts back, and what it leaves alone

- **Exactly what was saved**, never a made-up default. If HomePower does not know what the battery had (for example a
  takeover saved by an older HomePower version, or a battery already in third-party control when HomePower found it), it
  leaves the battery as it is and logs a warning telling you to set it in the vendor app.
- **Changes you made in the vendor app meanwhile are kept.** HomePower remembers every reserve it wrote, and only undoes
  its own: a different reserve, an Anker work mode other than third-party control, or a STREAM 5000 operating mode other
  than self-consumption is taken as your newer choice and left as it is.

### What to check in the vendor app afterwards

- **Anker app:** the work mode (self-consumption, time-of-use, custom...) and anything that depends on it, such as
  time-of-use plans.
- **Anker X1:** the communications watchdog (put back to what it had, unless you changed it meanwhile). If you turned on
  *Also set the X1's own import and export limits to HomePower's* (Settings): HomePower takes its limits back off, putting
  back what each one had before (an installer's limit, or off), when you clear the limit, turn the option off, switch the
  X1 to monitor-only, delete it or remove HomePower. They are deliberately left in place over a restart or reload, so they
  still hold while Home Assistant is down, and they stay if the X1 cannot be reached at that moment (the log says so; when
  you delete an X1 that cannot be reached, a Home Assistant notification tells you to check its limits in the Anker app).
  Check them in the app, or with your installer if the limits are part of your grid connection agreement.
- **EcoFlow app (Ultra / Ultra X):** the backup reserve, and that the energy strategy is still the one you use.
- **EcoFlow app (STREAM 5000 series):** the backup reserve switch and level, and the operating mode (timer tasks, AI
  mode...), including any timer tasks themselves.
- **EcoFlow ES5000 over local Modbus:** that the battery is back on its own energy management, and whether you still want
  *Third-party control* (Modbus TCP) switched on in Energy management.
- **Generic Modbus / entity batteries:** the mode the battery is in, since HomePower puts back the *Auto* value you mapped
  rather than what the battery had.

Check especially after removing HomePower, switching a battery to monitor-only, Safe mode, a restart or a HomePower update
while a battery was held, a hand-back while the battery was offline, a *command failed* alert, or a log warning that a
battery was "left as it is". EcoFlow's app can take a few minutes to show the settings HomePower wrote or put back.

## Modes, scenes and overrides

- **Pause:** HomePower keeps reading but sends no commands.
- **Test run (dry run):** Settings > Test run. HomePower works out its plan as normal but sends nothing; each command it would have sent is listed under Events as `DRY RUN: would send ...`. Unlike Pause, the plan keeps being worked out, so it is the safe way to try new schedules, goals or a new battery.
- **Grid down (Anker X1):** when an X1 reports the grid is lost it is running the house itself, so HomePower sends it no commands until the grid is back, shows a *Grid down* chip and raises an alert. Backup mode is not switched on for you, because it would stop the battery discharging below the reserve during the outage.
- **Tariff comparison (Insights):** HomePower records your grid import and export every half-hour (about 200 days; the half-hourly house load and solar are kept for the what-if below). List other tariffs in Settings > Compare tariffs, one per line (`Octopus Go; peak=0.28; cheap=0.07; window=00:30-05:30; export=0.15; standing=0.45`; rates per kWh, standing per day), and Insights prices the same recorded usage on each. It is what your house and battery actually did, so a tariff that suits a different battery schedule could do better than shown. A *Month by month* chart shows each tariff's cost per month, and if another tariff would have been cheaper than the first one you list (taken to be the one you are on) for the last three or more complete months, Insights says so and sends one heads-up a day.
- **Surplus load in cheap hours:** the solar-surplus switch can also be switched on during cheap-rate hours (Settings > Solar surplus).
- **Anker X1 own limits (experimental, off by default):** mirrors HomePower's import and export limit into the X1's own limit registers so they hold if Home Assistant is down. A limit HomePower wrote itself follows your setting up and down; a limit set by anyone else (an installer's grid-code limit) is only ever tightened, never raised or switched off. When HomePower's limit is no longer wanted (cleared, option off, X1 monitor-only or deleted, HomePower removed) the slot gets back exactly what it had before HomePower first wrote it, so a tightened installer limit returns to the installer's value rather than to off. The addresses (10074-10079) are the ones in Anker's own X1 Modbus protocol document.
- **Battery details (the cog on a battery card, or *Details* on the Batteries tab):** everything about one battery in one place, laid out like its page in Home Assistant. *Readings now* and *HomePower's settings for it*; *Settings you can change*, with the same limits and rules as the vendor's own integration (see below); *On the battery itself*, which is what the battery reports (an Anker's named registers with units, an EcoFlow's values named the way the EcoFlow integration names them); and the matching *Home Assistant device* with its controls and sensors. Changes to the battery's own settings always ask first, are checked before anything is sent, and can only be made by an administrator.
  - *Anker Solarbank:* operating mode (only the modes that battery supports), charging limit (80-100 %), discharge limit (0-20 %) and backup reserve (0-100 %, only while the reserve is switched on in the Anker app; kept between the discharge and charging limits).
  - *Anker X1:* work mode, export and import limit mode and value (watts, or percent of rated power), and the comms watchdog (30-3600 s).
  - *EcoFlow STREAM (cloud):* AC 1 and AC 2 outputs, feed-in control, the self-powered and AI operating modes, and the backup reserve level (3-95 %, at least 3 above the discharge limit). The STREAM 5000 series is read-only here for now.
  - What HomePower itself drives on a battery (an Anker's operating mode, an EcoFlow's reserve level and operating modes) is shown but locked while HomePower controls that battery; turn on *monitor only* in Edit to change it yourself.
  - The controls of the matching Home Assistant device (numbers, choices, switches, buttons) work as they do on the device's own page, through Home Assistant's services.
- **Readings stale, not Offline:** a failed read is often just another integration holding the battery's Modbus connection for a moment, so a battery is only shown Offline (and the offline alert raised) once reads have failed for the time set in Settings > Limits > "Show a battery offline after" (5 minutes by default), or straight away when the network reports the battery is not there. Until then it shows "Readings stale" with its last good values. Control is unchanged: nothing is commanded or counted from a battery that was not read.
- **A Solarbank 4 or Max AC that keeps showing Offline:** on these two, Anker has confirmed a firmware defect: the battery's Modbus service stops answering while the battery cannot reach Anker's cloud, and comes back when it can (opening the Anker app prompts it to reconnect). HomePower cannot change that, but it says so on the battery's card and in its sheet, with when it was last read and why the last read failed (refused, no reply, connected but silent, closed), and keeps the last ten dropouts and how long each lasted (Details > Connection). It also waits properly for a battery that answers slowly instead of giving up and asking again, which used to show a slow-but-working battery as offline. Installing Anker's firmware update that makes the Modbus service independent of the cloud is the real fix.
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
  everything else and ends by itself; a restart of Home Assistant in the meantime does not cancel it.
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

Alerts appear under **Settings > Repairs** while the problem lasts; heads-ups and summaries appear as Home Assistant
notifications. Both optionally go to a `notify.*` service as well.

- **Alerts:** a battery is offline (a minute after it is shown Offline: see
  *Readings stale* under [Modes, scenes and overrides](#modes-scenes-and-overrides)), a command failed, a battery is not following commands, manufacturer protection is
  active, a battery's round-trip efficiency has dropped well below its usual, a battery alarm, a solar string that has
  stopped or weakened, or the cheap-rate hours no longer match the price history. Each clears itself from Repairs when
  resolved; any left from before a restart are cleared at start and raised again if still true. Some can be fixed right
  there: a battery that is not following commands, or not charging when told to, offers *Run Test control now* and
  *Switch this battery to monitor-only*; changed cheap-rate hours offer to use the hours found in the price history.
- **Heads-ups (optional):** the home battery is full, the charge has fallen to a level you choose, or the cheap rate starts
  in 30 minutes and the battery is below a level you choose.
- **Daily summary (optional):** one message at a time you choose with the battery level, yesterday's charging and
  discharging, estimated savings, solar self-consumption and cheap-rate share.

## The panel

**Sidebar > HomePower** (administrators only):

| Tab | What is there |
|---|---|
| Overview | Live state of every battery with its *Why*, the home battery card, grid power, today's solar forecast, scenes, quick overrides, tariff notice, setup check, and a live power chart with a time scale you can change (15 minutes to 24 hours) |
| Plan | Goals, the price chart with planned charging, planned export, energy kept for dearer hours and grid events, the next 24 hours per battery, schedule clashes |
| Schedules | Add, edit, enable and delete schedules |
| Batteries | Add, edit and delete batteries; allow control; role, priority, solar panels, whether its meter measures the whole house |
| Insights | Savings today and this month, energy totals, solar forecast against measured solar (by day and by hour), solar self-consumption and cheap-rate share, what exporting pays now, solar strings (Anker X1, EcoFlow), battery health (weekly round-trip efficiency), long-term history charts. On any chart with a clickable legend, click an item to hide that series and again to show it (also on the Overview power chart) |
| Settings | Everything in the [settings reference](#settings-reference) |
| Diagnostics | Setup check, per-battery status and last command, Test control, the raw readings received, event log, copy debug info, hardware report |

**Settings > Devices & Services > HomePower > Configure** offers the essentials as native Home Assistant forms, with no
panel needed: add and remove batteries, allow control, add / change / remove schedules, add and remove goals, the
common settings, and *Prices, export and grid events* (paid export, battery wear, keeping energy for dearer hours, saving
sessions).

**Backup & restore** (panel > Settings) saves batteries, schedules, goals, scenes and settings to a file and restores them.
EcoFlow keys are only included if you tick the box.

## Settings reference

| Section | Settings |
|---|---|
| Grid meter | Import sensor, export sensor, positive-when-exporting, order of grid reading sources |
| Limits | House import limit, peak shaving, export limit, max total charge, max total discharge, re-apply cooldown, show a battery offline after (minutes, 5 by default) |
| Zero-import battery | Which battery keeps import at 0 W |
| Tariff & prices | Quick-fill presets, rates sensor, rates unit, standard rate, cheap rate, export price, estimate the cheap period from history, cheap-rate start and end, more cheap periods, save the battery for peak hours, keep stored energy for dearer hours, battery wear per kWh, currency symbol |
| Paid export | Export price sensor, discharge to the grid while exporting pays, the lowest charge to export down to |
| Grid events | Entities that say when a saving session runs (binary sensor, calendar, the Octopus event entity, input_boolean), charge up before the next one |
| Solar forecast | Leave room for the solar forecast, house base load, how much of the forecast to use, tomorrow's forecast sensor (optional), share that reaches the battery (goals) |
| Backup & away | Backup reserve, reserve by solar forecast, entity that turns backup mode on, away entity |
| Protect loads | Entities that block charging and/or discharging while busy |
| Alerts & heads-ups | Extra notify service, battery full, battery low level, cheap-rate-soon warning and its level, daily summary time |
| Solar surplus | The switch to run, export needed, minimum home battery charge |
| EcoFlow | Live feed (see below) |
| Backup & restore | Save to file, restore from file |
| If you remove HomePower | Erase everything when HomePower is deleted |

**EcoFlow live feed (opt-in):** makes EcoFlow batteries use EcoFlow's MQTT feed with the same keys, so readings are seconds
old instead of about 30 seconds. It is off by default because it opens a second connection on your EcoFlow account, which
could disturb another EcoFlow integration such as EcoFlow Cloud. If it cannot connect, normal polling continues, and
Settings > EcoFlow says why (and, while it works, how many live updates have arrived); Diagnostics shows the same per
battery. It has not been tested against a live EcoFlow account.

## Entities, services and card

**Sensors** (on one *HomePower* device):

- Whole system: battery power, stored energy, grid power, electricity price now, savings today, savings this month, home
  battery (all batteries as one, with totals in its attributes), solar self-consumption today, cheap-rate share of grid
  import today.
- Energy counters for the **Energy dashboard**: grid energy imported, grid energy exported, solar energy.
- Money totals Home Assistant keeps long-term statistics of, in Home Assistant's own currency: **Export earnings** (every
  kWh sent to the grid at the export price then; pick it as the export's *entity tracking the total compensation* in the
  Energy dashboard) and **Battery savings** (the running total of the estimate below).
- Per battery: charge, power, solar power (the battery's own reading, the same figure as the side panel's *Solar* chip;
  Anker register 10002), target (what HomePower wants it to do, with the reason in its attributes), energy charged,
  energy discharged, round-trip efficiency, cycles. In the Energy dashboard, add each battery under *Battery systems* with
  its *energy charged* as "energy going in" and *energy discharged* as "energy coming out". While a battery's readings
  are only stale (see *Readings stale*), its sensors keep their last good value and its target sensor's `stale` attribute
  is true; once it is shown Offline they become unknown.
- Anker X1: total PV input power, and each PV string's voltage, current and power (strings 5-8 are off until you enable them).

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

Batteries are named by their ID, shown as *Battery ID (for actions)* in each battery's details in the panel; `device_ids`
takes one ID or a list. A mistyped ID is refused with a message rather than ignored.

**Dashboard card (optional):** `type: custom:homepower-card` shows the home battery. It finds the sensor by itself;
`entity:`, `title:` and `show_batteries:` are optional.

**Diagnostics download** on the integration page, with keys, addresses and serial numbers removed.

## EcoFlow protocol notes

Reference notes on how EcoFlow STREAM batteries are controlled, gathered from EcoFlow's official Home Assistant integration
([EcoFlow-oasis-official/ha-EcoFlow-official](https://github.com/EcoFlow-oasis-official/ha-EcoFlow-official), MIT) and from
reading the EcoFlow Android app (version 6.18.0.136, package `com.ecoflow`, a React Native app whose STREAM screens are the
"EnergyStorage3" bundle) for interoperability with batteries the author owns. No EcoFlow code is included in HomePower. Everything
here is read from those sources and is **not yet confirmed on a real ES5000** unless it says so.

**Three ways to talk to a STREAM 5000 series battery**

| Route | What it needs | What it gives |
|---|---|---|
| Cloud developer API (what HomePower uses for Ultra / Ultra X, and optionally the ES5000) | Access Key + Secret Key from developer.ecoflow.com | Settings and readings through EcoFlow's servers; no power setpoint; rate limited |
| **Local Modbus TCP** (HomePower's "local Modbus" ES5000 profile) | Firmware 1.1.7.28 or later; Modbus TCP switched on in the app | Readings and a real power setpoint on your own network |
| Local LAN channel (the app's own) | A token fetched from EcoFlow's cloud for each session | Not used by HomePower: see below |

**Switching Modbus TCP on.** In the EcoFlow app: STREAM system > **Energy management** > section **Device integration** >
**Third-party control** (id `third_party_control`) > turn on **Modbus TCP** and choose the access permission (read/write or
read-only). It is off by default.
- The combined **System settings** page hides this entry on purpose (it is built with `restrictLocalModeEntries`), so look on the Energy management page.
- The entry is greyed out unless EcoFlow's cloud (`GET /thunder/compat/setting`) lists `MODBUS_TCP` as supported for the unit's firmware; otherwise the app says the firmware does not support it. What that check requires is not in the app.
- Supported models named in the app: **STREAM 5000** and **STREAM AC 5000** (serial prefixes ES21 and ES22).
- In the device message set the switch is `OpenApiConfig` (field 49 of the property command: `modbusTcpOnoff`, `localIpv4Addr`, `localPort`, `accessPermission` 0 = read/write, 1 = read only). Its current state, IP and port come back in the live readings (field 75).
- Do not change the same setting in the app and over Modbus at the same time; turning third-party control off in the app takes Home Assistant's access away.
- The app also has a separate **Local mode** ("cloud / local connection mode", `LocalModeConfig`, field 58). It cuts the device off from the cloud, allows Self-powered mode only, needs a meter or smart plug and disables firmware updates. It is **not** the Modbus switch and HomePower does not need it.

**Modbus connection.** Port 502, unit id 1. Wire address = register offset + 40001 (so offset 0x0206 is read at 40519 and the
system group id at 0x1004 is 44101). Function 03 for reads (at most 125 words) and 06 or 16 for writes. 32-bit values and
floats put the **high word first**; floats are IEEE-754 single precision. The device wants a write to the **heartbeat register
0x025F** at least once a minute while a client is connected (the official integration writes 1 every 20 s, HomePower does the
same). Units that form one STREAM system share a group id (0x1004); with several units the system is controlled through its
master unit (0x1001 is the master flag). The unit also advertises itself by mDNS as `_ecoflow._tcp.local.` with TXT fields
`model` and `serial`.

**Registers HomePower uses** (offset, type, access; the full map has about 200 points and is in the official integration)

| Offset | Meaning | Type / unit | Access |
|---|---|---|---|
| 0x0003 | Serial number | 8 words of text | R |
| 0x000B | Firmware version (e.g. 1.1.7.28 as four bytes) | UINT32 | R |
| 0x0206 | System consumption (house load) | FLOAT W | R |
| 0x0208 | System grid power | FLOAT W | R |
| 0x020A | System PV power | FLOAT W | R |
| 0x020C | System battery power (+ charging assumed) | FLOAT W | R |
| 0x020E | System state of charge | UINT16 % | R |
| 0x0217 | Backup reserve ratio | UINT16 % | RW |
| 0x0221 / 0x0223 | Max active output / input power | UINT32 W | R |
| 0x0227 | Battery full capacity | UINT32 Wh | R |
| 0x025F | Heartbeat (keep-alive) | UINT16 | W |
| 0x1400 / 0x1401 | Battery charge / discharge capability | UINT16 W | R |
| 0x1600 / 0x1602 | Battery charge / discharge power limit | UINT32 W | RW |
| 0x1604 - 0x160E | Meter point, inverter and grid port power limits (in / out) | UINT32 W | RW |
| **0x1610** | **Control point**: 0 none (the device's own energy management), 1 battery, 2 meter, 3 inverter, 4 grid port | UINT16 | RW |
| **0x1611** | **Battery target power** (used when the control point is battery) | INT16 W | RW |
| 0x1612 - 0x1614 | Meter / inverter / grid port target power | INT16 W | RW |
| 0x2200 / 0x2201 | SOC lower / upper limit | UINT16 % | RW |
| 0x0820 - 0x0823, 0x2300 - 0x2302 | Online battery count, per-pack SOC and SOH | UINT16 | R |
| 0x0800 - 0x080A, 0x2010 - 0x201A | System and device fault counts and codes | UINT16 | R |
| 0x0870 / 0x0880 / 0x08B0 / 0x08C0 / 0x08D0 | Grid import / export, battery charge / discharge, solar energy totals | FLOAT kWh | R |

When no control point is set, EcoFlow's own energy management runs the system and the limits above work together with the
app's own controls. Regulation-related settings (power unlocking, permission to export to the grid) can only be changed in the
app. All Modbus control stays inside the system's safety limits.

**What is not known yet:** the sign convention of the target power registers (HomePower assumes + charging, like the power
readings, and has a "Reverse the charge/discharge sign" option); whether the power limit registers must be raised before a
target is followed; and what firmware 1.1.7.28 does when the heartbeat stops. Test control on the first real run answers these.

**Cloud / app message format (the route HomePower already uses for the cloud).** Commands are protobuf messages with command
set 254 (source 32 for the cloud, 33 for the app, destination 2) and these command ids: 17 configuration write (Ultra / Ultra X,
for example `cfgBackupReverseSoc`), 37 action, **38 property command** (STREAM 5000 series), 39 live display upload, 40 runtime
upload, 43 property read. The property command's action id selects one sub-message (the field number equals the action id):

| Action id | Sub-message | Fields |
|---|---|---|
| 25 | `workMode` | SELF_CONSUMPTION 0, AI_MODE 1, TIMER_TASK 2, POU_MODE 3, EMERGENCY_STOP 4 |
| 29 | `BpSocConfig` | `chargeSocUpperLimit`, `dischargeSocLowerLimit` |
| 30 | `BpBackupConfig` | `backupSocOnOff`, `backupSoc` (the backup reserve HomePower raises on the cloud route) |
| 10 | `BkwPowerParameterConfig` | `onGridFeedPowerLimit`, `onGridTakePowerLimit`, `feedToGridLimitOnOff`, `ratedPower`, `homeMainLoadBalanceSet` ... |
| 24 | `CfgTimeScheduleTask` | schedules; task types AC_CHG 0, AC_DSG 1 ... |
| 19 | `DevSwitch` | `acSocketOnoff` |
| 49 | `OpenApiConfig` | Modbus TCP switch, address, port, access permission |
| 58 | `LocalModeConfig` | `localModeEnable` |

The app's cloud endpoints: `https://api-e.ecoflow.com` (Europe), `api-a` (Americas), `api` (elsewhere); live data comes from
the MQTT broker whose credentials `GET /iot-auth/app/certification` returns, on topics `/app/<userId>/<sn>/thing/property/set`,
`/set_reply`, `/get` and `/get_reply`. This matches what HomePower's cloud driver already sends.

**The app's local LAN channel (not used).** The app can also reach a unit directly over UDP, port 51011: it broadcasts a
`DISCOVER_REQ` and the unit answers with its serial, product type and port. Frames are little-endian: `0x5A5A`, version, encryption
(0 none, 1 AES-128-CBC, 2 AES-128-GCM), packet type, service id, length, payload and a CRC-16. A session starts with a random
number from each side; the key is HMAC-SHA256 based and needs a **token that EcoFlow's cloud issues**
(`POST /iot-auth/local-lan/auth`) for each session, so it is not a cloud-free path. Bluetooth uses the same framing. HomePower
uses neither; Modbus TCP is the supported local route.

**Checking the switch took effect.** Use the Diagnostics port scan on the battery before and after turning Modbus TCP on:
port 502 should open.

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
Removing HomePower also takes any import/export limits it wrote into an X1 back off (this needs the X1 to be reachable at
that moment). Afterwards, check each battery's settings in the vendor app (work mode, backup reserve, operating mode, and
on an X1 the limits and watchdog): see [What to check in the vendor app afterwards](#what-to-check-in-the-vendor-app-afterwards).
To erase all of it instead, tick **Erase everything when HomePower is deleted** (HomePower panel > Settings > If you remove
HomePower) before deleting; that also deletes its daily backup copies in `homepower_backups` (other files there are left alone). Home Assistant cannot ask at the moment of deleting, so the choice is made there.

## Known limits

- Sending commands has only been run against simulators, not real hardware (the local EcoFlow Modbus driver is built from EcoFlow's published register map and the app, and has never touched a real battery). New batteries are monitor-only until you allow
  control; run Test control before relying on it. **Help prove it:** after Test control and a day or two of control,
  press **Diagnostics > Hardware report** and send the file (an issue on the project's tracker is ideal). It holds only
  the model, software version, limits, Test control steps, how many commands worked, the measured charge rate and how
  closely the battery followed the plan: no names, keys, serial numbers or addresses. A model is listed as proven once a
  report shows Test control passed and at least 80% of the plan followed (the report says `"proven": true`). No model has
  a report yet.
- Over the cloud, EcoFlow STREAM batteries can be set to Auto, Hold or Charge (no set power, no discharge); Charge has not been tried on a real battery. The ES5000's local Modbus profile does set watts, but has not been run on real hardware either.
- **ES5000:** controlled through the same backup-reserve idea as the Ultra X, using EcoFlow's "STREAM 5000 Series" commands (reserve switch + level, operating mode). While HomePower holds or charges it, the unit is put in Self-consumption, and its own reserve and operating mode (for example Timer tasks) are put back on Auto. Untested on hardware: keep control off until Test control passes. An ES22 may be refused by EcoFlow's API. The reserve and mode HomePower replaced are saved, so a Home Assistant restart while a battery is held does not lose them.
- **EcoFlow checks:** Test control on an EcoFlow battery waits for the cloud and then reports whether it really started charging (an info line, not a pass/fail). Diagnostics lists the settings that changed between readings (`recent_setting_changes`), which shows what the EcoFlow app changed. After error 1006 (a model that refuses the commands) or a rate limit, HomePower backs off for five minutes and says so. An alert is raised if a battery told to charge does not for 20 minutes, and another if an EcoFlow battery's grid reading has the opposite sign to your grid meter.
- **Self-calibrating EcoFlow:** HomePower measures the power an EcoFlow battery really charges at (the median of recent readings while it was told to charge) and plans with that instead of the per-model estimate. A cloud battery gets one cycle to show a command's effect before it counts as unconfirmed, and at most 150 commands a day are sent to each (handing it back to Auto is never blocked). Each battery card can show "Why idle", the main thing in the way, and "Settings changed" with what the EcoFlow app changed.
- **Plan versus actual:** the Overview shows, per battery, what HomePower told it to do over the last 24 hours (dashed) against what it really did (solid), with a "followed the plan" percentage. A low figure means the battery is not following commands or something else is overriding them. The last 8 days are kept.
- **Proving EcoFlow control:** every Hold or Charge sent to an EcoFlow battery is watched for ten minutes and logged ("Command results" on its card, `command_traces` in Diagnostics): did it react, how fast, the most power seen. After three Charge commands in a row with no effect, HomePower stops sending that battery Charge (a schedule that asks for it leaves the battery on its own control, and the plan notes it) and says so; a Test control that sees it charge turns Charge back on. Test control now checks Hold first, then Charge. Until a battery has passed Test control, its first command waits for you to press "Allow first command" on its card. Each last command shows the inputs behind it (reason, charge level, price, grid). Alert notifications on a phone (a `notify.mobile_app_...` service) carry "Pause HomePower" and "Hand back to Auto" buttons.
- **Safe mode** (header button): hands every battery back to its own control, then sends nothing until you leave it. Pause only stops new commands and leaves batteries as they are (an X1 returns to its own control through its watchdog after 10 minutes).
- **EcoFlow control report** (Insights): per battery, how many commands worked, the average reaction time, the real charge rate, how well it followed the plan, and a plain verdict. The wait before a slow battery counts as "not following commands" adapts to how long it has really taken to react (up to four retries).
- **Warnings on save:** saving a schedule or goal still works, but you are told if it cannot do anything (a battery that cannot discharge), if its power setting is ignored (EcoFlow picks its own rate), or if its target is not above the battery's minimum charge.
- **Price changes:** if the cheap hours in the price history differ clearly from the ones in Settings for six hours, you get an alert to update them. **Keep stored energy for dearer hours** (Settings, off by default) holds a battery while the day plan (see [Paid export](#paid-export)) keeps its energy for dearer half-hours later, up to when buying it back is cheapest, but not while solar is charging it. Without prices for the coming hours it holds when a clearly dearer price is coming within six hours. When several batteries compete for a limited import, the one with the better measured efficiency gets the cheapest slots.
- **EcoFlow STREAM 5000 over local Modbus:** pick "EcoFlow STREAM 5000 / AC 5000 (ES5000) - local Modbus" when adding a battery. First switch it on in the EcoFlow app: STREAM system > **Energy management** (not the combined System settings page, which hides it) > Device integration > **Third-party control** > Modbus TCP. It is off by default and greyed out until EcoFlow's cloud accepts the firmware. HomePower then reads the battery every few seconds and sets its battery power in watts (control point 0x1610 = battery, target 0x1611), so Charge and Discharge work like the Anker batteries instead of the cloud reserve trick, with no rate limit. It writes EcoFlow's keep-alive register every 20 s and hands the battery back to its own energy management (control point 0) on Auto. The register map is from EcoFlow's official integration (EcoFlow-oasis-official/ha-EcoFlow-official, MIT); wire addresses are the register offset + 40001, unit id 1. **Not yet verified on hardware:** the sign of the power setpoint is assumed to be + charging; if Test control does the opposite, tick "Reverse the charge/discharge sign". Do not change the same setting in the app and in HomePower at once, and keep control off until Test control passes.
- **Find local control ports** (Diagnostics): type a device's IP address and press Scan. It checks a short fixed list of ports local control usually uses (502 Modbus TCP, 1883 MQTT, 8883 MQTT over TLS, 80/443/8080 web, 4840 OPC UA and a few others) and shows which are open. Scan again after a firmware update, or after switching something on in the vendor app, and it shows what changed. The **Wide scan** button checks every port from 1 to 10,000 on that one device (a minute or two; it gives up and says "incomplete" after 90 seconds, only one runs at a time) and lists just the open ones with a count of the rest, in case local control uses an unusual port. Both only connect and listen for a few bytes: they send nothing, try no login, and refuse any address outside your own network (192.168.x.x, 10.x.x.x, 172.16-31.x.x, link-local and loopback). Meant for batteries that may have gained a local connection, for example an EcoFlow ES5000 after firmware 1.1.7.28, which adds third-party control. That local connection is Modbus TCP on port 502 (see the previous item and [EcoFlow protocol notes](#ecoflow-protocol-notes)): after switching it on in the EcoFlow app, a scan should show 502 open.
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
is in `core/engine.py` (one pure function, fuzz-tested against safety invariants); where stored energy is worth most is
`core/dayplan.py`. The coordinator (`coordinator.py`: the control loop, applying and verifying commands, Test control) is
split by concern: `coord_prices.py` (rates, export, wear, the day plan, grid events), `coord_alerts.py` (alerts, Repairs,
notifications), `coord_forecast.py` (solar forecast, room for solar, weather), `coord_inputs.py` (grid and other readings
from Home Assistant), `coord_accounting.py` (energy, savings and money totals, learning), `coord_reports.py` (the panel's
views, setup check, debug file and hardware report) and `coord_base.py` (what they share). The manager likewise:
`manager.py`, `manager_details.py` (registers, battery settings, the details sheet) and `manager_backup.py`; a loaded
HomePower keeps them on its config entry (`runtime.py`). Checks against
the real Home Assistant classes are in `tests/ha_harness/` (see its README; `python tests/ha_harness/run_all.py` runs them
all, and GitHub Actions runs them on each push; a release is published only when the whole suite, these included, passes).
See [docs/DESIGN.md](docs/DESIGN.md).
Other checks: `tests/*.mjs` and `tests/panel_dom/*.mjs` run the panel in jsdom (`node tests/panel_features_smoke.mjs`; GitHub
Actions runs them on each push too), `tests/panel_dom/panel_browser.mjs` runs it in real headless Chromium (needs
`npm install playwright`), and `tests/test_ecoflow_modbus.py` runs the local EcoFlow driver against a
fake register space that follows EcoFlow's published map.
