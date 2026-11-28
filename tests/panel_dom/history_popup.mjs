// Clicking a figure on the Overview opens a popup with its history, read from the recorder through HomePower's own sensors.
import fs from "fs";
import { JSDOM } from "jsdom";
import path from "path";
import { fileURLToPath, pathToFileURL } from "url";
const HERE = path.dirname(fileURLToPath(import.meta.url));
const PANEL = pathToFileURL(path.join(HERE, "..", "..", "custom_components", "homepower", "frontend", "homepower-panel.js")).href;
const payload = JSON.parse(fs.readFileSync(process.argv[2] || path.join(HERE, "payload.json"), "utf8"));
const w = new JSDOM("<body></body>", { url: "http://localhost/" }).window;
for (const k of ["HTMLElement", "customElements", "document", "MouseEvent", "Event", "KeyboardEvent"]) globalThis[k] = w[k];
Object.defineProperty(globalThis, "localStorage", { value: w.localStorage, configurable: true });
Object.defineProperty(globalThis, "navigator", { value: {}, configurable: true });
process.on("unhandledRejection", (e) => { console.log("UNHANDLED", e && e.stack); process.exitCode = 1; });
await import(`${PANEL}?v=h`); await customElements.get("homepower-panel").ready;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
let failures = 0;
const must = (n, ok, x = "") => { console.log(ok ? "ok  " : "FAIL", n, ok ? "" : x); if (!ok) failures++; };
const clone = (x) => JSON.parse(JSON.stringify(x));

const get = clone(payload.get);
const dev = get.devices[0];
get.devices = [dev, { ...clone(dev), device_id: "b", name: "Second" }];
get.entity_ids = { battery_power: "sensor.bp", grid_power: "sensor.gp", stored_energy: "sensor.se", home_battery: "sensor.hb",
  savings_today: "sensor.sv", price_now: "sensor.pn",
  devices: { [dev.device_id]: { power: "sensor.a_p", soc: "sensor.a_s", solar: "sensor.a_pv" }, b: { power: "sensor.b_p", soc: "sensor.b_s", solar: null } } };
get.live.price_now = 0.245; get.live.savings = { today: 1.2, month: 8 };
get.live.combined = { soc: 55, stored_kwh: 5, capacity_kwh: 10, usable_kwh: 4, flow_w: 800, total: 2, online: 2, load_w: 400, runtime_h: null, full_in_h: 2 };
get.live.solar_forecast = { today_kwh: 6, source: "forecast_solar" };

const asked = [];
const now = Date.now(), H = 3600e3;
const stats = (ids) => Object.fromEntries(ids.map((id) => [id, [0, 1, 2, 3].map((i) => ({ start: now - (4 - i) * H, mean: id === "sensor.gp" ? -300 + i * 200 : 500 * i }))]));
let fail = false;
const el = document.createElement("homepower-panel"); document.body.appendChild(el);
el.hass = { language: "en", states: { "sensor.pn": { state: "0.245", attributes: { unit_of_measurement: "£/kWh" } }, "sensor.bp": { state: "1.5", attributes: { unit_of_measurement: "kW" } } },
  connection: { subscribeMessage: () => Promise.resolve(() => {}) },
  callWS: async (m) => {
    if (m.type === "homepower/get") return clone(get);
    if (m.type === "recorder/statistics_during_period" || m.type === "history/history_during_period") {
      asked.push(m);
      if (fail) throw new Error("recorder is busy");
      if (m.type === "history/history_during_period") return { "sensor.pn": [{ s: "0.30", lu: (now - 5 * H) / 1000 }, { s: "0.12", lu: (now - 2 * H) / 1000 }] };
      return stats(m.statistic_ids);
    }
    return {};
  } };
