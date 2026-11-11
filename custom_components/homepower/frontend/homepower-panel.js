/* HomePower sidebar panel - dependency-free web component for Home Assistant. */
const MODES = {
  auto: { label: "Auto", color: "#3b82f6", icon: "mdi:auto-fix", help: "Device's own self-consumption logic" },
  charge: { label: "Charge", color: "#16a34a", icon: "mdi:battery-charging", help: "Force charge" },
  discharge: { label: "Discharge", color: "#f59e0b", icon: "mdi:battery-arrow-down", help: "Force discharge" },
  hold: { label: "Hold", color: "#6b7280", icon: "mdi:pause-circle", help: "Neither charge nor discharge" },
};
const SERIES = ["#4c8bf5", "#e8710a", "#9334e6", "#12a4af", "#d93f6b", "#7a8b1f"];
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtW = (w) => (w == null ? "–" : Math.abs(w) >= 1000 ? `${(w / 1000).toFixed(2)} kW` : `${Math.round(w)} W`);
const fmtKwh = (wh) => `${(wh / 1000).toFixed(1)} kWh`;
const slug = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");

const CSS = `
:host{display:block;background:var(--primary-background-color);color:var(--primary-text-color);min-height:100vh;font-family:var(--paper-font-body1_-_font-family,Roboto,sans-serif)}
*{box-sizing:border-box}
header{display:flex;align-items:center;gap:12px;padding:12px 20px;background:var(--app-header-background-color,var(--primary-color));color:var(--app-header-text-color,#fff);position:sticky;top:0;z-index:5}
header h1{font-size:20px;font-weight:500;margin:0;flex:1;display:flex;align-items:center;gap:8px}
header ha-menu-button,header ha-icon-button{color:inherit}
nav{display:flex;gap:4px;padding:0 16px;background:var(--card-background-color);border-bottom:1px solid var(--divider-color);overflow-x:auto}
nav button{background:none;border:0;border-bottom:3px solid transparent;color:var(--secondary-text-color);padding:14px 16px;font:inherit;font-weight:500;cursor:pointer;display:flex;gap:6px;align-items:center;white-space:nowrap}
nav button.on{color:var(--primary-color);border-color:var(--primary-color)}
main{padding:16px;max-width:1200px;margin:0 auto}
.grid{display:grid;gap:16px}
.g-tiles{grid-template-columns:repeat(auto-fit,minmax(190px,1fr))}
.g-cards{grid-template-columns:repeat(auto-fit,minmax(310px,1fr))}
.card{background:var(--card-background-color);border-radius:14px;padding:16px;box-shadow:var(--ha-card-box-shadow,0 1px 3px rgba(0,0,0,.18));border:1px solid var(--divider-color)}
.card h2{font-size:15px;margin:0 0 12px;font-weight:600;display:flex;align-items:center;gap:8px}
.tile .lbl{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--secondary-text-color)}
.tile .val{font-size:28px;font-weight:600;margin:4px 0}
.tile .sub{font-size:12px;color:var(--secondary-text-color)}
.bat{display:flex;gap:14px;align-items:center}
.bat .info{flex:1;min-width:0}
.bat .nm{font-weight:600;font-size:16px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.pw{font-size:22px;font-weight:600;margin:2px 0}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.chip{font-size:12px;padding:3px 9px;border-radius:99px;background:var(--secondary-background-color);color:var(--primary-text-color);display:inline-flex;gap:4px;align-items:center;border:1px solid var(--divider-color)}
.chip.m{color:#fff;border:0}
.chip.warn{background:#b45309;color:#fff;border:0}
.chip.bad{background:#b91c1c;color:#fff;border:0}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.ring text{fill:var(--primary-text-color)}
.spark{width:100%;height:46px;margin-top:10px}
.plot{display:flex;margin:8px 0 2px}
.plot-y{position:relative;width:58px;flex:none}
.plot-y span{position:absolute;right:6px;transform:translateY(-50%);font-size:11px;color:var(--secondary-text-color);white-space:nowrap}
.plot-main{flex:1;min-width:0}
.plot-main svg{display:block;width:100%;border-left:1px solid var(--divider-color);border-bottom:1px solid var(--divider-color)}
.plot-x{position:relative;height:18px;margin-top:4px}
.plot-x span{position:absolute;transform:translateX(-50%);font-size:11px;color:var(--secondary-text-color);white-space:nowrap}
.plot-x span:first-child{transform:none}.plot-x span:last-child{transform:translateX(-100%)}
.durs{display:inline-flex;gap:4px;flex-wrap:wrap;margin-left:12px;vertical-align:middle}
.hb{display:grid;grid-template-columns:auto 1fr;gap:18px;align-items:center}
.hb .nums{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px 18px}
.hb .nums b{display:block;font-size:20px;font-weight:600}.hb .nums span{font-size:12px;color:var(--secondary-text-color)}
.banner{display:flex;gap:12px;align-items:center;flex-wrap:wrap;background:var(--card-background-color);border:1px solid var(--warning-color,#b45309);border-left-width:5px;border-radius:12px;padding:12px 16px;margin-bottom:16px}
.banner .t{flex:1;min-width:200px}
.scenes{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.presets{display:flex;gap:6px;flex-wrap:wrap;margin:4px 0 10px}
.chk{display:grid;gap:6px}.chk .it{display:grid;grid-template-columns:24px 1fr auto;gap:8px;align-items:center;padding:6px 0;border-bottom:1px solid var(--divider-color)}
.chk .it:last-child{border-bottom:0}.chk .ok{color:#16a34a}.chk .no{color:#d97706}.chk small{display:block;color:var(--secondary-text-color)}
@media(max-width:700px){header{flex-wrap:wrap;padding:10px 12px;gap:8px}header h1{flex-basis:100%;order:-1;font-size:18px}header .btn{padding:6px 10px;font-size:12px}
 main{padding:10px}.hb{grid-template-columns:1fr;justify-items:center;text-align:center}.plot-y{width:46px}.card{padding:14px}nav button{padding:12px 10px}.durs{margin-left:0;margin-top:6px;display:flex}}
.tl{margin:8px 0 14px}
.tl .row{display:grid;grid-template-columns:130px 1fr;gap:10px;align-items:center;margin:8px 0}
.tl .nm{font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar{position:relative;height:26px;background:var(--secondary-background-color);border-radius:6px;overflow:hidden}
.bar.zi{background:repeating-linear-gradient(45deg,var(--secondary-background-color),var(--secondary-background-color) 6px,rgba(59,130,246,.18) 6px,rgba(59,130,246,.18) 12px)}
.seg{position:absolute;top:3px;bottom:3px;border-radius:4px;font-size:11px;color:#fff;padding:0 6px;display:flex;align-items:center;overflow:hidden;white-space:nowrap}
.now{position:absolute;top:0;bottom:0;width:2px;background:var(--primary-text-color);z-index:2}
.axis{display:grid;grid-template-columns:130px 1fr;gap:10px;font-size:11px;color:var(--secondary-text-color)}
.axis div:last-child{display:flex;justify-content:space-between}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--secondary-text-color);margin-top:8px}
.legend span{display:inline-flex;gap:6px;align-items:center}
.sw{width:12px;height:12px;border-radius:3px;display:inline-block}
button.btn{font:inherit;font-weight:500;border-radius:8px;border:1px solid var(--divider-color);background:var(--card-background-color);color:var(--primary-text-color);padding:8px 14px;cursor:pointer;display:inline-flex;gap:6px;align-items:center}
button.btn:hover{background:var(--secondary-background-color)}
button.btn.pri{background:var(--primary-color);color:var(--text-primary-color,#fff);border-color:transparent}
button.btn.danger{color:#dc2626}
button.btn.sm{padding:4px 10px;font-size:13px}
.bar-actions{display:flex;justify-content:space-between;align-items:center;margin:0 0 14px;gap:10px;flex-wrap:wrap}
.bar-actions h2{margin:0;font-size:18px}
.empty{text-align:center;padding:40px 20px;color:var(--secondary-text-color)}
.empty ha-icon{--mdc-icon-size:56px;opacity:.5}
.empty h3{color:var(--primary-text-color);margin:10px 0 4px}
.sch{display:flex;gap:12px;align-items:center}
.sch .when{font-size:20px;font-weight:600}
.sch .meta{flex:1;min-width:0}
.sch .sub{font-size:13px;color:var(--secondary-text-color);margin-top:2px}
.sch.off{opacity:.55}
.dayrow{display:flex;gap:3px;margin-top:6px}
.dayrow span{font-size:10px;width:26px;text-align:center;padding:2px 0;border-radius:4px;background:var(--secondary-background-color);color:var(--secondary-text-color)}
.dayrow span.on{background:var(--primary-color);color:var(--text-primary-color,#fff)}
.log{font-family:monospace;font-size:12px;max-height:240px;overflow:auto}
.log div{padding:3px 0;border-bottom:1px solid var(--divider-color)}
.log .t{color:var(--secondary-text-color)}
.switch{position:relative;width:42px;height:24px;flex:none}
.switch input{opacity:0;width:0;height:0}
.switch i{position:absolute;inset:0;background:var(--disabled-text-color,#999);border-radius:99px;cursor:pointer;transition:.15s}
.switch i:before{content:"";position:absolute;width:18px;height:18px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.15s}
.switch input:checked+i{background:var(--primary-color)}
.switch input:checked+i:before{transform:translateX(18px)}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:20;display:flex;align-items:flex-start;justify-content:center;overflow:auto;padding:24px 12px}
.modal{background:var(--card-background-color);border-radius:16px;width:100%;max-width:640px;padding:22px;box-shadow:0 10px 40px rgba(0,0,0,.4)}
.modal h2{margin:0 0 4px;font-size:20px}
.modal .lead{color:var(--secondary-text-color);margin:0 0 16px;font-size:14px}
.modal h3{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--secondary-text-color);margin:20px 0 8px}
.f{display:flex;flex-direction:column;gap:4px;margin:10px 0}
.f label{font-size:13px;color:var(--secondary-text-color)}
.f .hint{font-size:12px;color:var(--secondary-text-color)}
input[type=text],input[type=number],input[type=time],input[type=date],input[type=month],select{font:inherit;padding:9px 10px;border-radius:8px;border:1px solid var(--divider-color);background:var(--secondary-background-color);color:var(--primary-text-color);width:100%}
input:focus,select:focus{outline:2px solid var(--primary-color);outline-offset:-1px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.three{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.seg-pick{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.seg-pick label{border:2px solid var(--divider-color);border-radius:10px;padding:10px 6px;text-align:center;cursor:pointer;font-size:13px;display:flex;flex-direction:column;align-items:center;gap:4px;color:var(--primary-text-color)}
.seg-pick input{display:none}
.seg-pick input:checked+span{font-weight:700}
.seg-pick label:has(input:checked){border-color:var(--c);background:color-mix(in srgb,var(--c) 18%,transparent)}
.daypick{display:flex;gap:6px;flex-wrap:wrap}
.daypick label{cursor:pointer}
.daypick input{display:none}
.daypick span{display:block;padding:8px 11px;border-radius:8px;border:1px solid var(--divider-color);font-size:13px}
.daypick input:checked+span{background:var(--primary-color);color:var(--text-primary-color,#fff);border-color:transparent}
.presets{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px}
.presets label{border:2px solid var(--divider-color);border-radius:10px;padding:10px;cursor:pointer;font-size:13px}
.presets input{display:none}
.presets label:has(input:checked){border-color:var(--primary-color);background:color-mix(in srgb,var(--primary-color) 14%,transparent)}
.checks{display:flex;gap:8px;flex-wrap:wrap}
.checks label{display:flex;gap:6px;align-items:center;border:1px solid var(--divider-color);border-radius:8px;padding:6px 10px;font-size:14px;color:var(--primary-text-color)}
.err{color:#ef4444;font-size:14px;margin-top:12px;min-height:18px}
.foot{display:flex;justify-content:space-between;gap:8px;margin-top:18px;flex-wrap:wrap}
.foot .r{display:flex;gap:8px;margin-left:auto}
details summary{cursor:pointer;color:var(--secondary-text-color);font-size:13px;margin:12px 0 0}
.rng{display:flex;gap:10px;align-items:center}.rng input[type=range]{flex:1}.rng output{min-width:70px;text-align:right;font-weight:600}
.tbl{width:100%;border-collapse:collapse;font-size:13px}
.tbl th,.tbl td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--divider-color)}
.tbl th{color:var(--secondary-text-color);font-weight:500}
.banner{border-radius:10px;padding:10px 14px;margin:0 0 14px;font-size:14px}
.banner.warn{background:#b45309;color:#fff}.banner.info{background:var(--secondary-background-color)}
.seg2{position:absolute;top:3px;bottom:3px;border-radius:4px;color:#fff;font-size:11px;padding:0 5px;display:flex;align-items:center;overflow:hidden;white-space:nowrap}
.rule{display:grid;grid-template-columns:2fr 1fr 1fr auto;gap:8px;align-items:end;margin:6px 0}
.steps div{padding:3px 0}.ok{color:#16a34a}.bad{color:#ef4444}
@media(max-width:600px){.tl .row,.axis{grid-template-columns:80px 1fr}.two,.three{grid-template-columns:1fr}.seg-pick{grid-template-columns:repeat(2,1fr)}}
`;

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
    if (this._unsub) { this._unsub.then?.((u) => u()); this._unsub = null; }
    clearInterval(this._timer);
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
    if (this._modal || !this._loaded) return;
    const now = Date.now();
    if (!force && now - this._lastRender < 1900) return;
    if (this.tab === "overview") this._render();
    else if (["plan", "insights", "diagnostics"].includes(this.tab) && now - (this._lastTabLoad || 0) > 30000) this._tabLoad();
  }

  async _tabLoad() {
    if (this._modal) return;
    this._lastTabLoad = Date.now();
    this._tabError = null;
    try {
      if (this.tab === "plan") this._preview = await this._hass.callWS({ type: "homepower/preview", hours: 24 });
      else if (this.tab === "diagnostics") this._diag = await this._hass.callWS({ type: "homepower/diagnostics" });
      else if (this.tab === "insights") await this._loadHistory();
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
    const html = `<header><ha-menu-button></ha-menu-button><h1><ha-icon icon="mdi:home-battery"></ha-icon>HomePower</h1>
      <button class="btn sm" data-act="flag" data-flag="backup" data-on="${this.live.backup ? 0 : 1}" title="Keep a reserve for power cuts">${this.live.backup ? "Backup: ON" : "Backup mode"}</button>
      <button class="btn sm" data-act="flag" data-flag="away" data-on="${this.live.away ? 0 : 1}" title="Use the away schedules">${this.live.away ? "Away: ON" : "Away mode"}</button>
      <button class="btn sm" data-act="pause" title="Stop HomePower sending any commands">${paused ? "Resume control" : "Pause control"}</button></header>
      ${paused ? `<div style="background:#b45309;color:#fff;padding:10px 20px;font-weight:500">HomePower is paused: it is only reading your batteries and will not send any commands. Batteries keep whatever they were last told to do.</div>` : ""}
      <nav>${tabs.map(([k, i, l]) => `<button class="${this.tab === k ? "on" : ""}" data-tab="${k}"><ha-icon icon="${i}"></ha-icon>${l}</button>`).join("")}</nav>
      <main>${body}</main>`;
    if (html === this._lastHtml) return;
    this._lastHtml = html;
    app.innerHTML = html;
    const mb = app.querySelector("ha-menu-button");
    if (mb) { mb.hass = this._hass; mb.narrow = this._narrow; }
  }

  _color(i) { return SERIES[i % SERIES.length]; }

  _hours(h) { return h == null ? "–" : h < 1 ? `${Math.round(h * 60)} min` : h >= 48 ? `${Math.round(h / 24)} days` : `${h.toFixed(1)} h`; }

  // All batteries as one: charge level, stored energy, flow, and how long it will last / take to fill.
  _homeBattery() {
    const c = this.live.combined;
    if (!c || c.soc == null || (this.data.devices || []).length < 2) return "";
    const flow = c.flow_w || 0, kind = Math.abs(flow) < 20 ? "Idle" : flow > 0 ? "Charging" : "Discharging";
    const n = (v, l) => `<div><b>${v}</b><span>${l}</span></div>`;
    return `<div class="card" style="margin-bottom:16px"><h2><ha-icon icon="mdi:home-battery"></ha-icon>Home battery <span class="sub" style="font-weight:400;font-size:13px">all ${c.total} batteries together${c.online < c.total ? ` (${c.online} online)` : ""}</span></h2>
      <div class="hb">${this._ring(c.soc, 110)}<div class="nums">
        ${n(fmtKwh(c.stored_kwh * 1000), `stored of ${fmtKwh(c.capacity_kwh * 1000)}`)}
        ${n(fmtKwh(c.usable_kwh * 1000), "usable above the minimum")}
        ${n(`${kind} ${fmtW(Math.abs(flow))}`, "right now")}
        ${c.runtime_h != null ? n(this._hours(c.runtime_h), `lasts at ${fmtW(Math.abs(flow) > 20 ? Math.abs(flow) : c.load_w)}`) : ""}
        ${c.full_in_h != null ? n(this._hours(c.full_in_h), "until full at this rate") : ""}
        ${c.load_w != null ? n(fmtW(c.load_w), "house load") : ""}</div></div></div>`;
  }

  // Cheap-rate hours are the single most important setting for planning: nudge until they are entered.
  _tariffBanner() {
    const s = this.data.settings || {};
    if ((s.offpeak_start && s.offpeak_end) || s.offpeak_extra || (this.live.setup || []).length) return "";   // the setup check covers it
    const inf = this.live.tariff_inferred;
    if (!inf && this.live.price_now != null) return "";   // per-slot prices (Agile, Nord Pool...): no fixed cheap hours to enter
    const msg = inf ? `Your cheap-rate hours are being <b>estimated</b> as ${inf.start}-${inf.end} from price history, which can run up to an hour late.`
      : "HomePower does not know your cheap-rate hours yet, so goals cannot pick the cheapest time to charge.";
    return `<div class="banner"><ha-icon icon="mdi:cash-clock"></ha-icon><div class="t">${msg} Enter them once for exact planning.</div>
      <button class="btn pri sm" data-act="goto-tariff">Set my tariff</button></div>`;
  }

  _scenesCard() {
    const sc = this.data.scenes || [], L = this.live, paused = !!this.data.paused;
    const on = (x) => !!x.backup === !!L.backup && !!x.away === !!L.away && !!x.paused === paused;
    return `<div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:palette-swatch-variant"></ha-icon>Scenes</h2>
      <div class="scenes">${sc.map((x) => `<span style="display:inline-flex"><button class="btn sm ${on(x) ? "pri" : ""}" data-act="apply-scene" data-id="${esc(x.name)}" title="Switch to this scene">${esc(x.name)}</button><button class="btn sm" data-act="edit-scene" data-id="${esc(x.name)}" title="Edit" aria-label="Edit ${esc(x.name)}">⋯</button></span>`).join("")}
        <button class="btn sm" data-act="add-scene"><ha-icon icon="mdi:plus"></ha-icon>New scene</button></div>
      <p class="sub" style="font-size:12px;color:var(--secondary-text-color);margin:8px 0 0">A scene is a saved combination of backup / away / pause and optional limits, switched in one tap (or with the homepower.apply_scene service).</p></div>`;
  }

  _sceneModal(x, isNew) {
    const st = x.settings || {}, num = (k, l) => `<div class="f"><label>${l}</label><input type="number" id="sc-${k}" min="0" step="100" value="${st[k] ?? ""}" placeholder="unchanged"></div>`;
    this._openModal(`<div class="overlay" data-close-bg><div class="modal" role="dialog" aria-label="Scene">
      <h2>${isNew ? "New scene" : "Edit scene"}</h2>
      <div class="f"><label>Name</label><input type="text" id="sc-name" value="${esc(x.name || "")}" placeholder="e.g. EV charging night"></div>
      <label style="display:flex;gap:8px;align-items:center;margin:8px 0"><input type="checkbox" id="sc-backup" ${x.backup ? "checked" : ""}>Backup mode on</label>
      <label style="display:flex;gap:8px;align-items:center;margin:8px 0"><input type="checkbox" id="sc-away" ${x.away ? "checked" : ""}>Away mode on</label>
      <label style="display:flex;gap:8px;align-items:center;margin:8px 0"><input type="checkbox" id="sc-paused" ${x.paused ? "checked" : ""}>Pause control (only read, send no commands)</label>
      <h3>Limits this scene sets (leave empty to keep the current value)</h3>
      <div class="two">${num("import_limit_w", "House import limit (W)")}${num("export_limit_w", "Export limit (W)")}</div>
      <div class="two">${num("total_charge_limit_w", "Max total charge (W)")}${num("total_discharge_limit_w", "Max total discharge (W)")}</div>
      <div class="err" id="m-err"></div>
      <div class="foot">${isNew ? "<span></span>" : `<button class="btn danger" data-act="del-scene" data-id="${esc(x.name)}">Delete</button>`}
        <div class="r"><button class="btn" data-act="close">Cancel</button><button class="btn pri" data-act="save-scene" data-orig="${isNew ? "" : esc(x.name)}">Save</button></div></div>
    </div></div>`);
  }

  _collectScene() {
    const r = this.shadowRoot, v = (id) => r.getElementById(id).value, settings = {};
    for (const k of ["import_limit_w", "export_limit_w", "total_charge_limit_w", "total_discharge_limit_w"]) if (v("sc-" + k) !== "") settings[k] = +v("sc-" + k);
    return { name: v("sc-name").trim(), backup: r.getElementById("sc-backup").checked, away: r.getElementById("sc-away").checked, paused: r.getElementById("sc-paused").checked, settings };
  }

  _overview() {
    const ds = this.data.devices;
    if (!ds.length) {
      return `<div class="card empty"><ha-icon icon="mdi:home-battery-outline"></ha-icon><h3>No batteries yet</h3>
        Add your first battery to start seeing live power and SOC.<br><br>
        <button class="btn pri" data-act="add-battery"><ha-icon icon="mdi:plus"></ha-icon>Add battery</button></div>`;
    }
    let net = 0, stored = 0, cap = 0, online = 0;
    const rd = ds.map((d) => {
      const r = this._reading(d);
      if (r.power != null) net += r.power;
      if (r.soc != null) stored += (r.soc / 100) * d.capacity_wh;
      cap += d.capacity_wh;
      if (r.online) online++;
      return r;
    });
    const grid = this._gridW();
    const s = this.data.settings || {};
    const netTxt = Math.abs(net) < 20 ? "Idle" : net > 0 ? "Charging" : "Discharging";
    const tile = (l, v, sub, c) => `<div class="card tile"><div class="lbl">${l}</div><div class="val" ${c ? `style="color:${c}"` : ""}>${v}</div><div class="sub">${sub}</div></div>`;
    const tiles = `<div class="grid g-tiles">
      ${tile("Battery flow", fmtW(Math.abs(net)), netTxt + (s.total_charge_limit_w && net > 0 ? ` · cap ${fmtW(s.total_charge_limit_w)}` : "") + (s.total_discharge_limit_w && net < 0 ? ` · cap ${fmtW(s.total_discharge_limit_w)}` : ""), net > 20 ? MODES.charge.color : net < -20 ? MODES.discharge.color : "")}
      ${tile("Stored energy", fmtKwh(stored), `${cap ? Math.round((stored / cap) * 100) : 0}% of ${fmtKwh(cap)}`)}
      ${tile("Grid", grid == null ? "–" : fmtW(Math.abs(grid)), grid == null ? "No grid sensor set (Settings)" : Math.abs(grid) < 20 ? "Balanced" : grid > 0 ? "Importing" : "Exporting", grid == null ? "" : grid > 20 ? "#dc2626" : grid < -20 ? MODES.charge.color : "")}
      ${tile("Batteries online", `${online} / ${ds.length}`, "Reading directly from the batteries")}
      ${this.live.savings ? tile("Saved today", `${this.live.currency || "£"}${(this.live.savings.today).toFixed(2)}`, `This month ${this.live.currency || "£"}${this.live.savings.month.toFixed(2)} (estimate)`, this.live.savings.today >= 0 ? MODES.charge.color : "#dc2626") : ""}
      ${this.live.price_now != null ? tile("Price now", `${this.live.currency || "£"}${this.live.price_now.toFixed(3)}/kWh`, "From your rates") : ""}
    </div>`;
    const cards = ds.map((d, i) => this._batCard(d, rd[i], i)).join("");
    return `${this._alertsBanner()}${this._setupCard(false)}${this._tariffBanner()}${this._homeBattery()}${tiles}
      <h2 style="margin:22px 0 10px;font-size:18px">Batteries</h2>
      <div class="grid g-cards">${cards}</div>
      ${this._scenesCard()}
      <div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:chart-areaspline"></ha-icon>Power, last ${this._durLabel(this._flowMin())}<span class="durs">${this._durButtons()}</span></h2>${this._flowChart()}</div>
      <div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:timeline-clock"></ha-icon>Today's plan</h2>${this._timeline()}</div>
      <div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:text-box-outline"></ha-icon>Activity</h2>${this._log()}</div>`;
  }

  _ring(soc, size = 96) {
    const r = 40, c = 2 * Math.PI * r, v = soc == null ? 0 : Math.max(0, Math.min(100, soc));
    const col = soc == null ? "#9ca3af" : soc < 20 ? "#dc2626" : soc < 50 ? "#f59e0b" : "#16a34a";
    return `<svg class="ring" width="${size}" height="${size}" viewBox="0 0 100 100" role="img" aria-label="State of charge ${soc == null ? "unknown" : Math.round(soc) + " percent"}">
      <circle cx="50" cy="50" r="${r}" fill="none" stroke="var(--divider-color)" stroke-width="10"/>
      <circle cx="50" cy="50" r="${r}" fill="none" stroke="${col}" stroke-width="10" stroke-linecap="round" stroke-dasharray="${(v / 100) * c} ${c}" transform="rotate(-90 50 50)"/>
      <text x="50" y="${soc == null ? 56 : 54}" text-anchor="middle" font-size="${soc == null ? 20 : 24}" font-weight="700">${soc == null ? "–" : Math.round(soc) + "%"}</text></svg>`;
  }

  _batCard(d, r, i) {
    const lv = r.lv || {};
    const p = r.power;
    const state = !r.online ? "Offline" : p == null ? "–" : Math.abs(p) < 20 ? "Idle" : p > 0 ? "Charging" : "Discharging";
    const col = !r.online ? "#9ca3af" : p > 20 ? MODES.charge.color : p < -20 ? MODES.discharge.color : "var(--secondary-text-color)";
    const mode = lv.mode && MODES[lv.mode];
    const want = lv.desired_mode && MODES[lv.desired_mode];
    const chips = [];
    if (want) chips.push(`<span class="chip m" style="background:${want.color}" title="${esc(want.help)}">Target: ${want.label}${lv.desired_power_w ? " " + fmtW(lv.desired_power_w) : ""}</span>`);
    else if (r.online) chips.push(`<span class="chip">Device's own control</span>`);
    if (mode) chips.push(`<span class="chip"><span class="dot" style="background:${mode.color}"></span>Mode: ${mode.label}</span>`);
    if (lv.protected) chips.push(`<span class="chip warn"><ha-icon icon="mdi:shield-alert" style="--mdc-icon-size:14px"></ha-icon>Protection active</span>`);
    if (lv.reason) chips.push(`<span class="chip" title="Why HomePower is doing this">${esc(lv.reason)}</span>`);
    if (lv.override) chips.push(`<span class="chip m" style="background:#7c3aed">Manual: ${(MODES[lv.override.mode] || {}).label} until ${new Date(lv.override.until).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>`);
    if (d.control_enabled === false) chips.push(`<span class="chip warn" title="HomePower reads this battery but never sends it commands">Monitor only</span>`);
    if (lv.guard_held) chips.push(`<span class="chip" title="Held by the SOC limit">SOC limit reached</span>`);
    if (lv.last_error) chips.push(`<span class="chip bad" title="${esc(lv.last_error)}">Last write failed</span>`);
    if (this.data.settings.zero_import_device === d.device_id) chips.push(`<span class="chip"><ha-icon icon="mdi:scale-balance" style="--mdc-icon-size:14px"></ha-icon>Zero-import</span>`);
    for (const sid of lv.active_schedules || []) chips.push(`<span class="chip"><ha-icon icon="mdi:calendar-clock" style="--mdc-icon-size:14px"></ha-icon>${esc(sid)}</span>`);
    return `<div class="card"><div class="bat">${this._ring(r.soc)}
      <div class="info"><div class="nm"><span class="dot" style="background:${this._color(i)}"></span>${esc(d.name)}</div>
      <div class="pw" style="color:${col}">${state}${r.online && p != null && Math.abs(p) >= 20 ? " · " + fmtW(Math.abs(p)) : ""}</div>
      <div class="sub" style="font-size:12px;color:var(--secondary-text-color)">${r.soc != null ? fmtKwh((r.soc / 100) * d.capacity_wh) + " of " : ""}${fmtKwh(d.capacity_wh)} · max ${fmtW(d.max_charge_w)} in / ${fmtW(d.max_discharge_w)} out</div>
      <div class="chips">${chips.join("")}</div></div></div>${this._spark(this._hist[d.device_id] || [], this._color(i))}
      <div style="display:flex;gap:8px;margin-top:10px"><button class="btn sm" data-act="override" data-id="${esc(d.device_id)}"><ha-icon icon="mdi:hand-back-right" style="--mdc-icon-size:16px"></ha-icon>Override</button>${lv.override ? `<button class="btn sm" data-act="clear-override" data-id="${esc(d.device_id)}">Clear override</button>` : ""}</div></div>`;
  }

  _spark(h, color) {
    const pts = h.filter((x) => x.p != null);
    if (pts.length < 2) return `<div class="spark" style="font-size:12px;color:var(--secondary-text-color);display:flex;align-items:center">Collecting live data…</div>`;
    return `<svg class="spark" viewBox="0 0 300 46" preserveAspectRatio="none" aria-hidden="true">${this._path(pts, 300, 46, color, true)}</svg>`;
  }

  _path(pts, W, H, color, area) {
    const max = Math.max(500, ...pts.map((x) => Math.abs(x.p)));
    const t0 = pts[0].t, t1 = pts[pts.length - 1].t || t0 + 1;
    const X = (t) => ((t - t0) / Math.max(1, t1 - t0)) * W;
    const Y = (p) => H / 2 - (p / max) * (H / 2 - 2);
    const line = pts.map((x, i) => `${i ? "L" : "M"}${X(x.t).toFixed(1)},${Y(x.p).toFixed(1)}`).join("");
    return `<line x1="0" x2="${W}" y1="${H / 2}" y2="${H / 2}" stroke="var(--divider-color)" stroke-dasharray="3 3"/>
      ${area ? `<path d="${line}L${X(t1)},${H / 2}L0,${H / 2}Z" fill="${color}" opacity=".15"/>` : ""}
      <path d="${line}" fill="none" stroke="${color}" stroke-width="2" vector-effect="non-scaling-stroke"/>`;
  }

  // ---- chart helpers: nice axis ticks, a plot with labelled Y and X axes, adjustable live duration ----
  _niceTicks(lo, hi, n = 4) {
    const span = Math.max(hi - lo, 1e-9), raw = span / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = ([1, 2, 2.5, 5, 10].map((m) => m * mag).find((x) => x >= raw)) || 10 * mag;
    const a = Math.floor(lo / step + 1e-9) * step, b = Math.ceil(hi / step - 1e-9) * step, ticks = [];
    for (let v = a; v <= b + step / 2; v += step) ticks.push(Math.round(v / step) * step);
    return { lo: a, hi: b, ticks };
  }
  _downsample(pts, maxN = 300) {
    if (pts.length <= maxN) return pts;
    const k = Math.ceil(pts.length / maxN), out = [];
    for (let i = 0; i < pts.length; i += k) {
      const c = pts.slice(i, i + k);
      out.push({ t: c.reduce((a, x) => a + x.t, 0) / c.length, v: c.reduce((a, x) => a + x.v, 0) / c.length });
    }
    return out;
  }
  _fmtT(t, span) {
    const d = new Date(t);
    if (span <= 36 * 3600e3) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    if (span <= 3 * 86400e3) return d.toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
    return d.toLocaleDateString([], { month: "short", day: "numeric" });
  }
  // series: [{pts:[{t,v}], color, width}]; o: {lo, hi, ticks, tmin, tmax, fmtY, height, ariaLabel}
  _plot(series, o) {
    const W = 800, H = 200, { lo, hi, ticks, tmin, tmax } = o, height = o.height || 210;
    const X = (t) => ((t - tmin) / Math.max(1, tmax - tmin)) * W;
    const Y = (v) => H - ((v - lo) / Math.max(1e-9, hi - lo)) * H;
    const xt = [0, 0.25, 0.5, 0.75, 1].map((f) => tmin + f * (tmax - tmin));
    const grid = ticks.map((v) => `<line x1="0" x2="${W}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}" stroke="var(--divider-color)" stroke-opacity="${v === 0 && lo < 0 ? 1 : 0.5}" ${v === 0 && lo < 0 ? "" : 'stroke-dasharray="3 4"'} vector-effect="non-scaling-stroke"/>`).join("")
      + xt.slice(1, -1).map((t) => `<line x1="${X(t).toFixed(1)}" x2="${X(t).toFixed(1)}" y1="0" y2="${H}" stroke="var(--divider-color)" stroke-opacity=".3" vector-effect="non-scaling-stroke"/>`).join("");
    const lines = series.map((s) => s.pts.length < 2 ? "" : `<path d="${s.pts.map((p, i) => `${i ? "L" : "M"}${X(p.t).toFixed(1)},${Math.min(H, Math.max(0, Y(p.v))).toFixed(1)}`).join("")}" fill="none" stroke="${s.color}" stroke-width="${s.width || 2.2}" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>`).join("");
    const ylab = ticks.map((v) => `<span style="top:${((1 - (v - lo) / Math.max(1e-9, hi - lo)) * 100).toFixed(2)}%">${o.fmtY(v)}</span>`).join("");
    const xlab = xt.map((t, i) => `<span style="left:${i * 25}%">${this._fmtT(t, tmax - tmin)}</span>`).join("");
    return `<div class="plot"><div class="plot-y" style="height:${height}px">${ylab}</div>
      <div class="plot-main"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:${height}px" role="img" aria-label="${esc(o.ariaLabel || "Chart")}">${grid}${lines}</svg>
      <div class="plot-x">${xlab}</div></div></div>`;
  }

  _flowMin() {
    let v = this._flowMinV;
    if (!v) { try { v = parseInt(localStorage.getItem("homepower.flowMin"), 10); } catch (e) { /* storage unavailable */ } }
    return (this._flowMinV = [15, 30, 60, 180, 360, 720, 1440].includes(v) ? v : 30);
  }
  _durLabel(m) { return m < 60 ? `${m} minutes` : m === 60 ? "hour" : `${m / 60} hours`; }
  _durButtons() {
    const cur = this._flowMin();
    return [[15, "15m"], [30, "30m"], [60, "1h"], [180, "3h"], [360, "6h"], [720, "12h"], [1440, "24h"]]
      .map(([m, l]) => `<button class="btn sm ${m === cur ? "pri" : ""}" data-act="flowdur" data-m="${m}">${l}</button>`).join("");
  }
  // History older than this page has been open comes from Home Assistant's own statistics (5-minute averages).
  async _backfillFlow() {
    const mins = this._flowMin(), ids = (this.data.entity_ids || {}).devices || {};
    const list = Object.values(ids).map((e) => e && e.power).filter(Boolean);
    const key = `${mins}:${list.join(",")}`;
    if (!list.length || mins <= 30 || (this._flowBack && this._flowBack.key === key && Date.now() - this._flowBack.at < 120000)) return;
    this._flowBack = { key, at: Date.now(), data: (this._flowBack && this._flowBack.key === key) ? this._flowBack.data : {} };
    try {
      const end = new Date(), start = new Date(end - mins * 60e3);
      const res = await this._hass.callWS({ type: "recorder/statistics_during_period", start_time: start.toISOString(), end_time: end.toISOString(),
        statistic_ids: list, period: "5minute", types: ["mean"] });
      this._flowBack.data = res || {};
      if (this.tab === "overview" && !this._modal) this._render();
    } catch (e) { /* recorder statistics are optional: the live samples still draw */ }
  }

  _flowChart() {
    const ds = this.data.devices, mins = this._flowMin(), now = Date.now(), tmin = now - mins * 60e3;
    this._backfillFlow();
    const back = (this._flowBack && this._flowBack.data) || {}, ids = (this.data.entity_ids || {}).devices || {};
    const series = ds.map((d, i) => {
      const live = (this._hist[d.device_id] || []).filter((x) => x.p != null && x.t >= tmin).map((x) => ({ t: x.t, v: x.p }));
      const first = live.length ? live[0].t : now;
      const eid = (ids[d.device_id] || {}).power;
      const old = mins > 30 && eid ? (back[eid] || []).filter((r) => r.mean != null).map((r) => ({ t: typeof r.start === "number" ? r.start : Date.parse(r.start), v: r.mean })).filter((x) => x.t >= tmin && x.t < first) : [];
      return { d, i, pts: this._downsample([...old, ...live]), color: this._color(i), width: 2.4 };
    }).filter((s) => s.pts.length > 1);
    if (!series.length) return `<div class="sub" style="color:var(--secondary-text-color)">Collecting live data… the chart fills in as readings arrive.</div>`;
    const all = series.flatMap((s) => s.pts);
    const m = Math.max(500, ...all.map((x) => Math.abs(x.v)));
    const nt = this._niceTicks(-m, m, 4);
    const t0 = Math.min(tmin, ...all.map((x) => x.t));
    const covered = Math.round((now - Math.min(...all.map((x) => x.t))) / 60e3);
    return `${this._plot(series, { lo: nt.lo, hi: nt.hi, ticks: nt.ticks, tmin: t0, tmax: now, fmtY: (v) => (v === 0 ? "0" : fmtW(v)), ariaLabel: "Battery power over time" })}
      <div class="legend">${series.map((s) => `<span><i class="sw" style="background:${s.color}"></i>${esc(s.d.name)}</span>`).join("")}
      <span>Above 0 = charging, below = discharging${covered < mins - 2 ? ` · showing the last ${covered} min so far (the page records while open${mins > 30 ? "; older data comes from Home Assistant history" : ""})` : ""}</span></div>`;
  }

  _segmentsToday(s) {
    const now = new Date();
    const dayOk = (dt) => {
      if (s.on_date) return s.on_date === `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
      const wd = (dt.getDay() + 6) % 7;
      if (!s.days.includes(wd)) return false;
      if (s.season_start && s.season_end) {
        const [sm, sd] = s.season_start.split("-").map(Number), [em, ed] = s.season_end.split("-").map(Number);
        const cur = (dt.getMonth() + 1) * 100 + dt.getDate(), a = sm * 100 + sd, b = em * 100 + ed;
        return a <= b ? cur >= a && cur <= b : cur >= a || cur <= b;
      }
      return true;
    };
    const hm = (t) => { const [h, m] = t.split(":").map(Number); return h + m / 60; };
    if (s.all_day) return dayOk(now) ? [[0, 24]] : [];
    const a = hm(s.start), b = hm(s.end);
    const yest = new Date(now.getTime() - 864e5);
    const out = [];
    if (b > a) { if (dayOk(now)) out.push([a, b]); }
    else { if (dayOk(now)) out.push([a, 24]); if (dayOk(yest)) out.push([0, b]); }
    return out;
  }

  _timeline() {
    const ds = this.data.devices;
    if (!ds.length) return "";
    const now = new Date(), nowH = now.getHours() + now.getMinutes() / 60;
    const rows = ds.map((d) => {
      const zi = this.data.settings.zero_import_device === d.device_id;
      const mine = this.data.schedules.filter((s) => s.enabled && (!s.device_ids.length || s.device_ids.includes(d.device_id))).sort((x, y) => x.priority - y.priority);
      const segs = mine.flatMap((s) => this._segmentsToday(s).map(([a, b]) => `<div class="seg" title="${esc(s.id)}: ${s.start}–${s.end}" style="left:${(a / 24) * 100}%;width:${((b - a) / 24) * 100}%;background:${MODES[s.mode].color}">${b - a > 2 ? esc(s.id) : ""}</div>`));
      return `<div class="row"><div class="nm" title="${esc(d.name)}">${esc(d.name)}</div><div class="bar ${zi ? "zi" : ""}">${segs.join("")}<div class="now" style="left:${(nowH / 24) * 100}%"></div></div></div>`;
    }).join("");
    const ticks = `<div class="axis"><div></div><div><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span></div></div>`;
    const legend = `<div class="legend">${Object.entries(MODES).map(([k, m]) => `<span><i class="sw" style="background:${m.color}"></i>${m.label}</span>`).join("")}${this.data.settings.zero_import_device ? `<span><i class="sw" style="background:repeating-linear-gradient(45deg,#cbd5e1,#cbd5e1 3px,#93b4f5 3px,#93b4f5 6px)"></i>Zero-import when no schedule applies</span>` : ""}<span><i class="sw" style="background:var(--primary-text-color);width:3px"></i>Now</span></div>`;
    return `<div class="tl">${rows}${ticks}</div>${legend}`;
  }

  _log() {
    const ev = (this.live.events || []).slice().reverse().slice(0, 30);
    if (!ev.length) return `<div class="sub" style="color:var(--secondary-text-color)">Nothing yet. Commands HomePower sends to your batteries will be listed here.</div>`;
    const name = (id) => (this.data.devices.find((d) => d.device_id === id) || {}).name || id;
    return `<div class="log">${ev.map(([t, id, m]) => `<div><span class="t">${esc(new Date(t).toLocaleTimeString())}</span> <b>${esc(name(id))}</b> ${esc(m)}</div>`).join("")}</div>`;
  }

  // ---- schedules tab -----------------------------------------------------
  _schedules() {
    const ss = this.data.schedules;
    const head = `<div class="bar-actions"><h2>Schedules</h2><button class="btn pri" data-act="add-schedule"><ha-icon icon="mdi:plus"></ha-icon>Add schedule</button></div>`;
    if (!this.data.devices.length) return head + `<div class="card empty"><ha-icon icon="mdi:home-battery-outline"></ha-icon><h3>Add a battery first</h3>Schedules tell your batteries what to do and when.<br><br><button class="btn pri" data-act="add-battery">Add battery</button></div>`;
    const tl = ss.length ? `<div class="card" style="margin-bottom:16px"><h2><ha-icon icon="mdi:timeline-clock"></ha-icon>Today</h2>${this._timeline()}</div>` : "";
    if (!ss.length) return head + `<div class="card empty"><ha-icon icon="mdi:calendar-clock"></ha-icon><h3>No schedules yet</h3>Try a cheap-rate overnight charge or an evening discharge.<br><br>
      <button class="btn pri" data-act="add-schedule" data-tpl="cheap">Cheap-rate overnight charge</button>
      <button class="btn" data-act="add-schedule" data-tpl="evening">Evening discharge</button></div>`;
    const names = (s) => !s.device_ids.length ? "All batteries" : s.device_ids.map((id) => (this.data.devices.find((d) => d.device_id === id) || {}).name || id).join(", ");
    const cards = ss.slice().sort((a, b) => a.start.localeCompare(b.start)).map((s) => {
      const m = MODES[s.mode];
      const bits = [];
      if (s.power_w) bits.push(fmtW(s.power_w));
      if (s.mode === "charge" && s.target_soc != null) bits.push(`up to ${s.target_soc}%`);
      if (s.mode === "discharge" && s.reserve_soc != null) bits.push(`keep ${s.reserve_soc}%`);
      if (s.condition_entity) bits.push(`while ${s.condition_entity} is on`);
      if (s.on_date) bits.push(`only ${s.on_date}`);
      if (s.season_start) bits.push(`${s.season_start} → ${s.season_end}`);
      if (s.priority) bits.push(`priority ${s.priority}`);
      return `<div class="card sch ${s.enabled ? "" : "off"}">
        <label class="switch" title="Enable / disable"><input type="checkbox" data-toggle="${esc(s.id)}" ${s.enabled ? "checked" : ""}><i></i></label>
        <div class="meta"><div class="when">${s.all_day ? "All day" : `${s.start} – ${s.end}`} <span class="chip m" style="background:${m.color};vertical-align:middle;margin-left:6px"><ha-icon icon="${m.icon}" style="--mdc-icon-size:14px"></ha-icon>${m.label}</span></div>
          <div style="font-weight:600;margin-top:2px">${esc(s.id)}</div>
          <div class="sub">${esc(names(s))}${bits.length ? " · " + esc(bits.join(" · ")) : ""}</div>
          <div class="dayrow">${DAYS.map((d, i) => `<span class="${s.days.includes(i) ? "on" : ""}">${d.slice(0, 2)}</span>`).join("")}</div></div>
        <div><button class="btn sm" data-act="edit-schedule" data-id="${esc(s.id)}"><ha-icon icon="mdi:pencil" style="--mdc-icon-size:16px"></ha-icon>Edit</button></div></div>`;
    }).join("");
    return head + tl + `<div class="grid g-cards">${cards}</div>
      <p class="sub" style="color:var(--secondary-text-color);font-size:13px;margin-top:16px">When several schedules overlap, the higher priority wins. With no schedule active, a battery follows its own settings (or zero-import, if enabled in Settings).</p>`;
  }

  _scheduleModal(s, isNew) {
    const devs = this.data.devices;
    const maxP = Math.max(1000, ...devs.map((d) => Math.max(d.max_charge_w, d.max_discharge_w)));
    const sel = new Set(s.device_ids);
    const html = `<div class="overlay" data-close-bg><div class="modal" role="dialog" aria-label="Schedule">
      <h2>${isNew ? "Add schedule" : "Edit schedule"}</h2><p class="lead">What should your batteries do, and when?</p>
      <div class="f"><label>Name</label><input type="text" id="s-name" value="${esc(s.id)}" placeholder="e.g. Cheap-rate overnight charge"></div>
      <h3>Which batteries</h3>
      <div class="checks">${devs.map((d) => `<label><input type="checkbox" name="s-dev" value="${esc(d.device_id)}" ${sel.has(d.device_id) ? "checked" : ""}>${esc(d.name)}</label>`).join("")}</div>
      <div class="f"><span class="hint">Leave all unticked to apply to every battery.</span></div>
      <h3>What to do</h3>
      <div class="seg-pick">${Object.entries(MODES).map(([k, m]) => `<label style="--c:${m.color}"><input type="radio" name="s-mode" value="${k}" ${s.mode === k ? "checked" : ""}><ha-icon icon="${m.icon}" style="color:${m.color}"></ha-icon><span>${m.label}</span></label>`).join("")}</div>
      <div class="f"><span class="hint" id="s-modehelp"></span><span class="hint" id="s-unsup" style="color:#ef4444"></span></div>
      <div id="s-power" class="f"><label>Power</label><div class="rng"><input type="range" id="s-pw" min="0" max="${maxP}" step="100" value="${s.power_w ?? Math.min(3000, maxP)}"><output id="s-pwo"></output></div></div>
      <div class="two"><div class="f" id="s-tgt"><label>Stop charging at SOC (%)</label><input type="number" id="s-target" min="0" max="100" value="${s.target_soc ?? ""}" placeholder="100"></div>
        <div class="f" id="s-res"><label>Don't discharge below SOC (%)</label><input type="number" id="s-reserve" min="0" max="100" value="${s.reserve_soc ?? ""}" placeholder="device minimum"></div></div>
      <h3>When</h3>
      <div class="two" id="s-times"><div class="f"><label>Start</label><input type="time" id="s-start" value="${s.start}"></div><div class="f"><label>End</label><input type="time" id="s-end" value="${s.end}"></div></div>
      <div class="f" id="s-overnight"><span class="hint">An end time earlier than the start runs overnight (e.g. 23:30 → 07:30).</span></div>
      <h3>Only while… (optional)</h3>
      <div class="f"><input type="text" id="s-cond" list="dl-cond" value="${esc(s.condition_entity || "")}" placeholder="binary_sensor.cheap_rate" autocomplete="off"><datalist id="dl-cond">${this._entityOptions(["binary_sensor", "input_boolean", "switch"])}</datalist>
        <span class="hint">Follow a tariff or any on/off entity (e.g. an Octopus or Agile cheap-rate sensor). The schedule only runs while it is on.</span></div>
      <label style="display:flex;gap:8px;align-items:center;margin:4px 0 12px"><input type="checkbox" id="s-allday" ${s.all_day ? "checked" : ""}>Ignore the times above; follow the entity only</label>
      <div class="daypick">${DAYS.map((d, i) => `<label><input type="checkbox" name="s-day" value="${i}" ${s.days.includes(i) ? "checked" : ""}><span>${d}</span></label>`).join("")}</div>
      <details ${s.season_start || s.on_date || s.priority ? "open" : ""}><summary>More options: season, one-off date, priority</summary>
        <div class="two"><div class="f"><label>Season starts (MM-DD)</label><input type="text" id="s-ss" value="${esc(s.season_start || "")}" placeholder="10-01" pattern="\\d\\d-\\d\\d"></div>
          <div class="f"><label>Season ends (MM-DD)</label><input type="text" id="s-se" value="${esc(s.season_end || "")}" placeholder="03-31" pattern="\\d\\d-\\d\\d"></div></div>
        <div class="two"><div class="f"><label>Only on this date</label><input type="date" id="s-date" value="${esc(s.on_date || "")}"></div>
          <div class="f"><label>Priority (higher wins)</label><input type="number" id="s-prio" value="${s.priority || 0}" min="-100" max="100"></div></div>
        <div class="two"><div class="f"><label>Applies when</label><select id="s-presence">${[["any", "Always"], ["home", "Home only"], ["away", "Away mode only"]].map(([v, l]) => `<option value="${v}" ${(s.presence || "any") === v ? "selected" : ""}>${l}</option>`).join("")}</select></div>
          <div class="f"><label>Share one total across batteries (W)</label><input type="number" id="s-total" min="0" step="100" value="${s.total_power_w ?? ""}" placeholder="off: each battery uses the power above"></div></div></details>
      <div class="err" id="m-err"></div>
      <div class="foot">${isNew ? "<span></span>" : `<button class="btn danger" data-act="del-schedule" data-id="${esc(s.id)}"><ha-icon icon="mdi:delete" style="--mdc-icon-size:16px"></ha-icon>Delete</button>`}
        <div class="r"><button class="btn" data-act="close">Cancel</button><button class="btn pri" data-act="save-schedule" data-orig="${isNew ? "" : esc(s.id)}">Save</button></div></div>
    </div></div>`;
    this._openModal(html);
    this._syncSchedForm();
  }

  _syncSchedForm() {
    const r = this.shadowRoot;
    const mode = (r.querySelector('input[name="s-mode"]:checked') || {}).value || "charge";
    const q = (id) => r.getElementById(id);
    const allDay = q("s-allday").checked;
    q("s-times").style.display = allDay ? "none" : "";
    q("s-overnight").style.display = allDay ? "none" : "";
    q("s-power").style.display = mode === "charge" || mode === "discharge" ? "" : "none";
    q("s-tgt").style.display = mode === "charge" ? "" : "none";
    q("s-res").style.display = mode === "discharge" ? "" : "none";
    q("s-modehelp").textContent = MODES[mode].help;
    const picked = [...r.querySelectorAll('input[name="s-dev"]:checked')].map((x) => x.value);
    const targets = this.data.devices.filter((d) => !picked.length || picked.includes(d.device_id));
    const bad = targets.filter((d) => d.supported_modes && !d.supported_modes.includes(mode)).map((d) => d.name);
    q("s-unsup").textContent = bad.length ? `${MODES[mode].label} isn't available on: ${bad.join(", ")}. Those batteries will be left to their own control.` : "";
    q("s-pwo").textContent = fmtW(+q("s-pw").value);
  }

  _collectSchedule() {
    const r = this.shadowRoot, v = (id) => r.getElementById(id).value;
    const mode = r.querySelector('input[name="s-mode"]:checked').value;
    const n = (id) => (v(id) === "" ? null : +v(id));
    return {
      id: v("s-name").trim(),
      device_ids: [...r.querySelectorAll('input[name="s-dev"]:checked')].map((x) => x.value),
      mode,
      power_w: mode === "charge" || mode === "discharge" ? +v("s-pw") : null,
      target_soc: mode === "charge" ? n("s-target") : null,
      reserve_soc: mode === "discharge" ? n("s-reserve") : null,
      start: v("s-start"), end: v("s-end"),
      days: [...r.querySelectorAll('input[name="s-day"]:checked')].map((x) => +x.value),
      season_start: v("s-ss").trim() || null, season_end: v("s-se").trim() || null,
      on_date: v("s-date") || null, priority: +v("s-prio") || 0, enabled: true,
      presence: v("s-presence") || "any", total_power_w: n("s-total"),
      all_day: r.getElementById("s-allday").checked, condition_entity: v("s-cond").trim() || null,
    };
  }

  _overrideModal(did) {
    const devs = this.data.devices;
    const html = `<div class="overlay" data-close-bg><div class="modal" role="dialog" aria-label="Manual override">
      <h2>Manual override</h2><p class="lead">Take control for a while. This beats any schedule, then HomePower goes back to normal.</p>
      <h3>Batteries</h3><div class="checks">${devs.map((d) => `<label><input type="checkbox" name="o-dev" value="${esc(d.device_id)}" ${d.device_id === did ? "checked" : ""}>${esc(d.name)}</label>`).join("")}</div>
      <h3>What to do</h3>
      <div class="seg-pick">${Object.entries(MODES).map(([k, m]) => `<label style="--c:${m.color}"><input type="radio" name="o-mode" value="${k}" ${k === "charge" ? "checked" : ""}><ha-icon icon="${m.icon}" style="color:${m.color}"></ha-icon><span>${m.label}</span></label>`).join("")}</div>
      <div class="f"><label>Power (W) — for charge / discharge</label><input type="number" id="o-pw" min="0" step="100" value="3000"></div>
      <div class="two"><div class="f"><label>Stop charging at SOC (%)</label><input type="number" id="o-soc" min="0" max="100" value="100"></div>
        <div class="f"><label>For how long</label><select id="o-min"><option value="30">30 minutes</option><option value="60" selected>1 hour</option><option value="120">2 hours</option><option value="240">4 hours</option><option value="720">12 hours</option></select></div></div>
      <div class="err" id="m-err"></div>
      <div class="foot"><span></span><div class="r"><button class="btn" data-act="close">Cancel</button><button class="btn pri" data-act="save-override">Start override</button></div></div>
    </div></div>`;
    this._openModal(html);
  }

  // ---- batteries tab -----------------------------------------------------
  _batteries() {
    const head = `<div class="bar-actions"><h2>Batteries</h2><button class="btn pri" data-act="add-battery"><ha-icon icon="mdi:plus"></ha-icon>Add battery</button></div>`;
    if (!this.data.devices.length) return head + `<div class="card empty"><ha-icon icon="mdi:home-battery-outline"></ha-icon><h3>No batteries yet</h3>HomePower controls your batteries through the integrations you already have in Home Assistant (Anker SOLIX, EcoFlow…).<br><br><button class="btn pri" data-act="add-battery">Add your first battery</button></div>`;
    const cards = this.data.devices.map((d, i) => {
      const r = this._reading(d);
      return `<div class="card"><div class="bat">${this._ring(r.soc, 80)}<div class="info">
        <div class="nm"><span class="dot" style="background:${this._color(i)}"></span>${esc(d.name)} <span class="chip ${r.online ? "" : "bad"}">${r.online ? "Online" : "Offline"}</span></div>
        <div class="sub" style="font-size:13px;color:var(--secondary-text-color);margin-top:4px">${esc(this._profile(d.kind).label)}<br>Modes: ${(d.supported_modes || []).map((m) => (MODES[m] || {}).label).join(", ")}<br>${fmtKwh(d.capacity_wh)} · ${fmtW(d.max_charge_w)} in / ${fmtW(d.max_discharge_w)} out · min SOC ${d.min_soc}%</div></div></div>
        <div style="margin-top:12px;display:flex;gap:8px"><button class="btn sm" data-act="edit-battery" data-id="${esc(d.device_id)}"><ha-icon icon="mdi:pencil" style="--mdc-icon-size:16px"></ha-icon>Edit</button>
          <button class="btn sm" data-act="toggle-control" data-id="${esc(d.device_id)}" data-on="${d.control_enabled === false ? 1 : 0}">${d.control_enabled === false ? "Allow control" : "Stop control (monitor only)"}</button></div></div>`;
    }).join("");
    return head + `<div class="grid g-cards">${cards}</div>`;
  }

  get _cat() { return this.data.catalogue || { profiles: [], drivers: {}, vendors: {} }; }
  _profile(id) { return this._cat.profiles.find((p) => p.id === id) || this._cat.profiles.find((p) => p.id === "custom") || { driver: "entities", label: id, vendor: "Other", notes: "" }; }
  _spec(pid) { return this._cat.drivers[this._profile(pid).driver] || { entities: [], mode_map: [], modes: [], connection: [] }; }

  _entityOptions(domains, platform) {
    const st = this._hass.states, en = this._hass.entities || {};
    let ids = Object.keys(st).filter((e) => domains.includes(e.split(".")[0]));
    const mine = platform ? ids.filter((e) => en[e] && en[e].platform === platform) : [];
    if (mine.length) ids = mine;
    return ids.sort().map((e) => `<option value="${esc(e)}">${esc(st[e].attributes.friendly_name || "")}</option>`).join("");
  }

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
  }

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
  }

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
  }

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
  }

  _collectBattery(origId) {
    const r = this.shadowRoot, v = (id) => (r.getElementById(id) || {}).value || "", pid = v("b-kind"), prof = this._profile(pid), spec = this._spec(pid);
    const name = v("b-name").trim();
    const out = {
      device_id: origId || slug(name) || "battery_" + Date.now().toString(36),
      name, kind: pid,
      capacity_wh: v("b-cap"), max_charge_w: v("b-mc"), max_discharge_w: v("b-md"), min_soc: v("b-min"), priority: v("b-prio"), role: (r.getElementById("b-role") || {}).value || "normal",
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
  }

  _collectConnection(prof, spec) {
    const r = this.shadowRoot, v = (k) => (r.getElementById("c-" + k) || {}).value || "";
    const c = Object.fromEntries(spec.connection.map((f) => [f.k, v(f.k).trim()]));
    if (prof.driver === "modbus_generic") { c.port = +c.port || 502; c.unit_id = +c.unit_id || 1; c.map = v("map"); }
    if (prof.driver === "anker_modbus") { c.port = +c.port || 502; c.unit_id = +c.unit_id || 1; }
    return c;
  }

  async _scanAnker() {
    const box = this.shadowRoot.getElementById("b-scan");
    box.innerHTML = `<div class="hint" style="font-size:13px;color:var(--secondary-text-color);margin-top:6px">Scanning your network… this can take a few seconds.</div>`;
    try {
      const { found } = await this._hass.callWS({ type: "homepower/discover/anker" });
      box.innerHTML = found.length
        ? `<div class="chips" style="margin-top:8px">${found.map((f) => `<button class="btn sm" data-act="pick-host" data-host="${esc(f.host)}" data-port="${f.port}">${esc(f.host)} · ${esc(f.model || "Anker battery")} · ${Math.round(f.soc)}%</button>`).join("")}</div>`
        : `<div class="hint" style="font-size:13px;color:var(--secondary-text-color);margin-top:6px">Nothing found. Check Modbus TCP is switched on in the Anker app, or type the battery's IP address below.</div>`;
    } catch (e) { box.innerHTML = `<div class="err">${esc(e.message || e)}</div>`; }
  }

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
  }

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
  }

  // ---- alerts banner ------------------------------------------------------
  _alertsBanner() {
    const out = [];
    const nameOf = (k) => { const id = k.split(":")[1]; return (this.data.devices.find((d) => d.device_id === id) || {}).name || id; };
    for (const a of this.live.alerts || []) {
      const kind = a.split(":")[0];
      out.push(`<div class="banner warn">${esc(nameOf(a))}: ${{ offline: "offline - HomePower cannot read it", error: "a command failed", unconfirmed: "is not following HomePower's commands", protect: "manufacturer protection is active" }[kind] || esc(kind)}</div>`);
    }
    for (const n of this.live.notes || []) out.push(`<div class="banner info">${esc(n)}</div>`);
    if (this.live.backup) out.push(`<div class="banner info">Backup mode is on: batteries are topped up to the backup reserve and not discharged below it.</div>`);
    if (this.live.away) out.push(`<div class="banner info">Away mode is on: only schedules for "away" (or "always") apply.</div>`);
    return out.join("");
  }

  // ---- plan tab: goals, 24h preview, conflicts ----------------------------------
  // Prices for the next 24 hours as bars, with the half-hours HomePower plans to charge in picked out.
  _priceChart() {
    const pv = this._preview, pr = (pv && pv.prices) || [];
    if (pr.length < 2) return "";
    const cur = this.live.currency || "£", W = 800, H = 200;
    const t0 = Date.parse(pr[0].start), t1 = Date.parse(pr[pr.length - 1].end), span = Math.max(1, t1 - t0);
    const nt = this._niceTicks(Math.min(0, ...pr.map((x) => x.price)), Math.max(...pr.map((x) => x.price), 0.01), 4);
    const X = (t) => ((t - t0) / span) * W, Y = (v) => H - ((v - nt.lo) / Math.max(1e-9, nt.hi - nt.lo)) * H;
    const planned = (a, b) => (pv.plan || []).some((x) => Date.parse(x.start) < b && Date.parse(x.end) > a)
      || Object.values(pv.segments || {}).some((l) => l.some((g) => g.mode === "charge" && Date.parse(g.start) < b && Date.parse(g.end) > a));
    const bars = pr.map((x) => {
      const a = Date.parse(x.start), b = Date.parse(x.end), y = Y(Math.max(x.price, 0)), h = Math.abs(Y(x.price) - Y(0)), chg = planned(a, b);
      return `<rect x="${(X(a) + 0.6).toFixed(1)}" y="${Math.min(y, Y(0)).toFixed(1)}" width="${Math.max(1, X(b) - X(a) - 1.2).toFixed(1)}" height="${Math.max(1, h).toFixed(1)}" fill="${chg ? MODES.charge.color : x.cheap ? "#60a5fa" : "var(--disabled-text-color,#9ca3af)"}" opacity="${chg ? 1 : 0.75}"><title>${new Date(a).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}: ${cur}${x.price.toFixed(3)}/kWh${chg ? " · charging planned" : ""}</title></rect>`;
    }).join("");
    const grid = nt.ticks.map((v) => `<line x1="0" x2="${W}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}" stroke="var(--divider-color)" stroke-opacity=".5" stroke-dasharray="3 4" vector-effect="non-scaling-stroke"/>`).join("");
    const now = Date.now(), nowLine = now > t0 && now < t1 ? `<line x1="${X(now).toFixed(1)}" x2="${X(now).toFixed(1)}" y1="0" y2="${H}" stroke="var(--primary-color)" stroke-width="2" vector-effect="non-scaling-stroke"/>` : "";
    const ylab = nt.ticks.map((v) => `<span style="top:${((1 - (v - nt.lo) / Math.max(1e-9, nt.hi - nt.lo)) * 100).toFixed(2)}%">${cur}${v.toFixed(2)}</span>`).join("");
    const xlab = [0, 0.25, 0.5, 0.75, 1].map((f, i) => `<span style="left:${i * 25}%">${this._fmtT(t0 + f * span, span)}</span>`).join("");
    return `<div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:chart-bar"></ha-icon>Prices and planned charging (per kWh)</h2>
      <div class="plot"><div class="plot-y" style="height:190px">${ylab}</div><div class="plot-main"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:190px" role="img" aria-label="Electricity prices for the next 24 hours">${grid}${bars}${nowLine}</svg><div class="plot-x">${xlab}</div></div></div>
      <div class="legend"><span><i class="sw" style="background:${MODES.charge.color}"></i>Charging planned</span><span><i class="sw" style="background:#60a5fa"></i>Cheap rate</span><span><i class="sw" style="background:var(--disabled-text-color,#9ca3af)"></i>Standard rate</span><span>Line = now</span></div></div>`;
  }

  // Is everything set up? Shown on Overview until all is done, and always on Diagnostics.
  _setupCard(always) {
    const items = this.live.setup || [], missing = items.filter((x) => !x.ok);
    if (!items.length || (!always && !missing.length)) return "";
    const btn = (x) => x.ok || !x.action ? "<span></span>" : x.action === "test" ? `<button class="btn sm" data-act="setup-test" data-id="${esc(x.target)}">Test now</button>` : `<button class="btn sm" data-tab="${esc(x.target)}">Fix</button>`;
    const list = (always ? items : missing).map((x) => `<div class="it"><ha-icon class="${x.ok ? "ok" : "no"}" icon="${x.ok ? "mdi:check-circle" : "mdi:alert-circle-outline"}"></ha-icon><div>${esc(x.title)}${x.detail ? `<small>${esc(x.detail)}</small>` : ""}</div>${btn(x)}</div>`).join("");
    return `<div class="card" style="margin-bottom:16px"><h2><ha-icon icon="mdi:clipboard-check-outline"></ha-icon>Setup check <span class="sub" style="font-weight:400;font-size:13px">${items.length - missing.length} of ${items.length} done</span></h2><div class="chk">${list}</div></div>`;
  }

  _planView() {
    const goals = this.data.goals || [];
    const names = (g) => !g.device_ids.length ? "All batteries" : g.device_ids.map((id) => (this.data.devices.find((d) => d.device_id === id) || {}).name || id).join(", ");
    const gcards = goals.map((g) => `<div class="card sch ${g.enabled ? "" : "off"}">
      <label class="switch"><input type="checkbox" data-gtoggle="${esc(g.id)}" ${g.enabled ? "checked" : ""}><i></i></label>
      <div class="meta"><div class="when">${g.target_soc}% by ${g.by}</div><div style="font-weight:600">${esc(g.id)}</div>
        <div class="sub">${esc(names(g))}${g.use_forecast ? " · less when solar is forecast" : ""}${g.max_power_w ? " · max " + fmtW(g.max_power_w) : ""}</div>
        <div class="dayrow">${DAYS.map((d, i) => `<span class="${g.days.includes(i) ? "on" : ""}">${d.slice(0, 2)}</span>`).join("")}</div></div>
      <div><button class="btn sm" data-act="edit-goal" data-id="${esc(g.id)}">Edit</button></div></div>`).join("");
    const pv = this._preview;
    let timeline = `<div class="sub" style="color:var(--secondary-text-color)">Loading…</div>`;
    if (pv) {
      const t0 = Date.now(), span = 24 * 3600e3;
      const rows = this.data.devices.map((d) => {
        const segs = (pv.segments[d.device_id] || []).map((sg) => {
          const a = Math.max(0, (Date.parse(sg.start) - t0) / span), b = Math.min(1, (Date.parse(sg.end) - t0) / span);
          if (b <= a) return "";
          const m = MODES[sg.mode] || MODES.hold;
          return `<div class="seg2" title="${esc(sg.source)}: ${esc(m.label)}${sg.power_w ? " " + fmtW(sg.power_w) : ""}" style="left:${a * 100}%;width:${(b - a) * 100}%;background:${m.color}">${b - a > 0.12 ? esc(sg.source) : ""}</div>`;
        }).join("");
        return `<div class="row"><div class="nm">${esc(d.name)}</div><div class="bar">${segs}<div class="now" style="left:0"></div></div></div>`;
      }).join("");
      const hrs = [0, 6, 12, 18, 24].map((h) => `<span>${new Date(t0 + h * 3600e3).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>`).join("");
      const conflicts = pv.conflicts.map((c) => `<div class="banner warn">Schedules ${c.schedules.map((x) => `"${esc(x)}"`).join(" and ")} clash on ${esc((this.data.devices.find((d) => d.device_id === c.device_id) || {}).name || c.device_id)} from ${new Date(c.start).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })} to ${new Date(c.end).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}. Give one a higher priority.</div>`).join("");
      const plan = pv.plan.length ? `<details><summary>Planned cheap-rate charging (${pv.plan.length} slots)</summary><table class="tbl"><tr><th>Battery</th><th>From</th><th>To</th><th>Power</th><th>Price</th></tr>${pv.plan.map((x) => `<tr><td>${esc((this.data.devices.find((d) => d.device_id === x.device_id) || {}).name || x.device_id)}</td><td>${new Date(x.start).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</td><td>${new Date(x.end).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</td><td>${fmtW(x.power_w)}</td><td>${x.price}</td></tr>`).join("")}</table></details>` : "";
      timeline = `${conflicts}<div class="tl">${rows}<div class="axis"><div></div><div>${hrs}</div></div></div>${plan}
        <div class="legend">${Object.entries(MODES).map(([k, m]) => `<span><i class="sw" style="background:${m.color}"></i>${m.label}</span>`).join("")}<span>Gaps = no instruction (the battery runs itself)</span></div>`;
    }
    return `<div class="bar-actions"><h2>Goals</h2><button class="btn pri" data-act="add-goal"><ha-icon icon="mdi:plus"></ha-icon>Add goal</button></div>
      ${goals.length ? `<div class="grid g-cards">${gcards}</div>` : `<div class="card empty"><ha-icon icon="mdi:target"></ha-icon><h3>No goals yet</h3>"Be at 90% by 07:00": HomePower charges in the cheapest half-hours to get there. It needs prices: set a rates entity or a cheap-rate tariff in Settings.<br><br><button class="btn pri" data-act="add-goal">Add a goal</button></div>`}
      ${this._priceChart()}
      <div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:timeline-clock"></ha-icon>What will happen next (24 hours)</h2>${this._tabError ? `<div class="err">${esc(this._tabError)}</div>` : ""}${timeline}</div>`;
  }

  _goalModal(g, isNew) {
    const devs = this.data.devices, sel = new Set(g.device_ids);
    this._openModal(`<div class="overlay" data-close-bg><div class="modal" role="dialog" aria-label="Goal">
      <h2>${isNew ? "Add goal" : "Edit goal"}</h2><p class="lead">Be at a charge level by a time, as cheaply as possible.</p>
      <div class="f"><label>Name</label><input type="text" id="gl-name" value="${esc(g.id)}" placeholder="e.g. Ready for the morning"></div>
      <h3>Which batteries</h3><div class="checks">${devs.map((d) => `<label><input type="checkbox" name="gl-dev" value="${esc(d.device_id)}" ${sel.has(d.device_id) ? "checked" : ""}>${esc(d.name)}</label>`).join("")}</div>
      <div class="f"><span class="hint">Leave all unticked for every battery. Batteries that cannot charge from the grid are skipped.</span></div>
      <div class="two"><div class="f"><label>Target charge (%)</label><input type="number" id="gl-target" min="1" max="100" value="${g.target_soc}"></div>
        <div class="f"><label>Be there by</label><input type="time" id="gl-by" value="${g.by}"></div></div>
      <div class="daypick">${DAYS.map((d, i) => `<label><input type="checkbox" name="gl-day" value="${i}" ${g.days.includes(i) ? "checked" : ""}><span>${d}</span></label>`).join("")}</div>
      <div class="f"><span class="hint">The day the deadline falls on.</span></div>
      <label style="display:flex;gap:8px;align-items:center;margin:8px 0"><input type="checkbox" id="gl-auto" ${g.auto_target ? "checked" : ""}>Work the target out from my usual use (the target above becomes the maximum; it learns for a few days first)</label>
      <label style="display:flex;gap:8px;align-items:center;margin:8px 0"><input type="checkbox" id="gl-forecast" ${g.use_forecast ? "checked" : ""}>Charge less when tomorrow's solar forecast is good (set the forecast entity in Settings)</label>
      <div class="f"><label>Max total charge power for this goal (W, optional)</label><input type="number" id="gl-max" min="0" step="100" value="${g.max_power_w ?? ""}"></div>
      <div class="err" id="m-err"></div>
      <div class="foot">${isNew ? "<span></span>" : `<button class="btn danger" data-act="del-goal" data-id="${esc(g.id)}">Delete</button>`}
        <div class="r"><button class="btn" data-act="close">Cancel</button><button class="btn pri" data-act="save-goal" data-orig="${isNew ? "" : esc(g.id)}">Save</button></div></div>
    </div></div>`);
  }

  _collectGoal() {
    const r = this.shadowRoot, v = (id) => r.getElementById(id).value;
    return { id: v("gl-name").trim(), device_ids: [...r.querySelectorAll('input[name="gl-dev"]:checked')].map((x) => x.value),
      target_soc: v("gl-target"), by: v("gl-by"), days: [...r.querySelectorAll('input[name="gl-day"]:checked')].map((x) => +x.value),
      use_forecast: r.getElementById("gl-forecast").checked, auto_target: r.getElementById("gl-auto").checked, max_power_w: v("gl-max") === "" ? null : +v("gl-max"), enabled: true };
  }

  // ---- insights tab: savings, energy, long-term history -----------------------------
  async _loadHistory() {
    const ids = this.data.entity_ids || {}, list = [];
    if (ids.battery_power) list.push(ids.battery_power);
    if (ids.grid_power) list.push(ids.grid_power);
    for (const d of this.data.devices) { const e = (ids.devices || {})[d.device_id]; if (e && e.soc) list.push(e.soc); }
    const hours = { "6h": 6, "12h": 12, "24h": 24, "3d": 72, "7d": 168, "30d": 720 }[this._range || "24h"];
    if (!list.length) { this._hist2 = {}; return; }
    const end = new Date(), start = new Date(end - hours * 3600e3);
    this._hist2 = await this._hass.callWS({ type: "recorder/statistics_during_period", start_time: start.toISOString(), end_time: end.toISOString(),
      statistic_ids: list, period: hours > 48 ? "hour" : "5minute", types: ["mean"] });
  }

  _lineChart(series, unit) {
    const all = series.flatMap((s) => s.pts);
    if (all.length < 2) return `<div class="sub" style="color:var(--secondary-text-color)">No history yet. It fills in as Home Assistant records the sensors.</div>`;
    const tmin = Math.min(...all.map((p) => p.t)), tmax = Math.max(...all.map((p) => p.t));
    let nt;
    if (unit === "%") nt = { lo: 0, hi: 100, ticks: [0, 25, 50, 75, 100] };
    else { const m = Math.max(500, ...all.map((p) => Math.abs(p.v))); nt = this._niceTicks(Math.min(0, ...all.map((p) => p.v)) < 0 ? -m : 0, m, 4); }
    const ser = series.map((s) => ({ color: s.color, pts: this._downsample(s.pts) }));
    return `${this._plot(ser, { lo: nt.lo, hi: nt.hi, ticks: nt.ticks, tmin, tmax, fmtY: (v) => (unit === "%" ? `${v}%` : v === 0 ? "0" : fmtW(v)), height: 220, ariaLabel: unit === "%" ? "Charge level history" : "Power history" })}
      <div class="legend">${series.map((s) => `<span><i class="sw" style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")}</div>`;
  }

  _healthCard() {
    const h = (this.live.health || {}), rows = this.data.devices.map((d) => {
      const x = h[d.device_id] || { efficiency: [], warning: null }, e = x.efficiency || [];
      return `<tr><td>${esc(d.name)}</td><td>${e.length ? e.map(([, v]) => v + "%").join(" → ") : "not enough history yet"}</td><td>${x.warning ? `<span style="color:#dc2626">${esc(x.warning)}</span>` : e.length >= 3 ? "Steady" : "–"}</td></tr>`;
    }).join("");
    if (!rows) return "";
    return `<div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:heart-pulse"></ha-icon>Battery health (weekly round-trip efficiency)</h2>
      <table class="tbl"><tr><th>Battery</th><th>Last weeks (oldest → newest)</th><th>Trend</th></tr>${rows}</table>
      <p class="sub" style="font-size:12px;color:var(--secondary-text-color)">Energy out divided by energy in, per full week. HomePower alerts you if a battery drops 10 points below its usual.</p></div>`;
  }

  _insightsView() {
    const L = this.live, E = L.energy || {}, cur = L.currency || "£", ids = this.data.entity_ids || {}, H = this._hist2 || {};
    const pts = (eid) => (H[eid] || []).filter((r) => r.mean != null).map((r) => ({ t: typeof r.start === "number" ? r.start : Date.parse(r.start), v: r.mean }));
    const powerSeries = [];
    if (ids.battery_power) powerSeries.push({ name: "Battery power (+ charging)", color: SERIES[0], pts: pts(ids.battery_power) });
    if (ids.grid_power) powerSeries.push({ name: "Grid (+ importing)", color: SERIES[1], pts: pts(ids.grid_power) });
    const socSeries = this.data.devices.map((d, i) => ({ name: d.name, color: this._color(i), pts: pts(((ids.devices || {})[d.device_id] || {}).soc) }));
    const kwh = (k) => (E[k] || 0).toFixed(1);
    const rows = this.data.devices.map((d) => {
      const cin = E[`bat:${d.device_id}_in`] || 0, out = E[`bat:${d.device_id}_out`] || 0;
      return `<tr><td>${esc(d.name)}</td><td>${kwh(`bat:${d.device_id}_in`)} kWh</td><td>${kwh(`bat:${d.device_id}_out`)} kWh</td><td>${cin >= 1 ? Math.round((out / cin) * 100) + "%" : "–"}</td><td>${(out / Math.max(d.capacity_wh / 1000, 0.1)).toFixed(1)}</td></tr>`;
    }).join("");
    const rng = ["6h", "12h", "24h", "3d", "7d", "30d"].map((r) => `<button class="btn sm ${((this._range || "24h") === r) ? "pri" : ""}" data-act="range" data-r="${r}">${r}</button>`).join(" ");
    const tile = (l, v, sub) => `<div class="card tile"><div class="lbl">${l}</div><div class="val">${v}</div><div class="sub">${sub}</div></div>`;
    return `<div class="bar-actions"><h2>Insights</h2></div>
      <div class="grid g-tiles">
        ${tile("Saved today", `${cur}${(L.savings ? L.savings.today : 0).toFixed(2)}`, "Estimate: avoided import minus grid-charging cost")}
        ${tile("Saved this month", `${cur}${(L.savings ? L.savings.month : 0).toFixed(2)}`, "Needs prices set in Settings")}
        ${tile("Grid imported", `${kwh("grid_in")} kWh`, `Exported ${kwh("grid_out")} kWh`)}
        ${tile("Solar", `${kwh("pv_in")} kWh`, "As reported by your batteries")}
        ${L.daily && L.daily.self_consumption != null ? tile("Solar used at home today", `${L.daily.self_consumption}%`, "Not exported to the grid") : ""}
        ${L.daily && L.daily.cheap_share != null ? tile("Cheap-rate share today", `${L.daily.cheap_share}%`, "Of the grid energy you imported") : ""}
      </div>${this._healthCard()}
      <div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:battery-sync"></ha-icon>Batteries (since HomePower started recording)</h2>
        <table class="tbl"><tr><th>Battery</th><th>Charged</th><th>Discharged</th><th>Round-trip</th><th>Cycles</th></tr>${rows || ""}</table>
        <p class="sub" style="font-size:12px;color:var(--secondary-text-color)">These are also Home Assistant sensors, so you can add them to the Energy dashboard.</p></div>
      <div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:chart-areaspline"></ha-icon>History ${rng}</h2>${this._tabError ? `<div class="err">${esc(this._tabError)}</div>` : ""}
        ${this._lineChart(powerSeries, "W")}<h2 style="margin-top:16px">Charge level</h2>${this._lineChart(socSeries, "%")}</div>`;
  }

  // ---- diagnostics tab -----------------------------------------------------------
  _diagView() {
    const dg = this._diag;
    if (!dg) return `<div class="bar-actions"><h2>Diagnostics</h2></div><div class="card">Loading…</div>`;
    const rows = Object.entries(dg.devices).map(([id, x]) => `<div class="card" style="margin-bottom:12px"><h2>${esc(x.name)} <span class="chip ${x.online ? "" : "bad"}">${x.online ? "Online" : "Offline"}</span> <span class="chip">${esc(x.driver || "")}</span> ${x.control_enabled ? "" : '<span class="chip warn">Monitor only</span>'}</h2>
      <table class="tbl"><tr><th>Connection</th><td>${esc(Object.entries(x.connection || {}).filter(([k]) => k !== "map").map(([k, v]) => `${k}=${v}`).join("  "))}</td></tr>
      <tr><th>Last good read</th><td>${x.last_read_ok ? new Date(x.last_read_ok).toLocaleString() : "never"}</td></tr>
      <tr><th>Battery reports</th><td>${esc(x.actual_mode || "?")}${x.word_order ? ` · 32-bit word order: ${esc(x.word_order)}` : ""}</td></tr>
      <tr><th>HomePower wants</th><td>${x.desired ? esc(x.desired.mode) + (x.desired.power_w ? " " + fmtW(x.desired.power_w) : "") : "nothing (battery runs itself)"} ${x.reason ? "· " + esc(x.reason) : ""}</td></tr>
      <tr><th>Last command</th><td>${x.last_command ? `${esc(x.last_command.mode)} ${x.last_command.power_w ? fmtW(x.last_command.power_w) : ""} at ${new Date(x.last_command.at).toLocaleTimeString()} - ${esc(x.last_command.result)}` : "none yet"}</td></tr>
      <tr><th>Problems</th><td>${x.last_error ? `<span class="bad">${esc(x.last_error)}</span>` : "none"}${x.unconfirmed ? ` · ${x.unconfirmed} unconfirmed` : ""}${x.release_pending ? " · holding control" : ""}</td></tr></table>
      <div style="margin-top:10px"><button class="btn sm" data-act="test-control" data-id="${esc(id)}"><ha-icon icon="mdi:flask-outline" style="--mdc-icon-size:16px"></ha-icon>Test control</button></div>
      <div class="steps" id="test-${esc(id)}" style="margin-top:8px">${this._tests && this._tests[id] ? this._tests[id] : ""}</div></div>`).join("");
    const ev = (dg.events || []).slice().reverse().slice(0, 40).map(([t, id, m]) => `<div><span class="t">${esc(new Date(t).toLocaleTimeString())}</span> <b>${esc(id)}</b> ${esc(m)}</div>`).join("");
    return `<div class="bar-actions"><h2>Diagnostics</h2><button class="btn" data-act="copy-debug"><ha-icon icon="mdi:content-copy" style="--mdc-icon-size:16px"></ha-icon>Copy debug info</button></div>
      ${(dg.alerts || []).length ? `<div class="banner warn">Active alerts: ${esc(dg.alerts.join(", "))}</div>` : ""}${this._setupCard(true)}${rows || '<div class="card empty">No batteries yet.</div>'}
      <div class="card"><h2>Recent activity</h2><div class="log">${ev || "Nothing yet."}</div></div>`;
  }

  async _runTest(id) {
    const el = this.shadowRoot.getElementById("test-" + id);
    if (el) el.innerHTML = `<div class="sub">Testing… this takes about 10 seconds. HomePower sends a tiny safe command, checks it, and hands the battery back.</div>`;
    let html;
    try {
      const r = await this._hass.callWS({ type: "homepower/device/test", device_id: id });
      html = `<div><b class="${r.ok ? "ok" : "bad"}">${r.ok ? "Passed: the battery follows HomePower's commands." : "Failed"}</b></div>` + r.steps.map((x) => `<div class="${x.ok ? "ok" : "bad"}">${x.ok ? "✔" : "✘"} ${esc(x.name)}${x.detail ? ` <span class="sub" style="color:var(--secondary-text-color)">- ${esc(x.detail)}</span>` : ""}</div>`).join("");
    } catch (e) { html = `<div class="bad">${esc(e.message || e)}</div>`; }
    (this._tests ||= {})[id] = html;
    const el2 = this.shadowRoot.getElementById("test-" + id);
    if (el2) el2.innerHTML = html;
  }

  // ---- settings tab (data driven) ---------------------------------------------------
  _settingsView() {
    const s = this.data.settings || {};
    const num = (k, label, hint, step = 1) => ({ k, t: "num", label, hint, step });
    const SEC = [
      ["Grid meter", "mdi:transmission-tower", [
        { k: "grid_import_entity", t: "entity", dom: ["sensor"], label: "Smart meter import sensor - optional", hint: (this.live.meter_entity && !s.grid_import_entity ? `Using ${this.live.meter_entity} automatically. ` : "") + "Power (W/kW) or a cumulative energy counter (kWh). Leave empty to use Hildebrand Glow (if set up) or the grid reading your batteries report." },
        { k: "grid_export_entity", t: "entity", dom: ["sensor"], label: "Smart meter export sensor - optional", hint: "Subtracted from import. Power or energy counter." },
        { k: "grid_invert", t: "bool", label: "Sensor is positive when exporting" }]],
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
        { k: "offpeak_auto", t: "bool", label: "Work out the night/cheap period from the tariff's price history" + (this.live.tariff_inferred ? ` (estimated ${this.live.tariff_inferred.start}-${this.live.tariff_inferred.end} from ${this.live.tariff_inferred.days} days of readings, which can run up to an hour late: type your exact times below to be sure)` : "") },
        { k: "offpeak_start", t: "time", label: "Cheap rate starts", hint: "Leave both times empty to use the period found from history." }, { k: "offpeak_end", t: "time", label: "Cheap rate ends" },
        { k: "offpeak_extra", t: "text", label: "More cheap periods (optional)", hint: "For tariffs with several cheap periods a day, e.g. 13:00-16:00, 22:00-00:00" },
        { k: "save_for_peak", t: "bool", label: "Save the battery for peak hours: don't let it discharge during cheap-rate periods" },
        { k: "currency", t: "text", label: "Currency symbol" }]],
      ["Solar forecast", "mdi:weather-sunny", [
        { k: "forecast_entity", t: "entity", dom: ["sensor"], label: "Tomorrow's solar forecast (kWh)", hint: "Solcast or Forecast.Solar 'tomorrow' sensor." },
        num("forecast_share", "Share of forecast solar that reaches the battery (0-1)", "", "0.05")]],
      ["Backup & away", "mdi:shield-home", [
        num("backup_reserve_pct", "Backup reserve (%)", "Backup mode tops up to this and never discharges below it."),
        { k: "storm_entity", t: "entity", dom: ["binary_sensor", "input_boolean", "switch"], label: "Turn backup mode on automatically while this is on (e.g. a weather alert)" },
        { k: "away_entity", t: "entity", dom: ["binary_sensor", "input_boolean", "person", "device_tracker"], label: "Away entity (on / away / not_home turns away mode on)" }]],
      ["Protect loads", "mdi:ev-station", [{ k: "load_rules", t: "rules", label: "Don't charge/discharge the battery while these are busy (EV charger, heat pump...)" }]],
      ["Alerts & heads-ups", "mdi:bell", [{ k: "notify_service", t: "text", label: "Also send alerts to (e.g. notify.mobile_app_phone)", hint: "Alerts always appear as Home Assistant notifications." },
        { k: "notify_full", t: "bool", label: "Tell me when the home battery is full" },
        num("notify_low_pct", "Tell me when the charge falls to (%, 0 = never)"),
        { k: "notify_cheap_soon", t: "bool", label: "Warn me 30 minutes before the cheap rate starts if the battery is below..." },
        num("cheap_soon_below_pct", "...this charge (%)"),
        { k: "daily_summary_time", t: "time", label: "Send a daily summary at", hint: "Battery level, yesterday's charging, savings and solar use. Leave empty for none." }]],
      ["Solar surplus", "mdi:water-boiler", [
        { k: "divert_entity", t: "entity", dom: ["switch", "input_boolean"], label: "Switch this on when the batteries are full and you are exporting", hint: (this.live.divert ? `Currently ${this.live.divert.on ? "ON" : "off"}. ` : "") + "For example an immersion heater. Switched off again when it would need grid power. Leave empty for none." },
        num("divert_export_w", "Export needed to switch it on (W)", "Roughly the power the device uses."),
        num("divert_min_soc", "Only when the home battery is at least (%)")]],
      ["EcoFlow", "mdi:flash", [{ k: "ecoflow_live", t: "bool", label: "Use EcoFlow's live feed for faster readings (seconds instead of 30 s)" },
        { k: "ecoflow_live_note", t: "note", label: "Off by default: it opens a second connection to EcoFlow with your keys, which could disturb another EcoFlow integration using the same account (such as EcoFlow Cloud). If readings stop updating after turning it on, turn it off again." }]],
      ["Backup & restore", "mdi:backup-restore", [{ k: "backup_restore", t: "backup", label: "" }]],
    ];
    const field = (f) => {
      const v = s[f.k] ?? "";
      if (f.t === "bool") return `<label style="display:flex;gap:8px;align-items:center;margin:8px 0"><input type="checkbox" id="st-${f.k}" ${s[f.k] ? "checked" : ""}>${esc(f.label)}</label>`;
      if (f.t === "select") return `<div class="f"><label>${esc(f.label)}</label><select id="st-${f.k}">${f.options.map(([val, l]) => `<option value="${val}" ${(v || "auto") === val ? "selected" : ""}>${l}</option>`).join("")}</select></div>`;
      if (f.t === "device") return `<div class="f"><label>${esc(f.label)}</label><select id="st-${f.k}"><option value="">None</option>${this.data.devices.map((d) => `<option value="${esc(d.device_id)}" ${v === d.device_id ? "selected" : ""}>${esc(d.name)}</option>`).join("")}</select></div>`;
      if (f.t === "note") return `<p class="hint" style="margin:0 0 8px">${esc(f.label)}</p>`;
      if (f.t === "presets") return `<div class="presets">${[["Octopus Go", "00:30", "05:30", ""], ["Intelligent Octopus Go", "23:30", "05:30", ""], ["Economy 7 (check your bill)", "00:30", "07:30", ""], ["23:30 - 07:30", "23:30", "07:30", ""], ["Cosy Octopus (3 periods)", "04:00", "07:00", "13:00-16:00, 22:00-00:00"]]
        .map(([n, a, b, x]) => `<button class="btn sm" data-act="tariff-preset" data-a="${a}" data-b="${b}" data-x="${x}">${esc(n)}${x ? "" : ` ${a}-${b}`}</button>`).join("")}</div><span class="hint">Fills the two time boxes below. Prices differ by tariff, so also enter the standard and cheap rate (or a rates sensor).</span>`;
      if (f.t === "backup") return `<p class="hint" style="margin-top:0">Save everything (batteries, schedules, goals, scenes and settings) to a file, or restore it later, for example after rebuilding Home Assistant.</p>
        <label style="display:flex;gap:8px;align-items:center;margin:6px 0"><input type="checkbox" id="bk-secrets">Include EcoFlow access keys in the file (keep the file private)</label>
        <div class="presets"><button class="btn sm" data-act="do-export"><ha-icon icon="mdi:download"></ha-icon>Download backup</button></div>
        <div class="f"><label>Restore from a backup file</label><input type="file" id="imp-file" accept=".json,application/json"></div>
        <div class="presets"><button class="btn sm" data-act="do-import"><ha-icon icon="mdi:upload"></ha-icon>Restore</button></div><div class="err" id="bk-msg"></div>`;
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
  }

  _ruleRow(r) {
    return `<div class="rule"><div class="f"><label>Entity</label><input type="text" class="r-ent" list="dl-rule" value="${esc(r.entity || "")}" placeholder="sensor.ev_charger_power"><datalist id="dl-rule">${this._entityOptions(["sensor", "binary_sensor", "switch", "input_boolean"])}</datalist></div>
      <div class="f"><label>Busy above (W, blank = on/off)</label><input type="number" class="r-above" value="${r.above_w ?? ""}"></div>
      <div class="f"><label>Block</label><select class="r-block">${["discharge", "charge", "both"].map((b) => `<option ${r.block === b ? "selected" : ""}>${b}</option>`).join("")}</select></div>
      <button class="btn sm" data-act="del-rule">✕</button></div>`;
  }

  _collectSettings() {
    const r = this.shadowRoot, out = {};
    for (const f of this._settingKeys || []) {
      if (f.t === "rules") {
        out[f.k] = [...r.querySelectorAll("#st-rules .rule")].map((row) => ({ entity: row.querySelector(".r-ent").value.trim(), above_w: row.querySelector(".r-above").value, block: row.querySelector(".r-block").value })).filter((x) => x.entity);
        continue;
      }
      const el = r.getElementById("st-" + f.k);
      if (!el) continue;
      out[f.k] = f.t === "bool" ? el.checked : el.value.trim ? el.value.trim() : el.value;
    }
    return out;
  }


  // ---- events --------------------------------------------------------------
  _openModal(html) { this._modal = true; this.shadowRoot.getElementById("modal").innerHTML = html; }
  _closeModal() { this._modal = null; this.shadowRoot.getElementById("modal").innerHTML = ""; this._render(); }
  _err(m) { const e = this.shadowRoot.getElementById("m-err"); if (e) e.textContent = m; }

  _blankSchedule(tpl) {
    const base = { id: "", device_ids: [], all_day: false, condition_entity: null, start: "23:30", end: "07:30", mode: "charge", power_w: 3000, target_soc: 100, reserve_soc: null, days: [0, 1, 2, 3, 4, 5, 6], priority: 0, season_start: null, season_end: null, on_date: null, enabled: true };
    if (tpl === "cheap") return { ...base, id: "Cheap-rate overnight charge" };
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
        if (!(await this._call({ type: "homepower/override", device_ids: ids, mode, power_w: moving ? +r.getElementById("o-pw").value : null,
              target_soc: charging ? +r.getElementById("o-soc").value : null, minutes: +r.getElementById("o-min").value }))) return;
        return this._closeModal();
      }
      case "pause": {
        const next = !this.data.paused;
        if (next && !confirm("Pause HomePower? It will stop sending commands. Batteries stay in whatever mode they were last put in.")) return;
        if (await this._call({ type: "homepower/pause", paused: next }, (m) => alert(m))) { this.data.paused = next; this._render(); }
        return;
      }
      case "add-schedule": return this._scheduleModal(this._blankSchedule(t.dataset.tpl), true);
      case "edit-schedule": return this._scheduleModal(this.data.schedules.find((s) => s.id === id), false);
      case "save-schedule": {
        const s = this._collectSchedule(), orig = t.dataset.orig;
        if (!s.id) return this._err("Give the schedule a name.");
        if (!orig && this.data.schedules.some((x) => x.id === s.id)) return this._err("A schedule with that name already exists.");
        if (orig) s.enabled = (this.data.schedules.find((x) => x.id === orig) || {}).enabled !== false;
        if (!(await this._call({ type: "homepower/schedule/save", schedule: s }))) return;
        if (orig && orig !== s.id) await this._call({ type: "homepower/schedule/delete", schedule_id: orig });
        await this._reload(); return this._closeModal();
      }
      case "del-schedule":
        if (!confirm(`Delete schedule "${id}"?`)) return;
        await this._call({ type: "homepower/schedule/delete", schedule_id: id });
        await this._reload(); return this._closeModal();
      case "add-battery": {
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
      case "toggle-control": {
        const on = t.dataset.on === "1";
        if (on && !confirm("Allow HomePower to send charge/discharge commands to this battery?")) return;
        if (await this._call({ type: "homepower/device/control", device_id: id, enabled: on }, (m) => alert(m))) await this._reload();
        return;
      }
      case "edit-battery": this._editingId = id; return this._batteryModal(dev(), false);
      case "save-battery": {
        const d = this._collectBattery(t.dataset.id);
        if (!d.name) return this._err("Give the battery a name.");
        this._editingId = null;
        if (!t.dataset.id && this.data.devices.some((x) => x.device_id === d.device_id)) return this._err("A battery with that name already exists.");
        if (!(await this._call({ type: "homepower/device/save", device: d }))) return;
        await this._reload(); return this._closeModal();
      }
      case "del-battery":
        if (!confirm("Remove this battery from HomePower? (The device itself is untouched.)")) return;
        await this._call({ type: "homepower/device/delete", device_id: id });
        await this._reload(); return this._closeModal();
      case "save-settings": {
        const msg = this.shadowRoot.getElementById("g-msg");
        const ok = await this._call({ type: "homepower/settings/save", settings: this._collectSettings() }, (m) => { msg.style.color = ""; msg.textContent = m; });
        if (ok) {
          this._savedNote = true;   // survives the re-render that follows
          await this._reload();
          setTimeout(() => { this._savedNote = false; if (this.tab === "settings" && !this._modal) this._render(); }, 3000);
        }
        return;
      }
      case "add-rule": { this.shadowRoot.getElementById("st-rules").insertAdjacentHTML("beforeend", this._ruleRow({ block: "discharge" })); return; }
      case "del-rule": { t.closest(".rule").remove(); return; }
      case "flag":
        if (await this._call({ type: "homepower/flag", flag: t.dataset.flag, enabled: t.dataset.on === "1" }, (m) => alert(m))) { this.live[t.dataset.flag] = t.dataset.on === "1"; this._render(); }
        return;
      case "add-goal": return this._goalModal({ id: "", device_ids: [], target_soc: 90, by: "07:00", days: [0, 1, 2, 3, 4, 5, 6], use_forecast: false, auto_target: false, max_power_w: null, enabled: true }, true);
      case "edit-goal": return this._goalModal((this.data.goals || []).find((x) => x.id === id), false);
      case "save-goal": {
        const g = this._collectGoal(), orig = t.dataset.orig;
        if (!g.id) return this._err("Give the goal a name.");
        if (!orig && (this.data.goals || []).some((x) => x.id === g.id)) return this._err("A goal with that name already exists.");
        if (orig) g.enabled = ((this.data.goals || []).find((x) => x.id === orig) || {}).enabled !== false;
        if (!(await this._call({ type: "homepower/goal/save", goal: g }))) return;
        if (orig && orig !== g.id) await this._call({ type: "homepower/goal/delete", goal_id: orig });
        await this._reload(); this._closeModal(); return this._tabLoad();
      }
      case "del-goal":
        if (!confirm(`Delete goal "${id}"?`)) return;
        await this._call({ type: "homepower/goal/delete", goal_id: id });
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
        if (!orig && (this.data.scenes || []).some((x) => x.name === sc.name)) return this._err("A scene with that name already exists.");
        if (!(await this._call({ type: "homepower/scene/save", scene: sc }, (m) => this._err(m)))) return;
        if (orig && orig !== sc.name) await this._call({ type: "homepower/scene/delete", name: orig });
        await this._reload(); return this._closeModal();
      }
      case "del-scene":
        if (!confirm(`Delete scene "${id}"?`)) return;
        await this._call({ type: "homepower/scene/delete", name: id });
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
        const txt = JSON.stringify(this._diag, null, 2);
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
    if (t.name === "s-mode" || t.id === "s-allday") this._syncSchedForm();
    if (t.id === "b-kind") this._renderProfile(t.value, {}, false);
    if (t.id === "b-devsel" && t.value) this._autofill(t.value);
    if (t.id === "b-control" && t.checked && !confirm("Allow HomePower to send charge/discharge commands to this battery?")) t.checked = false;
    if (t.id && t.id.startsWith("e-")) this._fillModeSelects();
    if (t.name === "s-dev") this._syncSchedForm();
  }
  _input(e) { if (e.target.id === "s-pw") this._syncSchedForm(); }
}

customElements.define("homepower-panel", HomePowerPanel);
