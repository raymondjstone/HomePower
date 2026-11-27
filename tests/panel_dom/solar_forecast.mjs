// Today's solar forecast on the Overview, and forecast against what came in Insights (by day and by hour), drawn from the
// shapes the backend sends (coord_forecast.solar_view / solar_comparison), including the edge cases.
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
globalThis.alert = () => {}; globalThis.confirm = () => true;
globalThis.URL.createObjectURL = () => "blob:x";
await import(`${PANEL}?v=s`); await customElements.get("homepower-panel").ready;
const el = document.createElement("homepower-panel"); document.body.appendChild(el);
const clone = (x) => JSON.parse(JSON.stringify(x));
const calls = [];
let cmp = null;
el.hass = { states: {}, language: "en", connection: { subscribeMessage: () => Promise.resolve(() => {}) },
  callWS: async (m) => { calls.push(m); return m.type === "homepower/get" ? clone(payload.get) : m.type === "homepower/solar_forecast" ? clone(cmp) : {}; } };
await new Promise((r) => setTimeout(r, 200));

let failures = 0;
const check = (ok, what) => { console.log(ok ? "ok  " : "FAIL", what); if (!ok) failures++; };
const app = () => el.shadowRoot.getElementById("app");
const draw = (tab, live) => {
  el.data = clone(payload.get); el.live = { ...clone(payload.get.live), ...live }; el.data.live = el.live;
  el.tab = tab; el._lastHtml = null; el._render();
  return app().textContent;
};

const days = Array.from({ length: 30 }, (_, i) => {
  const d = new Date(Date.UTC(2026, 9, 27 + i)).toISOString().slice(0, 10);
  return { day: d, forecast: i < 3 ? null : 8 + (i % 5), actual: i === 29 ? 2.1 : i < 1 ? null : 6 + (i % 4) };
});
const hours = Array.from({ length: 24 }, (_, h) => ({ hour: h, forecast: h >= 8 && h <= 16 ? 0.2 * (8 - Math.abs(12 - h)) : 0, actual: h <= 13 ? (h >= 8 ? 0.15 * (h - 7) : 0) : null }));
cmp = { days, day: days[29].day, hours, measured: true, source: "forecast_solar" };
const SF = { today_kwh: 9.6, tomorrow_kwh: 12.3, remaining_kwh: 4.2, so_far_kwh: 2.1, source: "forecast_solar" };

// ---- Overview tile
let t = draw("overview", { solar_forecast: SF });
check(t.includes("Solar forecast today") && t.includes("9.6 kWh") && t.includes("2.1 kWh so far") && t.includes("4.2 kWh still to come") && t.includes("tomorrow 12.3 kWh"),
  "overview: the tile shows today's forecast, so far, still to come and tomorrow");
check(/From Forecast\.Solar/.test(app().innerHTML), "overview: the tile says where the forecast is from");
t = draw("overview", { solar_forecast: { ...SF, so_far_kwh: null, remaining_kwh: null, tomorrow_kwh: null } });
check(t.includes("Solar forecast today") && !t.includes("so far") && !t.includes("could not be shown"), "overview: no measured solar or hourly data: just the forecast");
t = draw("overview", { solar_forecast: null });
check(!t.includes("Solar forecast today") && !t.includes("could not be shown"), "overview: no forecast set up: no tile");

// ---- Insights card
el._solarCmp = clone(cmp);
t = draw("insights", { solar_forecast: SF });
const html = app().innerHTML;
check(t.includes("Solar forecast against what came") && t.includes("By day") && t.includes("By hour"), "insights: the card with both charts");
check(/measured was\s*<b>\d+%<\/b> of what Forecast\.Solar forecast/.test(html), "insights: says how the measured solar compared with the forecast");
const svgs = [...app().querySelectorAll('svg[role="img"]')].filter((s) => /Solar forecast and measured/.test(s.getAttribute("aria-label")));
check(svgs.length === 2, "insights: two labelled charts");
check(svgs.every((s) => s.querySelectorAll("rect[rx]").length > 0 && s.querySelector("path[stroke-dasharray]") && s.querySelectorAll("circle").length > 0),
  "insights: measured bars, the forecast as a dashed line with dots");
