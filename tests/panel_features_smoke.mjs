// Renders the newer panel pieces with fake data and checks they produce sensible HTML (run: node tests/panel_features_smoke.mjs)
const els = {};
globalThis.HTMLElement = class { attachShadow(){ this.shadowRoot = { innerHTML:"", addEventListener(){}, getElementById:(i)=>(els[i] ||= { innerHTML:"", style:{}, value:"", checked:false, textContent:"" }), querySelector(){return {value:"charge"}}, querySelectorAll(){return []} }; } };
globalThis.localStorage = { getItem:()=>null, setItem(){} };
let cls; globalThis.customElements = { define:(n,c)=>cls=c };
await import("../custom_components/homepower/frontend/homepower-panel.js");
const p = new cls();
const dev = (id, n) => ({ device_id: id, name: n, supported_modes: ["auto", "hold"], max_charge_w: 3000, max_discharge_w: 3000, capacity_wh: 10000, min_soc: 10, entities: {} });
p.data = { devices: [dev("a", "A"), dev("b", "B")], schedules: [], goals: [], settings: {}, scenes: [{ name: "Holiday", backup: false, away: true, paused: false, settings: {} }, { name: "Normal", backup: false, away: false, paused: false, settings: {} }],
  catalogue: { profiles: [], strategies: {}, vendors: {} }, entity_ids: {} };
p.live = { devices: { a: { online: true, soc: 60, power_w: -500 }, b: { online: true, soc: 40, power_w: 0 } }, events: [], backup: false, away: true,
  combined: { online: 2, total: 2, soc: 52, stored_kwh: 10.4, usable_kwh: 8.4, capacity_kwh: 20, flow_w: -500, load_w: 800, runtime_h: 16.8, full_in_h: null },
  daily: { self_consumption: 82.5, cheap_share: 64 }, health: { a: { efficiency: [["w1", 90], ["w2", 88], ["w3", 71]], warning: "round-trip efficiency fell to 71%" }, b: { efficiency: [], warning: null } },
  energy: {}, tariff_inferred: { start: "00:00", end: "08:00", days: 5 } };
p._hass = { states: {} };
const must = (name, ok) => { console.log(ok ? "ok  " : "FAIL", name); if (!ok) process.exitCode = 1; };
const hb = p._homeBattery();
must("home battery card", hb.includes("Home battery") && hb.includes("52%") && hb.includes("16.8 h"));
must("tariff banner (estimated)", p._tariffBanner().includes("estimated") && p._tariffBanner().includes("goto-tariff"));
p.data.settings = {}; const keep = p.live.tariff_inferred; p.live.tariff_inferred = null; p.live.price_now = 0.2;
must("no banner for per-slot prices", p._tariffBanner() === "");
p.live.tariff_inferred = keep; p.live.price_now = null;
p.data.settings = { offpeak_start: "23:30", offpeak_end: "07:30" };
must("tariff banner hidden once set", p._tariffBanner() === "");
const sc = p._scenesCard();
must("scenes card", sc.includes("Holiday") && sc.includes('data-act="add-scene"') && sc.includes("btn sm pri"));
p._sceneModal({ name: "", backup: false, away: false, paused: false, settings: {} }, true);
must("scene modal", els.modal.innerHTML.includes("sc-import_limit_w") && els.modal.innerHTML.includes("save-scene"));
els["sc-name"] = { value: " X " }; for (const k of ["backup", "away", "paused"]) els["sc-" + k] = { checked: k === "away" };
for (const k of ["import_limit_w", "export_limit_w", "total_charge_limit_w", "total_discharge_limit_w"]) els["sc-" + k] = { value: k === "import_limit_w" ? "5000" : "" };
const c = p._collectScene(); must("scene collect", c.name === "X" && c.away && c.settings.import_limit_w === 5000 && Object.keys(c.settings).length === 1);
const st = p._settingsView();
must("settings: presets, notifications, backup", st.includes("tariff-preset") && st.includes("notify_cheap_soon") && st.includes("do-export") && st.includes("do-import"));
const ins = p._insightsView();
must("insights: health + daily", ins.includes("71%") && ins.includes("fell to 71%") && ins.includes("82.5%") && ins.includes("64%"));
p._goalModal({ id: "", device_ids: [], target_soc: 90, by: "07:30", days: [0,1,2,3,4,5,6], use_forecast: false, auto_target: true, max_power_w: null, enabled: true }, true);
must("goal modal has auto target", els.modal.innerHTML.includes('id="gl-auto" checked'));
const ov = p._overview();
must("overview assembles", ov.includes("Home battery") && ov.includes("Scenes") && ov.includes("Power, last"));

