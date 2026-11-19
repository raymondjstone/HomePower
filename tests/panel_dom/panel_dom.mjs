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
    case "homepower/device/details": return globalThis.__details || {};
    case "homepower/device/setting": return globalThis.__settingReply || { settings: [] };
    case "homepower/whatif": if (payload.whatif_error) throw new Error(payload.whatif_error); return payload.whatif;
    case "homepower/schedule/save": case "homepower/goal/save": return { ...(m.schedule || m.goal), warnings: ["a warning"] };
    default: return {};
  }
};
const hass = {
  states: {}, language: "en",
  callWS: async (m) => { calls.push(m); const r = replies(m); if (r && r.__error) throw new Error(r.__error); return JSON.parse(JSON.stringify(r)); },
  callService: async (domain, service, data) => { calls.push({ type: `(service) ${domain}.${service}`, ...data }); if (globalThis.__serviceError) throw new Error(globalThis.__serviceError); },
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
// The battery's details sheet with what a real battery sends: named registers, a matched Home Assistant device and its entities.
{
  const dev0 = el.data.devices[0], id = dev0.device_id;
  globalThis.__details = {
    actual: { soc: 62, grid_w: 300, load_w: 450, pv_w: 0, set_power_w: 1200, pack_v: [512, 513] }, serial: "SN123",
    registers: [
      { addr: 10064, name: "Work mode", access: "RW", kind: "setting", value: "third-party control", unit: "" },
      { addr: 10074, name: "Export power limit mode", access: "RW", kind: "setting", value: "off", unit: "" },
      { addr: 10014, name: "SOC", access: "RO", kind: "live", value: "62", unit: "%" },
      { addr: 10100, name: "Serial number", access: "RO", kind: "info", value: "SN123", unit: "" },
      { addr: 10284, name: "Pack 1 model name", access: "RO", kind: "info", value: "A1790", unit: "" },
      { addr: "backupReverseSoc", name: "Backup reserve level", access: "", kind: "setting", value: "10", unit: "%" },
      { addr: "<b>x</b>", name: "<b>x</b>", access: "", kind: "other", value: "{\"a\":1}", unit: "" },
    ],
    settings: [
      { key: "work_mode", label: "Operating mode", kind: "select", group: "Controls", value: 0, unit: "", min: null, max: null, step: null, options: [{ value: 0, label: "Self-consumption" }, { value: 1, label: "Time-of-use" }], hint: "", locked: "HomePower is managing this battery's mode." },
      { key: "charge_limit", label: "Charging limit", kind: "number", group: "Controls", value: 100, unit: "%", min: 80, max: 100, step: 1, options: [], hint: "Must be above the discharge limit.", locked: "" },
      { key: "ac1", label: "AC 1 output", kind: "switch", group: "Controls", value: true, unit: "", min: null, max: null, step: null, options: [], hint: "", locked: "" },
      { key: "export_limit_mode", label: "Export limit mode", kind: "select", group: "Configuration", value: 2, unit: "", min: null, max: null, step: null, options: [{ value: 0, label: "Disabled" }, { value: 2, label: "Fixed power" }], hint: "", locked: "" },
      { key: "backup_enabled", label: "Backup reserve switch", kind: "info", group: "Controls", value: "On", unit: "", min: null, max: null, step: null, options: [], hint: "", locked: "" },
    ],
    raw: { backupReverseSoc: 20, "<b>x</b>": { a: 1 } }, changes: [{ at: "2026-11-19 10:00:00", key: "workMode", from: 1, to: 2 }],
  };
  hass.devices = { hd1: { id: "hd1", name: "Garage Anker", manufacturer: "Anker", model: "X1", serial_number: "sn123", sw_version: "1.2" } };
  hass.entities = { "number.garage_export_limit": { entity_id: "number.garage_export_limit", device_id: "hd1" }, "sensor.garage_temp": { entity_id: "sensor.garage_temp", device_id: "hd1", entity_category: "diagnostic" }, "sensor.garage_soc": { entity_id: "sensor.garage_soc", device_id: "hd1" } };
  hass.states = { "number.garage_export_limit": { state: "3000", attributes: { friendly_name: "Garage Anker Export limit", min: 0, max: 5000, step: 100, unit_of_measurement: "W" } }, "sensor.garage_temp": { state: "31", attributes: { friendly_name: "Garage Anker Temperature", unit_of_measurement: "°C" } }, "sensor.garage_soc": { state: "62", attributes: { friendly_name: "Garage Anker State of charge", unit_of_measurement: "%" } } };
  el.tab = "batteries"; el._modal = null; el._render();
  await el._batteryDetails(id);
  const m = root.getElementById("modal"), txt = m.textContent;
  must("details sheet shows the battery's named settings", /Work mode/.test(txt) && /third-party control/.test(txt) && /Export power limit mode/.test(txt));
  must("details sheet shows units, serial and pack registers", /62 %/.test(txt) && /SN123/.test(txt) && /Battery pack 1/.test(txt));
  must("details sheet shows extra readings from the battery", /House load/.test(txt) && /Set point on the battery/.test(txt));
  must("details sheet finds the Home Assistant device by serial and lists its entities", /found by its serial number/.test(txt) && /Export limit/.test(txt) && /0 to 5000, step 100/.test(txt) && /Diagnostic \(1\)/.test(txt));
  must("details sheet shows EcoFlow-style values and setting changes, escaped", /Backup reserve level/.test(txt) && /Other values the battery sent/.test(txt) && /workMode: 1 → 2/.test(txt) && !m.querySelector("b"));
  must("details sheet is clean", !bad(txt), (txt.match(/.{0,40}(undefined|NaN|\[object Object\]).{0,40}/) || [""])[0]);
  // the settings that can be changed: editors, with the locked and read-only ones left alone
  const btn = (sel) => m.querySelector(sel);
  must("settings that can be changed show editors; a locked one and a read-only one do not",
    /Settings you can change/.test(txt) && !!btn('[data-act="setting-set"][data-key="charge_limit"]') && !!btn('[data-act="setting-set"][data-key="ac1"]') && !!btn('[data-act="setting-set"][data-key="export_limit_mode"]')
    && !btn('[data-act="setting-set"][data-key="work_mode"]') && !btn('[data-act="setting-set"][data-key="backup_enabled"]') && /HomePower is managing this battery/.test(txt) && /80 to 100/.test(txt));
  const press = async (b) => { b.dispatchEvent(new w.MouseEvent("click", { bubbles: true, composed: true })); await wait(40); };
  const sent = () => calls.filter((c) => c.type === "homepower/device/setting");
  globalThis.__settingReply = { settings: [{ key: "charge_limit", label: "Charging limit", kind: "number", group: "Controls", value: 90, unit: "%", min: 80, max: 100, step: 1, options: [], hint: "", locked: "" }], pending: false };
  root.getElementById("bs-charge_limit").value = "90";
  await press(btn('[data-act="setting-set"][data-key="charge_limit"]'));
  must("a number setting is sent to the backend as a number", sent().length === 1 && sent()[0].key === "charge_limit" && sent()[0].value === 90 && sent()[0].device_id === id, JSON.stringify(sent()));
  must("the sheet shows the new value and says it was saved", root.getElementById("bs-charge_limit").value === "90" && /Saved/.test(root.getElementById("modal").textContent));
  root.getElementById("bs-export_limit_mode").value = "2";
  await press(root.getElementById("modal").querySelector('[data-act="setting-set"][data-key="ac1"]'));
  must("what was chosen in another box survives sending a different setting", root.getElementById("bs-export_limit_mode").value === "2");
  must("a switch is sent as true or false", sent().at(-1).key === "ac1" && sent().at(-1).value === false, JSON.stringify(sent().at(-1)));
  root.getElementById("bs-export_limit_mode").value = "0";
  await press(root.getElementById("modal").querySelector('[data-act="setting-set"][data-key="export_limit_mode"]'));
  must("a choice is sent as its number", sent().at(-1).key === "export_limit_mode" && sent().at(-1).value === 0, JSON.stringify(sent().at(-1)));
  const before = sent().length;
  globalThis.confirm = () => false;
  await press(root.getElementById("modal").querySelector('[data-act="setting-set"][data-key="charge_limit"]'));
  must("nothing is sent when the change is not confirmed", sent().length === before);
  globalThis.confirm = () => true;
  globalThis.__settingReply = { __error: "Charging limit must be between 80 and 100 %" };
  root.getElementById("bs-charge_limit").value = "70";
  await press(root.getElementById("modal").querySelector('[data-act="setting-set"][data-key="charge_limit"]'));
  must("a refusal from the backend is shown next to the setting", /must be between 80 and 100/.test(root.getElementById("modal").textContent) && !bad(root.getElementById("modal").textContent));
  const sentBefore = sent().length;
  root.getElementById("bs-charge_limit").value = "";
  await press(root.getElementById("modal").querySelector('[data-act="setting-set"][data-key="charge_limit"]'));
  must("a blank number box is not sent as 0", sent().length === sentBefore && /Enter a number/.test(root.getElementById("modal").textContent));
  globalThis.__settingReply = null;
  // the controls of the matching Home Assistant device go through Home Assistant's own services
  const service = () => calls.filter((c) => String(c.type).startsWith("(service)"));
  root.getElementById("ha-number.garage_export_limit").value = "2500";
  await press(root.getElementById("modal").querySelector('[data-act="ha-set"][data-eid="number.garage_export_limit"]'));
  must("a Home Assistant number is set with number.set_value", service().length === 1 && service()[0].type === "(service) number.set_value" && service()[0].entity_id === "number.garage_export_limit" && service()[0].value === 2500, JSON.stringify(service()));
  globalThis.__serviceError = "Not allowed";
  root.getElementById("ha-number.garage_export_limit").value = "abc";
  await press(root.getElementById("modal").querySelector('[data-act="ha-set"][data-eid="number.garage_export_limit"]'));
  must("a number that is not a number is not sent", service().length === 1);
  root.getElementById("ha-number.garage_export_limit").value = "100";
  await press(root.getElementById("modal").querySelector('[data-act="ha-set"][data-eid="number.garage_export_limit"]'));
  must("a refusal from Home Assistant is shown", /Not allowed/.test(root.getElementById("modal").textContent));
  globalThis.__serviceError = null;
  must("read-only entities have no controls", !root.getElementById("modal").querySelector('[data-eid="sensor.garage_soc"]'));
  el._closeModal();
  globalThis.__details = null; hass.devices = {}; hass.entities = {}; hass.states = {};
  await el._batteryDetails(id);
  must("details sheet copes with no registers and no Home Assistant device", /No Home Assistant device was found/.test(root.getElementById("modal").textContent) && !bad(root.getElementById("modal").textContent));
  el._closeModal();
}

// The rest of the details sheet: every kind of Home Assistant control, richer live readings, finding the Home Assistant device other ways,
// a failed details call, groups staying open, the EcoFlow "pending" message and the cog on the Overview card.
{
  const dev0 = el.data.devices[0], id = dev0.device_id, modal = () => root.getElementById("modal");
  const press = async (b) => { b.dispatchEvent(new w.MouseEvent("click", { bubbles: true, composed: true })); await wait(40); };
  const services = () => calls.filter((c) => String(c.type).startsWith("(service)")).map((c) => `${c.type.slice(10)} ${JSON.stringify(Object.fromEntries(Object.entries(c).filter(([k]) => k !== "type")))}`);
  const dev = { id: "hd1", name: "Garage Anker", manufacturer: "Anker", model: "X1", serial_number: "sn123" };
  hass.devices = { hd1: dev };
  const ent = (eid, extra = {}) => [eid, { entity_id: eid, device_id: "hd1", ...extra }];
  hass.entities = Object.fromEntries([ent("select.garage_mode"), ent("switch.garage_ac"), ent("switch.garage_off"), ent("button.garage_reboot"), ent("text.garage_label"),
    ent("number.garage_gone"), ent("sensor.garage_soc"), ent("sensor.garage_temp", { entity_category: "diagnostic" })]);
  hass.states = {
    "select.garage_mode": { state: "Auto", attributes: { friendly_name: "Garage Anker Mode", options: ["Auto", "Eco"] } },
    "switch.garage_ac": { state: "on", attributes: { friendly_name: "Garage Anker AC" } }, "switch.garage_off": { state: "off", attributes: { friendly_name: "Garage Anker Spare" } },
    "button.garage_reboot": { state: "2026-11-01T00:00:00", attributes: { friendly_name: "Garage Anker Reboot" } },
    "text.garage_label": { state: "Shed", attributes: { friendly_name: "Garage Anker Label" } },
    "number.garage_gone": { state: "unavailable", attributes: { friendly_name: "Garage Anker Gone", min: 0, max: 10 } },
    "sensor.garage_soc": { state: "62", attributes: { friendly_name: "Garage Anker SOC", unit_of_measurement: "%" } },
    "sensor.garage_temp": { state: "31", attributes: { friendly_name: "Garage Anker Temp", unit_of_measurement: "°C" } },
  };
  globalThis.__details = { actual: {}, serial: "SN123", registers: [], settings: [] };
  el.tab = "batteries"; el._modal = null; el._render();
  await el._batteryDetails(id);
  const q = (eid) => modal().querySelector(`[data-act="ha-set"][data-eid="${eid}"]`);
  root.getElementById("ha-select.garage_mode").value = "Eco"; await press(q("select.garage_mode"));
  await press(q("switch.garage_ac")); await press(q("switch.garage_off")); await press(q("button.garage_reboot"));
  root.getElementById("ha-text.garage_label").value = "Garage"; await press(q("text.garage_label"));
  const got = services();
  must("a Home Assistant choice, switches, button and text field use the standard services",
    got.includes('select.select_option {"entity_id":"select.garage_mode","option":"Eco"}') && got.includes('switch.turn_off {"entity_id":"switch.garage_ac"}')
    && got.includes('switch.turn_on {"entity_id":"switch.garage_off"}') && got.includes('button.press {"entity_id":"button.garage_reboot"}')
    && got.includes('text.set_value {"entity_id":"text.garage_label","value":"Garage"}'), got.join(" | "));
  must("an unavailable entity and a sensor have no controls", !q("number.garage_gone") && !q("sensor.garage_soc") && !q("sensor.garage_temp"));
  // groups the user opened or closed stay that way when the sheet is redrawn
  const grp = (name) => [...modal().querySelectorAll("details")].find((d) => (d.querySelector("summary") || {}).textContent.startsWith(name));
  grp("Diagnostic").open = true; grp("Controls and settings").open = false;
  await press(q("switch.garage_ac"));
  must("groups keep their open or closed state when the sheet is redrawn", grp("Diagnostic").open === true && grp("Controls and settings").open === false);
  el._closeModal();

  // the EcoFlow "pending" message
  const ecoSetting = { key: "ac1", label: "AC 1 output", kind: "switch", group: "Controls", value: true, unit: "", min: null, max: null, step: null, options: [], hint: "", locked: "" };
  globalThis.__details = { actual: {}, registers: [], settings: [ecoSetting] };
  globalThis.__settingReply = { settings: [{ ...ecoSetting, value: false }], pending: true };
  await el._batteryDetails(id);
  await press(modal().querySelector('[data-act="setting-set"][data-key="ac1"]'));
  must("an EcoFlow change says it is sent and the cloud applies it shortly", /EcoFlow's cloud applies it/.test(modal().textContent));
  globalThis.__settingReply = null; el._closeModal();

  // richer live readings: every row of the sheet that depends on what the battery reports
  const savedLive = el.live, savedDev = { ...dev0 };
  Object.assign(dev0, { driver: "anker_modbus", entities: {}, role: "reserve", solar: "yes", grid_meter: "yes", invert_power: true, mode_map: { charge: "Charge" } });
  el.live = { ...el.live, devices: { ...el.live.devices, [id]: {
    online: true, soc: 62, power_w: 1200, pv_w: 300, has_solar: true, temp_c: 31.2, firmware: "1.0.19.16", mode: "charge", desired_mode: "charge", desired_power_w: 1500,
    reason: "cheap rate", why: "waiting for the cheap rate", limits: [100, 10, 20, true], measured_charge_w: 1200, off_grid: true, protected: true, guard_held: true,
    alarms: ["meter 1 communication failure"], active_schedules: ["Night charge"], override: { mode: "hold", until: new Date(Date.now() + 3600e3).toISOString() },
    last_error: "write refused", charge_verdict: "unreliable", traces: [{ at: "2026-11-19T01:00:00", mode: "charge", ok: true, reaction_s: 5, peak_w: 1500 }, { at: "2026-11-19T02:00:00", mode: "charge", ok: false, peak_w: 0 }],
    setting_changes: [{ at: "2026-11-19 10:00", key: "workMode", from: 1, to: 2 }] } } };
  globalThis.__details = { actual: { grid_w: -250, load_w: 450, set_power_w: 1500, target_soc: 90, reserve_soc: 20, pack_v: [527, 526] }, serial: "SN123", registers: [], settings: [] };
  hass.devices = {}; hass.entities = {}; hass.states = {};
  await el._batteryDetails(id);
  const t2 = modal().textContent;
  must("the sheet shows the battery's alarms, override, limits, command results and changes",
    /meter 1 communication failure/.test(t2) && /Night charge/.test(t2) && /Manual override/.test(t2) && /charges to 100%, stops discharging at 10%, backup reserve 20%/.test(t2)
    && /reacted after 5 s/.test(t2) && /no effect/.test(t2) && /workMode: 1 → 2/.test(t2) && /write refused/.test(t2) && /Not working/.test(t2) && /Grid down/.test(t2)
    && /Protection active/.test(t2) && /SOC limit reached/.test(t2) && /Charges at \(measured\)/.test(t2) && /export/.test(t2) && /Reserve it holds/.test(t2) && /Power sign flipped/.test(t2) && /Mode name: charge/.test(t2), t2.slice(0, 600));
  must("the richer sheet is clean", !bad(t2), (t2.match(/.{0,40}(undefined|NaN|\[object Object\]).{0,40}/) || [""])[0]);
  el._closeModal();

  // finding the Home Assistant device: by the entities mapped to the battery, then by name
  Object.assign(dev0, { entities: { soc: "sensor.garage_soc" } });
  hass.devices = { hd1: dev }; hass.entities = Object.fromEntries([ent("sensor.garage_soc")]); hass.states = { "sensor.garage_soc": { state: "62", attributes: { friendly_name: "Garage Anker SOC" } } };
  globalThis.__details = { actual: {}, registers: [], settings: [] };
  await el._batteryDetails(id);
  must("the Home Assistant device is found through the entities mapped to the battery", /found by the entities you chose/.test(modal().textContent));
  el._closeModal();
  Object.assign(dev0, { entities: {} });
  hass.devices = { hd2: { id: "hd2", name: dev0.name } }; hass.entities = {}; hass.states = {};
  await el._batteryDetails(id);
  must("the Home Assistant device is found by name, and one with no entities says so", /found by its name/.test(modal().textContent) && /no entities/.test(modal().textContent));
  el._closeModal();

  // the details call failing leaves the rest of the sheet usable
  globalThis.__details = { __error: "register read failed" };
  await el._batteryDetails(id);
  must("a failed details call is shown and the rest of the sheet is still there", /register read failed/.test(modal().textContent) && /Readings now/.test(modal().textContent) && !bad(modal().textContent));
  el._closeModal();

  // the cog on an Overview card opens the sheet
  Object.keys(dev0).forEach((k) => delete dev0[k]); Object.assign(dev0, savedDev); el.live = savedLive; globalThis.__details = null; hass.devices = {}; hass.entities = {}; hass.states = {};
  el.tab = "overview"; el._modal = null; el._render();
  const cog = root.querySelector('#app [data-act="battery-details"]');
  must("each battery card on the Overview has a cog", !!cog);
  if (cog) { await press(cog); must("the cog opens that battery's sheet", !!root.getElementById("bd-" + id)); }
  el._closeModal();
}
// A Solarbank that has gone silent: the card and the sheet say when it was last read and why it may be offline.
{
  const dev0 = el.data.devices[0], id = dev0.device_id, savedLive = el.live, savedDev = { ...dev0 };
  Object.assign(dev0, { driver: "anker_modbus", entities: {} });
  const hint = "The Solarbank 4 and Max AC stop answering on your network while they cannot reach Anker's cloud.";
  el.live = { ...el.live, devices: { ...el.live.devices, [id]: { online: false, soc: null, power_w: null, last_read_ok: "2026-11-19T10:42:00+00:00", offline_hint: hint, offline_reason: "connected, but the battery did not answer in time" } } };
  el.tab = "overview"; el._modal = null; el._render();
  const card = app().textContent;
  must("an offline Solarbank's card says when it was last read and why it may be offline", /Offline/.test(card) && /Last read/.test(card) && /stop answering on your network/.test(card) && /Connected, but the battery did not answer in time/.test(card) && !bad(card), card.slice(0, 300));
  el.live.devices[id].offline_hint = null; el._lastHtml = ""; el._render();
  must("another offline battery gets the last-read time but no Anker explanation", /Last read/.test(app().textContent) && !/Anker's cloud/.test(app().textContent));
  el.live.devices[id].offline_hint = hint;
  globalThis.__details = { actual: {}, registers: [], settings: [] };
  payload.diag.devices[id].recent_outages = [{ at: "2026-11-19 09:10:00", seconds: 45 }, { at: "2026-11-19 10:42:00", seconds: 600 }];
  await el._batteryDetails(id);
  const sheet = root.getElementById("modal").textContent;
  must("the sheet says why the battery is offline, the last failure and the recent dropouts",
    /Why offline/.test(sheet) && /Last read/.test(sheet) && /Last failure/.test(sheet) && /did not answer in time/.test(sheet) && /Recent dropouts/.test(sheet) && /silent for 10 min/.test(sheet) && /silent for 45 s/.test(sheet) && !bad(sheet), sheet.slice(0, 300));
  delete payload.diag.devices[id].recent_outages;
  el._closeModal();
  Object.keys(dev0).forEach((k) => delete dev0[k]); Object.assign(dev0, savedDev); el.live = savedLive; globalThis.__details = null;
}
must("no unhandled errors from any button", rejections.length === 0, rejections.map((e) => String(e && e.stack || e).split("\n")[0]).join(" || "));
const sent = new Set(calls.map((c) => c.type));
must("safe mode and debug file reached the backend", sent.has("homepower/safe") && sent.has("homepower/debug") && [...sent].some((t) => t.startsWith("(download)")), [...sent].join(","));
must("pause reached the backend", sent.has("homepower/pause"));
console.log("calls made:", [...sent].sort().join(", "));
console.log("alerts shown:", alerts.slice(0, 4));
process.exit(failures ? 1 : 0);
