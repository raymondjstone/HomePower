// Every tab, drawn 3000 times from the real payload with values nulled or removed at random (Home Assistant sends a NaN as
// null): the page must never come out blank. A drawing error shows a message instead (see _render).
import fs from "fs";
import { JSDOM } from "jsdom";
import path from "path";
import { fileURLToPath, pathToFileURL } from "url";
const HERE = path.dirname(fileURLToPath(import.meta.url));
const PANEL = pathToFileURL(path.join(HERE, "..", "..", "custom_components", "homepower", "frontend", "homepower-panel.js")).href;
const payload = JSON.parse(fs.readFileSync(process.argv[2] || path.join(HERE, "payload.json"), "utf8"));
const w = new JSDOM("<body></body>", { url: "http://localhost/" }).window;
for (const k of ["HTMLElement", "customElements", "document", "MouseEvent", "Event"]) globalThis[k] = w[k];
Object.defineProperty(globalThis, "localStorage", { value: w.localStorage, configurable: true });
Object.defineProperty(globalThis, "navigator", { value: {}, configurable: true });
globalThis.alert = () => {}; globalThis.confirm = () => true;
console.error = () => {};      // the drawing errors are expected here: what matters is that the page shows them
await import(`${PANEL}?v=f`); await customElements.get("homepower-panel").ready;
const el = document.createElement("homepower-panel"); document.body.appendChild(el);
const clone = (x) => JSON.parse(JSON.stringify(x));
el.hass = { states: {}, language: "en", callWS: async (m) => m.type === "homepower/get" ? clone(payload.get) : {}, connection: { subscribeMessage: () => Promise.resolve(() => {}) } };
await new Promise((r) => setTimeout(r, 200));
let seed = 7; const rnd = () => ((seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff);
const junk = [null];
function mutate(o, depth = 0) {
  if (!o || typeof o !== "object" || depth > 4) return;
  for (const k of Object.keys(o)) {
    const r = rnd();
    if (r < 0.06) o[k] = junk[Math.floor(rnd() * junk.length)];
    else if (r < 0.09) delete o[k];
    else mutate(o[k], depth + 1);
  }
}
const found = new Map();
const tabs = ["overview", "plan", "schedules", "batteries", "insights", "diagnostics", "settings"];
for (let i = 0; i < 3000; i++) {
  const live = clone(payload.get.live); mutate(live);
  el.live = live; el.data = clone(payload.get); el.data.live = live;
  if (rnd() < 0.3) mutate(el.data.devices);
  el.tab = tabs[i % tabs.length]; el._lastHtml = null;
  let key = null;
  try { el._render(); } catch (e) { key = "escaped _render: " + String(e); }
  const app = el.shadowRoot.getElementById("app");
  if (!key && !(app && app.textContent.trim() && app.querySelector("nav"))) key = `blank ${el.tab} page`;
  if (key) found.set(key, (found.get(key) || 0) + 1);
}
for (const [k, n] of found) console.log("FAIL", n, k);
console.log(found.size ? "FAIL" : "ok  ", "3000 random renders: never blank, never an escaped error");
process.exit(found.size ? 1 : 0);
