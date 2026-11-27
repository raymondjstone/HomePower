// Diagnostics > Report an issue: the GitHub link carries the versions and battery types, and nothing that identifies anyone.
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
await import(`${PANEL}?v=r`); await customElements.get("homepower-panel").ready;
const el = document.createElement("homepower-panel"); document.body.appendChild(el);
const clone = (x) => JSON.parse(JSON.stringify(x));
el.hass = { states: {}, language: "en", connection: { subscribeMessage: () => Promise.resolve(() => {}) },
  callWS: async (m) => (m.type === "homepower/get" ? clone(payload.get) : m.type === "homepower/diagnostics" ? clone(payload.diag) : {}) };
await new Promise((r) => setTimeout(r, 200));

let failures = 0;
const check = (ok, what) => { console.log(ok ? "ok  " : "FAIL", what); if (!ok) failures++; };
check(!!(payload.get.versions || {}).homepower && !!payload.get.versions.home_assistant, "the backend sends both versions");

const one = payload.get.devices[0];
el.data = { ...clone(payload.get), devices: ["Secret Name", "Other & <b>", "x"].map((name, i) => ({ ...clone(one), device_id: `serial-SN123${i}`, name,
  kind: i === 2 ? "not-a-known-kind" : one.kind, connection: { host: "192.168.7.7", serial: "SN999999" } })) };
el.tab = "diagnostics"; await el._tabLoad(); el._render();
const a = [...el.shadowRoot.querySelectorAll("a")].find((x) => x.textContent.includes("Report an issue"));
check(!!a && a.target === "_blank" && a.rel.includes("noopener"), "there is a link that opens in a new tab");
const url = new URL(a.href), body = url.searchParams.get("body");
check(url.origin + url.pathname === "https://github.com/raymondjstone/HomePower/issues/new", "it goes to GitHub's new issue page");
const label = payload.get.catalogue.profiles.find((p) => p.id === one.kind).label;
check(body.includes(`HomePower: ${payload.get.versions.homepower}`) && body.includes(`Home Assistant: ${payload.get.versions.home_assistant}`), "both versions are filled in");
check(body.includes(`- ${label} x2`) && body.includes("- Unknown type"), "the battery types are filled in, counted");
check(!/Secret Name|Other|SN123|SN999|192\.168|not-a-known-kind/.test(body), "no names, ids, addresses or serial numbers");
process.exit(failures ? 1 : 0);