// axis labels are plain numbers (a floating-point step once printed 0.6000000000000001)
const ylabels = svgs.flatMap((s) => [...s.querySelectorAll('text[text-anchor="end"]')].map((x) => x.textContent));
check(ylabels.length > 0 && ylabels.every((x) => /^-?\d+(\.\d{1,3})?$/.test(x)), `insights: axis labels are tidy numbers (${ylabels.join(", ")})`);
el._solarCmp = { ...clone(cmp), hours: cmp.hours.map((r, h) => ({ ...r, forecast: h === 12 ? 0.65 : 0, actual: h === 12 ? 0.6 : 0 })) };
draw("insights", { solar_forecast: SF });
const small = [...app().querySelectorAll('svg[role="img"]')].filter((s) => /each hour/.test(s.getAttribute("aria-label")))
  .flatMap((s) => [...s.querySelectorAll('text[text-anchor="end"]')].map((x) => x.textContent));
check(small.join(",") === "0,0.2,0.4,0.6,0.8", `insights: a small hourly range has steps 0, 0.2 ... 0.8, not 0.6000000000000001 (${small.join(", ")})`);
el._solarCmp = clone(cmp);
draw("insights", { solar_forecast: SF });
const tips = [...svgs[1].querySelectorAll("title")].map((x) => x.textContent);
check(tips.length === 24 && tips[12].includes("forecast 1.60 kWh") && tips[20].includes("measured –"), "insights: every hour has a tooltip with both values (future hours: nothing measured)");
check(t.includes("Measured solar") && t.includes("Forecast") && html.includes("Download CSV"), "insights: legend and a CSV of the figures");
const dayTips = [...svgs[0].querySelectorAll("title")].map((x) => x.textContent);
check(dayTips[dayTips.length - 1].includes("today, so far"), "insights: today's bar is marked as the solar so far");
{ // the percentage uses whole days only: today's partial figure is left out
  const whole = days.slice(0, -1).filter((r) => r.forecast != null && r.actual != null && r.forecast > 0);
  const pct = Math.round((100 * whole.reduce((a, r) => a + r.actual, 0)) / whole.reduce((a, r) => a + r.forecast, 0));
  check(html.includes(`<b>${pct}%</b>`) && html.includes(`last ${whole.length} days`), `insights: the percentage (${pct}%) leaves today out`);
}
check(app().querySelector('[data-act="solar-day"][data-day]') !== null, "insights: Today / Yesterday buttons");
el._solarCmp = { ...clone(cmp), measured: false, days: cmp.days.map((r) => ({ ...r, actual: null })), hours: cmp.hours.map((r) => ({ ...r, actual: null })) };
t = draw("insights", { solar_forecast: SF });
check(t.includes("only the forecast is shown") && !t.includes("could not be shown"), "insights: no battery measures solar: says so, still draws the forecast");
el._solarCmp = { days: [], day: "2026-11-25", hours: [], measured: true, source: null };
t = draw("insights", { solar_forecast: null });
check(!t.includes("Solar forecast against what came") && !t.includes("could not be shown"), "insights: no forecast and no history: no card");
el._solarCmp = { days: [{ day: "2026-11-25", forecast: 0, actual: 0 }], day: "2026-11-25", hours: Array.from({ length: 24 }, (_, h) => ({ hour: h, forecast: 0, actual: 0 })), measured: true, source: "forecast_solar" };
t = draw("insights", { solar_forecast: { ...SF, today_kwh: 0 } });
check(!t.includes("could not be shown"), "insights: an all-zero day (night, snow) draws without error");

// ---- the day buttons ask for that day
el._solarCmp = clone(cmp);
draw("insights", { solar_forecast: SF });
calls.length = 0;
const btn = app().querySelector('[data-act="solar-day"][data-day]');
btn.click();
await new Promise((r) => setTimeout(r, 100));
check(calls.some((m) => m.type === "homepower/solar_forecast" && m.day === btn.dataset.day), "insights: a day button asks the backend for that day");
console.log(failures ? "FAIL" : "ok  ", "solar forecast on the Overview and in Insights");
process.exit(failures ? 1 : 0);