// ---- setup check, price chart, battery role, new settings
p.live.setup = [{ key: "batteries", ok: true, title: "At least one battery added", detail: "", action: "tab", target: "batteries" },
  { key: "tested:a", ok: false, title: "A: control tested", detail: "Run Test control once.", action: "test", target: "a" },
  { key: "tariff", ok: false, title: "Cheap-rate hours confirmed", detail: "Enter them.", action: "tab", target: "settings" }];
const su = p._setupCard(false);
must("setup card lists only what is missing", su.includes("1 of 3 done") && su.includes("setup-test") && su.includes('data-tab="settings"') && !su.includes("At least one battery"));
must("setup card (full) lists everything", p._setupCard(true).includes("At least one battery"));
p.live.setup = p.live.setup.map((i) => ({ ...i, ok: true }));
must("setup card hides when all done", p._setupCard(false) === "" && p._setupCard(true) !== "");
const t0 = Date.now() - 600e3, slot = (i, price, cheap) => ({ start: new Date(t0 + i * 1800e3).toISOString(), end: new Date(t0 + (i + 1) * 1800e3).toISOString(), price, cheap });
p._preview = { segments: { a: [] }, conflicts: [], notes: [], plan: [{ device_id: "a", start: new Date(t0 + 2 * 1800e3).toISOString(), end: new Date(t0 + 3 * 1800e3).toISOString(), power_w: 2000, price: 0.07 }],
  prices: Array.from({ length: 48 }, (_, i) => slot(i, i < 16 ? 0.07 : 0.29, i < 16)) };
const pc = p._priceChart();
must("price chart: 48 bars, 1 planned, axes", (pc.match(/<rect/g) || []).length === 48 && (pc.match(/charging planned<\/title>/g) || []).length === 1 && pc.includes("£0.30") && pc.includes("plot-x"));
p._preview.prices = []; must("price chart hidden without prices", p._priceChart() === "");
const st2 = p._settingsView();
must("settings: more windows, save for peak, summary, diverter, Cosy", ["st-offpeak_extra", "st-save_for_peak", "st-daily_summary_time", "st-divert_entity", "Cosy Octopus"].every((k) => st2.includes(k)));

// ---- charge source on schedules
p._scheduleModal({ ...p._blankSchedule("night"), source: "solar" }, true);
must("schedule modal offers the charge source", els.modal.innerHTML.includes('id="s-source"') && els.modal.innerHTML.includes('value="solar" selected'));
p.data.schedules = [{ id: "Sun", device_ids: [], start: "10:00", end: "15:00", mode: "charge", power_w: 2000, source: "solar", days: [0,1,2,3,4,5,6], enabled: true, priority: 0 }];
must("schedule card says solar only", p._schedules().includes("solar only"));

// ---- solar-aware goals, panels on a battery, export price
p._goalModal({ id: "g", device_ids: [], target_soc: 90, by: "16:00", days: [0,1,2,3,4,5,6], use_forecast: false, auto_target: false, source: "both", max_power_w: null, enabled: true }, false);
must("goal modal offers the charge source", els.modal.innerHTML.includes('id="gl-source"') && els.modal.innerHTML.includes('value="both" selected'));
p.data.goals = [{ id: "g", device_ids: [], target_soc: 90, by: "16:00", days: [0], use_forecast: false, source: "solar", enabled: true }];
must("goal card says solar only", p._planView().includes("solar only"));
p.live.devices.a = { online: true, soc: 60, power_w: 500, pv_w: 820, has_solar: true };
must("battery card shows its solar", p._overview().includes("Solar 820 W"));
must("settings has the export price", p._settingsView().includes("st-export_price"));

// ---- configurable grid reading order
p.data.settings = { grid_order: "glow,sensor" }; p.live.grid_source = "sensor.glow_power";
const go = p._settingsView();
must("grid order list: saved order first, unused ones unticked", go.indexOf('data-key="glow"') < go.indexOf('data-key="sensor"') && go.indexOf('data-key="sensor"') < go.indexOf('data-key="battery"')
  && /data-key="glow"><input type="checkbox" class="o-on" checked/.test(go) && /data-key="battery"><input type="checkbox" class="o-on" {2}aria/.test(go) && go.includes("Using now: sensor.glow_power"));
