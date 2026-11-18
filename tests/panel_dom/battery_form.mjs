// The add-battery form for the local EcoFlow profile: it shows the IP/port/reverse-sign fields, the values typed are collected into the
// connection, and editing a saved battery shows its saved values again.
// usage: node battery_form.mjs [payload.json] [catalogue.json]   (a fresh catalogue: python dumping core.profiles.catalogue())
import fs from "fs";
import { JSDOM } from "jsdom";
import path from "path";
import { fileURLToPath, pathToFileURL } from "url";
const HERE = path.dirname(fileURLToPath(import.meta.url));
const PAYLOAD = process.argv[2] || path.join(HERE, "payload.json");
const PANEL = pathToFileURL(path.join(HERE, "..", "..", "custom_components", "homepower", "frontend", "homepower-panel.js")).href;
const payload = JSON.parse(fs.readFileSync(PAYLOAD, "utf8"));
if (process.argv[3]) payload.get.catalogue = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const w = new JSDOM("<body></body>", { url: "http://localhost/" }).window;
for (const k of ["HTMLElement", "customElements", "document", "MouseEvent", "Event"]) globalThis[k] = w[k];
Object.defineProperty(globalThis, "localStorage", { value: w.localStorage, configurable: true });
Object.defineProperty(globalThis, "navigator", { value: {}, configurable: true });
globalThis.alert = () => {}; globalThis.confirm = () => true;
process.on("unhandledRejection", (e) => { console.log("UNHANDLED", e && e.stack); process.exitCode = 1; });
await import(`${PANEL}?v=1`);
const el = document.createElement("homepower-panel"); document.body.appendChild(el);
el.hass = { states: {}, callWS: async (m) => (m.type === "homepower/get" ? payload.get : {}), connection: { subscribeMessage: () => Promise.resolve(() => {}) } };
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
await wait(200);
let failures = 0;
const must = (name, ok, extra = "") => { console.log(ok ? "ok  " : "FAIL", name, ok ? "" : extra); if (!ok) failures++; };
const root = el.shadowRoot;
must("the catalogue has the local EcoFlow profile", payload.get.catalogue.profiles.some((p) => p.id === "ecoflow_stream_5000_local"));

el.tab = "batteries"; el._render();
root.querySelector('[data-act="add-battery"]').dispatchEvent(new w.MouseEvent("click", { bubbles: true, composed: true }));
await wait(50);
const kind = root.getElementById("b-kind");
kind.value = "ecoflow_stream_5000_local";
kind.dispatchEvent(new w.Event("change", { bubbles: true, composed: true }));
await wait(50);
must("form shows host, port and reverse-sign fields", ["c-host", "c-port", "c-reverse_sign"].every((i) => root.getElementById(i)), [...root.querySelectorAll("#b-conn [id]")].map((e) => e.id).join(","));
must("setup help mentions where the switch is", /Third-party control/.test(root.getElementById("b-conn").textContent));
root.getElementById("b-name").value = "Cellar ES5000";
root.getElementById("c-host").value = "10.0.0.213";
root.getElementById("c-reverse_sign").checked = true;
const out = el._collectBattery(null);
must("collected connection has host, port, unit and the reverse flag", out.connection.host === "10.0.0.213" && out.connection.port === 502 && out.connection.unit_id === 1 && out.connection.reverse_sign === true, JSON.stringify(out.connection));
must("kind is carried", out.kind === "ecoflow_stream_5000_local", out.kind);

// editing a saved battery shows its saved values
el._modal = null; el._render();
el._batteryModal({ device_id: "es", name: "ES", kind: "ecoflow_stream_5000_local", capacity_wh: 5000, max_charge_w: 3000, max_discharge_w: 2300, min_soc: 10, priority: 0,
  entities: {}, mode_map: {}, connection: { host: "10.0.0.9", port: 5020, unit_id: 1, reverse_sign: true }, control_enabled: true }, false);
await wait(50);
must("editing shows the saved host, port and flag", root.getElementById("c-host").value === "10.0.0.9" && root.getElementById("c-port").value === "5020" && root.getElementById("c-reverse_sign").checked === true);
const out2 = el._collectBattery("es");
must("saving the edit keeps them", out2.connection.host === "10.0.0.9" && out2.connection.port === 5020 && out2.connection.reverse_sign === true, JSON.stringify(out2.connection));
process.exit(failures ? 1 : 0);
