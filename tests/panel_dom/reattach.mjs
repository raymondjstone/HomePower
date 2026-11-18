// The panel keeps working after Home Assistant detaches and re-attaches it, and typing in the port-scan box survives a refresh.
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
process.on("unhandledRejection", (e) => { console.log("UNHANDLED", e && e.stack); process.exitCode = 1; });
await import(`${PANEL}?v=1`);
let subs = 0, unsubs = 0;
const el = document.createElement("homepower-panel"); document.body.appendChild(el);
el.hass = { states: {}, callWS: async (m) => (m.type === "homepower/get" ? payload.get : m.type === "homepower/diagnostics" ? payload.diag : {}),
  connection: { subscribeMessage: () => { subs++; return Promise.resolve(() => { unsubs++; }); } } };
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
await wait(200);
let failures = 0;
const must = (n, ok, x = "") => { console.log(ok ? "ok  " : "FAIL", n, ok ? "" : x); if (!ok) failures++; };
must("subscribed once at the start", subs === 1 && el._timer, `${subs}`);
el.remove(); await wait(20);
must("detached: unsubscribed and the timer stopped", unsubs === 1 && !el._timer, `${unsubs}`);
document.body.appendChild(el); await wait(100);
must("re-attached: subscribed again and the timer runs", subs === 2 && !!el._timer, `${subs}`);
el.remove(); document.body.appendChild(el); await wait(100);
must("detached and attached again keeps exactly one live feed", subs === 3 && unsubs === 2, `${subs}/${unsubs}`);

el.tab = "diagnostics"; await el._tabLoad(); el._render();
const box = el.shadowRoot.getElementById("ps-host");
box.value = "192.168.1.5"; box.dispatchEvent(new w.Event("input", { bubbles: true, composed: true }));
el._render();
must("typed scan address survives a re-render", el.shadowRoot.getElementById("ps-host").value === "192.168.1.5", el.shadowRoot.getElementById("ps-host").value);
process.exit(failures ? 1 : 0);
