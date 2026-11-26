/* HomePower panel: part of homepower-panel.js (loaded by it; methods are added to the panel class).
   The battery details sheet: readings, HomePower's settings, the settings the battery lets you change, what it reports itself,
   its solar strings and the matching Home Assistant device. */
export default (U) => {
  const { MODES, SERIES, DAYS, esc, fmtW, fmtKwh, slug, CSS } = U;
  return {
  // The sheet for one battery: HomePower's settings, the live readings, the settings the battery lets you change (checked and sent by
  // the backend), what the battery itself reports (Anker's named registers, EcoFlow's values) and the Home Assistant device and its
  // controls. Opened from the cog on a battery card.
  async _batteryDetails(id) {
    const d = this.data.devices.find((x) => x.device_id === id);
    if (!d) return;
    this._settingMsg = {}; this._haMsg = {};
    const mine = (this._detail = { d, dg: null, det: null });
    this._openModal(this._detailsHtml(d, null, null));
    const [dg, det] = await Promise.all([
      this._hass.callWS({ type: "homepower/diagnostics" }).then((r) => (r.devices || {})[id] || false).catch(() => false),
      this._hass.callWS({ type: "homepower/device/details", device_id: id }).catch((e) => ({ error: e.message || String(e) })),
    ]);
    if (this._detail !== mine) return;      // another battery's sheet was opened while this one was loading
    this._detail = { d, dg, det };
    this._paintDetails();
  },

  // Draws the sheet again from what it already has, keeping the scroll position and which groups were open.
  _paintDetails() {
    const x = this._detail, box = this.shadowRoot.getElementById("modal");
    if (!x || !this._modal || !box || !this.shadowRoot.getElementById("bd-" + x.d.device_id)) return;
    const open = new Map([...box.querySelectorAll("details")].map((e) => [(e.querySelector("summary") || {}).textContent, e.open]));
    const top = (box.querySelector(".modal") || {}).scrollTop || 0;
    const typed = new Map([...box.querySelectorAll('input[id^="bs-"], select[id^="bs-"], input[id^="ha-"], select[id^="ha-"]')].map((e) => [e.id, e.value]));      // what was typed or chosen in the other boxes
    box.innerHTML = this._detailsHtml(x.d, x.dg, x.det);
    for (const e of box.querySelectorAll("details")) { const k = (e.querySelector("summary") || {}).textContent; if (open.has(k)) e.open = open.get(k); }
    for (const [id, v] of typed) { const e = box.querySelector('[id="' + id + '"]'); if (e && e.value !== v) e.value = v; }
    const m = box.querySelector(".modal"); if (m) m.scrollTop = top;
  },

  // The battery's own settings, as editors: a number, a choice or a switch each, with the vendor's limits; one that cannot be changed
  // right now shows why.
  _settingsHtml(d, det) {
    const list = (det && det.settings) || [];
    if (!list.length) return "";
    const id = esc(d.device_id), flash = this._settingMsg || {};
    const shown = (x) => (x.kind === "select" ? ((x.options.find((o) => o.value === x.value) || {}).label ?? x.value) : x.kind === "switch" ? (x.value ? "On" : "Off") : `${x.value}${x.unit ? " " + x.unit : ""}`);
    const row = (x) => {
      const k = esc(x.key), msg = flash[x.key] ? `<div class="${flash[x.key].err ? "err" : "sub"}" style="font-size:12px">${esc(flash[x.key].text)}</div>` : "";
      let ctl;
      if (x.kind === "info") ctl = esc(String(x.value));
      else if (x.locked) ctl = `${esc(shown(x))} <span class="chip warn">locked</span><div class="sub" style="font-size:12px;color:var(--secondary-text-color)">${esc(x.locked)}</div>`;
      else if (x.kind === "number") ctl = `<input type="number" id="bs-${k}" value="${esc(x.value)}" min="${x.min}" max="${x.max}" step="${x.step || 1}" style="width:96px"> ${esc(x.unit)} <button class="btn sm pri" data-act="setting-set" data-id="${id}" data-key="${k}">Set</button> <span class="sub" style="font-size:12px;color:var(--secondary-text-color)">${x.min} to ${x.max}</span>`;
      else if (x.kind === "select") ctl = `<select id="bs-${k}">${x.options.map((o) => `<option value="${esc(o.value)}" ${o.value === x.value ? "selected" : ""}>${esc(o.label)}</option>`).join("")}</select> <button class="btn sm pri" data-act="setting-set" data-id="${id}" data-key="${k}">Set</button>`;
      else ctl = `${x.value ? "On" : "Off"} <button class="btn sm" data-act="setting-set" data-id="${id}" data-key="${k}" data-value="${x.value ? 0 : 1}">Turn ${x.value ? "off" : "on"}</button>`;
      return `<tr><th>${esc(x.label)}</th><td>${ctl}${x.hint && !x.locked && x.kind !== "info" ? `<div class="sub" style="font-size:12px;color:var(--secondary-text-color)">${esc(x.hint)}</div>` : ""}${msg}</td></tr>`;
    };
    return ["Controls", "Configuration"].map((g) => { const l = list.filter((x) => x.group === g); return l.length ? `<div style="margin-top:6px;font-weight:600">${g}</div><div><table class="tbl">${l.map(row).join("")}</table></div>` : ""; }).join("");
  },

  // Sends one change to the battery (the backend checks it against the vendor's own limits and rules first).
  async _applySetting(id, key, given) {
    const x = this._detail, s = ((x && x.det && x.det.settings) || []).find((q) => q.key === key);
    if (!s) return;
    const msg = (this._settingMsg ||= {});
    let value = given !== undefined ? given : (this.shadowRoot.getElementById("bs-" + key) || {}).value;
    if (s.kind === "switch") value = String(value) === "1";
    else {
      const typed = String(value ?? "").trim();      // a blank box is not 0
      value = typed === "" ? NaN : Number(typed);
      if (!Number.isFinite(value)) { msg[key] = { err: true, text: "Enter a number." }; return this._paintDetails(); }
    }
    const what = s.kind === "select" ? ((s.options.find((o) => o.value === value) || {}).label ?? value) : s.kind === "switch" ? (value ? "on" : "off") : `${value}${s.unit ? " " + s.unit : ""}`;
    if (!confirm(`Change "${s.label}" on ${x.d.name} to ${what}?
This is sent to the battery now.`)) return;
    msg[key] = { text: "Sending…" }; this._paintDetails();
    try {
      const r = await this._hass.callWS({ type: "homepower/device/setting", device_id: id, key, value });
      if ((r.settings || []).length) x.det.settings = x.det.settings.map((o) => r.settings.find((n) => n.key === o.key) || o);      // the new values, keeping any control the reply did not mention
      msg[key] = { text: r.pending ? "Sent. EcoFlow's cloud applies it within a few seconds: press Refresh to see it." : "Saved. The battery now reports this value." };
    } catch (e) { msg[key] = { err: true, text: e.message || String(e) }; }
    this._paintDetails();
  },

  // A control of the matching Home Assistant device, changed the way Home Assistant itself does it (its own services).
  async _haSet(eid, op) {
    const dom = eid.split(".")[0], el = this.shadowRoot.getElementById("ha-" + eid), msg = (this._haMsg ||= {});
    let service, data = { entity_id: eid };
    if (op === "number") { service = "set_value"; data.value = Number(el && el.value); if (!el || el.value === "" || !Number.isFinite(data.value)) { msg[eid] = { err: true, text: "Enter a number." }; return this._paintDetails(); } }
    else if (op === "text") { service = "set_value"; data.value = el ? el.value : ""; }
    else if (op === "select") { service = "select_option"; data.option = el ? el.value : ""; }
    else if (op === "on" || op === "off") service = op === "on" ? "turn_on" : "turn_off";
    else if (op === "press") service = "press";
    else return;
    msg[eid] = { text: "Sending…" }; this._paintDetails();
    try { await this._hass.callService(dom, service, data); msg[eid] = { text: "Sent." }; }
    catch (e) { msg[eid] = { err: true, text: e.message || String(e) }; }
    this._paintDetails();
    setTimeout(() => this._paintDetails(), 1500);      // Home Assistant has the new state by then
  },

  // The Home Assistant device(s) that belong to this battery: the one its mapped entities sit on, else one whose serial
  // number or name matches. Returns [{dev, why}].
  _haDevicesFor(d, serial) {
    const hd = this._hass.devices || {}, en = this._hass.entities || {};
    const norm = (x) => String(x || "").trim().toLowerCase();
    const out = new Map();
    for (const eid of Object.values(d.entities || {})) {
      const e = en[eid];
      if (e && e.device_id && hd[e.device_id]) out.set(e.device_id, "the entities you chose for this battery");
    }
    if (!out.size && serial) {
      for (const x of Object.values(hd)) if (norm(x.serial_number) === norm(serial)) out.set(x.id, "its serial number");
    }
    if (!out.size) {
      const names = [norm(d.name), norm(this._profile(d.kind).label)].filter(Boolean);
      for (const x of Object.values(hd)) if (names.includes(norm(x.name_by_user || x.name))) out.set(x.id, "its name");
    }
    return [...out].map(([id, why]) => ({ dev: hd[id], why }));
  },

  _haDeviceHtml(dev, why) {
    const st = this._hass.states, en = Object.values(this._hass.entities || {}).filter((e) => e.device_id === dev.id);
    const nm = dev.name_by_user || dev.name || dev.id;
    const label = (e) => { const f = (st[e.entity_id] || { attributes: {} }).attributes.friendly_name || e.name || e.entity_id; return f.startsWith(nm) && f.length > nm.length ? f.slice(nm.length).trim() : f; };
    const cat = (e) => String(e.entity_category ?? "");
    const control = new Set(["number", "select", "switch", "button", "text", "time", "date", "datetime", "input_number", "input_select", "input_boolean"]);
    const row = (e) => {
      const s = st[e.entity_id], a = (s && s.attributes) || {};
      let v = s ? s.state : "not available";
      if (a.unit_of_measurement && !["unavailable", "unknown"].includes(v)) v += " " + a.unit_of_measurement;
      const dom = e.entity_id.split(".")[0];
      const extra = [dom === "number" && a.min != null ? `${a.min} to ${a.max}${a.step ? ", step " + a.step : ""}` : "", dom === "select" && (a.options || []).length ? `options: ${a.options.join(", ")}` : ""].filter(Boolean).join(" · ");
      const unavailable = !s || ["unavailable", "unknown"].includes(s.state), id = esc(e.entity_id), m = (this._haMsg || {})[e.entity_id];
      let edit = "";
      if (!unavailable && ["number", "input_number"].includes(dom)) edit = `<input type="number" id="ha-${id}" value="${esc(s.state)}" ${a.min != null ? `min="${a.min}"` : ""} ${a.max != null ? `max="${a.max}"` : ""} step="${a.step || "any"}" style="width:96px"> <button class="btn sm pri" data-act="ha-set" data-op="number" data-eid="${id}">Set</button>`;
      else if (!unavailable && ["select", "input_select"].includes(dom)) edit = `<select id="ha-${id}">${(a.options || []).map((o) => `<option ${o === s.state ? "selected" : ""}>${esc(o)}</option>`).join("")}</select> <button class="btn sm pri" data-act="ha-set" data-op="select" data-eid="${id}">Set</button>`;
      else if (!unavailable && ["switch", "input_boolean"].includes(dom)) edit = `<button class="btn sm" data-act="ha-set" data-op="${s.state === "on" ? "off" : "on"}" data-eid="${id}">Turn ${s.state === "on" ? "off" : "on"}</button>`;
      else if (!unavailable && dom === "button") edit = `<button class="btn sm" data-act="ha-set" data-op="press" data-eid="${id}">Press</button>`;
      else if (!unavailable && ["text", "input_text"].includes(dom)) edit = `<input type="text" id="ha-${id}" value="${esc(s.state)}" style="width:160px"> <button class="btn sm pri" data-act="ha-set" data-op="text" data-eid="${id}">Set</button>`;
      const shownValue = edit && ["number", "input_number", "select", "input_select", "text", "input_text"].includes(dom) ? "" : esc(v);
      return `<tr><th title="${esc(e.entity_id)}">${esc(label(e))}</th><td>${shownValue}${shownValue && edit ? " " : ""}${edit}${a.unit_of_measurement && edit && ["number", "input_number"].includes(dom) ? " " + esc(a.unit_of_measurement) : ""}${extra ? `<div class="sub" style="font-size:12px;color:var(--secondary-text-color)">${esc(extra)}</div>` : ""}${m ? `<div class="${m.err ? "err" : "sub"}" style="font-size:12px">${esc(m.text)}</div>` : ""}</td></tr>`;
    };
    const ctl = en.filter((e) => control.has(e.entity_id.split(".")[0]) || ["config", "1"].includes(cat(e)));
    const diag = en.filter((e) => !ctl.includes(e) && ["diagnostic", "2"].includes(cat(e)));
    const rest = en.filter((e) => !ctl.includes(e) && !diag.includes(e));
    const grp = (title, list, open) => (list.length ? `<details ${open ? "open" : ""} style="margin-top:6px"><summary>${title} (${list.length})</summary><table class="tbl">${list.sort((a, b) => label(a).localeCompare(label(b))).map(row).join("")}</table></details>` : "");
    return `<div class="sub" style="font-size:13px;color:var(--secondary-text-color);margin:4px 0">${esc(nm)}${dev.manufacturer ? " · " + esc(dev.manufacturer) : ""}${dev.model ? " " + esc(dev.model) : ""}${dev.sw_version ? " · software " + esc(dev.sw_version) : ""}${dev.hw_version ? " · hardware " + esc(dev.hw_version) : ""} (found by ${esc(why)})</div>`
      + (en.length ? grp("Controls and settings", ctl, true) + grp("Readings", rest, true) + grp("Diagnostic", diag, false) : `<div class="sub" style="font-size:13px;color:var(--secondary-text-color)">This device has no entities.</div>`);
  },

  _detailsHtml(d, dg, det) {
    const id = d.device_id, r = this._reading(d), lv = r.lv || {}, prof = this._profile(d.kind);
    const when = (s) => (s ? new Date(s).toLocaleString([], this._tzOpt()) : null);
    const yn = (v) => (v ? "Yes" : "No");
    const L = (lv.limits || null);
    const rows = (list) => `<table class="tbl">${list.filter(([, v]) => v != null && v !== "").map(([k, v]) => `<tr><th>${esc(k)}</th><td>${v}</td></tr>`).join("")}</table>`;
    const t = (v) => (v == null ? null : esc(String(v)));
    const w = (v) => (v == null ? null : fmtW(v));
    const p = r.power, A = (det && det.actual) || {}, settingsHtml = this._settingsHtml(d, det);
    const regv = (re) => { const x = ((det && det.registers) || []).find((q) => re.test(q.name)); return x ? x.value : null; };
    const info = [regv(/^model( name)?$/i), (regv(/^software version$/i) || lv.firmware) ? "Software " + (regv(/^software version$/i) || lv.firmware) : null, det && det.serial ? "Serial " + det.serial : null].filter(Boolean);
    const settings = rows([
      ["Model", esc(prof.label)], ["Connects through", t(prof.driver)],
      ["Capacity", d.capacity_wh ? fmtKwh(d.capacity_wh) : null], ["Max charge", d.max_charge_w ? fmtW(d.max_charge_w) : null], ["Max discharge", d.max_discharge_w ? fmtW(d.max_discharge_w) : null],
      ["Minimum charge", d.min_soc != null ? d.min_soc + "%" : null], ["Priority", t(d.priority)], ["Role", d.role === "reserve" ? "Reserve" : "Everyday"],
      ["Solar panels attached", t(d.solar || "auto")], ["Meter measures whole house", t(d.grid_meter || "auto")],
      ["Modes", (d.supported_modes || []).map((m) => (MODES[m] || {}).label || m).join(", ")],
      ["HomePower may send commands", yn(d.control_enabled !== false)],
      ["Zero-import battery", this.data.settings.zero_import_device === id ? "Yes" : null],
      ...Object.entries(d.entities || {}).filter(([, v]) => v).map(([k, v]) => [`Entity: ${k}`, esc(v)]),
      ...Object.entries(d.mode_map || {}).filter(([, v]) => v).map(([k, v]) => [`Mode name: ${k}`, esc(v)]),
      ...(d.invert_power ? [["Power sign flipped", "Yes"]] : []),
    ]);
    const live = rows([
      ["Status", r.stale ? "Readings stale (showing the last good read)" : r.online ? "Online" : r.waiting ? "Waiting for readings" : "Offline"], ["Why offline", !r.online && !r.waiting ? t(lv.offline_hint) : null],
      ["Last failure", r.stale ? t(lv.stale_reason) : !r.online ? t(lv.offline_reason) : null], ["Last read", r.stale || !r.online ? when(lv.last_read_ok) : null], ["Charge", r.soc != null ? Math.round(r.soc) + "%" : null],
      ["Stored energy", r.soc != null && d.capacity_wh ? fmtKwh((r.soc / 100) * d.capacity_wh) : null],
      ["Power", p == null ? null : Math.abs(p) < 20 ? "Idle" : (p > 0 ? "Charging " : "Discharging ") + fmtW(Math.abs(p))],
      ["Solar input", lv.pv_w != null ? fmtW(lv.pv_w) : w(A.pv_w)], ["House load", w(A.load_w)],
      ["Grid", A.grid_w != null ? `${fmtW(Math.abs(A.grid_w))} ${A.grid_w > 0 ? "import" : A.grid_w < 0 ? "export" : ""}` : null],
      ["Set point on the battery", A.set_power_w != null ? fmtW(A.set_power_w) : null],
      ["Target charge it holds", A.target_soc != null ? A.target_soc + "%" : null], ["Reserve it holds", A.reserve_soc != null ? A.reserve_soc + "%" : null],
      ["Temperature", lv.temp_c != null ? lv.temp_c.toFixed(1) + " °C" : null],
      ["Battery pack readings", (A.pack_v || []).length ? esc(A.pack_v.join(", ")) + " (raw, one per pack)" : null],
      ["Software version", t(lv.firmware)], ["Mode it reports", t((MODES[lv.mode] || {}).label || lv.mode)],
      ["HomePower wants", (() => {
        const plan = lv.desired_mode ? esc((MODES[lv.desired_mode] || {}).label || lv.desired_mode) + (lv.desired_power_w ? " " + fmtW(lv.desired_power_w) : "") : null;
        const idle = d.control_enabled === false ? "monitor only" : this.data.paused ? "HomePower is paused" : null;   // then nothing is sent to it
        if (idle) return `Nothing: ${idle}` + (plan && lv.desired_mode !== "auto" ? ` (the plan would say ${plan})` : "");
        return plan || "Nothing (the battery runs itself)";
      })()],
      ["Because", t(lv.reason)], ["Why idle", lv.why && !lv.last_error ? esc(lv.why) : null],
      ["Battery's own limits", L ? `charges to ${L[0]}%, stops discharging at ${L[1]}%${L[3] ? `, backup reserve ${L[2]}%` : ""}` : null],
      ["Charges at (measured)", lv.measured_charge_w ? fmtW(lv.measured_charge_w) : null],
      ["Grid down", lv.off_grid ? "Yes" : null], ["Protection active", lv.protected ? "Yes" : null], ["SOC limit reached", lv.guard_held ? "Yes" : null],
      ["Alarms", (lv.alarms || []).length ? esc(lv.alarms.join(", ")) : null],
      ["Active schedules", (lv.active_schedules || []).length ? esc(lv.active_schedules.join(", ")) : null],
      ["Manual override", lv.override ? `${esc((MODES[lv.override.mode] || {}).label || lv.override.mode)} until ${when(lv.override.until)}` : null],
      ["Last write failed", t(lv.last_error)],
      ["Charge command", lv.charge_verdict === "unreliable" ? "Not working (no effect)" : null],
      ["Command results", (lv.traces || []).length ? (lv.traces || []).map((x) => esc(`${(x.at || "").slice(5, 16).replace("T", " ")} ${x.mode}: ${x.ok ? "reacted after " + x.reaction_s + " s" : "no effect"}, peak ${x.peak_w} W`)).join("<br>") : null],
      ["Battery ID (for actions)", `<code>${esc(id)}</code>`],
      ["Settings changed", (lv.setting_changes || []).length ? lv.setting_changes.map((c) => esc(`${c.at} ${c.key}: ${c.from} → ${c.to}`)).join("<br>") : null],
    ]);
    const loading = `<div class="sub" style="font-size:13px;color:var(--secondary-text-color)">Loading…</div>`;
    const none = (m) => `<div class="sub" style="font-size:13px;color:var(--secondary-text-color)">${m}</div>`;
    // What HomePower knows about the connection.
    let tech = dg === null ? loading : none("Not available.");
    if (dg) {
      const conn = Object.entries(dg.connection || {}).filter(([k]) => k !== "map").map(([k, v]) => `${k}=${v}`).join("  ");
      const lc = dg.last_command;
      tech = rows([
        ["Connection", conn ? esc(conn) : null], ["Last good read", when(dg.last_read_ok) || "never"],
        ["32-bit word order", t(dg.word_order)], ["Driver error", t(dg.driver_error)],
        ["Recent dropouts", (dg.recent_outages || []).length ? dg.recent_outages.slice().reverse().map((o) => esc(`${o.at}: silent for ${o.seconds >= 120 ? Math.round(o.seconds / 60) + " min" : o.seconds + " s"}`)).join("<br>") : null],
        ["Solar readings held back", dg.pv_readings_ignored ? `${dg.pv_readings_ignored} (EcoFlow sometimes reports grid charging as solar; those readings were replaced by the last good one)` : null],
        ["Last command", lc ? `${esc(lc.mode)}${lc.power_w ? " " + fmtW(lc.power_w) : ""} at ${esc(new Date(lc.at).toLocaleTimeString([], this._tzOpt()))} - ${esc(lc.result)}` : "none yet"],
        ["Unconfirmed commands", dg.unconfirmed ? dg.unconfirmed : null], ["Holding control", dg.release_pending ? "Yes" : null],
      ]);
    }
    // What the battery itself reports: Anker's named registers, or EcoFlow's values.
    let onBattery = det === null ? loading : "";
    if (det && det.error && !det.registers && !det.raw) onBattery = `<div class="err">${esc(det.error)}</div>`;
    else if (det) {
      if (det.error) onBattery += `<div class="err">${esc(det.error)}</div>`;
      const fmt = (x) => `${esc(x.value)}${x.unit ? " " + esc(x.unit) : ""}`;
      const tbl = (list) => `<table class="tbl">${list.map((x) => `<tr><th title="${typeof x.addr === "number" ? "Register " + x.addr : esc(x.addr)}">${esc(x.name)}</th><td>${fmt(x)}${x.access === "RW" ? ' <span class="chip">can be changed</span>' : ""} ${x.addr !== "" && x.addr != null ? `<span class="sub" style="font-size:11px;color:var(--secondary-text-color)">${esc(x.addr)}</span>` : ""}</td></tr>`).join("")}</table>`;
      if (det.registers) {
        const groups = [["setting", "Controls and settings", true], ["live", "Sensors", true], ["alarm", "Alarms", true], ["info", "Identity and diagnostics", false], ["other", "Other values the battery sent", false]];
        const main = det.registers.filter((x) => !/^Pack \d+ /.test(x.name)), packs = det.registers.filter((x) => /^Pack \d+ /.test(x.name));
        for (const [k, title, open] of groups) {
          const l = main.filter((x) => x.kind === k);
          if (l.length) onBattery += `<details ${open ? "open" : ""} style="margin-top:6px"><summary>${title} (${l.length})</summary>${tbl(l)}</details>`;
        }
        const nums = [...new Set(packs.map((x) => x.name.match(/^Pack (\d+) /)[1]))];
        for (const n of nums) {
          const l = packs.filter((x) => x.name.startsWith(`Pack ${n} `)).map((x) => ({ ...x, name: x.name.replace(/^Pack \d+ /, "") }));
          onBattery += `<details style="margin-top:6px"><summary>Battery pack ${n} (${l.length})</summary>${tbl(l)}</details>`;
        }
        if (det.registers.some((x) => typeof x.addr === "number")) onBattery += `<p class="sub" style="font-size:12px;color:var(--secondary-text-color)">Names are from Anker's own register list. Units are shown only where HomePower knows them; other numbers are as the register holds them. Energy totals depend on the battery's software version. The Diagnostics tab has the full register tool.</p>`;
        else onBattery += `<p class="sub" style="font-size:12px;color:var(--secondary-text-color)">Names and units follow the Ecoflow-Cloud Home Assistant integration; a value it does not name is listed under its own key.</p>`;
      }
      if (!det.registers && det.raw && Object.keys(det.raw).length) onBattery += `<details style="margin-top:6px"><summary>Every value the battery has sent (${Object.keys(det.raw).length})</summary><table class="tbl">${Object.entries(det.raw).sort(([a], [b]) => a.localeCompare(b)).map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(typeof v === "object" && v !== null ? JSON.stringify(v) : String(v))}</td></tr>`).join("")}</table></details>`;
      if ((det.changes || []).length) onBattery += `<details style="margin-top:6px"><summary>Settings changed lately (${det.changes.length})</summary><table class="tbl">${det.changes.slice().reverse().map((c) => `<tr><th>${esc(c.at)}</th><td>${esc(c.key)}: ${esc(c.from)} → ${esc(c.to)}</td></tr>`).join("")}</table></details>`;
      if (!onBattery) onBattery = none("HomePower reads this battery through Home Assistant entities, so see the Home Assistant device below for what it offers.");
    }
    // The Home Assistant device that goes with it.
    let ha = "";
    try {
      const found = this._haDevicesFor(d, det && det.serial);
      ha = found.length ? found.map((f) => this._haDeviceHtml(f.dev, f.why)).join("") : none("No Home Assistant device was found for this battery. If you also use the vendor's Home Assistant integration, its device is matched by serial number or by having the same name as this battery.");
    } catch (e) { ha = none("Could not read the Home Assistant devices."); }
    return `<div class="overlay" data-close-bg><div class="modal" role="dialog" aria-label="Battery details" id="bd-${esc(id)}" style="max-width:760px">
      <h2>${esc(d.name)} ${this._linkChip(r)}</h2>
      <div style="display:flex;gap:16px;align-items:center;margin-bottom:8px">${this._ring(r.soc, 80)}<div class="sub" style="font-size:13px;color:var(--secondary-text-color)">${esc(prof.label)}${info.length ? `<br>${info.map(esc).join(" · ")}` : ""}</div></div>
      <h3>Readings now</h3>${live}
      ${this._pvHtml((det && det.pv) || lv.pv) ? `<h3>Solar strings</h3>${this._pvHtml((det && det.pv) || lv.pv)}` : ""}
      <h3>HomePower's settings for it</h3>${settings}
      ${settingsHtml ? `<h3>Settings you can change</h3>${settingsHtml}` : ""}${det && det.settings_error ? `<div class="err">${esc(det.settings_error)}</div>` : ""}
      <h3>On the battery itself</h3>${onBattery}
      <h3>Home Assistant device</h3>${ha}
      <h3>Connection</h3>${tech}
      <div class="foot"><span></span><div class="r"><button class="btn" data-act="battery-details" data-id="${esc(id)}"><ha-icon icon="mdi:refresh" style="--mdc-icon-size:16px"></ha-icon>Refresh</button><button class="btn" data-act="edit-battery" data-id="${esc(id)}"><ha-icon icon="mdi:pencil" style="--mdc-icon-size:16px"></ha-icon>Edit</button><button class="btn pri" data-act="close">Close</button></div></div>
    </div></div>`;
  },

  // An inverter's solar strings (Anker X1 PV1-PV8): live volts, amps and watts, a bar for each string's share of the power now,
  // and its health against its own usual share of a day's solar (see core/pv_health.py). Unused strings are left out.
  _pvHtml(pv) {
    if (!pv || !(pv.strings || []).length) return "";
    const used = pv.strings.filter((s) => s.state !== "unused");
    if (!used.length) return "";
    const total = used.reduce((a, s) => a + (s.watts || 0), 0);
    const STATE = { ok: ["OK", ""], weak: ["Weaker than usual", "warn"], dead: ["Not producing", "bad"], learning: ["Learning", ""] };
    const row = (s) => {
      const [label, cls] = STATE[s.state] || [s.state, ""];
      const share = total > 0 && s.watts != null ? Math.round((100 * s.watts) / total) : null;
      const live = s.watts != null ? `${fmtW(s.watts)} <span class="sub">(${s.volts} V × ${s.amps} A)</span>` : '<span class="sub">no reading</span>';
      const bar = share != null ? `<div title="${share}% of the solar now" style="height:6px;border-radius:3px;background:var(--divider-color,#ddd);margin-top:4px"><div style="height:6px;border-radius:3px;width:${share}%;background:var(--primary-color)"></div></div>` : "";
      const usual = s.usual_share != null ? `today ${s.today_share != null ? s.today_share + "%" : "-"} of the solar, usually ${s.usual_share}%` : "usual share not known yet (needs 3 sunny days)";
      return `<tr><th>PV${s.string}</th><td>${live}${bar}<div class="sub" style="font-size:12px">${esc(usual)}</div>${s.note ? `<div class="sub" style="font-size:12px">${esc(s.note)}</div>` : ""}</td><td><span class="chip ${cls}">${esc(label)}</span></td></tr>`;
    };
    return `<table class="tbl">${used.map(row).join("")}${pv.total_w != null ? `<tr><th>All strings</th><td>${fmtW(pv.total_w)} <span class="sub">(the inverter's own total)</span></td><td></td></tr>` : ""}</table>`;
  },

  };
};
