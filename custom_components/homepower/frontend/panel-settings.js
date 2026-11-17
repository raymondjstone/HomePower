/* HomePower panel: part of homepower-panel.js (loaded by it; methods are added to the panel class). */
export default (U) => {
  const { MODES, SERIES, DAYS, esc, fmtW, fmtKwh, slug, CSS } = U;
  return {
  _settingsView() {
    const s = this.data.settings || {};
    const num = (k, label, hint, step = 1) => ({ k, t: "num", label, hint, step });
    const SEC = [
      ["Grid meter", "mdi:transmission-tower", [
        { k: "grid_import_entity", t: "entity", dom: ["sensor"], label: "Smart meter import sensor - optional", hint: (this.live.meter_entity && !s.grid_import_entity ? `Using ${this.live.meter_entity} automatically. ` : "") + "Power (W/kW) or a cumulative energy counter (kWh). Leave empty to use Hildebrand Glow (if set up) or the grid reading your batteries report." },
        { k: "grid_export_entity", t: "entity", dom: ["sensor"], label: "Smart meter export sensor - optional", hint: "Subtracted from import. Power or energy counter." },
        { k: "grid_invert", t: "bool", label: "Sensor is positive when exporting" },
        { k: "grid_order", t: "order", label: "Where the grid reading comes from, most trusted first", hint: (this.live.grid_source ? `Using now: ${this.live.grid_source}. ` : "No grid reading right now. ") + "The first one with a reading is used. Untick one to never use it.",
          items: [["sensor", "The sensor chosen above"], ["battery", "A battery that measures the whole house (set per battery)"], ["glow", "Hildebrand Glow smart-meter sensor, found automatically"], ["cloud", "EcoFlow's cloud figure"]] }]],
      ["Limits", "mdi:shield-check", [
        num("import_limit_w", "House import limit (W)", "Caps total charging; with peak shaving on, batteries also discharge to stay under it."),
        { k: "peak_shaving", t: "bool", label: "Peak shaving: discharge to keep import under the limit" },
        num("export_limit_w", "Export limit (W, 0 = none)", "For grid connection rules (G98/G99): charge or trim discharge to stay under it."),
        num("total_charge_limit_w", "Max total charge (W, 0 = none)"), num("total_discharge_limit_w", "Max total discharge (W, 0 = none)"),
        num("cooldown_s", "Re-apply cooldown (s)", "Stops the same command being re-sent too often.")]],
      ["Zero-import battery", "mdi:scale-balance", [{ k: "zero_import_device", t: "device", label: "Keeps grid import at 0 W when nothing else applies" }]],
      ["Tariff & prices", "mdi:cash", [
        { k: "tariff_presets", t: "presets", label: "Quick fill the cheap-rate hours" },
        { k: "rates_entity", t: "entity", dom: ["sensor", "event"], label: "Tariff sensor (Octopus, Agile, Nord Pool, Glow rate...)", hint: (this.live.rates_entity && !s.rates_entity ? `Using ${this.live.rates_entity} automatically. ` : "") + "A list of {start, end, value} in its attributes, or a plain price sensor." },
        { k: "rates_unit", t: "select", label: "Rates are in", options: [["auto", "Detect automatically"], ["pence", "Pence (or cents)"], ["main", "Pounds (or euros)"]] },
        num("price_peak", "Standard rate", "Used when there is no rates entity, or to fill gaps.", "any"), num("price_offpeak", "Cheap rate", "", "any"),
        num("export_price", "Export price (what you are paid per kWh, optional)", "Same unit as the prices above. When exporting pays more than buying the energy back at your cheapest rate, solar goals leave the surplus to export." + (this.live.export_better ? " Right now: exporting pays more." : ""), "any"),
        { k: "offpeak_auto", t: "bool", label: "Work out the night/cheap period from the tariff's price history" + (this.live.tariff_inferred ? ` (estimated ${this.live.tariff_inferred.start}-${this.live.tariff_inferred.end} from ${this.live.tariff_inferred.days} days of readings, which can run up to an hour late: type your exact times below to be sure)` : "") },
        { k: "offpeak_start", t: "time", label: "Cheap rate starts", hint: "Leave both times empty to use the period found from history." }, { k: "offpeak_end", t: "time", label: "Cheap rate ends" },
        { k: "offpeak_extra", t: "text", label: "More cheap periods (optional)", hint: "For tariffs with several cheap periods a day, e.g. 13:00-16:00, 22:00-00:00" },
        { k: "save_for_peak", t: "bool", label: "Save the battery for peak hours: don't let it discharge during cheap-rate periods" },
        { k: "hold_for_dearer", t: "bool", label: "Keep stored energy for dearer hours: don't discharge when a clearly dearer price is coming in the next 6 hours (not while solar is charging the battery)" },
        { k: "currency", t: "text", label: "Currency symbol" }]],
      ["Solar forecast", "mdi:weather-sunny", [
        { k: "solar_room", t: "bool", label: "Leave room for the solar forecast: charge batteries with panels overnight only as far as the next day's solar allows" },
        num("base_load_w", "House base load (W)", "Your steady use (fridge, standby...). Outside cheap hours it is assumed to come from the batteries, which frees room for solar before the sun is strong. 0 = none."),
        { k: "solar_room_note", t: "note", label: this._solarRoomText() },
        { k: "forecast_entity", t: "entity", dom: ["sensor"], label: "Tomorrow's solar forecast (kWh) - optional", hint: "Leave empty to use Forecast.Solar or Solcast automatically. If you pick one, use its 'tomorrow' sensor." },
        num("forecast_share", "Share of forecast solar that reaches the battery (0-1)", "", "0.05")]],
      ["Backup & away", "mdi:shield-home", [
        num("backup_reserve_pct", "Backup reserve (%)", "Backup mode tops up to this and never discharges below it."),
        { k: "forecast_reserve_note", t: "note", label: this._forecastReserveText() },
        num("forecast_reserve_kwh", "Keep a reserve when the day's solar forecast is below (kWh, 0 = off)", "Before midday it uses today's forecast, after it tomorrow's. On a dull day the batteries are not drained below the reserve; on a sunny day they go down to their normal minimum. Needs a solar forecast."),
        num("forecast_reserve_pct", "...the reserve on those days (%)"),
        { k: "storm_entity", t: "entity", dom: ["binary_sensor", "input_boolean", "switch"], label: "Turn backup mode on automatically while this is on (e.g. a weather alert)" },
        { k: "away_entity", t: "entity", dom: ["binary_sensor", "input_boolean", "person", "device_tracker"], label: "Away entity (on / away / not_home turns away mode on)" }]],
      ["Protect loads", "mdi:ev-station", [{ k: "load_rules", t: "rules", label: "Don't charge/discharge the battery while these are busy (EV charger, heat pump...)" }]],
      ["Alerts & heads-ups", "mdi:bell", [{ k: "notify_service", t: "text", label: "Also send alerts to (e.g. notify.mobile_app_phone)", hint: "Alerts always appear as Home Assistant notifications." },
        { k: "notify_full", t: "bool", label: "Tell me when the home battery is full" },
        num("notify_low_pct", "Tell me when the charge falls to (%, 0 = never)"),
        { k: "notify_cheap_soon", t: "bool", label: "Warn me 30 minutes before the cheap rate starts if the battery is below..." },
        num("cheap_soon_below_pct", "...this charge (%)"),
        { k: "daily_summary_time", t: "time", label: "Send a daily summary at", hint: "Battery level, yesterday's charging, savings and solar use. Leave empty for none." },
        { k: "weekly_digest", t: "bool", label: "Also send a weekly digest every Monday (at the summary time above, or 08:00): savings, grid use, outages and battery health" }]],
      ["Solar surplus", "mdi:water-boiler", [
        { k: "divert_entity", t: "entity", dom: ["switch", "input_boolean"], label: "Switch this on when the batteries are full and you are exporting", hint: (this.live.divert ? `Currently ${this.live.divert.on ? "ON" : "off"}. ` : "") + "For example an immersion heater. Switched off again when it would need grid power. Leave empty for none." },
        num("divert_export_w", "Export needed to switch it on (W)", "Roughly the power the device uses."),
        num("divert_min_soc", "Only when the home battery is at least (%)"),
        { k: "divert_cheap", t: "bool", label: "Also switch it on during cheap-rate hours (needs a cheap rate set under Tariff)" }]],
      ["Anker X1 limits", "mdi:transmission-tower", [
        { k: "x1_grid_limits", t: "bool", label: "Also set the X1's own import and export limits to HomePower's (experimental)" },
        { k: "x1_grid_limits_note", t: "note", label: "Writes the limit into the X1 itself so it still holds if Home Assistant is down. It only ever sets a limit that is stricter than one already active, so an installer's grid-code limit is never raised or switched off. The register addresses come from a community integration, not from Anker: check them first with Diagnostics > Registers: save baseline / compare while changing the limit in the Anker app." }]],
      ["Compare tariffs", "mdi:scale-balance", [
        { k: "compare_tariffs_note", t: "note", label: "HomePower records your grid import and export every half-hour. List other tariffs here, one per line, and Insights shows what the same usage would have cost on each. Rates are per kWh in your currency; standing is per day. Example: Octopus Go; peak=0.28; cheap=0.07; window=00:30-05:30; export=0.15; standing=0.45. Add your current tariff as one line to compare against it." },
        { k: "compare_tariffs", t: "area", label: "Tariffs (one per line)" }]],
      ["EcoFlow", "mdi:flash", [{ k: "ecoflow_live", t: "bool", label: "Use EcoFlow's live feed for faster readings (seconds instead of 30 s)" },
        { k: "ecoflow_live_note", t: "note", label: "Off by default: it opens a second connection to EcoFlow with your keys, which could disturb another EcoFlow integration using the same account (such as EcoFlow Cloud). If readings stop updating after turning it on, turn it off again." }]],
      ["Backup & restore", "mdi:backup-restore", [{ k: "backup_restore", t: "backup", label: "" }]],
      ["Test run", "mdi:test-tube", [{ k: "dry_run", t: "bool", label: "Test run (dry run): work everything out and show it, but send no commands to any battery" },
        { k: "dry_run_note", t: "note", label: "Unlike Pause, the plan is still worked out and each command it would send is listed under Events as 'DRY RUN: would send ...'. Use it to try new schedules, goals or a new battery safely. Batteries keep whatever they were last told to do." }]],
      ["If you remove HomePower", "mdi:delete-outline", [{ k: "erase_on_remove", t: "bool", label: "Erase everything when HomePower is deleted from Home Assistant" },
        { k: "erase_on_remove_note", t: "note", label: "Off: your batteries, EcoFlow keys, schedules, goals, scenes and history stay saved, so adding HomePower again brings everything back. On: all of it is erased when you delete HomePower in Settings > Devices & services. Home Assistant cannot ask at the moment of deleting, so choose here first." }]],
    ];
    const field = (f) => {
      const v = s[f.k] ?? "";
      if (f.t === "bool") return `<label style="display:flex;gap:8px;align-items:center;margin:8px 0"><input type="checkbox" id="st-${f.k}" ${s[f.k] ? "checked" : ""}>${esc(f.label)}</label>`;
      if (f.t === "select") return `<div class="f"><label>${esc(f.label)}</label><select id="st-${f.k}">${f.options.map(([val, l]) => `<option value="${val}" ${(v || "auto") === val ? "selected" : ""}>${l}</option>`).join("")}</select></div>`;
      if (f.t === "device") return `<div class="f"><label>${esc(f.label)}</label><select id="st-${f.k}"><option value="">None</option>${this.data.devices.map((d) => `<option value="${esc(d.device_id)}" ${v === d.device_id ? "selected" : ""}>${esc(d.name)}</option>`).join("")}</select></div>`;
      if (f.t === "order") {
        const cur = String(s[f.k] || f.items.map(([k]) => k).join(",")).split(",").map((x) => x.trim()).filter(Boolean);
        const all = [...cur.filter((k) => f.items.some(([i]) => i === k)), ...f.items.map(([k]) => k).filter((k) => !cur.includes(k))];
        const label = Object.fromEntries(f.items);
        return `<div class="f"><label>${esc(f.label)}</label><div id="st-${f.k}" class="ordlist">${all.map((k) => `<div class="ord" data-key="${k}"><input type="checkbox" class="o-on" ${cur.includes(k) ? "checked" : ""} aria-label="Use ${esc(label[k])}"><span>${esc(label[k])}</span><button class="btn sm" data-act="ord-up" title="Move up" aria-label="Move up">▲</button><button class="btn sm" data-act="ord-down" title="Move down" aria-label="Move down">▼</button></div>`).join("")}</div>${f.hint ? `<span class="hint">${esc(f.hint)}</span>` : ""}</div>`;
      }
      if (f.t === "area") return `<div class="f"><label>${esc(f.label)}</label><textarea id="st-${f.k}" rows="5" style="width:100%;box-sizing:border-box;font-family:inherit" autocomplete="off">${esc(v ?? "")}</textarea></div>`;
      if (f.t === "note") return `<p class="hint" style="margin:0 0 8px">${esc(f.label)}</p>`;
      if (f.t === "presets") return `<div class="presets">${[["Octopus Go", "00:30", "05:30", ""], ["Intelligent Octopus Go", "23:30", "05:30", ""], ["Economy 7 (check your bill)", "00:30", "07:30", ""], ["23:30 - 07:30", "23:30", "07:30", ""], ["Cosy Octopus (3 periods)", "04:00", "07:00", "13:00-16:00, 22:00-00:00"]]
        .map(([n, a, b, x]) => `<button class="btn sm" data-act="tariff-preset" data-a="${a}" data-b="${b}" data-x="${x}">${esc(n)}${x ? "" : ` ${a}-${b}`}</button>`).join("")}</div><span class="hint">Fills the two time boxes below. Prices differ by tariff, so also enter the standard and cheap rate (or a rates sensor).</span>`;
      if (f.t === "backup") return `<p class="hint" style="margin-top:0">Save everything (batteries, schedules, goals, scenes and settings) to a file, or restore it later, for example after rebuilding Home Assistant.</p>
        <label style="display:flex;gap:8px;align-items:center;margin:6px 0"><input type="checkbox" id="bk-secrets">Include EcoFlow access keys in the file (keep the file private)</label>
        <div class="presets"><button class="btn sm" data-act="do-export"><ha-icon icon="mdi:download"></ha-icon>Download backup</button></div>
        <div class="f"><label>Restore from a backup file</label><input type="file" id="imp-file" accept=".json,application/json"></div>
        <div class="presets"><button class="btn sm" data-act="do-import"><ha-icon icon="mdi:upload"></ha-icon>Restore</button></div><div class="err" id="bk-msg"></div>
        <div class="f" style="margin-top:12px"><label>Automatic daily copies (kept in Home Assistant's config folder, newest 7)</label>${(this._backups || []).length ? this._backups.map((b) => `<div style="display:flex;gap:10px;align-items:center;margin:4px 0"><span>${esc(b.name.replace(/^homepower-|\.json$/g, ""))}</span><span class="sub">${Math.max(1, Math.round(b.size / 1024))} KB</span><button class="btn sm" data-act="restore-backup" data-id="${esc(b.name)}">Restore</button></div>`).join("") : '<span class="hint">None yet: the first is written a day after HomePower starts. Press Back up now to make one.</span>'}</div>
        <div class="presets"><button class="btn sm" data-act="backup-now"><ha-icon icon="mdi:content-save"></ha-icon>Back up now</button></div>`;
      if (f.t === "rules") {
        const rows = (s.load_rules || []).map((r) => this._ruleRow(r)).join("");
        return `<div class="f"><label>${esc(f.label)}</label><div id="st-rules">${rows}</div><div><button class="btn sm" data-act="add-rule">+ Add rule</button></div></div>`;
      }
      const dl = f.t === "entity" ? `list="dl-st-${f.k}"` : "", opts = f.t === "entity" ? `<datalist id="dl-st-${f.k}">${this._entityOptions(f.dom)}</datalist>` : "";
      const type = f.t === "num" ? "number" : f.t === "time" ? "time" : "text";
      return `<div class="f"><label>${esc(f.label)}</label><input type="${type}" id="st-${f.k}" ${dl} value="${esc(v ?? "")}" ${f.t === "num" ? `step="${f.step || 1}"` : ""} autocomplete="off">${opts}${f.hint ? `<span class="hint">${esc(f.hint)}</span>` : ""}</div>`;
    };
    this._settingKeys = SEC.flatMap(([, , fs]) => fs);
    return `<div class="bar-actions"><h2>Settings</h2></div><div style="max-width:720px">${SEC.map(([h, icon, fs]) => `<div class="card" style="margin-bottom:16px"><h2><ha-icon icon="${icon}"></ha-icon>${h}</h2>${fs.map(field).join("")}</div>`).join("")}
      <div class="err" id="g-msg" ${this._savedNote ? 'style="color:#16a34a"' : ""}>${this._savedNote ? "Saved." : ""}</div><div class="foot"><span></span><div class="r"><button class="btn pri" data-act="save-settings">Save settings</button></div></div></div>`;
  },

  _ruleRow(r) {
    return `<div class="rule"><div class="f"><label>Entity</label><input type="text" class="r-ent" list="dl-rule" value="${esc(r.entity || "")}" placeholder="sensor.ev_charger_power"><datalist id="dl-rule">${this._entityOptions(["sensor", "binary_sensor", "switch", "input_boolean"])}</datalist></div>
      <div class="f"><label>Busy above (W, blank = on/off)</label><input type="number" class="r-above" value="${r.above_w ?? ""}"></div>
      <div class="f"><label>Block</label><select class="r-block">${["discharge", "charge", "both"].map((b) => `<option ${r.block === b ? "selected" : ""}>${b}</option>`).join("")}</select></div>
      <button class="btn sm" data-act="del-rule">✕</button></div>`;
  },

  _collectSettings() {
    const r = this.shadowRoot, out = {};
    for (const f of this._settingKeys || []) {
      if (f.t === "rules") {
        out[f.k] = [...r.querySelectorAll("#st-rules .rule")].map((row) => ({ entity: row.querySelector(".r-ent").value.trim(), above_w: row.querySelector(".r-above").value, block: row.querySelector(".r-block").value })).filter((x) => x.entity);
        continue;
      }
      const el = r.getElementById("st-" + f.k);
      if (!el) continue;
      if (f.t === "order") { out[f.k] = [...(el.querySelectorAll ? el.querySelectorAll(".ord") : [])].filter((row) => row.querySelector(".o-on").checked).map((row) => row.dataset.key).join(","); continue; }
      out[f.k] = f.t === "bool" ? el.checked : el.value.trim ? el.value.trim() : el.value;
    }
    return out;
  },

  };
};
