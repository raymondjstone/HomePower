// The Flow tab: house, grid, solar and each battery, with the live figures and the direction each line's dots move.
// Updates change the figures in place (the page is not redrawn), so the moving dots carry on from where they are.
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
process.on("unhandledRejection", (e) => { console.log("UNHANDLED", e && e.stack); process.exitCode = 1; });
await import(`${PANEL}?v=f`); await customElements.get("homepower-panel").ready;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
let failures = 0;
const must = (n, ok, x = "") => { console.log(ok ? "ok  " : "FAIL", n, ok ? "" : x); if (!ok) failures++; };
const clone = (x) => JSON.parse(JSON.stringify(x));

const get = clone(payload.get);
const dev = get.devices[0];
get.devices = [{ ...dev, device_id: "a", name: "Garage", driver: "anker" }, { ...clone(dev), device_id: "b:2", name: "Utility <b>", driver: "anker" }];
get.live.devices = { a: { online: true, soc: 64, power_w: -820, pv_w: 1350, has_solar: true }, "b:2": { online: true, soc: 38, power_w: 450, pv_w: 0 } };
get.live.grid_w = 240; get.live.house_w = 1960;
get.entity_ids = { grid_power: "sensor.gp", battery_power: "sensor.bp", devices: { a: { power: "sensor.a_p" } } };
const el = document.createElement("homepower-panel"); document.body.appendChild(el);
el.hass = { language: "en", states: {}, connection: { subscribeMessage: () => Promise.resolve(() => {}) },
  callWS: async (m) => (m.type === "homepower/get" ? clone(get) : {}) };
await wait(200);
const sr = el.shadowRoot;
must("there is a Flow tab", !!sr.querySelector('[data-tab="flow"]'));
sr.querySelector('[data-tab="flow"]').dispatchEvent(new w.MouseEvent("click", { bubbles: true, composed: true }));
await wait(20);
const txt = (id) => (sr.getElementById(id) || { textContent: "" }).textContent.replace(/\s+/g, " ").trim();
must("the house shows what it is using", /1\.96 kW/.test(txt("fn-house")), txt("fn-house"));
must("the grid shows importing", /240 W/.test(txt("fn-grid")) && /Importing/.test(txt("fn-grid")), txt("fn-grid"));
must("solar is shown with its power", /1\.35 kW/.test(txt("fn-solar")), txt("fn-solar"));
must("each battery has its own circle with its charge level and state", /64%/.test(txt("fn-bat-a")) && /discharging/.test(txt("fn-bat-a")) && /38%/.test(txt("fn-bat-b_3a2")) && /charging/.test(txt("fn-bat-b_3a2")), txt("fn-bat-a") + " | " + txt("fn-bat-b_3a2"));
must("a battery name is escaped", !sr.querySelector(".flow b:not(.fv)") && /Utility <b>/.test(txt("fn-bat-b_3a2")));
const sp = el._flowSpeed;
must("dots move towards the house for import, solar and a discharging battery", sp.grid > 0 && sp.solar > 0 && sp["bat:a"] > 0, JSON.stringify(sp));
must("and away from it for a charging battery", sp["bat:b:2"] < 0, JSON.stringify(sp));
must("more power moves faster", sp.solar > sp.grid);
const lbl = (k) => [...sr.querySelectorAll(".flbl")].find((x) => x.dataset.k === k);
must("each line carries its live value", lbl("grid").textContent === "240 W" && lbl("bat:b:2").textContent === "450 W", lbl("grid").textContent);
must("the summary says where the power comes from", /Grid 240 W/.test(txt("fsum")) && /Batteries 820 W/.test(txt("fsum")) && /charging batteries 450 W/.test(txt("fsum")), txt("fsum"));

const svgBefore = sr.querySelector(".flow > svg");
el.live = { ...el.live, grid_w: -900, devices: { ...el.live.devices, a: { ...el.live.devices.a, power_w: 5 } } };
el._tick(true); await wait(10);
must("an update changes the figures in place, without redrawing the lines", sr.querySelector(".flow > svg") === svgBefore);
must("exporting reverses the grid line", el._flowSpeed.grid < 0 && /Exporting/.test(txt("fn-grid")), JSON.stringify(el._flowSpeed));
must("an idle battery's line stops and loses its label", el._flowSpeed["bat:a"] === 0 && lbl("bat:a").textContent === "" && /idle/.test(txt("fn-bat-a")));

el.live = { ...el.live, grid_w: null, house_w: null, devices: { ...el.live.devices, a: { online: false } } };
el._tick(true); await wait(10);
must("no grid reading and an offline battery are shown as such", /No reading/.test(txt("fn-grid")) && /Offline/.test(txt("fn-bat-a")) && sr.getElementById("fn-bat-a").classList.contains("off"));

sr.querySelector('#fn-grid[data-act="hist"]').dispatchEvent(new w.MouseEvent("click", { bubbles: true, composed: true }));
await wait(20);
must("clicking the grid opens its history", !!sr.querySelector(".modal.hist"));

el.data.devices = []; el._closeModal(); el._lastHtml = null; el._render();
must("with no batteries it says so", /No batteries yet/.test(sr.getElementById("app").textContent));
process.exit(failures || process.exitCode ? 1 : 0);