const mkRow = (k, on) => ({ dataset: { key: k }, querySelector: () => ({ checked: on }) });
els["st-grid_order"] = { querySelectorAll: () => [mkRow("battery", true), mkRow("glow", false), mkRow("sensor", true)] };
must("grid order collects ticked rows in order", p._collectSettings().grid_order === "battery,sensor");

// ---- EcoFlow report, plan versus actual, weather, why-idle chips, safe mode
p.live.devices.a = { online: true, soc: 40, power_w: 1200, mode: "charge", desired_mode: "charge", why: "Held by the software charge limit.", awaiting_confirm: true,
  charge_verdict: "unreliable", measured_charge_w: 2700, traces: [{ at: "2026-03-01T10:00:00", mode: "charge", ok: false, peak_w: 0 }], setting_changes: [{ at: "t", key: "workMode", from: "A", to: "B" }],
  report: { sent: 6, worked: 5, avg_reaction_s: 40, charge_rate_w: 2700, follows_plan_pct: 88, verdict: "works", advice: "Control looks reliable." }, cycling: null };
const rows = [];
for (let i = 0; i < 48; i++) rows.push([new Date(Date.UTC(2026, 2, 1, 0, 30 * i)).toISOString(), i % 3 ? 1500 : null, i % 2 ? 1400 : -200]);
p.live.plan_actual = { a: { rows, match_pct: 87 }, b: { rows: [], match_pct: null } };
p.live.health = { a: { efficiency: [["w1", 90]], warning: null, cycling: { net_per_kwh: 0.12, worth_it: true, efficiency_pct: 85, measured: false } } };
p.live.weather = { status: "raising the overnight target", tomorrow_c: -2, source: "weather entity (daily)", lately_c: 6, k_wh_per_degree: 800, days_learned: 14, extra_wh: 2100 };
p.live.startup = "Started. Restored 2 learned charge rates.";
p.data.safe = true; p.data.paused = true;
const card = p._ecoflowCard();
must("ecoflow report card", card.includes("5 of 6") && card.includes("40 s") && card.includes("Control looks reliable") && card.includes("88%"));
const plan = p._planCard();
must("plan versus actual card", plan.includes("Plan versus actual") && plan.includes("Followed the plan 87%") && plan.includes("stroke-dasharray") && !plan.includes("NaN"));
must("weather text", p._weatherText().includes("-2 °C") && p._weatherText().includes("0.80 kWh") && p._weatherText().includes("raising"));
const ovw = p._overview();
must("battery chips: why idle, measured rate, confirm button, charge not working, changes",
  ovw.includes("Why idle") && ovw.includes("measured") && ovw.includes('data-act="confirm-first"') && ovw.includes("Charge not working") && ovw.includes("Settings changed (1)"));
must("health card shows cycling", p._healthCard().includes("Worth cycling") && p._healthCard().includes("Yes: about"));
must("settings has the cold weather group", p._settingsView().includes("weather_boost") && p._settingsView().includes("Open-Meteo"));
p._diag = { alerts: [], devices: {}, decisions: [], events: [] };
const dv = p._diagView();
must("diagnostics: download button and startup note", dv.includes('data-act="download-debug"') && dv.includes("Restored 2 learned charge rates"));

// ---- find local control ports
els["ps-host"] = { value: "192.168.1.50" };
const row = (port, service, state, banner = "") => ({ port, service, state, banner });
const scans = [
  { host: "192.168.1.50", address: "192.168.1.50", results: [row(502, "Modbus TCP", "closed"), row(1883, "MQTT", "closed"), row(80, "HTTP", "no answer")] },
  { host: "192.168.1.50", address: "192.168.1.50", results: [row(502, "Modbus TCP", "open"), row(1883, "MQTT", "closed"), row(80, "HTTP", "open", "nginx <b>x</b>")] },
];
let scanN = 0; const sent = []; p._render = () => {};      // this fake page cannot re-render; the card is built straight from the stored state
p._hass.callWS = async (m) => { sent.push(m); if (m.type === "homepower/scan_ports") return scans[scanN++]; return {}; };
must("port scan card is on the diagnostics tab", p._portScanCard().includes("Find local control ports") && p._portScanCard().includes('data-act="scan-ports"') && p._portScanCard().includes('id="ps-host"'));
await p._scanPorts();
must("first scan: nothing listening, one request with the typed address", p._portScanCard().includes("Nothing is listening") && sent.at(-1).host === "192.168.1.50" && !p._portScanCard().includes("Changed since"));
await p._scanPorts();
const after = p._portScanCard();
must("second scan: shows the open ports and what changed", after.includes("2 open: 502 (Modbus TCP), 80 (HTTP)") && after.includes("502 is now open") && after.includes("OPEN"));
must("a banner is escaped, never run as HTML", after.includes("nginx &lt;b&gt;x&lt;/b&gt;") && !after.includes("<b>x</b>"));
p._hass.callWS = async () => { throw new Error("this only scans devices on your own network"); };
await p._scanPorts();
must("a refused address shows the reason", p._portScanCard().includes("only scans devices on your own network"));
els["ps-host"] = { value: "   " };
await p._scanPorts();
must("an empty address asks for one and sends nothing", p._portScanCard().includes("Type the device&#39;s IP address") && sent.length === 2);

