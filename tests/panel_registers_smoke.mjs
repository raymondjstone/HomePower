// Renders the "Registers: show all" table from a fake scan and checks names, text decoding and the CSV link (run: node tests/panel_registers_smoke.mjs)
const els = {};
globalThis.HTMLElement = class { attachShadow(){ this.shadowRoot = { innerHTML:"", addEventListener(){}, getElementById:(i)=>(els[i] ||= { innerHTML:"", style:{}, value:"", checked:false, addEventListener(){} }), querySelector(){return { innerHTML:"" }}, querySelectorAll(){return []} }; } };
globalThis.localStorage = { getItem:()=>null, setItem(){} };
let cls; globalThis.customElements = { define:(n,c)=>cls=c };
await import("../custom_components/homepower/frontend/homepower-panel.js");
const p = new cls();
p.data = { devices: [{ device_id: "x1", name: "My X1" }] };
p._hass = { callWS: async () => ({ holding: { 10064: 3, 10112: 11825 }, input: { 10014: 100, 10112: 11825, 10113: 11824, 10999: 7 },
  names: { 10064: ["Work mode", "RW", "setting"], 10014: ["SOC", "RO", "live"], 10112: ["Software version", "RO", "info"], 10113: ["Software version", "RO", "info"] } }) };
await p._showRegisters("x1");
const html = p._scans.x1.html;
const ok = (n, c) => { console.log(c ? "ok  " : "FAIL", n); if (!c) process.exitCode = 1; };
ok("count", html.includes("5 registers"));
ok("download link", /download="registers-My_X1\.csv" href="blob:/.test(html));
ok("names + access", html.includes("Work mode") && html.includes("SOC") && html.includes('class="chip">RW'));
ok("unknown register flagged", html.includes("not in Anker\u2019s list"));
ok("text decoded low byte first", html.includes("<td>1.</td>") && html.includes("<td>0.</td>"));
ok("holding/input columns", html.includes("<td>3</td><td>–</td>"));
console.log(p._regRows.x1.length, "rows");

// ---- 32-bit decode in show-all
p._hass = { callWS: async () => ({ order: "little", holding: {}, input: { 10002: 0x1234 & 0xFFFF, 10003: 0, 10012: 65326, 10013: 65535 },
  names: { 10002: ["PV power", "RO", "live", 10002, 2], 10003: ["PV power", "RO", "live", 10002, 2], 10012: ["Grid power", "RO", "live", 10012, 2], 10013: ["Grid power", "RO", "live", 10012, 2] } }) };
await p._showRegisters("x1");
ok("32-bit decoded, low word first, signed", p._regRows.x1.find((x) => x.a === "10012").d === "-210" && p._regRows.x1.find((x) => x.a === "10002").d === "4660");
ok("second register of a pair has no decoded value", p._regRows.x1.find((x) => x.a === "10013").d === "");

// ---- watch: paint highlights what moved
els["watch-res-x1"] = { innerHTML: "" };
const w = { id: "x1", addrs: [10064, 10014], first: { 10064: 3, 10014: 50 }, last: { 10064: 4, 10014: 50 }, names: { 10064: ["Work mode", "RW", "setting"], 10014: ["SOC", "RO", "live"] },
  log: [{ t: "12:00:00", a: 10064, nm: "Work mode", k: "setting", x: 3, y: 4 }] };
p._watchPaint(w, new Set([10064]));
const wh = els["watch-res-x1"].innerHTML;
ok("watch highlights the register that moved", wh.includes("rgba(234,179,8,.45)") && wh.includes("Work mode") && wh.includes("3 → 4"));

// ---- insights cards
p.data = { devices: [{ device_id: "x1", name: "My X1" }] };
p.live = { trend: { x1: { days: [["2026-11-01", 31, 0.2], ["2026-11-02", 33, 3.5]], warning: "its battery packs have drifted apart" } },
  grid_events: [{ did: "x1", name: "My X1", start: "2026-11-02T03:00:00+00:00", end: "2026-11-02T03:20:00+00:00", soc_start: 80, soc_end: 60 }] };
ok("trend card shows the warning", p._trendCard().includes("drifted apart") && p._trendCard().includes("31 33"));
ok("grid outage card shows duration and charge", p._gridEventsCard().includes("20 min") && p._gridEventsCard().includes("80% → 60%"));
const m = { months: ["2026-08", "2026-09", "2026-10"], totals: { Now: [10, 12, 11], Go: [8, 9, 9] }, cheaper_than_current: { tariff: "Go", current: "Now", months: 3, saved: 7 } };
const chart = p._monthChart(m, "£");
ok("month chart has bars, legend and the flag", (chart.match(/<rect/g) || []).length === 6 && chart.includes("Go has been cheaper than Now for the last 3 months"));
ok("month chart skipped with one month", p._monthChart({ months: ["2026-10"], totals: { Now: [1] } }, "£") === "");
p.live = { forecast_reserve: { day: "2026-11-16", forecast_kwh: 3.2, below_kwh: 8, pct: 60, active: true } };
ok("reserve text", p._forecastReserveText().includes("kept above 60%"));
const hold = p._blankSchedule("hold");
ok("hold window template is a one-day hold", hold.mode === "hold" && /^\d{4}-\d\d-\d\d$/.test(hold.on_date) && hold.priority === 50);

// ---- readiness, what-if, decisions, labels
p.live = { readiness: { usable_kwh: 8.2, hours: 11.5, capped: false, basis: "usual use at this time of day" }, currency: "£" };
ok("readiness card", p._readinessCard().includes("about</b>") === false && p._readinessCard().includes("about 11.5 h") && p._readinessCard().includes("8.2 kWh"));
p.live = { readiness: { usable_kwh: 8, hours: null, capped: false, basis: "x" } };
ok("readiness unknown", p._readinessCard().includes("not worked out yet"));
p._whatif = { in: { br: 10, bc: 0, ar: 10, ac: 80 }, result: { days: 30, base: { cost: 50, import_kwh: 300, export_kwh: 10 }, alt: { cost: 42.5, import_kwh: 320, export_kwh: 8 }, tariff: "Go", capacity_kwh: 10, max_kw: 3.6 } };
p.live = { currency: "£" };
ok("what-if shows the saving", p._whatifCard().includes("would have saved £7.50") && p._whatifCard().includes("Go"));
p._whatif = { in: { br: 10, bc: 0, ar: 10, ac: 80 }, error: "needs at least 3 days" };
ok("what-if shows the error", p._whatifCard().includes("needs at least 3 days"));
els["watch-res-x1"] = { innerHTML: "" };
p._watchPaint({ id: "x1", addrs: [10080], first: { 10080: 1 }, last: { 10080: 2 }, names: {}, notes: { 10080: "Grid fail sensitivity" }, log: [{ t: "1", a: 10080, nm: "", k: undefined, x: 1, y: 2 }] }, new Set([10080]));
ok("label shown in the watch table and log, with a label button", els["watch-res-x1"].innerHTML.includes("✎ Grid fail sensitivity") && els["watch-res-x1"].innerHTML.includes('data-act="reg-label"'));
p.data = { devices: [{ device_id: "x1", name: "My X1" }] };
p.live = { trend: { x1: { days: [["2026-11-01", 31, 0.2], ["2026-11-02", 33, 0.3], ["2026-11-03", 34, 0.2]], warning: null } } };
ok("temperature chart drawn", p._trendCard().includes("<path") && p._trendCard().includes("Hottest temperature each day"));
ok("csv links", p._tariffCsv({ days: 30, tariffs: [{ name: "A", import_cost: 1, export_earned: 0, standing: 0, total: 1 }], monthly: null }).includes("tariff-comparison.csv")
  && p._csvLink("x.csv", [["a", 'b"c']]).includes('download="x.csv"'));
