// History for the charts is asked for in watts: otherwise Home Assistant answers in whatever unit the sensor is set to show.
import fs from "fs";
import { JSDOM } from "jsdom";
import path from "path";
import { fileURLToPath, pathToFileURL } from "url";
const HERE = path.dirname(fileURLToPath(import.meta.url));
const PANEL = pathToFileURL(path.join(HERE, "..", "..", "custom_components", "homepower", "frontend", "homepower-panel.js")).href;
const payload = JSON.parse(fs.readFileSync(path.join(HERE, "payload.json"), "utf8"));
const w = new JSDOM("<body></body>", { url: "http://localhost/" }).window;
for (const k of ["HTMLElement", "customElements", "document", "MouseEvent", "Event"]) globalThis[k] = w[k];
Object.defineProperty(globalThis, "localStorage", { value: w.localStorage, configurable: true });
Object.defineProperty(globalThis, "navigator", { value: {}, configurable: true });
await import(`${PANEL}?v=u`); await customElements.get("homepower-panel").ready;
const el = document.createElement("homepower-panel"); document.body.appendChild(el);
const clone = (x) => JSON.parse(JSON.stringify(x));
const asked = [];
const get = { ...clone(payload.get), entity_ids: { battery_power: "sensor.bp", grid_power: "sensor.gp", devices: { a: { power: "sensor.a_power", soc: "sensor.a_soc" } } } };
el.hass = { states: {}, language: "en", connection: { subscribeMessage: () => Promise.resolve(() => {}) },
  callWS: async (m) => { if (m.type === "recorder/statistics_during_period") { asked.push(m); return {}; } return m.type === "homepower/get" ? clone(get) : {}; } };
await new Promise((r) => setTimeout(r, 200));
el._flowMinV = 180; el._backfillFlow();
await el._loadHistory();
await new Promise((r) => setTimeout(r, 50));
const ok = asked.length >= 2 && asked.every((m) => m.units && m.units.power === "W");
console.log(ok ? "ok  " : "FAIL", "both history requests ask for watts", ok ? "" : JSON.stringify(asked.map((m) => m.units)));
process.exit(ok ? 0 : 1);
