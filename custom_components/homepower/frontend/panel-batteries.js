/* HomePower panel: part of homepower-panel.js (loaded by it; methods are added to the panel class). */
export default (U) => {
  const { MODES, SERIES, DAYS, esc, fmtW, fmtKwh, slug, CSS } = U;
  return {
  // ---- batteries tab -----------------------------------------------------
  _batteries() {
    const head = `<div class="bar-actions"><h2>Batteries</h2><button class="btn pri" data-act="add-battery"><ha-icon icon="mdi:plus"></ha-icon>Add battery</button></div>`;
    if (!this.data.devices.length) return head + `<div class="card empty"><ha-icon icon="mdi:home-battery-outline"></ha-icon><h3>No batteries yet</h3>HomePower controls your batteries through the integrations you already have in Home Assistant (Anker SOLIX, EcoFlow…).<br><br><button class="btn pri" data-act="add-battery">Add your first battery</button></div>`;
    const cards = this.data.devices.map((d, i) => {
      const r = this._reading(d);
      return `<div class="card"><div class="bat">${this._ring(r.soc, 80)}<div class="info">
        <div class="nm"><span class="dot" style="background:${this._color(i)}"></span>${esc(d.name)} <span class="chip ${r.online ? "" : "bad"}">${r.online ? "Online" : "Offline"}</span></div>
        <div class="sub" style="font-size:13px;color:var(--secondary-text-color);margin-top:4px">${esc(this._profile(d.kind).label)}<br>Modes: ${(d.supported_modes || []).map((m) => (MODES[m] || {}).label).join(", ")}<br>${fmtKwh(d.capacity_wh)} · ${fmtW(d.max_charge_w)} in / ${fmtW(d.max_discharge_w)} out · min SOC ${d.min_soc}%</div></div></div>
        <div style="margin-top:12px;display:flex;gap:8px;align-items:center"><button class="btn sm" title="Move earlier" data-act="move-battery" data-dir="-1" data-id="${esc(d.device_id)}" ${i === 0 ? "disabled" : ""}><ha-icon icon="mdi:arrow-left" style="--mdc-icon-size:16px"></ha-icon></button><button class="btn sm" title="Move later" data-act="move-battery" data-dir="1" data-id="${esc(d.device_id)}" ${i === this.data.devices.length - 1 ? "disabled" : ""}><ha-icon icon="mdi:arrow-right" style="--mdc-icon-size:16px"></ha-icon></button><button class="btn sm" title="All settings and readings" data-act="battery-details" data-id="${esc(d.device_id)}"><ha-icon icon="mdi:cog" style="--mdc-icon-size:16px"></ha-icon>Details</button><button class="btn sm" data-act="edit-battery" data-id="${esc(d.device_id)}"><ha-icon icon="mdi:pencil" style="--mdc-icon-size:16px"></ha-icon>Edit</button>
          <button class="btn sm" data-act="toggle-control" data-id="${esc(d.device_id)}" data-on="${d.control_enabled === false ? 1 : 0}">${d.control_enabled === false ? "Allow control" : "Stop control (monitor only)"}</button></div></div>`;
    }).join("");
    return head + `<div class="grid g-cards">${cards}</div>`;
  },

  get _cat() { return this.data.catalogue || { profiles: [], drivers: {}, vendors: {} }; },

  _profile(id) { return this._cat.profiles.find((p) => p.id === id) || this._cat.profiles.find((p) => p.id === "custom") || { driver: "entities", label: id, vendor: "Other", notes: "" }; },

  _spec(pid) { return this._cat.drivers[this._profile(pid).driver] || { entities: [], mode_map: [], modes: [], connection: [] }; },

  _entityOptions(domains, platform) {
    const st = this._hass.states, en = this._hass.entities || {};
    let ids = Object.keys(st).filter((e) => domains.includes(e.split(".")[0]));
    const mine = platform ? ids.filter((e) => en[e] && en[e].platform === platform) : [];
    if (mine.length) ids = mine;
    return ids.sort().map((e) => `<option value="${esc(e)}">${esc(st[e].attributes.friendly_name || "")}</option>`).join("");
  },

  _batteryModal(d, isNew) {
    const cat = this._cat;
    const vendors = [...new Set(cat.profiles.map((p) => p.vendor))];
    const profOpts = vendors.map((v) => `<optgroup label="${esc(v)}">${cat.profiles.filter((p) => p.vendor === v).map((p) => `<option value="${esc(p.id)}" ${p.id === d.kind ? "selected" : ""}>${esc(p.label)}</option>`).join("")}</optgroup>`).join("");
    const html = `<div class="overlay"><div class="modal" role="dialog" aria-label="Battery">
      <h2>${isNew ? "Add battery" : "Edit battery"}</h2><p class="lead">HomePower connects to your battery directly. It does not need any other integration.</p>
      <h3>1 · Which battery?</h3>
      <div class="f"><select id="b-kind">${profOpts}</select></div>
      <div id="b-notes"></div>
      <h3>2 · Connect</h3>
      <div id="b-conn"></div>
      <div id="b-test"></div>
      <h3>3 · Name</h3>
      <div class="f"><label>Name</label><input type="text" id="b-name" value="${esc(d.name)}" placeholder="e.g. Garage battery"></div>
      <p class="hint" style="font-size:13px;color:var(--secondary-text-color);margin:6px 0">Capacity, power limits and minimum charge are read from the battery itself. Open "Limits" only to override them.</p>
      <details ${isNew ? "" : "open"}><summary>Limits (read from the battery; override only if needed)</summary>
        <div class="three"><div class="f"><label>Capacity (Wh)</label><input type="number" id="b-cap" value="${d.capacity_wh ?? ""}" placeholder="from the battery"></div>
          <div class="f"><label>Max charge (W)</label><input type="number" id="b-mc" value="${d.max_charge_w ?? ""}" placeholder="from the battery"></div>
          <div class="f"><label>Max discharge (W)</label><input type="number" id="b-md" value="${d.max_discharge_w ?? ""}" placeholder="from the battery"></div></div>
        <div class="two"><div class="f"><label>Minimum SOC (%)</label><input type="number" id="b-min" min="0" max="100" value="${d.min_soc ?? ""}" placeholder="from the battery"></div>
          <div class="f"><label>Priority (lower = discharges first)</label><input type="number" id="b-prio" value="${d.priority ?? 0}"></div></div>
        <div class="f"><label>Does this battery's meter measure the whole house?</label><select id="b-gridmeter">${[["auto", "Usual for this model"], ["yes", "Yes: it sits at the grid connection, so its grid reading is the house's"], ["no", "No: it only measures itself"]].map(([v, l]) => `<option value="${v}" ${(d.grid_meter || "auto") === v ? "selected" : ""}>${l}</option>`).join("")}</select>
          <span class="hint">Only a battery that measures the whole house can supply HomePower's grid reading. Plug-in batteries usually measure only themselves; an Anker X1 measures the house.</span></div>
        <div class="f"><label>Solar panels attached to this battery</label><select id="b-solar">${[["auto", "Detect automatically"], ["yes", "Yes"], ["no", "No"]].map(([v, l]) => `<option value="${v}" ${(d.solar || "auto") === v ? "selected" : ""}>${l}</option>`).join("")}</select></div>
        <div class="f"><label>Role</label><select id="b-role"><option value="normal" ${d.role !== "reserve" ? "selected" : ""}>Everyday: used whenever the house needs it</option><option value="reserve" ${d.role === "reserve" ? "selected" : ""}>Reserve: held back until the other batteries have run down</option></select>
          <span class="hint">A reserve battery needs control allowed and a battery that supports Hold. Solar still charges it.</span></div></details>
      <h3>4 · Control</h3>
      <label style="display:flex;gap:8px;align-items:flex-start"><input type="checkbox" id="b-control" ${d.control_enabled ? "checked" : ""} style="margin-top:3px"><span>Let HomePower send commands to this battery.<br><span class="hint" style="font-size:12px;color:var(--secondary-text-color)">Leave off at first: HomePower only reads and shows it. Turn on once the test readings above look right.</span></span></label>
      <div class="err" id="m-err"></div>
      <div class="foot">${isNew ? "<span></span>" : `<button class="btn danger" data-act="del-battery" data-id="${esc(d.device_id)}"><ha-icon icon="mdi:delete" style="--mdc-icon-size:16px"></ha-icon>Remove</button>`}
        <div class="r"><button class="btn" data-act="close">Cancel</button><button class="btn pri" data-act="save-battery" data-id="${isNew ? "" : esc(d.device_id)}">Save</button></div></div>
    </div></div>`;
    this._openModal(html);
    this._draft = d;
    this._renderProfile(d.kind, d, true);
  },

  // Rebuilds the parts of the battery form that depend on the chosen model.
  _renderProfile(pid, d, keepValues) {
    const r = this.shadowRoot, prof = this._profile(pid), spec = this._spec(pid), cat = this._cat;
    const conn = (keepValues && d.connection) || {};
    const vend = (cat.vendors[prof.vendor] || {}).regions;
    r.getElementById("b-notes").innerHTML = `<div class="sub" style="font-size:13px;color:var(--secondary-text-color);margin-top:6px">
      ${esc(prof.notes || "")}
      <div class="chips"><span class="chip">Modes: ${(spec.modes || []).map((m) => (MODES[m] || {}).label).join(", ")}</span></div>
      ${vend ? `<details><summary>Regions & setup notes</summary><p>${esc(vend)}</p></details>` : ""}</div>`;
    r.getElementById("b-test").innerHTML = "";
    const box = r.getElementById("b-conn");
    const help = spec.setup_help ? `<p class="hint" style="font-size:13px;color:var(--secondary-text-color);margin:0 0 6px">${esc(spec.setup_help)}</p>` : "";
    if (prof.driver === "anker_modbus") {
      box.innerHTML = `${help}
        <div class="f"><button class="btn" data-act="scan-anker"><ha-icon icon="mdi:radar" style="--mdc-icon-size:16px"></ha-icon>Scan my network</button><div id="b-scan"></div></div>
        <div class="three"><div class="f" style="grid-column:span 2"><label>Battery IP address *</label><input type="text" id="c-host" value="${esc(conn.host || "")}" placeholder="192.168.1.50"></div>
          <div class="f"><label>Port</label><input type="number" id="c-port" value="${conn.port || 502}"></div></div>
        <input type="hidden" id="c-unit_id" value="${conn.unit_id || 1}">
        <button class="btn" data-act="test-battery"><ha-icon icon="mdi:lan-check" style="--mdc-icon-size:16px"></ha-icon>Test connection</button>`;
    } else if (prof.driver === "ecoflow_modbus") {
      box.innerHTML = `${help}
        <div class="three"><div class="f" style="grid-column:span 2"><label>Battery IP address *</label><input type="text" id="c-host" value="${esc(conn.host || "")}" placeholder="192.168.1.50"></div>
          <div class="f"><label>Port</label><input type="number" id="c-port" value="${conn.port || 502}"></div></div>
        <input type="hidden" id="c-unit_id" value="${conn.unit_id || 1}">
        <div class="f"><label><input type="checkbox" id="c-reverse_sign" ${conn.reverse_sign ? "checked" : ""}> Reverse the charge/discharge sign</label><span class="hint">Only if Test control charges when it should discharge.</span></div>
        <button class="btn" data-act="test-battery"><ha-icon icon="mdi:lan-check" style="--mdc-icon-size:16px"></ha-icon>Test connection</button>`;
    } else if (prof.driver === "ecoflow_stream") {
      const regions = spec.connection[0].options;
      box.innerHTML = `${help}
        <div class="f"><label>Region *</label><select id="c-region">${regions.map((o) => `<option value="${o.value}" ${(conn.region || "eu") === o.value ? "selected" : ""}>${esc(o.label)}</option>`).join("")}</select></div>
        <div class="two"><div class="f"><label>Access Key *</label><input type="text" id="c-access_key" value="${esc(conn.access_key || "")}" autocomplete="off"></div>
          <div class="f"><label>Secret Key *</label><input type="password" id="c-secret_key" value="" placeholder="${conn.secret_saved ? "(saved - leave blank to keep)" : ""}" autocomplete="new-password"></div></div>
        <div class="f"><button class="btn" data-act="find-eco"><ha-icon icon="mdi:magnify" style="--mdc-icon-size:16px"></ha-icon>Find my batteries</button></div>
        <div class="f"><label>Battery *</label><select id="c-sn">${conn.sn ? `<option value="${esc(conn.sn)}" selected>${esc(conn.sn)}</option>` : `<option value="">Press "Find my batteries" first</option>`}</select></div>
        <button class="btn" data-act="test-battery"><ha-icon icon="mdi:lan-check" style="--mdc-icon-size:16px"></ha-icon>Test connection</button>`;
    } else if (prof.driver === "modbus_generic") {
      const mp = conn.map || prof.preset_map || {};
      box.innerHTML = `${help}
        <div class="three"><div class="f" style="grid-column:span 2"><label>Device IP address *</label><input type="text" id="c-host" value="${esc(conn.host || "")}" placeholder="192.168.1.60"></div>
          <div class="f"><label>Port</label><input type="number" id="c-port" value="${conn.port || 502}"></div></div>
        <div class="f"><label>Modbus unit ID</label><input type="number" id="c-unit_id" value="${conn.unit_id || prof.default_unit || 1}"></div>
        <div class="f"><label>Register map (JSON)</label><textarea id="c-map" rows="9" style="font-family:monospace;font-size:12px;width:100%;padding:8px;border-radius:8px;border:1px solid var(--divider-color);background:var(--secondary-background-color);color:var(--primary-text-color)">${esc(JSON.stringify(mp, null, 2))}</textarea>
          <span class="hint">Needs at least "soc" and "power". Add "setpoint" and "mode" only if you want HomePower to control it.</span></div>
        <button class="btn" data-act="test-battery"><ha-icon icon="mdi:lan-check" style="--mdc-icon-size:16px"></ha-icon>Test connection</button>`;
    } else {
      const ent = (keepValues && d.entities) || {};
      const en = this._hass.entities || {}, dv = this._hass.devices || {};
      const devs = Object.values(dv).filter((x) => Object.values(en).some((e) => e.device_id === x.id));
      box.innerHTML = `${help}
        ${devs.length ? `<div class="f"><label>Auto-fill from a device</label><select id="b-devsel"><option value="">Choose a device…</option>${devs.map((x) => `<option value="${esc(x.id)}">${esc(x.name_by_user || x.name || x.id)}</option>`).join("")}</select></div>` : ""}
        ${spec.entities.map((f) => `<div class="f"><label>${esc(f.label)}${f.req ? " *" : ""}</label><input type="text" id="e-${f.k}" list="dl-${f.k}" value="${esc(ent[f.k] || "")}" placeholder="${f.domains[0]}.…" autocomplete="off"><datalist id="dl-${f.k}">${this._entityOptions(f.domains)}</datalist></div>`).join("")}
        <label style="display:flex;gap:8px;align-items:center;margin-top:8px"><input type="checkbox" id="b-invchk" ${keepValues && d.invert_power ? "checked" : ""}>Power sensor reports discharging as positive (flip the sign)</label>
        <h3>Mode names</h3><div class="two">${spec.mode_map.map((m) => `<div class="f"><label>${esc(m.label)}${m.optional ? " (optional)" : ""}</label><select id="mm-${m.k}"></select></div>`).join("")}</div>`;
      this._pendingModes = (keepValues && d.mode_map) || {};
      this._fillModeSelects(pid);
    }
  },

  _fillModeSelects(pid) {
    const r = this.shadowRoot, spec = this._spec(pid || r.getElementById("b-kind").value);
    for (const m of spec.mode_map || []) {
      const sel = r.getElementById("mm-" + m.k);
      if (!sel) continue;
      const eid = (r.getElementById("e-" + m.entity) || {}).value;
      const st = this._hass.states[eid];
      const opts = (st && st.attributes.options) || [];
      const cur = sel.value || (this._pendingModes || {})[m.k] || "";
      const all = [...new Set([...opts, ...(cur && !opts.includes(cur) ? [cur] : [])])];
      const guess = cur || (m.rx ? all.find((o) => new RegExp(m.rx, "i").test(o)) : "") || "";
      sel.innerHTML = `<option value="">${m.optional ? "(not used)" : all.length ? "Choose…" : "Pick the select entity first"}</option>` + all.map((o) => `<option ${o === guess ? "selected" : ""}>${esc(o)}</option>`).join("");
    }
  },

  _autofill(devId) {
    const r = this.shadowRoot, pid = r.getElementById("b-kind").value;
    const ens = this._hass.entities || {};
    const ents = Object.values(ens).filter((e) => e.device_id === devId).map((e) => e.entity_id);
    for (const f of this._spec(pid).entities) {
      const cands = ents.filter((e) => f.domains.includes(e.split(".")[0]));
      const name = (e) => `${e} ${(this._hass.states[e] || { attributes: {} }).attributes.friendly_name || ""}`;
      const best = f.rx ? cands.find((e) => new RegExp(f.rx, "i").test(name(e))) : null;
      if (best) r.getElementById("e-" + f.k).value = best;
    }
    this._fillModeSelects(pid);
  },

  _collectBattery(origId) {
    const r = this.shadowRoot, v = (id) => (r.getElementById(id) || {}).value || "", pid = v("b-kind"), prof = this._profile(pid), spec = this._spec(pid);
    const name = v("b-name").trim();
    const out = {
      device_id: origId || slug(name) || "battery_" + Date.now().toString(36),
      name, kind: pid,
      capacity_wh: v("b-cap"), max_charge_w: v("b-mc"), max_discharge_w: v("b-md"), min_soc: v("b-min"), priority: v("b-prio"), role: (r.getElementById("b-role") || {}).value || "normal", solar: (r.getElementById("b-solar") || {}).value || "auto", grid_meter: (r.getElementById("b-gridmeter") || {}).value || "auto",
      control_enabled: !!(r.getElementById("b-control") || {}).checked,
    };
    if (prof.driver === "entities") {
      out.invert_power = !!(r.getElementById("b-invchk") || {}).checked;
      out.entities = Object.fromEntries(spec.entities.map((f) => [f.k, v("e-" + f.k).trim()]));
      out.mode_map = Object.fromEntries(spec.mode_map.map((m) => [m.k, v("mm-" + m.k)]));
    } else {
      out.connection = this._collectConnection(prof, spec);
    }
    return out;
  },

  _collectConnection(prof, spec) {
    const r = this.shadowRoot, v = (k) => (r.getElementById("c-" + k) || {}).value || "";
    const c = Object.fromEntries(spec.connection.map((f) => [f.k, v(f.k).trim()]));
    if (prof.driver === "modbus_generic") { c.port = +c.port || 502; c.unit_id = +c.unit_id || 1; c.map = v("map"); }
    if (prof.driver === "anker_modbus") { c.port = +c.port || 502; c.unit_id = +c.unit_id || 1; }
    if (prof.driver === "ecoflow_modbus") { c.port = +c.port || 502; c.unit_id = +c.unit_id || 1; c.reverse_sign = !!(r.getElementById("c-reverse_sign") || {}).checked; }
    return c;
  },

  async _scanAnker() {
    const box = this.shadowRoot.getElementById("b-scan");
    box.innerHTML = `<div class="hint" style="font-size:13px;color:var(--secondary-text-color);margin-top:6px">Scanning your network… this can take a few seconds.</div>`;
    try {
      const { found } = await this._hass.callWS({ type: "homepower/discover/anker" });
      box.innerHTML = found.length
        ? `<div class="chips" style="margin-top:8px">${found.map((f) => `<button class="btn sm" data-act="pick-host" data-host="${esc(f.host)}" data-port="${f.port}">${esc(f.host)} · ${esc(f.model || "Anker battery")} · ${Math.round(f.soc)}%</button>`).join("")}</div>`
        : `<div class="hint" style="font-size:13px;color:var(--secondary-text-color);margin-top:6px">Nothing found. Check Modbus TCP is switched on in the Anker app, or type the battery's IP address below.</div>`;
    } catch (e) { box.innerHTML = `<div class="err">${esc(e.message || e)}</div>`; }
  },

  async _findEco() {
    const r = this.shadowRoot, v = (k) => r.getElementById("c-" + k).value.trim();
    if (!v("access_key") || !v("secret_key")) return this._err("Enter the Access Key and Secret Key first.");
    this._err("");
    try {
      const { devices } = await this._hass.callWS({ type: "homepower/discover/ecoflow", region: v("region"), access_key: v("access_key"), secret_key: v("secret_key") });
      r.getElementById("c-sn").innerHTML = devices.length
        ? devices.map((d) => `<option value="${esc(d.sn)}">${esc(d.name)} (${esc(d.sn)})${d.online ? "" : " - offline"}</option>`).join("")
        : `<option value="">No batteries on that account</option>`;
    } catch (e) { this._err(e.message || String(e)); }
  },

  async _testBattery(origId) {
    const r = this.shadowRoot, box = r.getElementById("b-test");
    box.innerHTML = `<div class="hint" style="font-size:13px;color:var(--secondary-text-color);margin-top:8px">Connecting…</div>`;
    const d = this._collectBattery(origId);
    try {
      const info = await this._hass.callWS({ type: "homepower/probe", device: { kind: d.kind, device_id: origId || "", connection: d.connection } });
      const p = info.power_w;
      box.innerHTML = `<div class="card" style="margin-top:8px;border-color:#16a34a"><b style="color:#16a34a">Connected</b> · ${esc(info.model || "battery")}${info.serial ? " · " + esc(info.serial) : ""}
        <div class="chips">${info.capacity_wh ? `<span class="chip">${(info.capacity_wh / 1000).toFixed(1)} kWh</span>` : ""}<span class="chip">Charge ${info.soc != null ? Math.round(info.soc) + "%" : "–"}</span>${info.max_charge_w || info.max_discharge_w ? `<span class="chip">Up to ${info.max_charge_w || "?"} W in / ${info.max_discharge_w || "?"} W out</span>` : ""}<span class="chip">${p == null ? "–" : Math.abs(p) < 20 ? "Idle" : (p > 0 ? "Charging " : "Discharging ") + fmtW(Math.abs(p))}</span>${info.grid_w != null ? `<span class="chip">Grid ${fmtW(Math.abs(info.grid_w))} ${info.grid_w > 0 ? "import" : "export"}</span>` : ""}${info.pv_w != null ? `<span class="chip">Solar ${fmtW(info.pv_w)}</span>` : ""}</div></div>`;
      const fill = (id, val) => { const el = r.getElementById(id); if (el && !el.value && val) el.value = val; };
      fill("b-cap", info.capacity_wh); fill("b-mc", info.max_charge_w); fill("b-md", info.max_discharge_w); fill("b-min", info.min_soc);
      if (!r.getElementById("b-name").value && info.model) r.getElementById("b-name").value = info.model;
    } catch (e) { box.innerHTML = `<div class="err">Could not read the battery: ${esc(e.message || e)}</div>`; }
  },

  };
};
