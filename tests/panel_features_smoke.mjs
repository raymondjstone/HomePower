// Renders the newer panel pieces with fake data and checks they produce sensible HTML (run: node tests/panel_features_smoke.mjs)
const els = {};
globalThis.HTMLElement = class { attachShadow(){ this.shadowRoot = { innerHTML:"", addEventListener(){}, getElementById:(i)=>(els[i] ||= { innerHTML:"" }), querySelector(){return null}, querySelectorAll(){return []} }; } };
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
