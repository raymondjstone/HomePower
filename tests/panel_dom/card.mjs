// The Lovelace card: finds the sensor, draws it, escapes names, and only redraws when its own sensor changed.
import { JSDOM } from "jsdom";
import path from "path";
import { fileURLToPath, pathToFileURL } from "url";
const HERE = path.dirname(fileURLToPath(import.meta.url));
const w = new JSDOM("<body></body>", { url: "http://localhost/" }).window;
for (const k of ["HTMLElement", "customElements", "document"]) globalThis[k] = w[k];
globalThis.window = w;
await import(pathToFileURL(path.join(HERE, "..", "..", "custom_components", "homepower", "frontend", "homepower-card.js")).href);
let failures = 0;
const must = (n, ok, x = "") => { console.log(ok ? "ok  " : "FAIL", n, ok ? "" : x); if (!ok) failures++; };
const el = document.createElement("homepower-card"); document.body.appendChild(el);
el.setConfig({});
const sensor = { entity_id: "sensor.homepower_home_battery", state: "62", last_updated: "t1",
  attributes: { homepower: true, soc: 62, stored_kwh: 6.2, capacity_kwh: 10, flow_w: -400, runtime_h: 15.5, full_in_h: null,
    batteries: [{ name: "<b>Garage</b>", soc: 62, power_w: -400, online: true }, { name: "Shed", soc: null, power_w: null, online: false }] } };
const states = { "sensor.homepower_home_battery": sensor, "light.x": { entity_id: "light.x", state: "on", last_updated: "a", attributes: {} } };
el.hass = { states };
const html = el.shadowRoot.innerHTML;
must("shows charge, flow and runtime", /62%/.test(html) && /Discharging 400 W/.test(html) && /15\.5 h/.test(html), html.slice(0, 200));
must("battery names are escaped", html.includes("&lt;b&gt;Garage") && !html.includes("<b>Garage"));
must("an offline battery with no charge draws a dash", /–/.test(html));
const marker = el.shadowRoot.querySelector("ha-card, .top"); marker.setAttribute("data-mark", "1");
el.hass = { states: { ...states, "light.x": { entity_id: "light.x", state: "off", last_updated: "b", attributes: {} } } };   // an unrelated entity changed
must("an unrelated state change does not redraw", el.shadowRoot.querySelector("[data-mark]") !== null);
el.hass = { states: { ...states, "sensor.homepower_home_battery": { ...sensor, last_updated: "t2", attributes: { ...sensor.attributes, soc: 40 } } } };
must("its own change redraws", el.shadowRoot.innerHTML.includes("40%") && el.shadowRoot.querySelector("[data-mark]") === null);
process.exit(failures ? 1 : 0);
