# HomePower feature tour: narration

Voice: Piper en_GB-vctk-medium, speaker p249.

**00:00:00  Intro**  
This is HomePower, a battery manager for Home Assistant. It talks to your home batteries directly, over your own network or the manufacturer's own interface, then schedules and optimises when they charge and discharge. Everything you'll see lives in one sidebar panel. Let's take a tour.

**00:00:15  Overview**  
The Overview tab opens with the home battery: every battery added together as one. Here, two batteries hold twelve point two kilowatt hours, are discharging at one and a half kilowatts, and will last nearly seven hours at that rate.

**00:00:28  Overview**  
Below that, if the grid went down now, HomePower estimates how long the batteries could run the house, from your learned usual use. Then the tiles: battery flow, stored energy, grid power, batteries online, today's savings, and the electricity price right now.

**00:00:42  Overview · Batteries**  
Each battery gets its own card, with its charge ring, its power and limits, and a Why chip that says exactly why it's doing what it's doing. A live trace runs underneath, and the Override button is one tap away.

**00:00:53  Overview · Scenes and live power**  
Scenes are saved combinations of backup, away, pause and limits. Normal, Holiday, Storm coming and Hands off come built in, and you can add your own. Below them is a live power chart for each battery.

**00:01:04  Overview · Live power**  
Its time scale runs from fifteen minutes up to twenty-four hours, and clicking an item in the legend hides or shows that line.

**00:01:11  Overview · Today's plan**  
At the bottom, today's plan shows what each battery will do across the day, and the activity log records every schedule that starts or finishes, and every hand-back.

**00:01:20  Manual override**  
A manual override forces a mode on some or all batteries for a set time: Auto, Charge, Discharge or Hold, with an optional power and a level to stop at. It beats everything else, ends by itself, and survives a restart of Home Assistant.

**00:01:32  Edit scene**  
Editing a scene lets you choose backup, away or pause, and change the house import limit, the export limit, and the total charge and discharge caps while it's active.

**00:01:41  Battery details**  
The cog on any battery opens its details sheet: its readings now, HomePower's settings for it, the settings you can change on the battery itself, its Home Assistant device, and its connection, all in one place.

**00:01:53  Plan**  
The Plan tab starts with goals. A goal is something like ninety percent by seven in the morning. HomePower works out how much energy is missing, and charges in the cheapest half-hours before the deadline, re-planning every cycle.

**00:02:05  Plan**  
The price chart covers the next twenty-four hours, with the planned charging in green. It also marks planned export, energy kept back for dearer hours, and grid saving sessions. Underneath is a timeline of what each battery will do next.

**00:02:18  Edit goal**  
A goal can charge from the grid, from solar only, or from solar first and then the cheapest grid slots. It can learn your usual use and charge only what the day needs, and it can charge less when the solar forecast is good.

**00:02:29  Schedules**  
Schedules tell one, several or all batteries to charge, discharge, hold, or run on Auto during a time window. Today's timeline shows them at a glance. Here, there's an evening peak discharge, and a cheap night charge.

**00:02:41  Edit schedule**  
Each schedule has a power, a level to stop at, start and end times that can cross midnight, and days of the week. It can follow a tariff entity instead of the clock, apply only when you're home or away, run for a season, charge from the grid, from solar only, or both, and share one total power across several batteries.

**00:02:58  Hold window**  
A hold window is a one-off. Pick a date and a time, and the batteries neither charge nor discharge. Handy for a planned outage, or an expensive evening event.

**00:03:07  Batteries**  
On the Batteries tab, you add, edit, reorder and control each battery. A new battery starts monitor-only: HomePower reads it, but sends nothing until you allow control.

**00:03:17  Add battery**  
Supported batteries include the Anker SOLIX Solarbank range and the X1 over local Modbus, the EcoFlow STREAM range through EcoFlow's developer interface or local Modbus, Victron GX for monitoring, any Modbus battery described by a small register map, and anything else through Home Assistant entities. Anker batteries are found with a network scan.

**00:03:35  Edit battery**  
Each battery has a role, everyday or reserve, a priority that decides which discharges first, whether it has solar panels, and whether its meter measures the whole house.

**00:03:45  How HomePower decides**  
Every ten seconds, HomePower reads every battery and decides what each should do. The first rule that applies wins: a manual override, then the backup reserve, a grid event, a schedule, a goal plan, the zero-import battery, and finally paid export. After that come peak shaving, export limiting and hold-back, and anything left is handed back to the battery's own control. Every decision has a visible reason.

**00:04:07  Insights**  
Insights tracks savings today and this month, grid import and export, solar, and what exporting pays right now. The tariff comparison replays your recorded usage against other tariffs, and battery health tracks the weekly round-trip efficiency.

**00:04:20  Insights · Plan versus actual**  
Plan versus actual draws what each battery was told against what it really did over the last twenty-four hours, with a match percentage, so a battery that isn't following commands stands out straight away.

**00:04:30  Insights · What if, and history**  
What if I changed the battery settings replays your house load and solar through a simple battery model, comparing two reserve and top-up settings. Then come lifetime totals for each battery, and long-term history charts of power and charge level, from six hours to thirty days.

**00:04:45  Settings · Grid meter and limits**  
Settings holds everything else. Grid meter picks where the import and export reading comes from, in order of preference. Limits cover the house import limit, peak shaving, the G ninety-eight or G ninety-nine export limit, and overall charge and discharge caps.

**00:04:58  Settings · Tariff and prices**  
The zero-import battery holds grid import at zero watts. Tariff and prices takes a rates sensor, such as Octopus Agile, or a simple two-rate tariff, with quick-fill buttons for Octopus Go, Intelligent Octopus Go, Economy Seven, and Cosy Octopus.

**00:05:13  Settings · Paid export and grid events**  
Paid export sells stored energy in the best-paid half-hours, but only when that beats buying it back later, allowing for losses and battery wear. Grid events follow saving sessions, discharging through the event, and can charge up beforehand.

**00:05:26  Settings · Solar forecast and cold weather**  
The solar forecast, from Forecast dot Solar or Solcast, leaves room for tomorrow's sun, so it's never wasted on a full battery. Cold weather raises the overnight target when a cold day is coming.

**00:05:37  Settings · Backup, loads and alerts**  
Backup and away modes can switch on by themselves, from a weather warning or your presence. Protect loads stops the batteries charging or discharging while an E V charger or heat pump is running. Alerts and heads-ups go to Home Assistant's repairs and notifications, and to any notify service.

**00:05:53  Settings · And more**  
There's also a solar surplus switch for an immersion heater, the Anker X1's own limits, tariff comparisons, EcoFlow keys, backup and restore with daily automatic copies, and a test run mode that plans as normal, but sends nothing.

**00:06:06  Diagnostics**  
Diagnostics shows each battery's connection, readings and last command, with Test control, which sends a tiny, safe command and checks the battery follows it. There are Modbus register tools, a local port scanner, a Why it did that log for every decision, debug downloads, and a one-click Report an issue.

**00:06:22  Dashboard card**  
For your dashboards, the HomePower card shows the home battery at a glance: total charge, stored energy, flow, how long it will last, and each battery underneath. HomePower also adds sensors for the Energy dashboard, switches for pause, backup and away, and services for your automations.

**00:06:38  Outro**  
That's HomePower. Install it from the Home Assistant Marketplace, add a battery, and let it do the planning. Thanks for watching.
