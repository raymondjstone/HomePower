// Second-audit panel checks: a hostile currency symbol is not injected, a refused delete stays on screen, a failed first load recovers.
import fs from "fs";
import { JSDOM } from "jsdom";
import path from "path";
import { fileURLToPath, pathToFileURL } from "url";
const HERE = path.dirname(fileURLToPath(import.meta.url));
const PANEL = pathToFileURL(path.join(HERE, "..", "..", "custom_components", "homepower", "frontend", "homepower-panel.js")).href;
const payload = JSON.parse(fs.readFileSync(process.argv[2] || path.join(HERE, "payload.json"), "utf8"));
const clone = (x) => JSON.parse(JSON.stringify(x));
const w = new JSDOM("<body></body>", { url: "http://localhost/" }).window;
for (const k of ["HTMLElement", "customElements", "document", "MouseEvent", "Event"]) globalThis[k] = w[k];
Object.defineProperty(globalThis, "localStorage", { value: w.localStorage, configurable: true });
Object.defineProperty(globalThis, "navigator", { value: {}, configurable: true });
const alerts = []; globalThis.alert = (m) => alerts.push(m); globalThis.confirm = () => true;
process.on("unhandledRejection", (e) => { console.log("UNHANDLED", e && e.stack); process.exitCode = 1; });
await import(`${PANEL}?v=1`);
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
let failures = 0;
const must = (n, ok, x = "") => { console.log(ok ? "ok  " : "FAIL", n, ok ? "" : x); if (!ok) failures++; };
const evil = '<img src=x id=pwn onerror="window.PWN=1">';
const get = clone(payload.get); get.live.currency = evil; get.settings.currency = evil; get.live.savings = { today: 1.5, month: 3 };
let fail = true, deleteError = "could not be handed back";
const el = document.createElement("homepower-panel"); document.body.appendChild(el);
el.hass = { states: {}, callWS: async (m) => {
  if (m.type === "homepower/get") { if (fail) throw new Error("still starting"); return clone(get); }
  if (m.type === "homepower/device/delete") throw new Error(deleteError);
  return {}; }, connection: { subscribeMessage: () => Promise.resolve(() => {}) } };
await wait(150);
must("the first load failed and says so", /Couldn't load/.test(el.shadowRoot.textContent));
fail = false; el._lastTry = 0; el._tick(true); await wait(150);
must("it retries by itself and recovers", el._loaded === true && !/Couldn't load/.test(el.shadowRoot.textContent), String(el._loadError));
el.tab = "overview"; el._render();
must("a hostile currency symbol is not injected into the overview", el.shadowRoot.querySelectorAll("#pwn").length === 0);
el.tab = "batteries"; el._render();
const id = (get.devices[0] || {}).device_id;
if (id) {
  el._batteryModal(get.devices[0], false); await wait(30);
  el.shadowRoot.querySelector('[data-act="del-battery"]').dispatchEvent(new w.MouseEvent("click", { bubbles: true, composed: true }));
  await wait(80);
  must("a refused delete keeps the dialog open and shows why", !!el._modal && /handed back/.test(el.shadowRoot.getElementById("modal").textContent), el.shadowRoot.getElementById("modal").textContent.slice(-120));
} else must("a battery exists to test delete", false);
// the timeline's "now" is Home Assistant's time, not the browser's
const probe = (tz) => { el._hass = { ...el._hass, config: { time_zone: tz } }; const n = el._haNow(); return n.getHours(); };
const hourIn = (tz) => +new Intl.DateTimeFormat("en-GB", { timeZone: tz, hour: "2-digit", hourCycle: "h23" }).format(new Date());
must("the clock follows Home Assistant's time zone", probe("Pacific/Auckland") === hourIn("Pacific/Auckland") && probe("America/Los_Angeles") === hourIn("America/Los_Angeles"));
must("an unknown time zone falls back instead of failing", typeof probe("Not/AZone") === "number");
process.exit(failures ? 1 : 0);
