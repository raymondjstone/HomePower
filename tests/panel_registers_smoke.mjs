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
