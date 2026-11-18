// Loads the real (split) panel in a real DOM with payloads from the real backend, renders every tab and presses every button.
import fs from "fs";
import { JSDOM } from "jsdom";

import path from "path";
import { fileURLToPath, pathToFileURL } from "url";
const HERE = path.dirname(fileURLToPath(import.meta.url));
const PAYLOAD = process.argv[2] || path.join(HERE, "payload.json");
const PANEL = pathToFileURL(path.join(HERE, "..", "..", "custom_components", "homepower", "frontend", "homepower-panel.js")).href;
const payload = JSON.parse(fs.readFileSync(PAYLOAD, "utf8"));
const dom = new JSDOM("<!doctype html><body></body>", { url: "http://localhost/", pretendToBeVisual: true });
const w = dom.window;
for (const k of ["HTMLElement", "customElements", "document", "Node", "Event", "MouseEvent", "CustomEvent", "Blob", "FileReader"]) globalThis[k] = w[k];
Object.defineProperty(globalThis, "navigator", { value: { clipboard: { writeText: async () => {} } }, configurable: true });
Object.defineProperty(globalThis, "localStorage", { value: w.localStorage, configurable: true });
const alerts = [];
globalThis.alert = (m) => alerts.push(String(m)); globalThis.confirm = () => true; globalThis.prompt = () => null;
URL.createObjectURL = () => "blob:x"; URL.revokeObjectURL = () => {};
const rejections = [];
process.on("unhandledRejection", (e) => rejections.push(e));
w.HTMLAnchorElement.prototype.click = function () { calls.push({ type: "(download) " + this.download }); };

const calls = [];
const replies = (m) => {
  switch (m.type) {
    case "homepower/get": return payload.get;
    case "homepower/diagnostics": return payload.diag;
    case "homepower/preview": return payload.preview;
    case "homepower/tariffs/compare": return payload.tariffs;
    case "homepower/backups": return { backups: [] };
    case "homepower/debug": return { made_at: "now" };
    case "homepower/device/test": return { ok: true, steps: [{ name: "sent hold", ok: true, detail: "d" }, { name: "battery actually charged", ok: false, info: true, detail: "d" }] };
    case "homepower/device/scan": return { holding: { 10001: 5 }, input: { 10002: 7 }, order: "big", names: {} };
    case "homepower/device/read_registers": return { values: { 10001: 5 }, order: "big", names: {} };
    case "homepower/whatif": if (payload.whatif_error) throw new Error(payload.whatif_error); return payload.whatif;
    case "homepower/schedule/save": case "homepower/goal/save": return { ...(m.schedule || m.goal), warnings: ["a warning"] };
    default: return {};
  }
};
const hass = {
  states: {}, language: "en",
  callWS: async (m) => { calls.push(m); return JSON.parse(JSON.stringify(replies(m))); },
  connection: { subscribeMessage: (cb) => { setTimeout(() => cb(payload.get.live), 10); return Promise.resolve(() => {}); } },
};

let failures = 0;
const must = (name, ok, extra = "") => { console.log(ok ? "ok  " : "FAIL", name, ok ? "" : extra); if (!ok) failures++; };
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

await import(`${PANEL}?v=1.2.3-abcdef12`);
const el = document.createElement("homepower-panel");
document.body.appendChild(el);
el.hass = hass;
await wait(300);
const root = el.shadowRoot;
const app = () => root.getElementById("app");
must("panel loaded its data", el._loaded === true && el.data.devices.length === 1, JSON.stringify(el._loadError));

const bad = (text) => /undefined|NaN|\[object Object\]|null%/.test(text);
for (const tab of ["overview", "plan", "schedules", "batteries", "insights", "diagnostics", "settings"]) {
  el.tab = tab; el._modal = null;
  await el._tabLoad();
  try { el._render(); } catch (e) { console.log("RENDER THREW on", tab, String(e.stack).split("\n").slice(0, 4).join(" | ")); }
  await wait(20);
  const text = app().textContent;
  must(`tab ${tab} renders without undefined/NaN`, app().innerHTML.length > 400 && !bad(text), (text.match(/.{0,40}(undefined|NaN|\[object Object\]).{0,40}/) || [""])[0]);
  const acts = [...new Set([...root.querySelectorAll("[data-act]")].map((b) => b.dataset.act))];
  for (const act of acts) {
    const btn = root.querySelector(`[data-act="${act}"]`);
    el.tab = tab; el._modal = null; el._render();
    const target = root.querySelector(`[data-act="${act}"]`) || btn;
    const before = rejections.length;
    try { target.dispatchEvent(new w.MouseEvent("click", { bubbles: true, composed: true })); } catch (e) { rejections.push(e); }
    await wait(30);
    if (rejections.length > before) must(`tab ${tab}: button ${act} did not throw`, false, String(rejections.at(-1)?.stack || rejections.at(-1)).split("\n").slice(0, 3).join(" | "));
    if (el._modal) { const m = root.getElementById("modal").innerHTML; if (bad(root.getElementById("modal").textContent)) must(`tab ${tab}: modal from ${act} clean`, false, (root.getElementById("modal").textContent.match(/.{0,40}(undefined|NaN).{0,40}/) || [""])[0]); el._closeModal(); }
  }
  console.log("   ", tab, "buttons pressed:", acts.length);
}
must("no unhandled errors from any button", rejections.length === 0, rejections.map((e) => String(e && e.stack || e).split("\n")[0]).join(" || "));
const sent = new Set(calls.map((c) => c.type));
must("safe mode and debug file reached the backend", sent.has("homepower/safe") && sent.has("homepower/debug") && [...sent].some((t) => t.startsWith("(download)")), [...sent].join(","));
must("pause reached the backend", sent.has("homepower/pause"));
console.log("calls made:", [...sent].sort().join(", "));
console.log("alerts shown:", alerts.slice(0, 4));
process.exit(failures ? 1 : 0);
