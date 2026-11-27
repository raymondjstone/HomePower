// Every text box, number box, drop-down and text area in the panel has a name a screen reader can say: a linked
// <label for=...>, a wrapping <label>, an aria-label or a title; no id is used twice; every action can be reached with the
// keyboard. Every tab is drawn and every form is opened.
import fs from "fs";
import { JSDOM } from "jsdom";
import path from "path";
import { fileURLToPath, pathToFileURL } from "url";
const HERE = path.dirname(fileURLToPath(import.meta.url));
const PANEL = pathToFileURL(path.join(HERE, "..", "..", "custom_components", "homepower", "frontend", "homepower-panel.js")).href;
const payload = JSON.parse(fs.readFileSync(path.join(HERE, "payload.json"), "utf8"));
const w = new JSDOM("<body></body>", { url: "http://localhost/" }).window;
for (const k of ["HTMLElement", "customElements", "document", "MouseEvent", "Event", "MutationObserver"]) globalThis[k] = w[k];
Object.defineProperty(globalThis, "localStorage", { value: w.localStorage, configurable: true });
Object.defineProperty(globalThis, "navigator", { value: {}, configurable: true });
globalThis.alert = () => {}; globalThis.confirm = () => true;
await import(`${PANEL}?v=a`); await customElements.get("homepower-panel").ready;
const el = document.createElement("homepower-panel"); document.body.appendChild(el);
const clone = (x) => JSON.parse(JSON.stringify(x));
el.hass = { states: {}, language: "en", connection: { subscribeMessage: () => Promise.resolve(() => {}) },
  callWS: async (m) => ({ "homepower/get": clone(payload.get), "homepower/diagnostics": clone(payload.diag || { devices: {} }),
    "homepower/preview": clone(payload.preview || {}), "homepower/tariffs/compare": { tariffs: [] }, "homepower/backups": { backups: [] } }[m.type] ?? {}) };
await new Promise((r) => setTimeout(r, 200));
const tick = () => new Promise((r) => setTimeout(r, 0));          // let the MutationObserver run

const unnamed = (root, where) => [...root.querySelectorAll("input, select, textarea")]
  .filter((c) => !["checkbox", "radio", "hidden", "button", "submit"].includes((c.type || "").toLowerCase()))
  .filter((c) => !(c.id && root.querySelector(`label[for="${c.id}"]`)) && !c.closest("label") && !c.getAttribute("aria-label") && !c.getAttribute("title"))
  .map((c) => `${where}: <${c.tagName.toLowerCase()} id="${c.id}"> near "${(c.closest(".f")?.textContent || "").trim().slice(0, 40)}"`);

// Also: no id used twice (a label's for= and the panel's own getElementById would find the wrong one), and nothing that acts
// on a click is out of reach of the keyboard (only buttons, links and form controls take focus).
const sane = (root, where) => {
  const out = [], seen = new Map();
  for (const e of root.querySelectorAll("[id]")) seen.set(e.id, (seen.get(e.id) || 0) + 1);
  for (const [i, n] of seen) if (n > 1) out.push(`${where}: id "${i}" used ${n} times`);
  for (const e of root.querySelectorAll("[data-act]")) {
    const tag = e.tagName.toLowerCase();
    if (!["button", "a", "input", "select", "textarea", "label", "option"].includes(tag) && !e.hasAttribute("tabindex"))
      out.push(`${where}: <${tag} data-act="${e.dataset.act}"> cannot be reached with the keyboard`);
  }
  return out;
};

const found = [];
for (const tab of ["overview", "plan", "schedules", "batteries", "insights", "diagnostics", "settings"]) {
  el.tab = tab; el._lastHtml = null; await el._tabLoad(); el._render(); await tick();
  found.push(...unnamed(el.shadowRoot, tab), ...sane(el.shadowRoot, tab));
}
const id = payload.get.devices[0].device_id;
const forms = [["add-battery", {}], ["edit-battery", { id }], ["add-schedule", { tpl: "cheap" }], ["add-goal", {}], ["add-scene", {}], ["override", { id }]];
for (const [act, data] of forms) {
  const b = document.createElement("button"); b.dataset.act = act; Object.assign(b.dataset, data);
  try { await el._click({ target: b }); } catch (e) { found.push(`${act}: could not open (${e.message})`); continue; }
  await tick();
  found.push(...unnamed(el.shadowRoot.getElementById("modal"), act), ...sane(el.shadowRoot, act));   // the form with the page beneath it
  el._closeModal(); await tick();
}
for (const f of found) console.log("FAIL", f);
console.log(found.length ? "FAIL" : "ok  ", "every field in every tab and form has a name; no duplicate ids; every action reachable by keyboard");
process.exit(found.length ? 1 : 0);
