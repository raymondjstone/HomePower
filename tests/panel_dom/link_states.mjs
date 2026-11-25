// A battery whose reads are failing is shown as "Readings stale" (with its last values) or "Waiting for readings" until the
// offline-after time, and only then "Offline": on the Overview and Batteries tabs, in its details sheet and in Diagnostics.
// The random-render test never produces these states (it only removes values), so they are drawn here on purpose.
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
await import(`${PANEL}?v=l`); await customElements.get("homepower-panel").ready;
const el = document.createElement("homepower-panel"); document.body.appendChild(el);
const clone = (x) => JSON.parse(JSON.stringify(x));
el.hass = { states: {}, language: "en", callWS: async (m) => m.type === "homepower/get" ? clone(payload.get) : {}, connection: { subscribeMessage: () => Promise.resolve(() => {}) } };
await new Promise((r) => setTimeout(r, 200));

const id = payload.get.devices[0].device_id;
const base = payload.get.live.devices[id];
const STATES = {
  stale: { ...base, online: true, stale: true, link: "stale", stale_reason: "connected, but the battery did not answer in time", last_read_ok: "2026-11-19T10:42:00+00:00" },
  waiting: { ...base, online: false, stale: false, link: "stale", soc: null, power_w: null, offline_hint: null, offline_reason: "the battery refused the connection" },
  offline: { ...base, online: false, stale: false, link: "offline", soc: null, power_w: null, offline_reason: "the network reports the battery is not there" },
  online: { ...base, online: true, stale: false, link: "online" },
};
const EXPECT = { stale: "Readings stale", waiting: "Waiting for readings", offline: "Offline", online: "Online" };
let failures = 0;
const check = (ok, what) => { console.log(ok ? "ok  " : "FAIL", what); if (!ok) failures++; };
const text = () => el.shadowRoot.getElementById("app").textContent;

for (const [state, dev] of Object.entries(STATES)) {
  el.data = clone(payload.get);
  el.live = { ...clone(payload.get.live), devices: { ...payload.get.live.devices, [id]: dev } };
  el.data.live = el.live;
  for (const tab of ["overview", "batteries"]) {
    el.tab = tab; el._lastHtml = null; el._render();
    const t = text();
    // (the Overview card of an online battery shows Charging / Idle / Discharging rather than an "Online" chip)
    const want = state === "online" && tab === "overview" ? null : EXPECT[state];
    check(!t.includes("could not be shown") && (!want || t.includes(want)), `${tab} tab, ${state}: ${want ? `shows "${want}"` : "drawn"}`);
    if (state !== "offline") check(!/\bOffline\b/.test(t.replace(/offline after/gi, "")), `${tab} tab, ${state}: not called Offline`);
  }
  const r = el._reading(el.data.devices[0]);
  const sheet = el._detailsHtml(el.data.devices[0], null, null);
  check(sheet.includes(EXPECT[state]), `details sheet, ${state}: shows "${EXPECT[state]}"`);
  if (state === "stale") check(sheet.includes("did not answer in time") && r.soc != null, "details sheet, stale: last failure and the last charge level shown");
  el._diag = { devices: { [id]: { ...(payload.diag?.devices?.[id] || {}), name: "A", online: state === "online" || state === "stale" ? state === "online" : false,
                                  link: state === "stale" ? "stale" : dev.link, driver: "anker_modbus", control_enabled: true } } };
  el.tab = "diagnostics"; el._lastHtml = null; el._render();
  check(!text().includes("could not be shown"), `diagnostics tab, ${state}: drawn`);
}
// Diagnostics shows this cycle's raw read: a failed read that is not yet offline says so
el._diag = { devices: { [id]: { name: "A", online: false, link: "stale", driver: "anker_modbus", control_enabled: true } } };
el.tab = "diagnostics"; el._lastHtml = null; el._render();
check(text().includes("Read failed (not offline yet)"), 'diagnostics: a failed read before the offline-after time is "Read failed (not offline yet)"');
console.log(failures ? "FAIL" : "ok  ", "link states drawn everywhere");
process.exit(failures ? 1 : 0);