// ---- wide scan
els["ps-host"] = { value: "10.0.0.213" };
const wideReply = (open, counts, complete = true) => ({ host: "10.0.0.213", address: "10.0.0.213", wide: true, total: 10000, complete, counts: { open: open.length, closed: 9990, "no answer": 0, skipped: 0, ...counts }, results: open.map((x) => row(...x, "open")) });
const wides = [wideReply([]), wideReply([[8883, "MQTT over TLS"], [4321, ""]]), wideReply([[4321, ""]], { skipped: 300, closed: 9600 }, false)];
let widN = 0; sent.length = 0;
p._hass.callWS = async (m) => { sent.push(m); return wides[widN++]; };
must("wide scan button is on the card", p._portScanCard().includes('data-act="scan-ports-wide"') && p._portScanCard().includes("Wide scan (1-10,000)"));
await p._scanPorts(true);
must("wide scan sends wide=true and says no open port", sent[0].wide === true && p._portScanCard().includes("No open port found") && p._portScanCard().includes("Checked ports 1 to 10,000"));
await p._scanPorts(true);
const w2 = p._portScanCard();
must("second wide scan lists open ports and what changed", w2.includes("2 open") && w2.includes("4321 is now open") && w2.includes("8883 (MQTT over TLS)") && w2.includes("wide scan"));
await p._scanPorts(true);
const w3 = p._portScanCard();
must("an incomplete wide scan says so and shows a port that closed", w3.includes("Incomplete") && w3.includes("300 ports were not checked") && w3.includes("8883 is no longer open"));
p._hass.callWS = async (m) => ({ host: "10.0.0.213", address: "10.0.0.213", wide: false, results: [row(502, "Modbus TCP", "closed")] });
await p._scanPorts(false);
must("a normal scan after wide ones does not compare against a wide scan", !p._portScanCard().includes("Changed since") && !p._portScanCard().includes("No change since"));

// ---- alert banners: every kind the coordinator raises reads as a sentence
p.live.alerts = ["offline:a", "alarm:a:Meter 1 communication failure", "stuck:b", "tariffdrift", "gridsign:b"];
const ab = p._alertsBanner();
must("alert banners name the battery and say what is wrong", ab.includes("A: offline") && ab.includes("A: reports: Meter 1 communication failure")
  && ab.includes("B: was told to charge") && ab.includes("cheap-rate hours no longer match") && ab.includes("B: its grid reading") && !ab.includes(": tariffdrift") && !ab.includes("undefined"));
p.live.alerts = [];

// ---- a battery edit whose save fails keeps testing that battery (its saved secret and open connection), not a new one
{
  const btn = (act, ds = {}) => ({ target: { closest: (sel) => (sel === "[data-close-bg]" ? null : { dataset: { act, ...ds } }) } });
  const saved = p._collectBattery; p._collectBattery = (id) => ({ device_id: id || "new", name: "A", kind: "custom", connection: {} });
  const calls = []; p._hass.callWS = async (m) => { calls.push(m); if (m.type === "homepower/device/save") throw new Error("capacity is required"); return {}; };
  p._batteryModal = () => {}; await p._click(btn("edit-battery", { id: "a" }));
  await p._click(btn("save-battery", { id: "a" }));
  const tb = p._testBattery; let tested = null; p._testBattery = (id) => { tested = id; };
  await p._click(btn("test-battery"));
  must("a failed battery save keeps the battery being edited", tested === "a");
  p._testBattery = tb; p._collectBattery = saved;
}

