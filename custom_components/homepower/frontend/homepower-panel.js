/* HomePower sidebar panel - dependency-free web component for Home Assistant.
   The panel is split over several files (panel-*.js). Each adds its methods to the class below; they are loaded with
   this file's own ?v= tag so a new version is never mixed with cached old parts. */
const V = new URL(import.meta.url).search;
const FILES = ["overview", "charts", "schedules", "batteries", "details", "plan", "insights", "diag", "settings"];
const U = await import(`./panel-util.js${V}`);
const { MODES, SERIES, DAYS, esc, fmtW, fmtKwh, slug, CSS } = U;
const PARTS = await Promise.all(FILES.map((f) => import(`./panel-${f}.js${V}`)));

class HomePowerPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.tab = "overview";
    this.data = { devices: [], schedules: [], settings: {}, presets: {}, live: { devices: {}, events: [] } };
    this.live = { devices: {}, events: [] };
    this._hist = {};
    this._lastSample = 0;
    this._lastRender = 0;
    this._modal = null;
    this._loaded = false;
  }

  set hass(h) {
    const first = !this._hass;
    this._hass = h;
    if (first) this._init();
    this._sample();
  }

  set narrow(v) { this._narrow = v; }

  set panel(v) {}

  set route(v) {}

  disconnectedCallback() {
    if (this._watchStop) this._watchStop();         // a register watch must not keep reading the battery while the panel is not shown
    if (this._unsub) { this._unsub.then?.((u) => u()); this._unsub = null; }
    clearInterval(this._timer);
    this._timer = null;
  }

  // Home Assistant may detach the panel (a hidden tab) and attach the same element again later: pick the live feed up again.
  connectedCallback() {
    if (!this._loaded || this._unsub || this._timer || !this._hass) return;
    this._subscribe();
    this._timer = setInterval(() => this._tick(false), 2000);
    this._reload().then(() => this._render());
  }

  async _init() {
    this.shadowRoot.innerHTML = `<style>${CSS}</style><div id="app"></div><div id="modal"></div>`;
    this.shadowRoot.addEventListener("click", (e) => this._click(e));
    this.shadowRoot.addEventListener("change", (e) => this._change(e));
    this.shadowRoot.addEventListener("input", (e) => this._input(e));
    await this._reload();
    this._subscribe();
    this._timer = setInterval(() => this._tick(false), 2000);
  }

  _subscribe() {
    if (this._unsub) this._unsub.then?.((u) => u());
    this._unsub = this._hass.connection.subscribeMessage((ev) => { this.live = ev; if (ev.paused !== undefined) this.data.paused = ev.paused; this._tick(true); }, { type: "homepower/subscribe" });
  }

  async _reload() {
    try {
      this.data = await this._hass.callWS({ type: "homepower/get" });
      this.live = this.data.live || this.live;
      this._loaded = true;
      this._loadError = null;                       // a later success clears an earlier failure
    } catch (e) {
      this._loadError = e.message || String(e);
    }
    this._render();
  }

  // ---- live data -------------------------------------------------------
  _num(eid) {
    const s = eid && this._hass.states[eid];
    const v = s ? parseFloat(s.state) : NaN;
    return Number.isFinite(v) ? v : null;
  }

  _reading(d) {
    const e = d.entities || {};
    const lv = (this.live.devices || {})[d.device_id] || {};
    if (d.driver && d.driver !== "entities") {  // HomePower talks to this battery itself
      return { soc: lv.soc ?? null, power: lv.power_w ?? null, online: !!lv.online, lv };
    }
    const bad = (k) => { const st = e[k] && this._hass.states[e[k]]; return !st || ["unavailable", "unknown"].includes(st.state); };
    // Whichever control entity this battery family has tells us whether it is reachable.
    const probe = ["mode", "work_mode", "engage", "backup_reserve", "soc"].find((k) => e[k]);
    const online = probe ? !bad(probe) : false;
    let p;
    if (e.charge_power || e.discharge_power) {
      const ch = this._num(e.charge_power), dis = this._num(e.discharge_power);
      p = ch == null && dis == null ? null : (ch || 0) - (dis || 0);
    } else {
      p = this._num(e.power);
      if (p != null && d.invert_power) p = -p;
    }
    return { soc: this._num(e.soc), power: p, online, lv };
  }

  _gridW() {
    const s = this.data.settings || {};
    if (this.live.grid_w != null) return this.live.grid_w;  // worked out server-side (meter sensors, units, export, Glow)
    const v = this._num(s.grid_import_entity);
    if (v == null) return null;
    return s.grid_invert ? -v : v;
  }

  _sample() {
    const now = Date.now();
    if (now - this._lastSample < 5000 || !this._loaded) return;
    this._lastSample = now;
    for (const d of this.data.devices) {
      const r = this._reading(d);
      const h = (this._hist[d.device_id] ||= []);
      h.push({ t: now, p: r.power });
      while (h.length && h[0].t < now - 24 * 3600e3) h.shift();
    }
    const g = this._gridW();
    (this._hist.__grid ||= []).push({ t: now, p: g });
    while (this._hist.__grid.length && this._hist.__grid[0].t < now - 24 * 3600e3) this._hist.__grid.shift();
  }

  _tick(force) {
    if (!this._loaded) {                            // the first load failed (Home Assistant still starting, a reload): keep trying
      if (!this._retrying && Date.now() - (this._lastTry || 0) > 8000) {
        this._retrying = true; this._lastTry = Date.now();
        this._reload().then(() => this._subscribeIfNeeded?.()).finally(() => { this._retrying = false; });
      }
      return;
    }
    if (this._modal) return;
    const now = Date.now();
    const busy = this.tab === "diagnostics" && !!this.shadowRoot.querySelector('[id^="reg-t-"]');   // a register list is open: a refresh would wipe its filter
    if (!force && now - this._lastRender < 1900) return;
    if (this.tab === "overview") this._render();
    else if (["plan", "insights", "diagnostics"].includes(this.tab) && now - (this._lastTabLoad || 0) > 30000 && !(this._watch && this._watch.timer) && !busy) this._tabLoad();
  }

  async _tabLoad() {
    if (this._modal) return;
    this._lastTabLoad = Date.now();
    this._tabError = null;
    try {
      if (this.tab === "plan") this._preview = await this._hass.callWS({ type: "homepower/preview", hours: 24 });
      else if (this.tab === "settings") this._backups = ((await this._hass.callWS({ type: "homepower/backups" }).catch(() => ({ backups: [] }))).backups) || [];
      else if (this.tab === "diagnostics") this._diag = await this._hass.callWS({ type: "homepower/diagnostics" });
      else if (this.tab === "insights") { await this._loadHistory(); this._tariffs = await this._hass.callWS({ type: "homepower/tariffs/compare", days: 30 }).catch((e) => ({ error: e.message || String(e) })); }
    } catch (e) { this._tabError = e.message || String(e); }
    if (!this._modal) this._render();
  }

  // ---- rendering -------------------------------------------------------
  _render() {
    const app = this.shadowRoot.getElementById("app");
    if (!app) return;
    this._lastRender = Date.now();
    const tabs = [["overview", "mdi:view-dashboard", "Overview"], ["plan", "mdi:target", "Plan"], ["schedules", "mdi:calendar-clock", "Schedules"], ["batteries", "mdi:home-battery", "Batteries"], ["insights", "mdi:chart-line", "Insights"], ["settings", "mdi:cog", "Settings"], ["diagnostics", "mdi:stethoscope", "Diagnostics"]];
    let body = "";
    if (this._loadError) body = `<div class="card empty"><ha-icon icon="mdi:alert-circle"></ha-icon><h3>Couldn't load HomePower</h3>${esc(this._loadError)}</div>`;
    else if (this.tab === "overview") body = this._overview();
    else if (this.tab === "schedules") body = this._schedules();
    else if (this.tab === "batteries") body = this._batteries();
    else if (this.tab === "plan") body = this._planView();
    else if (this.tab === "insights") body = this._insightsView();
    else if (this.tab === "diagnostics") body = this._diagView();
    else body = this._settingsView();
    const paused = !!this.data.paused;
    const html = `<header><ha-menu-button></ha-menu-button><h1><ha-icon icon="mdi:home-battery"></ha-icon>Home Power</h1>
      <button class="btn sm" data-act="flag" data-flag="backup" data-on="${this.live.backup ? 0 : 1}" title="Keep a reserve for power cuts">${this.live.backup ? "Backup: ON" : "Backup mode"}</button>
      <button class="btn sm" data-act="flag" data-flag="away" data-on="${this.live.away ? 0 : 1}" title="Use the away schedules">${this.live.away ? "Away: ON" : "Away mode"}</button>
      <button class="btn sm" data-act="pause" title="Stop HomePower sending any commands">${paused ? "Resume control" : "Pause control"}</button>
      <button class="btn sm ${this.data.safe ? "pri" : ""}" data-act="safe" title="Hand every battery back to its own control and send nothing until you switch this off (stronger than Pause, which leaves them as they are)">${this.data.safe ? "Leave safe mode" : "Safe mode"}</button></header>
      ${!paused && (this.data.settings || {}).dry_run ? `<div style="background:#2563eb;color:#fff;padding:10px 20px;font-weight:500">Test run: HomePower works out its plan but sends no commands. Turn it off in Settings.</div>` : ""}
      ${paused ? `<div style="background:#b45309;color:#fff;padding:10px 20px;font-weight:500">HomePower is paused: it is only reading your batteries and will not send any commands. Batteries keep whatever they were last told to do.</div>` : ""}
      <nav>${tabs.map(([k, i, l]) => `<button class="${this.tab === k ? "on" : ""}" data-tab="${k}"><ha-icon icon="${i}"></ha-icon>${l}</button>`).join("")}</nav>
      <main>${body}</main>`;
    if (html === this._lastHtml) return;
    this._lastHtml = html;
    // A header button (Pause, Backup, Away, Safe) redraws the page: keep what was typed into Settings and not yet saved.
    const typed = this._keepForm && this.tab === "settings" ? [...app.querySelectorAll('[id^="st-"]')].filter((e) => "value" in e || "checked" in e).map((e) => [e.id, e.type === "checkbox" ? e.checked : e.value]) : null;
    app.innerHTML = html;
    if (typed) { const now = [...app.querySelectorAll('[id^="st-"]')]; for (const [id, v] of typed) { const e = now.find((x) => x.id === id); if (e) { if (e.type === "checkbox") e.checked = v; else e.value = v; } } }
    const mb = app.querySelector("ha-menu-button");
    if (mb) { mb.hass = this._hass; mb.narrow = this._narrow; }
  }

  _color(i) { return SERIES[i % SERIES.length]; }

  _hours(h) { return h == null ? "–" : h < 1 ? `${Math.round(h * 60)} min` : h >= 48 ? `${Math.round(h / 24)} days` : `${h.toFixed(1)} h`; }

  // ---- events --------------------------------------------------------------
  _openModal(html) { this._modal = true; this.shadowRoot.getElementById("modal").innerHTML = html; }

  _closeModal() { this._modal = null; this.shadowRoot.getElementById("modal").innerHTML = ""; this._render(); }

  _err(m) { const e = this.shadowRoot.getElementById("m-err"); if (e) e.textContent = m; }

  _blankSchedule(tpl) {
    const base = { id: "", device_ids: [], all_day: false, condition_entity: null, start: "23:30", end: "07:30", mode: "charge", power_w: 3000, target_soc: 100, reserve_soc: null, days: [0, 1, 2, 3, 4, 5, 6], priority: 0, season_start: null, season_end: null, on_date: null, enabled: true };
    if (tpl === "cheap") return { ...base, id: "Cheap-rate overnight charge" };
    if (tpl === "hold") {
      const d = new Date();
      return { ...base, id: "Hold window", mode: "hold", start: "17:00", end: "20:00", power_w: null, target_soc: null, priority: 50, on_date: `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}` };
    }
    if (tpl === "evening") return { ...base, id: "Evening discharge", mode: "discharge", start: "17:00", end: "22:00", target_soc: null, reserve_soc: 20 };
    return base;
  }

  async _call(msg, errTarget, wantResult) {
    try { const res = await this._hass.callWS(msg); return wantResult ? res : true; }
    catch (e) { (errTarget || ((m) => this._err(m)))(e.message || String(e)); return false; }
  }

  async _click(e) {
    const bg = e.target.closest("[data-close-bg]");
    if (bg && e.target === bg) return this._closeModal();
    const t = e.target.closest("[data-tab],[data-act]");
    if (!t) return;
    if (t.dataset.tab) { this.tab = t.dataset.tab; this._render(); return this._tabLoad(); }
    const { act, id } = t.dataset;
    const dev = () => this.data.devices.find((d) => d.device_id === id);
    switch (act) {
      case "close": return this._closeModal();
      case "override": return this._overrideModal(id);
      case "clear-override":
        await this._call({ type: "homepower/override/clear", device_ids: [id] }, (m) => alert(m));
        return;
      case "save-override": {
        const r = this.shadowRoot, mode = r.querySelector('input[name="o-mode"]:checked').value;
        const ids = [...r.querySelectorAll('input[name="o-dev"]:checked')].map((x) => x.value);
        if (!ids.length) return this._err("Pick at least one battery.");
        const charging = mode === "charge", moving = charging || mode === "discharge";
        const num = (id) => { const v = r.getElementById(id).value.trim(); return v === "" ? null : +v; };     // a cleared box means "not set", not 0
        if (!(await this._call({ type: "homepower/override", device_ids: ids, mode, power_w: moving ? num("o-pw") : null,
              target_soc: charging ? num("o-soc") : null, minutes: +r.getElementById("o-min").value }))) return;
        return this._closeModal();
      }
      case "safe": {
        const next = !this.data.safe;
        if (next && !confirm("Safe mode hands every battery back to its own control and stops HomePower sending any commands until you switch it off. Continue?")) return;
        if (await this._call({ type: "homepower/safe", enabled: next }, (m) => alert(m))) { this._keepForm = true; await this._reload(); this._keepForm = false; }
        return;
      }
      case "download-debug":
      case "download-hw-report": {
        const hw = act === "download-hw-report";
        const r = await this._call({ type: hw ? "homepower/hardware_report" : "homepower/debug" }, (m) => alert(m), true);
        if (!r) return;
        const a = document.createElement("a");
        a.href = URL.createObjectURL(new Blob([JSON.stringify(r, null, 2)], { type: "application/json" }));
        a.download = `homepower-${hw ? "hardware-report" : "debug"}-${new Date().toISOString().slice(0, 10)}.json`;
        a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
        return;
      }
      case "pause": {
        const next = !this.data.paused;
        if (next && !confirm("Pause HomePower? It will stop sending commands. Batteries stay in whatever mode they were last put in.")) return;
        if (await this._call({ type: "homepower/pause", paused: next }, (m) => alert(m))) { this.data.paused = next; this._keepForm = true; this._render(); this._keepForm = false; }
        return;
      }
      case "add-schedule": return this._scheduleModal(this._blankSchedule(t.dataset.tpl), true);
      case "edit-schedule": return this._scheduleModal(this.data.schedules.find((s) => s.id === id), false);
      case "save-schedule": {
        const s = this._collectSchedule(), orig = t.dataset.orig;
        if (!s.id) return this._err("Give the schedule a name.");
        if (orig !== s.id && this.data.schedules.some((x) => x.id === s.id)) return this._err("A schedule with that name already exists.");
        if (orig) s.enabled = (this.data.schedules.find((x) => x.id === orig) || {}).enabled !== false;
        const saved = await this._call({ type: "homepower/schedule/save", schedule: s }, undefined, true);
        if (!saved) return;
        if (orig && orig !== s.id) await this._call({ type: "homepower/schedule/delete", schedule_id: orig });
        await this._reload(); this._closeModal();
        if ((saved.warnings || []).length) alert("Saved, but note:\n\n" + saved.warnings.join("\n\n"));
        return;
      }
      case "del-schedule":
        if (!confirm(`Delete schedule "${id}"?`)) return;
        if (!(await this._call({ type: "homepower/schedule/delete", schedule_id: id }))) return;      // an error stays on screen (the dialog stays open)
        await this._reload(); return this._closeModal();
      case "add-battery": {
        this._editingId = null;                    // a cancelled edit must not leak its id into a new battery's test
        const first = (this._cat.profiles[0] || {}).id || "custom";
        return this._batteryModal({ kind: first, name: "", capacity_wh: null, max_charge_w: null, max_discharge_w: null, min_soc: 10, priority: 0, entities: {}, mode_map: {}, connection: {}, control_enabled: false }, true);
      }
      case "scan-anker": return this._scanAnker();
      case "pick-host": {
        const r = this.shadowRoot;
        r.getElementById("c-host").value = t.dataset.host; r.getElementById("c-port").value = t.dataset.port;
        return this._testBattery("");
      }
      case "find-eco": return this._findEco();
      case "test-battery": return this._testBattery(this._editingId || "");
      case "scan-show": return this._showRegisters(id);
      case "scan-watch": return this._watchRegisters(id);
      case "whatif-run": {
        const r = this.shadowRoot, g = (k) => +r.getElementById("wi-" + k).value;
        const inn = { br: g("br"), bc: g("bc"), ar: g("ar"), ac: g("ac") };
        try {
          const res = await this._hass.callWS({ type: "homepower/whatif", days: 30, base: { reserve: inn.br, cheap: inn.bc }, alt: { reserve: inn.ar, cheap: inn.ac } });
          this._whatif = { in: inn, result: res };
        } catch (e) { this._whatif = { in: inn, error: e.message || String(e) }; }
        return this._render();
      }
      case "watch-start": return this._watchStart(id);
      case "watch-stop": return this._watchStop();
      case "reg-label": {
        const a = +t.dataset.a, cur = (((this._regNotes || {})[id]) || {})[a] || "";
        const label = prompt(`Your name for register ${a} (leave empty to remove it):`, cur);
        if (label === null) return;
        const res = await this._call({ type: "homepower/device/register_notes", device_id: id, address: a, label }, (m) => alert(m), true);
        if (!res) return;
        (this._regNotes ||= {})[id] = res.notes;
        if (this._watch && this._watch.id === id) { this._watch.notes = res.notes; if (this._watch.last) this._watchPaint(this._watch, new Set()); }
        return;
      }
      case "reg-share": {
        const d = this.data.devices.find((x) => x.device_id === id) || {}, notes = ((this._regNotes || {})[id]) || (await this._loadNotes(id));
        const a = document.createElement("a");
        a.href = URL.createObjectURL(new Blob([JSON.stringify({ homepower_register_notes: 1, battery: d.name, model: d.kind, software: ((this.live.devices || {})[id] || {}).firmware || "", registers: notes }, null, 2)], { type: "application/json" }));
        a.download = `homepower-registers-${(d.name || id).replace(/[^\w-]+/g, "_")}.json`; a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 5000);
        return;
      }
      case "backup-now": {
        const msg = this.shadowRoot.getElementById("bk-msg");
        const res = await this._call({ type: "homepower/backups/now" }, (m) => { if (msg) msg.textContent = m; }, true);
        if (!res) return;
        this._backups = res.backups; return this._render();
      }
      case "restore-backup": {
        const msg = this.shadowRoot.getElementById("bk-msg");
        if (!confirm(`Restore from ${id}? Items with the same name are replaced.`)) return;
        const res = await this._call({ type: "homepower/backups/restore", name: id }, (m) => { if (msg) msg.textContent = m; }, true);
        if (!res) return;
        await this._reload();
        const a = res.added || {};
        alert(`Restored ${a.devices || 0} batteries, ${a.schedules || 0} schedules, ${a.goals || 0} goals and ${a.scenes || 0} scenes.` + ((res.skipped || []).length ? `\n\nSkipped:\n- ${res.skipped.join("\n- ")}` : ""));
        return;
      }
      case "scan-ports": return this._scanPorts(false);
      case "scan-ports-wide": return this._scanPorts(true);
      case "scan-save": case "scan-compare": return this._scanRegisters(id, act === "scan-compare");
      case "confirm-first": {
        if (!confirm("Allow HomePower to send its first real command to this battery? It will raise the battery's backup reserve; Auto puts it back.")) return;
        if (await this._call({ type: "homepower/device/confirm", device_id: id }, (m) => alert(m))) await this._reload();
        return;
      }
      case "toggle-control": {
        const on = t.dataset.on === "1";
        if (on && !confirm("Allow HomePower to send charge/discharge commands to this battery?")) return;
        if (await this._call({ type: "homepower/device/control", device_id: id, enabled: on }, (m) => alert(m))) await this._reload();
        return;
      }
      case "move-battery": {
        const ids = this.data.devices.map((d) => d.device_id), i = ids.indexOf(id), j = i + (+t.dataset.dir);
        if (i < 0 || j < 0 || j >= ids.length) return;
        [ids[i], ids[j]] = [ids[j], ids[i]];
        if (await this._call({ type: "homepower/device/reorder", device_ids: ids }, (m) => alert(m))) await this._reload();
        return;
      }
      case "battery-details": return this._batteryDetails(id);
      case "setting-set": return this._applySetting(id, t.dataset.key, t.dataset.value);
      case "ha-set": return this._haSet(t.dataset.eid, t.dataset.op);
      case "edit-battery": this._editingId = id; return this._batteryModal(dev(), false);
      case "save-battery": {
        const d = this._collectBattery(t.dataset.id);
        if (!d.name) return this._err("Give the battery a name.");
        if (!t.dataset.id && this.data.devices.some((x) => x.device_id === d.device_id)) return this._err("A battery with that name already exists.");
        if (!(await this._call({ type: "homepower/device/save", device: d }))) return;     // the dialog stays open: Test still tests this battery
        this._editingId = null;
        await this._reload(); return this._closeModal();
      }
      case "del-battery":
        if (!confirm("Remove this battery from HomePower? (The device itself is untouched.)")) return;
        if (!(await this._call({ type: "homepower/device/delete", device_id: id }))) return;          // e.g. "could not be handed back": say so, do not close
        await this._reload(); return this._closeModal();
      case "save-settings": {
        const msg = this.shadowRoot.getElementById("g-msg");
        const ok = await this._call({ type: "homepower/settings/save", settings: this._collectSettings() }, (m) => { msg.style.color = ""; msg.textContent = m; });
        if (ok) {
          this._savedNote = true;   // survives the re-render that follows
          await this._reload();
          setTimeout(() => { this._savedNote = false; if (this.tab === "settings" && !this._modal && !this.shadowRoot.activeElement) this._render(); }, 3000);   // not while the user is typing
        }
        return;
      }
      case "ord-up": { const row = t.closest(".ord"); if (row.previousElementSibling) row.parentNode.insertBefore(row, row.previousElementSibling); return; }
      case "ord-down": { const row = t.closest(".ord"); if (row.nextElementSibling) row.parentNode.insertBefore(row.nextElementSibling, row); return; }
      case "add-rule": { this.shadowRoot.getElementById("st-rules").insertAdjacentHTML("beforeend", this._ruleRow({ block: "discharge" })); return; }
      case "del-rule": { t.closest(".rule").remove(); return; }
      case "flag":
        if (await this._call({ type: "homepower/flag", flag: t.dataset.flag, enabled: t.dataset.on === "1" }, (m) => alert(m))) { this.live[t.dataset.flag] = t.dataset.on === "1"; this._keepForm = true; this._render(); this._keepForm = false; }
        return;
      case "add-goal": return this._goalModal({ id: "", device_ids: [], target_soc: 90, by: "07:00", days: [0, 1, 2, 3, 4, 5, 6], use_forecast: false, auto_target: false, max_power_w: null, enabled: true }, true);
      case "edit-goal": return this._goalModal((this.data.goals || []).find((x) => x.id === id), false);
      case "save-goal": {
        const g = this._collectGoal(), orig = t.dataset.orig;
        if (!g.id) return this._err("Give the goal a name.");
        if (orig !== g.id && (this.data.goals || []).some((x) => x.id === g.id)) return this._err("A goal with that name already exists.");
        if (orig) g.enabled = ((this.data.goals || []).find((x) => x.id === orig) || {}).enabled !== false;
        const saved = await this._call({ type: "homepower/goal/save", goal: g }, undefined, true);
        if (!saved) return;
        if (orig && orig !== g.id) await this._call({ type: "homepower/goal/delete", goal_id: orig });
        await this._reload(); this._closeModal(); this._tabLoad();
        if ((saved.warnings || []).length) alert("Saved, but note:\n\n" + saved.warnings.join("\n\n"));
        return;
      }
      case "del-goal":
        if (!confirm(`Delete goal "${id}"?`)) return;
        if (!(await this._call({ type: "homepower/goal/delete", goal_id: id }))) return;
        await this._reload(); this._closeModal(); return this._tabLoad();
      case "range": this._range = t.dataset.r; return this._tabLoad();
      case "setup-test": this.tab = "diagnostics"; this._render(); await this._tabLoad(); return this._runTest(id);
      case "goto-tariff": this.tab = "settings"; this._render(); return this._tabLoad();
      case "tariff-preset": {
        const r = this.shadowRoot;
        r.getElementById("st-offpeak_start").value = t.dataset.a; r.getElementById("st-offpeak_end").value = t.dataset.b;
        r.getElementById("st-offpeak_extra").value = t.dataset.x || "";
        const auto = r.getElementById("st-offpeak_auto"); if (auto) auto.checked = false;
        return;
      }
      case "apply-scene":
        if (await this._call({ type: "homepower/scene/apply", name: id }, (m) => alert(m))) { await this._reload(); }
        return;
      case "add-scene": return this._sceneModal({ name: "", backup: false, away: false, paused: false, settings: {} }, true);
      case "edit-scene": return this._sceneModal((this.data.scenes || []).find((x) => x.name === id), false);
      case "save-scene": {
        const sc = this._collectScene(), orig = t.dataset.orig;
        if (!sc.name) return this._err("Give the scene a name.");
        if (orig !== sc.name && (this.data.scenes || []).some((x) => x.name === sc.name)) return this._err("A scene with that name already exists.");
        if (!(await this._call({ type: "homepower/scene/save", scene: sc }, (m) => this._err(m)))) return;
        if (orig && orig !== sc.name) await this._call({ type: "homepower/scene/delete", name: orig });
        await this._reload(); return this._closeModal();
      }
      case "del-scene":
        if (!confirm(`Delete scene "${id}"?`)) return;
        if (!(await this._call({ type: "homepower/scene/delete", name: id }))) return;
        await this._reload(); return this._closeModal();
      case "do-export": {
        const keys = this.shadowRoot.getElementById("bk-secrets").checked, msg = this.shadowRoot.getElementById("bk-msg");
        const dump = await this._call({ type: "homepower/export", include_secrets: keys }, (m) => { msg.textContent = m; }, true);
        if (!dump) return;
        const a = document.createElement("a");
        a.href = URL.createObjectURL(new Blob([JSON.stringify(dump, null, 2)], { type: "application/json" }));
        a.download = `homepower-backup-${new Date().toISOString().slice(0, 10)}.json`; a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 5000);
        return;
      }
      case "do-import": {
        const f = this.shadowRoot.getElementById("imp-file").files[0], msg = this.shadowRoot.getElementById("bk-msg");
        if (!f) { msg.textContent = "Choose a backup file first."; return; }
        let data;
        try { data = JSON.parse(await f.text()); } catch (e) { msg.textContent = "That file is not valid JSON."; return; }
        if (!confirm("Restore from this file? Items with the same name are replaced.")) return;
        const res = await this._call({ type: "homepower/import", data }, (m) => { msg.textContent = m; }, true);
        if (!res) return;
        await this._reload();
        const a = res.added || {};
        alert(`Restored ${a.devices || 0} batteries, ${a.schedules || 0} schedules, ${a.goals || 0} goals and ${a.scenes || 0} scenes.` + ((res.skipped || []).length ? `\n\nSkipped:\n- ${res.skipped.join("\n- ")}` : ""));
        return;
      }
      case "flowdur":
        this._flowMinV = parseInt(t.dataset.m, 10);
        try { localStorage.setItem("homepower.flowMin", String(this._flowMinV)); } catch (e) { /* optional */ }
        return this._render();
      case "test-control": return this._runTest(id);
      case "copy-debug": {
        const dbg = await this._call({ type: "homepower/debug" }, (m) => alert(m), true);   // the same scrubbed text as the downloadable file
        if (!dbg) return;
        const txt = JSON.stringify(dbg, null, 2);
        try { await navigator.clipboard.writeText(txt); alert("Debug info copied (secrets are hidden)."); }
        catch (e) { prompt("Copy this debug info:", txt); }
        return;
      }
    }
  }

  async _change(e) {
    const t = e.target;
    if (t.dataset && t.dataset.gtoggle) {
      const g = (this.data.goals || []).find((x) => x.id === t.dataset.gtoggle);
      if (g) { await this._call({ type: "homepower/goal/save", goal: { ...g, enabled: t.checked } }, (m) => alert(m)); await this._reload(); this._tabLoad(); }
      return;
    }
    if (t.dataset && t.dataset.toggle) {
      const s = this.data.schedules.find((x) => x.id === t.dataset.toggle);
      if (s) { await this._call({ type: "homepower/schedule/save", schedule: { ...s, enabled: t.checked } }, (m) => alert(m)); await this._reload(); }
      return;
    }
    if (t.name === "s-mode" || t.id === "s-allday" || t.id === "s-source") this._syncSchedForm();     // the source changes the help and which batteries can do it
    if (t.id === "b-kind") this._renderProfile(t.value, {}, false);
    if (t.id === "b-devsel" && t.value) this._autofill(t.value);
    if (t.id === "b-control" && t.checked && !confirm("Allow HomePower to send charge/discharge commands to this battery?")) t.checked = false;
    if (t.id && t.id.startsWith("e-")) this._fillModeSelects();
    if (t.name === "s-dev") this._syncSchedForm();
  }

  _input(e) {
    if (e.target.id === "s-pw") this._syncSchedForm();
    if (e.target.id === "ps-host") this._ports = { ...(this._ports || {}), host: e.target.value };   // kept across the tab's refresh
    const m = /^wi-(br|bc|ar|ac)$/.exec(e.target.id || "");
    if (m) this._whatif = { ...(this._whatif || {}), in: { br: 10, bc: 0, ar: 10, ac: 80, ...((this._whatif || {}).in || {}), [m[1]]: +e.target.value } };   // kept across the tab's periodic reload
  }

}

// Add each part's methods (and getters) to the class.
for (const part of PARTS) Object.defineProperties(HomePowerPanel.prototype, Object.getOwnPropertyDescriptors(part.default(U)));


customElements.define("homepower-panel", HomePowerPanel);
