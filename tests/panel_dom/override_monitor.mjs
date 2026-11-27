// A monitor-only battery (sent nothing): the override dialog shows it and it cannot be picked; the plan shows its row faded.
import fs from "fs";
import { JSDOM } from "jsdom";
import path from "path";
import { fileURLToPath, pathToFileURL } from "url";
const HERE = path.dirname(fileURLToPath(import.meta.url));
const PAYLOAD = process.argv[2] || path.join(HERE, "payload.json");
const PANEL = pathToFileURL(path.join(HERE, "..", "..", "custom_components", "homepower", "frontend", "homepower-panel.js")).href;
const payload = JSON.parse(fs.readFileSync(PAYLOAD, "utf8"));
const w = new JSDOM("<body></body>", { url: "http://localhost/" }).window;
for (const k of ["HTMLElement", "customElements", "document", "MouseEvent", "Event"]) globalThis[k] = w[k];
Object.defineProperty(globalThis, "localStorage", { value: w.localStorage, configurable: true });
Object.defineProperty(globalThis, "navigator", { value: {}, configurable: true });
globalThis.alert = () => {}; globalThis.confirm = () => true;
process.on("unhandledRejection", (e) => { console.log("UNHANDLED", e && e.stack); process.exitCode = 1; });
await import(`${PANEL}?v=1`);
const get = JSON.parse(JSON.stringify(payload.get));
get.devices.push({ ...get.devices[0], device_id: "watch_only", name: "Watched", control_enabled: false });   // a second, monitor-only battery
const el = document.createElement("homepower-panel"); document.body.appendChild(el);
el.hass = { states: {}, callWS: async (m) => (m.type === "homepower/get" ? get : m.type === "homepower/preview" ? (payload.preview || { segments: {}, conflicts: [], plan: [] }) : {}), connection: { subscribeMessage: () => Promise.resolve(() => {}) } };
await new Promise((r) => setTimeout(r, 200));
el._overrideModal(get.devices[0].device_id);
const boxes = [...el.shadowRoot.getElementById("modal").querySelectorAll('input[name="o-dev"]')];
const watched = boxes.find((b) => b.value === get.devices[1].device_id), ctl = boxes.find((b) => b.value === get.devices[0].device_id);
const ok = watched && watched.disabled && watched.parentElement.textContent.includes("(monitor only)") && ctl && !ctl.disabled && ctl.checked;
console.log(ok ? "ok   a monitor-only battery is shown and cannot be picked" : "FAIL monitor-only battery in the override dialog");
if (!ok) process.exitCode = 1;
el._closeModal && el._closeModal();
el.tab = "plan"; el._render(); await el._tabLoad(); await new Promise((r) => setTimeout(r, 50));
const rowTxt = [...el.shadowRoot.querySelectorAll(".tl .row .nm")].map((n) => n.textContent);
const ok2 = rowTxt.some((t) => t === "Watched (monitor only)") && !rowTxt.some((t) => t.startsWith(get.devices[0].name + " (monitor"));
console.log(ok2 ? "ok   the plan marks a monitor-only battery" : "FAIL plan rows: " + JSON.stringify(rowTxt));
if (!ok2) process.exitCode = 1;
const watchedDev = get.devices.find((x) => x.device_id === "watch_only");
el.live = { ...(el.live || {}), devices: { ...((el.live || {}).devices || {}), watch_only: { desired_mode: "charge", desired_power_w: 1500, online: true } } };
let html = ""; try { html = el._detailsHtml(watchedDev, {}, {}); } catch (e) { html = "ERR " + e.message; }
const ok3 = html.includes("Nothing: monitor only") && html.includes("the plan would say");
console.log(ok3 ? "ok   its details say nothing is sent (and what the plan would want)" : "FAIL details: " + html.slice(0, 300));
if (!ok3) process.exitCode = 1;
let card = ""; try { card = el._batCard(watchedDev, el._reading(watchedDev), 1); } catch (e) { card = "ERR " + e.message; }
const ok4 = !card.includes("Target:") && card.includes("Monitor only");
console.log(ok4 ? "ok   its Overview card shows no Target chip (nothing is sent)" : "FAIL card: " + card.slice(0, 300));
if (!ok4) process.exitCode = 1;
process.exit(process.exitCode || 0);