await wait(200);
el.tab = "overview"; el._render();
const sr = el.shadowRoot;
const keys = [...sr.querySelectorAll('[data-act="hist"]')].map((x) => x.dataset.k);
for (const k of ["flow", "stored", "grid", "savings", "price", "soc", "solar"]) must(`the ${k} figure opens a history popup`, keys.includes(k), JSON.stringify(keys));
must("every such figure can be reached by keyboard", [...sr.querySelectorAll('[data-act="hist"]')].every((x) => x.getAttribute("tabindex") === "0" && x.getAttribute("role") === "button"));
must("figures with no recorded sensor are not clickable (batteries online)", ![...sr.querySelectorAll('[data-act="hist"]')].some((x) => /Batteries online/.test(x.textContent)));

const click = (sel) => sr.querySelector(sel).dispatchEvent(new w.MouseEvent("click", { bubbles: true, composed: true }));
click('.tile[data-k="flow"]'); await wait(50);
const m1 = asked.at(-1);
must("battery flow asks for statistics of the total and each battery, in watts",
  m1 && m1.type === "recorder/statistics_during_period" && m1.units.power === "W" && ["sensor.bp", "sensor.a_p", "sensor.b_p"].every((x) => m1.statistic_ids.includes(x)), JSON.stringify(m1));
must("24 hours by default, as 5-minute averages", m1 && m1.period === "5minute" && Math.abs(Date.parse(m1.end_time) - Date.parse(m1.start_time) - 24 * H) < 5000);
must("the popup shows a chart with a legend for each battery", !!sr.querySelector("#modal .plot svg path[stroke]") && sr.querySelectorAll('#modal [data-act="hist-legend"]').length === 3, sr.getElementById("modal").innerHTML.slice(0, 400));
must("the live value in kW is shown in W", /1\.50 kW/.test(sr.getElementById("hist-body").textContent), sr.getElementById("hist-body").textContent);
click('#modal [data-act="hist-legend"]'); await wait(10);
must("a legend click hides that series and keeps the popup open", sr.querySelector('#modal [data-act="hist-legend"]').getAttribute("aria-pressed") === "false" && !!sr.querySelector(".modal.hist"));
click('#modal [data-act="hist-legend"]'); await wait(10);

click('#modal [data-act="hist-range"][data-h="168"]'); await wait(50);
must("7 days asks for hourly statistics", asked.at(-1).period === "hour", JSON.stringify(asked.at(-1)));
must("and the chosen range is marked", sr.querySelector('#modal [data-act="hist-range"][data-h="168"]').classList.contains("pri"));

sr.querySelector(".modal.hist").dispatchEvent(new w.KeyboardEvent("keydown", { key: "Escape", bubbles: true, composed: true }));
must("Escape closes the history popup", !sr.querySelector(".modal.hist"));

const price = sr.querySelector('.tile[data-k="price"]');
price.focus();
price.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Enter", bubbles: true, composed: true })); await wait(50);
const m2 = asked.at(-1);
must("Enter on a figure opens it; the price uses the recorded states (it has no statistics)",
  m2.type === "history/history_during_period" && m2.entity_ids.includes("sensor.pn") && m2.no_attributes === true, JSON.stringify(m2));
must("the price chart shows the recorded range", /0\.120/.test(sr.getElementById("hist-body").textContent) && /0\.300/.test(sr.getElementById("hist-body").textContent), sr.getElementById("hist-body").textContent);
click('#modal [data-act="close"]');

fail = true;
click('.hb [data-k="soc"]'); await wait(50);
must("a recorder error is shown in the popup, not thrown", /History is not available: recorder is busy/.test(sr.getElementById("hist-body").textContent));
click('#modal [data-act="close"]');
fail = false;

{ // with no entity ids (an older backend, or the lookup failed) nothing is clickable and nothing breaks
  const g2 = clone(get); g2.entity_ids = {};
  el.data = g2; el._render();
  must("without sensor ids no figure is clickable", sr.querySelectorAll('[data-act="hist"]').length === 0);
}
process.exit(failures || process.exitCode ? 1 : 0);
