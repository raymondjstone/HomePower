/* HomePower panel: part of homepower-panel.js (loaded by it; methods are added to the panel class). */
export default (U) => {
  const { MODES, SERIES, DAYS, esc, fmtW, fmtKwh, slug, CSS } = U;
  return {
  // ---- diagnostics tab -----------------------------------------------------------
  _portScanCard() {
    const ps = this._ports || {}, rows = ps.results || [];
    let out = "";
    if (ps.error) out = `<div class="err">${esc(ps.error)}</div>`;
    else if (ps.busy) out = `<div class="sub">Scanning… this takes a few seconds.</div>`;
    else if (rows.length) {
      const before = ps.prev && ps.prev.results ? Object.fromEntries(ps.prev.results.map((r) => [r.port, r.state])) : null;
      const open = rows.filter((r) => r.state === "open"), changed = before ? rows.filter((r) => before[r.port] && before[r.port] !== r.state) : [];
      const sum = open.length ? `<b class="ok">${open.length} open: ${open.map((r) => r.port + (r.service ? " (" + esc(r.service) + ")" : "")).join(", ")}</b>` : "<b>Nothing is listening on these ports.</b>";
      const diff = before ? (changed.length ? `<div class="ok" style="margin-top:4px">Changed since the scan at ${new Date(ps.prev.at).toLocaleTimeString()}: ${changed.map((r) => `${r.port} was ${esc(before[r.port])}, now ${esc(r.state)}`).join("; ")}</div>`
        : `<div class="sub" style="margin-top:4px">No change since the scan at ${new Date(ps.prev.at).toLocaleTimeString()}.</div>`) : "";
      out = `<p style="margin:8px 0 4px">${sum} <span class="sub" style="font-size:12px;color:var(--secondary-text-color)">(${esc(ps.address || ps.host)})</span></p>${diff}
        <table class="tbl"><tr><th>Port</th><th>Service</th><th>State</th><th>It says</th></tr>${rows.map((r) => `<tr><td>${r.port}</td><td>${esc(r.service)}</td><td class="${r.state === "open" ? "ok" : ""}">${r.state === "open" ? "OPEN" : esc(r.state)}</td><td>${esc(r.banner)}</td></tr>`).join("")}</table>`;
    }
    return `<div class="card" style="margin-bottom:12px"><h2>Find local control ports</h2>
      <p class="sub" style="font-size:12px;color:var(--secondary-text-color);margin-top:0">Checks whether a device on your network is listening on the ports local control usually uses (502 Modbus TCP, 1883 MQTT, ...). Use it after a firmware update or after switching something on in the vendor app: scan again and it shows what changed. It only connects and listens: it sends nothing, and it refuses addresses outside your own network.</p>
      <div class="row2"><input type="text" id="ps-host" placeholder="192.168.1.50" value="${esc(ps.host || "")}" style="min-width:200px" aria-label="Device IP address">
      <button class="btn pri" data-act="scan-ports"${ps.busy ? " disabled" : ""}><ha-icon icon="mdi:lan-connect" style="--mdc-icon-size:16px"></ha-icon>Scan</button></div>${out}</div>`;
  },

  async _scanPorts() {
    const host = ((this.shadowRoot.getElementById("ps-host") || {}).value || "").trim(), old = this._ports || {};
    if (!host) { this._ports = { ...old, host, busy: false, error: "Type the device's IP address first (find it in your router's device list)." }; return this._render(); }
    this._ports = { host, busy: true, prev: old.host === host && old.results ? { results: old.results, at: old.at } : (old.host === host ? old.prev : null) };
    this._render();
    const keep = this._ports.prev;
    const r = await this._call({ type: "homepower/scan_ports", host }, (m) => { this._ports = { host, busy: false, error: m, prev: keep }; }, true);
    if (r) this._ports = { host, results: r.results || [], address: r.address, at: Date.now(), prev: keep };
    this._render();
  },

  _diagView() {
    const dg = this._diag;
    if (!dg) return `<div class="bar-actions"><h2>Diagnostics</h2></div><div class="card">Loading…</div>`;
    const rows = Object.entries(dg.devices).map(([id, x]) => `<div class="card" style="margin-bottom:12px"><h2>${esc(x.name)} <span class="chip ${x.online ? "" : "bad"}">${x.online ? "Online" : "Offline"}</span> <span class="chip">${esc(x.driver || "")}</span> ${x.control_enabled ? "" : '<span class="chip warn">Monitor only</span>'}</h2>
      <table class="tbl"><tr><th>Connection</th><td>${esc(Object.entries(x.connection || {}).filter(([k]) => k !== "map").map(([k, v]) => `${k}=${v}`).join("  "))}</td></tr>
      <tr><th>Last good read</th><td>${x.last_read_ok ? new Date(x.last_read_ok).toLocaleString() : "never"}</td></tr>
      <tr><th>Battery reports</th><td>${esc(x.actual_mode || "?")}${x.word_order ? ` · 32-bit word order: ${esc(x.word_order)}` : ""}${(this.live.devices || {})[id] && this.live.devices[id].firmware ? ` · software ${esc(this.live.devices[id].firmware)}` : ""}</td></tr>
      <tr><th>HomePower wants</th><td>${x.desired ? esc(x.desired.mode) + (x.desired.power_w ? " " + fmtW(x.desired.power_w) : "") : "nothing (battery runs itself)"} ${x.reason ? "· " + esc(x.reason) : ""}</td></tr>
      <tr><th>Last command</th><td>${x.last_command ? `${esc(x.last_command.mode)} ${x.last_command.power_w ? fmtW(x.last_command.power_w) : ""} at ${new Date(x.last_command.at).toLocaleTimeString()} - ${esc(x.last_command.result)}${x.last_command.why ? `<div class="sub" style="font-size:12px;color:var(--secondary-text-color)">Because: ${esc(x.last_command.why)}${x.last_command.soc != null ? ` · charge ${Math.round(x.last_command.soc)}%` : ""}${x.last_command.target_soc != null ? ` → ${x.last_command.target_soc}%` : ""}${x.last_command.price != null ? ` · price ${(+x.last_command.price).toFixed(3)}` : ""}${x.last_command.grid_w != null ? ` · grid ${Math.round(x.last_command.grid_w)} W` : ""}</div>` : ""}` : "none yet"}</td></tr>
      <tr><th>Problems</th><td>${x.last_error ? `<span class="bad">${esc(x.last_error)}</span>` : "none"}${x.unconfirmed ? ` · ${x.unconfirmed} unconfirmed` : ""}${x.release_pending ? " · holding control" : ""}</td></tr></table>
      <div style="margin-top:10px"><button class="btn sm" data-act="test-control" data-id="${esc(id)}"><ha-icon icon="mdi:flask-outline" style="--mdc-icon-size:16px"></ha-icon>Test control</button>
        ${x.driver === "anker_modbus" ? `<button class="btn sm" data-act="scan-save" data-id="${esc(id)}" title="Read every register (read-only) and remember it as the starting point">Registers: save baseline</button> <button class="btn sm" data-act="scan-compare" data-id="${esc(id)}" title="Read the registers again and list what changed since the baseline">Registers: compare</button> <button class="btn sm" data-act="scan-show" data-id="${esc(id)}" title="Read every register (read-only) and list them all with their names and values">Registers: show all</button> <button class="btn sm" data-act="scan-watch" data-id="${esc(id)}" title="Watch chosen registers live (read-only): change one setting in the vendor app and see which register moves">Registers: watch</button> <a class="sub" style="font-size:12px" href="https://salesforce-knowledge-download.s3.us-west-2.amazonaws.com/000029136/en_US/000029136.pdf" target="_blank" rel="noopener">Anker's X1 register list (PDF)</a>` : ""}</div>
      <div class="steps" id="scan-${esc(id)}" style="margin-top:8px">${this._scans && this._scans[id] ? this._scans[id].html || "" : ""}</div>
      <div class="steps" id="test-${esc(id)}" style="margin-top:8px">${this._tests && this._tests[id] ? this._tests[id] : ""}</div></div>`).join("");
    const ev = (dg.events || []).slice().reverse().slice(0, 40).map(([t, id, m]) => `<div><span class="t">${esc(new Date(t).toLocaleTimeString())}</span> <b>${esc(id)}</b> ${esc(m)}</div>`).join("");
    const dec = (dg.decisions || []).map((e) => {
      const i = e.inputs || {};
      const bits = [i.charge_pct != null ? `charge ${Math.round(i.charge_pct)}%` : "", i.grid_w != null ? `grid ${fmtW(i.grid_w)}` : "", i.price_now != null ? `price ${i.price_now}` : "",
        i.cheap_rate ? "cheap rate" : "", i.backup_mode ? "backup mode" : "", i.away ? "away" : "", i.paused ? "paused" : "", i.forecast_kwh != null ? `solar forecast ${i.forecast_kwh} kWh` : "",
        i.reserve_floor_pct ? `reserve floor ${i.reserve_floor_pct}%` : "", i.solar_ceiling_pct != null ? `room-for-solar limit ${i.solar_ceiling_pct}%` : ""].filter(Boolean);
      return `<details><summary><span class="t">${esc(new Date(e.t).toLocaleTimeString())}</span> <b>${esc(e.name)}</b> → ${esc(e.mode)}${e.power_w ? " " + fmtW(e.power_w) : ""}: ${esc(e.reason)}</summary><div class="sub" style="margin:4px 0 8px 12px">${esc(bits.join(" · ") || "no readings")}${(i.notes || []).length ? "<br>" + esc(i.notes.join("; ")) : ""}</div></details>`;
    }).join("");
    return `<div class="bar-actions"><h2>Diagnostics</h2><span><button class="btn" data-act="download-debug" title="One file with the readings, command results, plan against actual and recent events (no keys or serial numbers): attach it to a bug report"><ha-icon icon="mdi:download" style="--mdc-icon-size:16px"></ha-icon>Download debug file</button> <button class="btn" data-act="copy-debug"><ha-icon icon="mdi:content-copy" style="--mdc-icon-size:16px"></ha-icon>Copy debug info</button></span></div>
      ${this.live.startup ? `<p class="sub" style="font-size:12px;color:var(--secondary-text-color);margin:0 0 8px">${esc(this.live.startup)}</p>` : ""}
      ${(dg.alerts || []).length ? `<div class="banner warn">Active alerts: ${esc(dg.alerts.join(", "))}</div>` : ""}${this._setupCard(true)}${rows || '<div class="card empty">No batteries yet.</div>'}${this._portScanCard()}
      <div class="card" style="margin-bottom:12px"><h2>Why it did that</h2><p class="sub" style="font-size:12px;color:var(--secondary-text-color);margin-top:0">Each time a battery's instruction changed: what it was told, the reason, and the readings behind it. Click one to open it.</p>${dec || '<span class="sub">Nothing yet.</span>'}</div>
      <div class="card"><h2>Recent activity</h2><div class="log">${ev || "Nothing yet."}</div></div>`;
  },

  // Find which register an app setting lives in: save a baseline, change ONE setting in the vendor app, compare.
  async _scanRegisters(id, compare) {
    const set = (html) => { (this._scans ||= {})[id] = { ...(this._scans[id] || {}), html }; const el = this.shadowRoot.getElementById("scan-" + id); if (el) el.innerHTML = html; };
    const base = (this._scans || {})[id] && this._scans[id].base;
    if (compare && !base) return set(`<div class="bad">Save a baseline first.</div>`);
    set(`<div class="sub">Reading registers (read-only)… this can take up to a minute.</div>`);
    let now;
    try { now = await this._hass.callWS({ type: "homepower/device/scan", device_id: id }); }
    catch (e) { return set(`<div class="bad">${esc(e.message || e)}</div>`); }
    if (!compare) {
      this._scans[id].base = now;
      const n = Object.keys(now.holding).length + Object.keys(now.input).length;
      return set(`<div class="ok">Baseline saved (${n} registers). Change one setting in the vendor app, wait a few seconds, then press Compare. Live readings (power, charge level) change on their own: ignore those.</div>`);
    }
    // Names come from Anker's own register document where the device has one (X1): live readings are set aside, since
    // power and energy move by themselves; what is left is what you changed. Reading the holding and input copies of a
    // register shows the same data, so each register is listed once.
    const names = now.names || base.names || {}, rows = [], live = [], seen = new Set();
    for (const kind of ["holding", "input"]) {
      for (const a of [...new Set([...Object.keys(base[kind]), ...Object.keys(now[kind])])].sort((p, q) => p - q)) {
        const x = base[kind][a], y = now[kind][a];
        if (x === undefined || y === undefined || x === y || seen.has(a)) continue;
        seen.add(a);
        const [nm, acc, k] = names[a] || ["not in Anker's list", "", ""];
        (k === "live" ? live : rows).push(`<tr><td>${a}</td><td>${esc(nm)}${acc ? ` <span class="chip">${acc}</span>` : ""}</td><td>${x} → ${y}</td></tr>`);
      }
    }
    const tbl = (r) => `<table class="tbl"><tr><th>Register</th><th>What it is</th><th>Change</th></tr>${r.join("")}</table>`;
    set((rows.length ? `<div><b>Changed</b> (settings, status and unknown registers)</div>${tbl(rows)}` : `<div class="sub">No setting or unknown register changed.</div>`)
      + (live.length ? `<details style="margin-top:8px"><summary class="sub">${live.length} live reading${live.length === 1 ? "" : "s"} also moved (power, energy, temperature: ignore)</summary>${tbl(live)}</details>` : ""));
  },

  // Every register the battery answers, with its name (where Anker's register list has one) and value; filterable, and
  // downloadable as a CSV file. Read-only.
  async _showRegisters(id) {
    const el = () => this.shadowRoot.getElementById("scan-" + id);
    const put = (html) => { (this._scans ||= {})[id] = { ...(this._scans[id] || {}), html }; if (el()) el().innerHTML = html; };
    put(`<div class="sub">Reading every register (read-only)… this can take up to a minute.</div>`);
    let r;
    try { r = await this._hass.callWS({ type: "homepower/device/scan", device_id: id }); }
    catch (e) { return put(`<div class="bad">${esc(e.message || e)}</div>`); }
    const names = r.names || {}, notes = await this._loadNotes(id), addrs = [...new Set([...Object.keys(r.holding), ...Object.keys(r.input)])].sort((a, b) => a - b);
    const text = (a) => {   // text registers (model, serial, software version) hold two characters each, low byte first
      const nm = (names[a] || [""])[0], v = r.input[a] ?? r.holding[a];
      if (!/model name|serial number|software version|hardware version/i.test(nm) || !v) return "";
      return String.fromCharCode(v & 255, v >> 8).replace(/[^ -~]/g, "");
    };
    const dec = (a) => {   // a number held in two registers: worked out from the first of the pair, in the battery's word order
      const [, , , first, n] = names[a] || [];
      if (n !== 2 || +a !== first || !r.order) return "";
      const w0 = r.input[a] ?? r.holding[a], w1 = r.input[+a + 1] ?? r.holding[+a + 1];
      if (w0 === undefined || w1 === undefined) return "";
      const u = r.order === "big" ? w0 * 65536 + w1 : w1 * 65536 + w0;
      return String(u > 2147483647 ? u - 4294967296 : u);
    };
    const rows = addrs.map((a) => { const [nm, acc, kind] = names[a] || ["", "", ""]; return { a, nm, acc, kind, h: r.holding[a], i: r.input[a], t: text(a), d: dec(a), lb: notes[a] || "" }; });
    const cell = (v) => (v === undefined ? "–" : v);
    const csv = ["register,name,access,type,holding,input,text,32-bit value,my label", ...rows.map((x) => [x.a, x.nm, x.acc, x.kind, x.h ?? "", x.i ?? "", x.t, x.d, x.lb].map((c) => `"${String(c).replace(/"/g, '""')}"`).join(","))].join("\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    const dev = (this.data.devices.find((d) => d.device_id === id) || {}).name || id;
    this._regRows = this._regRows || {}; this._regRows[id] = rows;
    const body = (only, q) => rows.filter((x) => (!only || x.nm) && (!q || `${x.a} ${x.nm} ${x.t} ${x.lb}`.toLowerCase().includes(q))).map((x) =>
      `<tr><td>${x.a}</td><td>${x.lb ? `<b>✎ ${esc(x.lb)}</b> ` : ""}${esc(x.nm) || '<span style="color:var(--secondary-text-color)">not in Anker’s list</span>'}${x.acc ? ` <span class="chip">${x.acc}</span>` : ""}</td><td>${cell(x.h)}</td><td>${cell(x.i)}</td><td>${esc(x.t)}</td><td>${esc(x.d)}</td></tr>`).join("");
    put(`<div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:6px">
        <b>${rows.length} registers</b> <a class="btn sm" download="registers-${esc(dev.replace(/[^\w-]+/g, "_"))}.csv" href="${url}">Download CSV</a>
        <input type="text" id="reg-q-${esc(id)}" placeholder="Filter by number or name" style="min-width:200px" autocomplete="off">
        <label style="display:flex;gap:4px;align-items:center"><input type="checkbox" id="reg-named-${esc(id)}"> Only registers in Anker's list</label></div>
      <div style="max-height:420px;overflow:auto"><table class="tbl" id="reg-t-${esc(id)}"><thead><tr><th>Register</th><th>Name</th><th>Holding</th><th>Input</th><th>Text</th><th>32-bit value</th></tr></thead><tbody>${body(false, "")}</tbody></table></div>
      <p class="sub" style="font-size:12px;color:var(--secondary-text-color)">Values are the raw 16-bit numbers; a 32-bit value is two registers (the X1 sends the low half first) and negative numbers show as 65536 minus the size, so 65326 is -210. The 32-bit column joins the two registers of a pair for you (first register of the pair only), as a signed number in the register's own unit: watts for power, and see Anker's list for the others. Holding and input are two views of the same register on most Anker batteries.</p>`);
    const root = this.shadowRoot, q = root.getElementById("reg-q-" + id), n = root.getElementById("reg-named-" + id);
    const refresh = () => { root.querySelector(`#reg-t-${id} tbody`).innerHTML = body(n.checked, q.value.trim().toLowerCase()); };
    if (q && n) { q.addEventListener("input", refresh); n.addEventListener("change", refresh); }
  },

  // Watch a few registers live (read-only): change one setting in the vendor app and see which register moves.
  _watchStop() {
    if (this._watch && this._watch.timer) { clearInterval(this._watch.timer); this._watch.timer = null; }
    const s = this.shadowRoot && this.shadowRoot.getElementById("watch-state");
    if (s && this._watch) s.textContent = "Stopped.";
  },

  async _loadNotes(id) {
    const r = await this._hass.callWS({ type: "homepower/device/register_notes", device_id: id }).catch(() => null);
    return ((this._regNotes ||= {})[id] = (r && r.notes) || {});
  },

  _watchRegisters(id) {
    this._watchStop();
    const html = `<div class="sub" style="margin-bottom:6px">Pick registers to watch (read-only, up to 64), press Start, then change ONE setting in the vendor app. A register that changes is highlighted and listed under Changes seen. Power and charge readings move by themselves: ignore those.</div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><input type="text" id="watch-regs-${esc(id)}" value="10000-10015, 10062-10080" style="min-width:260px" autocomplete="off" aria-label="Registers to watch, for example 10000-10015, 10064">
      <button class="btn sm pri" data-act="watch-start" data-id="${esc(id)}">Start</button> <button class="btn sm" data-act="watch-stop" data-id="${esc(id)}">Stop</button> <button class="btn sm" data-act="reg-share" data-id="${esc(id)}" title="Download the register names you have given as a file, to share with others">Share findings</button> <span class="sub" id="watch-state"></span></div>
      <div id="watch-res-${esc(id)}" style="margin-top:8px"></div>`;
    (this._scans ||= {})[id] = { ...(this._scans[id] || {}), html };
    const el = this.shadowRoot.getElementById("scan-" + id);
    if (el) el.innerHTML = html;
  },

  async _watchStart(id) {
    this._watchStop();
    const root = this.shadowRoot, state = root.getElementById("watch-state"), addrs = [];
    for (const part of root.getElementById("watch-regs-" + id).value.split(/[,;\s]+/).filter(Boolean)) {
      const m = part.match(/^(\d+)(?:-(\d+))?$/);
      if (!m) { state.textContent = `"${part}" is not a register or a range like 10000-10010.`; return; }
      for (let a = +m[1]; a <= +(m[2] || m[1]) && addrs.length <= 64; a++) addrs.push(a);
    }
    if (!addrs.length || addrs.length > 64) { state.textContent = "Choose between 1 and 64 registers."; return; }
    const w = (this._watch = { id, addrs, first: null, last: null, log: [], names: null, notes: await this._loadNotes(id), timer: null, busy: false });
    const tick = async () => {
      if (w.busy || this._watch !== w) return;
      const s = () => root.getElementById("watch-state");
      if (this.tab !== "diagnostics" || !root.getElementById("watch-res-" + id)) { clearInterval(w.timer); w.timer = null; return; }
      w.busy = true;
      try {
        const r = await this._hass.callWS({ type: "homepower/device/registers", device_id: id, addresses: addrs, names: !w.names });
        if (r.names) w.names = r.names;
        const now = r.values, t = new Date().toLocaleTimeString(), moved = new Set();
        if (!w.first) w.first = { ...now };
        for (const a of addrs) {
          const x = w.last ? w.last[a] : undefined, y = now[a];
          if (w.last && x !== y && y !== undefined && x !== undefined) { moved.add(a); w.log.unshift({ t, a, nm: ((w.names || {})[a] || [""])[0], k: ((w.names || {})[a] || [])[2], x, y }); }
        }
        w.last = now; w.log.length = Math.min(w.log.length, 200);
        this._watchPaint(w, moved);
        if (s()) s().textContent = `Watching ${addrs.length} registers, updated ${t}`;
      } catch (e) {
        clearInterval(w.timer); w.timer = null;
        if (s()) s().textContent = e.message || String(e);
      }
      w.busy = false;
    };
    state.textContent = "Starting…";
    w.timer = setInterval(tick, 3000);
    tick();
  },

  _watchPaint(w, moved) {
    const el = this.shadowRoot.getElementById("watch-res-" + w.id);
    if (!el) return;
    const name = (a) => ((w.notes || {})[a] ? `<b>✎ ${esc(w.notes[a])}</b> ` : "") + esc(((w.names || {})[a] || [""])[0]);
    const lab = (a) => `<button class="btn sm" data-act="reg-label" data-id="${esc(w.id)}" data-a="${a}" title="Give this register your own name">Label</button>`;
    const rows = w.addrs.map((a) => {
      const f = w.first[a], l = w.last[a], changed = f !== l && f !== undefined && l !== undefined;
      return `<tr style="${moved.has(a) ? "background:rgba(234,179,8,.45)" : changed ? "background:rgba(234,179,8,.18)" : ""}"><td>${a}</td><td>${name(a)}</td><td>${f ?? "–"}</td><td><b>${l ?? "–"}</b></td><td>${lab(a)}</td></tr>`;
    }).join("");
    const log = w.log.map((e) => `<div style="${e.k === "live" ? "opacity:.55" : ""}"><span class="t">${esc(e.t)}</span> <b>${e.a}</b> ${(w.notes || {})[e.a] ? `<b>✎ ${esc(w.notes[e.a])}</b> ` : ""}${esc(e.nm)} ${e.x} → ${e.y}${e.k === "live" ? " (live reading)" : ""} ${lab(e.a)}</div>`).join("");
    el.innerHTML = `<div style="max-height:300px;overflow:auto"><table class="tbl"><thead><tr><th>Register</th><th>Name</th><th>At start</th><th>Now</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
      <div style="margin-top:8px"><b>Changes seen</b></div><div class="log" style="max-height:200px;overflow:auto">${log || '<span class="sub">Nothing has changed yet.</span>'}</div>`;
  },

  async _runTest(id) {
    const el = this.shadowRoot.getElementById("test-" + id);
    if (el) el.innerHTML = `<div class="sub">Testing… this takes about 10 seconds. HomePower sends a tiny safe command, checks it, and hands the battery back.</div>`;
    let html;
    try {
      const r = await this._hass.callWS({ type: "homepower/device/test", device_id: id });
      html = `<div><b class="${r.ok ? "ok" : "bad"}">${r.ok ? "Passed: the battery follows HomePower's commands." : "Failed"}</b></div>` + r.steps.map((x) => `<div class="${x.ok ? "ok" : x.info ? "" : "bad"}">${x.ok ? "✔" : x.info ? "ℹ" : "✘"} ${esc(x.name)}${x.detail ? ` <span class="sub" style="color:var(--secondary-text-color)">- ${esc(x.detail)}</span>` : ""}</div>`).join("");
    } catch (e) { html = `<div class="bad">${esc(e.message || e)}</div>`; }
    (this._tests ||= {})[id] = html;
    const el2 = this.shadowRoot.getElementById("test-" + id);
    if (el2) el2.innerHTML = html;
  },

  // ---- settings tab (data driven) ---------------------------------------------------
  // Plain-language state of the room-for-solar calculation (Settings > Solar forecast).
  _solarRoomText() {
    const r = this.live.solar_room || {};
    if (r.enabled === false) return "Off: overnight charging is not limited by the forecast.";
    if (!(r.solar_batteries || []).length) return "No battery is marked as having solar panels (Batteries tab > edit > Solar panels), so nothing is limited.";
    if (r.missing === "forecast" || r.forecast_kwh == null) return "No solar forecast found, so solar is counted as zero and batteries charge fully. Add the Forecast.Solar or Solcast integration (Settings > Devices & services > Add integration).";
    if (r.missing === "sun") return "Sunrise and sunset could not be worked out: check your home location in Home Assistant's settings.";
    const b = Object.values(r.batteries || {}).map((x) => `${x.name}: up to ${x.ceiling}% (${x.solar_kwh} kWh of solar)`).join("; ");
    return `Forecast for ${r.day}: ${r.forecast_kwh} kWh, sunrise ${r.sunrise}, sunset ${r.sunset}. Charging ends ${r.charge_ends}. ${b}. Solar is shared between the batteries with panels by capacity; the base load by all batteries' capacity.`;
  },

  _forecastReserveText() {
    const r = this.live.forecast_reserve || {};
    if (!r.day) return "Off, or no solar forecast found yet.";
    if (r.forecast_kwh == null) return `Forecast for ${r.day} not available, so no reserve is held.`;
    return r.active ? `Forecast for ${r.day} is ${r.forecast_kwh} kWh (below ${r.below_kwh}): batteries are kept above ${r.pct}%.` : `Forecast for ${r.day} is ${r.forecast_kwh} kWh (not below ${r.below_kwh}): no extra reserve.`;
  },

  };
};