// ---- the month-by-month tariff chart keeps every grid line inside the chart
{
  const svg = p._monthChart({ months: ["2026-08", "2026-09", "2026-10"], totals: { A: [47, 40, 12], B: [30, 44, 9] }, cheaper_than_current: null }, "£");
  const ys = [...svg.matchAll(/<line [^>]*y1="([-\d.]+)"/g)].map((m) => +m[1]);
  must("month chart grid lines all sit inside the plot", ys.length >= 3 && ys.every((v) => v >= 8 - 0.01 && v <= 210 - 26 + 0.01) && svg.includes("£60"));
}

// ---- override: a cleared box is "not set" (full power / to full), not 0
{
  const btn = (act, ds = {}) => ({ target: { closest: (sel) => (sel === "[data-close-bg]" ? null : { dataset: { act, ...ds } }) } });
  const root = p.shadowRoot, qs = root.querySelector, qsa = root.querySelectorAll;
  root.querySelector = (sel) => (sel.includes("o-mode") ? { value: "charge" } : qs(sel));
  root.querySelectorAll = (sel) => (sel.includes("o-dev") ? [{ value: "a" }] : qsa(sel));
  els["o-pw"] = { value: "" }; els["o-soc"] = { value: " " }; els["o-min"] = { value: "60" };
  const calls = []; p._hass.callWS = async (m) => { calls.push(m); return {}; };
  await p._click(btn("save-override"));
  must("a cleared override box is sent as not set", calls[0] && calls[0].power_w === null && calls[0].target_soc === null);
  root.querySelector = qs; root.querySelectorAll = qsa;
}

// ---- times on the plan are shown on the home's clock, not the browser's
{
  p._hass.config = { time_zone: "Pacific/Auckland" };
  const at = Date.UTC(2026, 0, 15, 0, 0);                         // 13:00 in Auckland (summer, UTC+13)
  must("plan times use Home Assistant's time zone", p._fmtT(at, 3600e3).replace(/\D/g, "").startsWith("13"));
  p._hass.config = { time_zone: "Not/AZone" };
  must("an unknown zone falls back to the browser's clock", typeof p._fmtT(at, 3600e3) === "string");
  p._hass.config = {};
}

// ---- a register watch stops when the panel is hidden
{
  let stopped = 0; const ws = p._watchStop; p._watchStop = () => { stopped++; };
  p.disconnectedCallback();
  must("hiding the panel stops a register watch", stopped === 1);
  p._watchStop = ws;
}

// ---- day plan, grid events and solar strings
{
  const t0 = Date.now() - 600e3, iso = (i) => new Date(t0 + i * 1800e3).toISOString();
  p._preview = { segments: {}, conflicts: [], notes: [], plan: [],
    prices: Array.from({ length: 12 }, (_, i) => ({ start: iso(i), end: iso(i + 1), price: i === 4 ? 0.35 : 0.2, cheap: false })),
    day_plan: { until: iso(8), budget_wh: 3000, used_wh: 1800, threshold: 0.082, export_now_w: 0, hold_now: true,
      slots: Array.from({ length: 8 }, (_, i) => ({ start: iso(i), end: iso(i + 1), import: 0.2, export: 0.15, load_wh: 200, house_wh: i === 0 ? 0 : 200, export_wh: i === 4 ? 1500 : 0 })) },
    events: [{ start: iso(6), end: iso(8) }] };
  const ch = p._priceChart();
  must("price chart shows planned export, kept energy and the grid event", ch.includes("Exporting planned") && ch.includes("exporting about") && ch.includes("kept for dearer hours")
    && ch.includes("Grid event (saving session)") && ch.includes("Stored energy:"));
  const pvh = p._pvHtml({ total_w: 3600, strings: [{ string: 1, state: "ok", volts: 360, amps: 5, watts: 1800, today_share: 50, usual_share: 50 },
    { string: 2, state: "dead", volts: 301, amps: 0, watts: 0, note: "PV2 has given almost nothing" }, { string: 3, state: "unused" }] });
  must("solar strings table (details sheet part)", pvh.includes("PV1") && pvh.includes("Not producing") && !pvh.includes("PV3") && pvh.includes("All strings"));
  must("details sheet methods come from panel-details.js", typeof p._detailsHtml === "function" && typeof p._haDeviceHtml === "function");
  const st3 = p._settingsView();
  must("settings: paid export, wear, grid events, charge before an event", ["st-export_rates_entity", "st-export_discharge", "st-wear_cost", "st-event_entity", "st-event_charge_pct"].every((k) => st3.includes(k)));
}
