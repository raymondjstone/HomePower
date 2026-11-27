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

// a power entity in kW is shown in W, and the grid figure only ever comes from the server (the meter may be a kWh counter)
el.hass = { ...el._hass, states: { "sensor.p": { state: "-1.25", attributes: { unit_of_measurement: "kW" } }, "sensor.kwh": { state: "8123", attributes: { unit_of_measurement: "kWh" } } } };
const kw = el._reading({ device_id: "x", entities: { power: "sensor.p" } });
must("a kW power entity is read as W", kw.power === -1250, String(kw.power));
el.data.settings = { ...el.data.settings, grid_import_entity: "sensor.kwh" };
el.live = { ...el.live, grid_w: null };
must("no server grid reading shows none, not the meter's counter", el._gridW() === null, String(el._gridW()));

// unit id 0 is kept for a register-map device
el._closeModal();
el._batteryModal({ device_id: "m", name: "M", kind: "modbus_map", entities: {}, mode_map: {}, connection: { host: "10.0.0.9", port: 502, unit_id: 0 }, control_enabled: false }, false);
await wait(50);
const out3 = el._collectBattery("m");
must("unit id 0 survives an edit", root.getElementById("c-unit_id").value === "0" && out3.connection.unit_id === 0, JSON.stringify(out3.connection.unit_id));

// legends: clicking hides a series, clicking again shows it; the scale follows and the choice is remembered
el._modal = null; el.tab = "batteries"; el._render();
const pts = (v) => [{ t: 1000, v }, { t: 2000, v }];
const draw = () => el._lineChart([{ key: "a", name: "A", color: "#111", pts: pts(400) }, { key: "b", name: "B", color: "#222", pts: pts(9000) }], "W", "power");
const host = () => { let h = root.getElementById("lg-host"); if (!h) { h = document.createElement("div"); h.id = "lg-host"; root.getElementById("app").appendChild(h); } h.innerHTML = draw(); return h; };
const btn = () => host().querySelector('[data-act="legend"][data-key="b"]');
const click = () => btn().dispatchEvent(new w.MouseEvent("click", { bubbles: true, composed: true }));
const lines = () => host().querySelectorAll(".plot-main svg path").length;
const top = () => host().querySelector(".plot-y span:last-child").textContent;
must("legend items are buttons, shown to start with", btn().getAttribute("aria-pressed") === "true" && lines() === 2);
const full = top();
click();
must("a click on the legend hides that series only", lines() === 1 && btn().getAttribute("aria-pressed") === "false");
must("the scale fits what is left", top() !== full, `${full} -> ${top()}`);
must("the choice is remembered", localStorage.getItem("homepower.hidden").includes("power"));
click();
must("a second click shows it again", lines() === 2 && top() === full);
process.exit(failures ? 1 : 0);
